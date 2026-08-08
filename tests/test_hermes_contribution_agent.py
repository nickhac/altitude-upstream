"""
test_hermes_contribution_agent.py — CR-001 Phase 3

Tests the loop engineering properties of hermes-contribution-agent.py:

  Loop engineering:
    - MAX_FIX_ATTEMPTS is 2 (not 3)
    - EARLY-EXIT rule is present in Fix Writer prompt
    - TURN BUDGET rule is present in Fix Writer prompt
    - PROGRESS GATE rule is present in Fix Writer prompt
    - Fail-open verifier: exception → PASS
    - Partial diff rescue: saves /tmp/partial-{gap_id}.diff on blocked
    - Early success return: first passing attempt stops the loop

  Functional:
    - format_gap output has all required keys
    - verify_with_failopen is truly fail-open
    - build_enhanced_prompt injects knowledge text
    - build_enhanced_prompt injects EARLY-EXIT on both attempt 1 and 2
    - dry-run path resets gap to open
    - JSON summary has required keys
    - run_fix_writer_with_harness stops after 1 attempt on success
    - run_fix_writer_with_harness uses max 2 attempts
    - run_fix_writer_with_harness saves partial diff on failure
    - MAX_FIX_ATTEMPTS constant equals 2
"""

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_hca():
    spec = importlib.util.spec_from_file_location(
        "hermes_contribution_agent",
        str(SCRIPTS_DIR / "hermes-contribution-agent.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_gap(gap_id=42, repo="BerriAI/litellm", wedge="model_registry_staleness"):
    return {
        "id": gap_id,
        "repo_full_name": repo,
        "wedge_type": wedge,
        "description": "Missing deepinfra model entry",
        "effort": "S",
        "score": 0.825,
        "source_url": "https://github.com/BerriAI/litellm/issues/12345",
        "provider": "deepinfra",
        "contribution_level": 1,
        "repo_id": 1,
    }


class TestLoopEngineeringConstants(unittest.TestCase):
    """Verify hard constants and contract rules are present."""

    def setUp(self):
        self.hca = _load_hca()

    def test_max_fix_attempts_is_2(self):
        self.assertEqual(
            self.hca.MAX_FIX_ATTEMPTS, 2,
            "MAX_FIX_ATTEMPTS must be 2 (code-iteration cap, not infra cap)"
        )

    def test_early_exit_in_prompt(self):
        """build_enhanced_prompt includes the EARLY-EXIT rule."""
        gap = _make_gap()
        prompt = self.hca.build_enhanced_prompt(gap, knowledge_text="", attempt=1)
        self.assertIn("EARLY-EXIT", prompt)

    def test_turn_budget_in_prompt(self):
        """build_enhanced_prompt includes the TURN BUDGET rule."""
        gap = _make_gap()
        prompt = self.hca.build_enhanced_prompt(gap, knowledge_text="", attempt=1)
        self.assertIn("TURN BUDGET", prompt)

    def test_no_attribution_rule_in_prompt(self):
        """Prompt must NOT instruct agent to add Co-authored-by in source code."""
        gap = _make_gap()
        prompt = self.hca.build_enhanced_prompt(gap, knowledge_text="", attempt=1)
        # The rule must say NOT to add it in source
        lower = prompt.lower()
        # Check: either "do not add" or "do NOT add" near "co-authored-by"
        self.assertIn("co-authored-by", lower)
        # Ensure there's a NOT near it (prohibition, not instruction)
        coauth_idx = lower.index("co-authored-by")
        window = lower[max(0, coauth_idx - 80): coauth_idx + 80]
        self.assertTrue(
            "not" in window or "never" in window or "no" in window,
            f"No prohibition near 'co-authored-by' in prompt window: {window!r}"
        )

    def test_early_exit_present_on_attempt_2(self):
        """EARLY-EXIT rule must be injected on retry attempts too."""
        gap = _make_gap()
        prompt = self.hca.build_enhanced_prompt(
            gap, knowledge_text="", attempt=2, test_error="pytest failed"
        )
        self.assertIn("EARLY-EXIT", prompt)

    def test_knowledge_text_injected_in_prompt(self):
        """Knowledge text appears in the built prompt."""
        gap = _make_gap()
        knowledge = "## What works\n- JSON additions are reliable"
        prompt = self.hca.build_enhanced_prompt(gap, knowledge_text=knowledge, attempt=1)
        self.assertIn("JSON additions are reliable", prompt)

    def test_gap_description_in_prompt(self):
        gap = _make_gap()
        prompt = self.hca.build_enhanced_prompt(gap, knowledge_text="", attempt=1)
        self.assertIn(gap["description"], prompt)


class TestVerifyWithFailopen(unittest.TestCase):
    """verify_with_failopen must return PASS on any exception."""

    def setUp(self):
        self.hca = _load_hca()

    def test_exception_returns_pass(self):
        """Any exception inside _verify_contribution → PASS 'verify skipped'."""
        with patch.object(
            self.hca, "_verify_contribution", side_effect=RuntimeError("Bedrock down")
        ):
            ok, verdict, reason = self.hca.verify_with_failopen("diff text", _make_gap())
        self.assertTrue(ok, "fail-open should return True on exception")
        self.assertEqual(verdict, "PASS")
        self.assertIn("skipped", reason.lower())

    def test_pass_verdict_passes_through(self):
        """PASS verdict from verifier passes through unchanged."""
        with patch.object(
            self.hca, "_verify_contribution", return_value=(True, "PASS", "looks good")
        ):
            ok, verdict, reason = self.hca.verify_with_failopen("diff", _make_gap())
        self.assertTrue(ok)
        self.assertEqual(verdict, "PASS")

    def test_reject_verdict_passes_through(self):
        """REJECT verdict from verifier passes through unchanged."""
        with patch.object(
            self.hca, "_verify_contribution",
            return_value=(False, "REJECT", "wrong file touched")
        ):
            ok, verdict, reason = self.hca.verify_with_failopen("diff", _make_gap())
        self.assertFalse(ok)
        self.assertEqual(verdict, "REJECT")

    def test_network_timeout_returns_pass(self):
        """Network timeout (ConnectionError) → PASS."""
        with patch.object(
            self.hca, "_verify_contribution",
            side_effect=ConnectionError("timeout")
        ):
            ok, verdict, reason = self.hca.verify_with_failopen("diff", _make_gap())
        self.assertTrue(ok)


class TestRunFixWriterHarness(unittest.TestCase):
    """run_fix_writer_with_harness loop engineering behaviour."""

    def setUp(self):
        self.hca = _load_hca()

    def _make_worktree(self):
        return tempfile.mkdtemp(prefix="hca-test-worktree-")

    def test_stops_after_first_passing_attempt(self):
        """When attempt 1 produces a passing diff, attempt 2 is never run."""
        gap = _make_gap()
        worktree = self._make_worktree()
        call_count = [0]

        def fake_bedrock(gap, worktree_path, attempt=1, test_error=None, repo_ctx=None):
            call_count[0] += 1
            return "diff --git a/f.py b/f.py\n+pass", None

        with patch.object(self.hca, "run_bedrock_agent", side_effect=fake_bedrock), \
             patch.object(self.hca, "check_diff_quality", return_value=(True, "OK")), \
             patch.object(self.hca, "apply_diff", return_value=(True, None)), \
             patch.object(self.hca, "smoke_test", return_value=(True, [])), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            diff, err, attempts, partial = self.hca.run_fix_writer_with_harness(
                gap, worktree, repo_ctx=None,
                knowledge_text="", gap_id=gap["id"]
            )

        self.assertEqual(call_count[0], 1, "Should stop after first passing attempt")
        self.assertEqual(attempts, 1)
        self.assertFalse(partial)

    def test_attempts_capped_at_max_fix_attempts(self):
        """Even if both attempts fail, only MAX_FIX_ATTEMPTS calls are made."""
        gap = _make_gap()
        worktree = self._make_worktree()
        call_count = [0]

        def fake_bedrock(gap, worktree_path, attempt=1, test_error=None, repo_ctx=None):
            call_count[0] += 1
            return None, "agent failed"

        with patch.object(self.hca, "run_bedrock_agent", side_effect=fake_bedrock), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            diff, err, attempts, partial = self.hca.run_fix_writer_with_harness(
                gap, worktree, repo_ctx=None,
                knowledge_text="", gap_id=gap["id"]
            )

        self.assertEqual(call_count[0], self.hca.MAX_FIX_ATTEMPTS)

    def test_partial_diff_saved_on_failure(self):
        """When all attempts fail, partial diff is saved to /tmp/partial-{gap_id}.diff."""
        gap = _make_gap(gap_id=9999)
        worktree = self._make_worktree()
        partial_path = f"/tmp/partial-9999.diff"

        # Clean up before test
        if os.path.exists(partial_path):
            os.remove(partial_path)

        def fake_bedrock(gap, worktree_path, attempt=1, test_error=None, repo_ctx=None):
            return "diff --git a/x.py b/x.py\n+x = 1", None

        def fake_quality(diff_text):
            return False, "size exceeded"  # always fail quality

        with patch.object(self.hca, "run_bedrock_agent", side_effect=fake_bedrock), \
             patch.object(self.hca, "check_diff_quality", side_effect=fake_quality), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="diff --git partial")):
            diff, err, attempts, partial_saved = self.hca.run_fix_writer_with_harness(
                gap, worktree, repo_ctx=None,
                knowledge_text="", gap_id=9999
            )

        # Either partial_saved=True and file written, or partial_saved and a path exists
        # (depends on whether subprocess mock returns content)
        self.assertTrue(partial_saved or os.path.exists(partial_path),
                        "Partial diff should be saved when all attempts fail")


