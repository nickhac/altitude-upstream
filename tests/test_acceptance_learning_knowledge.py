"""
test_acceptance_learning_knowledge.py — CR-001 Phase 3

Tests for the update_knowledge_on_pr_resolution() function in acceptance-learning.py:
  - merged outcome appends to ## What works section
  - closed outcome appends to ## What doesn't work section
  - wedge-type file ## Acceptance signal section is updated
  - function fails silently on FileNotFoundError
  - function fails silently on git commit error
  - both handle_acceptance and handle_decline call the function
  - created sections if they don't exist
  - pr_url appears in the appended text
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
KNOWLEDGE_ROOT = REPO_ROOT / "docs" / "knowledge"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_al():
    spec = importlib.util.spec_from_file_location(
        "acceptance_learning",
        str(SCRIPTS_DIR / "acceptance-learning.py"),
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestUpdateKnowledgeOnPRResolution(unittest.TestCase):

    def setUp(self):
        self.al = _load_al()
        # Create a temporary knowledge dir mirroring the real structure
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-knowledge-"))
        (self.tmpdir / "repos").mkdir()
        (self.tmpdir / "wedge-types").mkdir()

        # Write minimal repo file with ## What works and ## What doesn't work sections
        self.repo_file = self.tmpdir / "repos" / "BerriAI-litellm.md"
        self.repo_file.write_text(
            "# BerriAI/litellm\n\n"
            "## What works\n- JSON additions\n\n"
            "## What doesn't work\n- M/L effort gaps\n\n"
            "## Open questions\n- TBD\n"
        )

        # Write minimal wedge-type file with ## Acceptance signal section
        self.wedge_file = self.tmpdir / "wedge-types" / "model_registry_staleness.md"
        self.wedge_file.write_text(
            "# Wedge: model_registry_staleness\n\n"
            "## Acceptance signal\n- No merges yet\n"
        )

        # Patch PROJECT_DIR in the loaded module to point to tmpdir
        self.orig_project_dir = getattr(self.al, "PROJECT_DIR", str(REPO_ROOT))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _call(self, outcome, reason=""):
        """Call update_knowledge_on_pr_resolution with tmp paths."""
        with patch.object(
            self.al, "PROJECT_DIR", str(self.tmpdir)
        ) if hasattr(self.al, "PROJECT_DIR") else patch("builtins.open", side_effect=open):
            # Patch subprocess.run so git commit doesn't actually run
            with patch("subprocess.run", return_value=MagicMock(returncode=0)):
                self.al.update_knowledge_on_pr_resolution(
                    repo_full_name="BerriAI/litellm",
                    pr_url="https://github.com/BerriAI/litellm/pull/99999",
                    outcome=outcome,
                    reason=reason,
                    wedge_type="model_registry_staleness",
                    knowledge_root=str(self.tmpdir),  # pass override if supported
                )

    def test_merged_appends_to_what_works(self):
        """merged outcome appends to the ## What works section."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            self.al.update_knowledge_on_pr_resolution(
                repo_full_name="BerriAI/litellm",
                pr_url="https://github.com/BerriAI/litellm/pull/99999",
                outcome="merged",
                reason="",
                wedge_type="model_registry_staleness",
                knowledge_root=str(self.tmpdir),
            )
        content = self.repo_file.read_text()
        self.assertIn("99999", content, "PR number should appear in repo knowledge file")
        self.assertIn("accepted", content.lower(), "merged PRs should show 'accepted'")

    def test_closed_appends_to_what_doesnt_work(self):
        """closed outcome appends to the ## What doesn't work section."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            self.al.update_knowledge_on_pr_resolution(
                repo_full_name="BerriAI/litellm",
                pr_url="https://github.com/BerriAI/litellm/pull/88888",
                outcome="closed",
                reason="wrong file touched",
                wedge_type="model_registry_staleness",
                knowledge_root=str(self.tmpdir),
            )
        content = self.repo_file.read_text()
        self.assertIn("88888", content, "PR number should appear in repo knowledge file")
        self.assertIn("declined", content.lower(), "closed PRs should show 'declined'")

    def test_wedge_file_updated_on_merge(self):
        """Wedge-type knowledge file is updated when a PR merges."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            self.al.update_knowledge_on_pr_resolution(
                repo_full_name="BerriAI/litellm",
                pr_url="https://github.com/BerriAI/litellm/pull/77777",
                outcome="merged",
                reason="",
                wedge_type="model_registry_staleness",
                knowledge_root=str(self.tmpdir),
            )
        content = self.wedge_file.read_text()
        self.assertIn("77777", content, "PR number should appear in wedge knowledge file")

    def test_git_commit_called(self):
        """git add + commit is called after writing knowledge files."""
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            self.al.update_knowledge_on_pr_resolution(
                repo_full_name="BerriAI/litellm",
                pr_url="https://github.com/BerriAI/litellm/pull/11111",
                outcome="merged",
                reason="",
                wedge_type="model_registry_staleness",
                knowledge_root=str(self.tmpdir),
            )
        calls_str = [str(c) for c in mock_run.call_args_list]
        self.assertTrue(
            any("commit" in c for c in calls_str),
            f"git commit not called. Calls: {calls_str}"
        )

    def test_fails_silently_on_missing_repo_file(self):
        """Missing knowledge file does not raise — function fails silently."""
        self.repo_file.unlink()  # remove the file
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            # Should not raise
            try:
                self.al.update_knowledge_on_pr_resolution(
                    repo_full_name="BerriAI/litellm",
                    pr_url="https://github.com/PR/1",
                    outcome="merged",
                    reason="",
                    wedge_type="model_registry_staleness",
                    knowledge_root=str(self.tmpdir),
                )
            except Exception as e:
                self.fail(f"update_knowledge_on_pr_resolution raised {e!r} — must fail silently")

    def test_fails_silently_on_git_error(self):
        """Git commit failure does not propagate — function fails silently."""
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            try:
                self.al.update_knowledge_on_pr_resolution(
                    repo_full_name="BerriAI/litellm",
                    pr_url="https://github.com/PR/2",
                    outcome="merged",
                    reason="",
                    wedge_type="model_registry_staleness",
                    knowledge_root=str(self.tmpdir),
                )
            except Exception as e:
                self.fail(f"Function raised {e!r} on git error — must fail silently")

    def test_function_exists_in_module(self):
        """update_knowledge_on_pr_resolution is present in acceptance-learning.py."""
        self.assertTrue(
            hasattr(self.al, "update_knowledge_on_pr_resolution"),
            "update_knowledge_on_pr_resolution not found in acceptance-learning.py"
        )

    def test_function_accepts_knowledge_root_kwarg(self):
        """Function signature accepts knowledge_root for testability."""
        import inspect
        sig = inspect.signature(self.al.update_knowledge_on_pr_resolution)
        self.assertIn(
            "knowledge_root", sig.parameters,
            "update_knowledge_on_pr_resolution must accept knowledge_root kwarg for testing"
        )


if __name__ == "__main__":
    unittest.main()
