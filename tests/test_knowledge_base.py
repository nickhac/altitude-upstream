"""
test_knowledge_base.py — CR-001 Phase 1

Tests for the docs/knowledge/ knowledge base:
  - All required files exist
  - Files have required sections
  - Exclusion-patterns.md parses correctly
  - get-repo-knowledge.py CLI paths: --list, --repo, --wedge, --infra, --json, missing file warning
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Repo root is one level up from tests/
REPO_ROOT = Path(__file__).parent.parent
KNOWLEDGE_ROOT = REPO_ROOT / "docs" / "knowledge"
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_gap_scanner():
    spec = importlib.util.spec_from_file_location(
        "gap_scanner", str(SCRIPTS_DIR / "gap-scanner.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_grk():
    """Load get-repo-knowledge as a module."""
    spec = importlib.util.spec_from_file_location(
        "get_repo_knowledge", str(SCRIPTS_DIR / "get-repo-knowledge.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestKnowledgeFilesExist(unittest.TestCase):
    """All required knowledge files must exist."""

    REQUIRED_FILES = [
        "repos/BerriAI-litellm.md",
        "repos/vllm-project-vllm.md",
        "repos/langchain-ai-langchain.md",
        "repos/run-llama-llama_index.md",
        "repos/openai-openai-python.md",
        "infrastructure/github-auth.md",
        "infrastructure/worktrees.md",
        "infrastructure/verification-pipeline.md",
        "gap-scanner/exclusion-patterns.md",
        "gap-scanner/scoring-signals.md",
        "wedge-types/model_registry_staleness.md",
        "wedge-types/missing_documentation.md",
        "wedge-types/broken_integration.md",
        "agent-prompts/fix-writer.md",
        "agent-prompts/verifier.md",
    ]

    def test_all_required_files_exist(self):
        missing = []
        for rel in self.REQUIRED_FILES:
            p = KNOWLEDGE_ROOT / rel
            if not p.exists():
                missing.append(rel)
        self.assertEqual(missing, [], f"Missing knowledge files: {missing}")


class TestKnowledgeFileStructure(unittest.TestCase):
    """Key files must contain required sections."""

    def _read(self, rel):
        return (KNOWLEDGE_ROOT / rel).read_text(encoding="utf-8")

    def test_litellm_has_what_works_section(self):
        content = self._read("repos/BerriAI-litellm.md")
        self.assertIn("## What works", content)

    def test_litellm_has_infrastructure_section(self):
        content = self._read("repos/BerriAI-litellm.md")
        self.assertIn("## Infrastructure", content)

    def test_litellm_has_what_doesnt_work_section(self):
        content = self._read("repos/BerriAI-litellm.md")
        self.assertIn("## What doesn't work", content)

    def test_github_auth_has_two_token_section(self):
        content = self._read("infrastructure/github-auth.md")
        self.assertIn("Two-Token", content)

    def test_worktrees_has_hard_reset_section(self):
        content = self._read("infrastructure/worktrees.md")
        self.assertIn("Hard reset", content)

    def test_verification_pipeline_has_gate_sequence(self):
        content = self._read("infrastructure/verification-pipeline.md")
        self.assertIn("Gate sequence", content)

    def test_exclusion_patterns_has_title_tokens_section(self):
        content = self._read("gap-scanner/exclusion-patterns.md")
        self.assertIn("title tokens", content.lower())

    def test_exclusion_patterns_has_labels_section(self):
        content = self._read("gap-scanner/exclusion-patterns.md")
        self.assertIn("labels", content.lower())

    def test_fix_writer_has_loop_contract(self):
        content = self._read("agent-prompts/fix-writer.md")
        self.assertIn("Loop engineering contract", content)
        self.assertIn("EARLY-EXIT", content)
        self.assertIn("TURN BUDGET", content)

    def test_verifier_has_loop_contract(self):
        content = self._read("agent-prompts/verifier.md")
        self.assertIn("Loop engineering contract", content)
        self.assertIn("Fail open", content)

    def test_model_registry_staleness_has_litellm_pattern(self):
        content = self._read("wedge-types/model_registry_staleness.md")
        self.assertIn("litellm pattern", content.lower())
        self.assertIn("backup", content.lower())


class TestExclusionPatternLoader(unittest.TestCase):
    """gap-scanner.py loads exclusion patterns from markdown at runtime."""

    def test_loads_from_markdown(self):
        gs = _load_gap_scanner()
        gs._cached_exclusions = None  # reset cache
        tokens, labels, phrases = gs.load_exclusion_patterns()
        self.assertIsInstance(tokens, list)
        self.assertIsInstance(labels, set)
        self.assertIsInstance(phrases, list)
        self.assertGreater(len(tokens), 5)
        self.assertGreater(len(labels), 3)
        self.assertGreater(len(phrases), 0)

    def test_known_tokens_present(self):
        gs = _load_gap_scanner()
        gs._cached_exclusions = None
        tokens, labels, phrases = gs.load_exclusion_patterns()
        self.assertIn("[rfc]", tokens)
        self.assertIn("roadmap", tokens)

    def test_known_labels_present(self):
        gs = _load_gap_scanner()
        gs._cached_exclusions = None
        tokens, labels, phrases = gs.load_exclusion_patterns()
        self.assertIn("wontfix", labels)
        self.assertIn("duplicate", labels)

    def test_fallback_on_missing_file(self):
        """If exclusion-patterns.md is temporarily missing, returns hardcoded defaults."""
        gs = _load_gap_scanner()
        gs._cached_exclusions = None
        original = gs.EXCLUSION_PATTERNS_FILE
        gs.EXCLUSION_PATTERNS_FILE = Path("/tmp/nonexistent-exclusions-xyz.md")
        try:
            tokens, labels, phrases = gs.load_exclusion_patterns()
            self.assertGreater(len(tokens), 0)
            self.assertGreater(len(labels), 0)
        finally:
            gs.EXCLUSION_PATTERNS_FILE = original
            gs._cached_exclusions = None

    def test_result_is_cached(self):
        """Second call returns cached result without re-parsing."""
        gs = _load_gap_scanner()
        gs._cached_exclusions = None
        first = gs.load_exclusion_patterns()
        second = gs.load_exclusion_patterns()
        self.assertIs(first, second)


class TestGetRepoKnowledgeCLI(unittest.TestCase):
    """get-repo-knowledge.py returns correct content for all query paths."""

    def _run(self, *args):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "get-repo-knowledge.py"), *args],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        return result

    def test_list_returns_all_categories(self):
        r = self._run("--list")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        for cat in ("repos", "wedge-types", "infrastructure", "gap-scanner"):
            self.assertIn(cat, data)

    def test_repo_returns_litellm_content(self):
        r = self._run("--repo", "BerriAI/litellm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("litellm", r.stdout.lower())
        self.assertIn("What works", r.stdout)

    def test_wedge_returns_model_registry_content(self):
        r = self._run("--wedge", "model_registry_staleness")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("model_registry_staleness", r.stdout.lower())

    def test_infra_returns_github_auth_content(self):
        r = self._run("--infra", "github-auth")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Two-Token", r.stdout)

    def test_json_flag_returns_dict(self):
        r = self._run("--repo", "BerriAI/litellm", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertIsInstance(data, dict)
        self.assertTrue(any("litellm" in k.lower() for k in data))

    def test_unknown_repo_warns_to_stderr(self):
        r = self._run("--repo", "nonexistent/repo")
        # Should warn on stderr but not crash fatally
        self.assertIn("WARNING", r.stderr)

    def test_combined_repo_wedge_infra(self):
        r = self._run(
            "--repo", "BerriAI/litellm",
            "--wedge", "model_registry_staleness",
            "--infra", "worktrees",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        # All three sections should appear
        self.assertIn("BerriAI/litellm", r.stdout)
        self.assertIn("model_registry_staleness", r.stdout.lower())
        self.assertIn("Worktree", r.stdout)


if __name__ == "__main__":
    unittest.main()