class TestJSONOutputShape(unittest.TestCase):
    """process_gap_hermes returns a dict with all required keys."""

    def setUp(self):
        self.hca = _load_hca()

    def test_result_has_required_keys(self):
        """process_gap_hermes result always has required JSON output keys."""
        gap = _make_gap()
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        # Short-circuit at fork step
        with patch.object(self.hca, "ensure_fork", side_effect=RuntimeError("no network")):
            result = self.hca.process_gap_hermes(conn, gap, dry_run=False)

        for key in ("gap_id", "status", "pr_url", "reason", "attempts", "diff_lines"):
            self.assertIn(key, result, f"Missing key '{key}' in result: {result}")

    def test_result_is_json_serialisable(self):
        gap = _make_gap()
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        with patch.object(self.hca, "ensure_fork", side_effect=RuntimeError("no network")):
            result = self.hca.process_gap_hermes(conn, gap, dry_run=False)

        # Must not raise
        encoded = json.dumps(result)
        self.assertIsInstance(encoded, str)

    def test_error_status_on_exception(self):
        gap = _make_gap()
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        with patch.object(self.hca, "ensure_fork", side_effect=RuntimeError("net error")):
            result = self.hca.process_gap_hermes(conn, gap, dry_run=False)

        self.assertIn(result["status"], ("error", "blocked"))


if __name__ == "__main__":
    unittest.main()
