"""
test_gap_scanner_exclusions.py — CR-001 Phase 1/2

Tests for the gap-scanner exclusion pattern system:
  - Exclusion patterns load from markdown
  - Fallback on missing file returns hardcoded defaults
  - Cache works (second call returns same object)
  - Title token filter rejects RFC / tracking issues
  - Label filter rejects epic / wontfix labels
  - Body phrase filter rejects umbrella issues
  - Known-good issues pass all filters
  - Reaction threshold filter (< 5 reactions rejected)
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_gap_scanner():
    spec = importlib.util.spec_from_file_location(
        "gap_scanner", str(SCRIPTS_DIR / "gap-scanner.py")
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _make_issue(title="Fix broken integration", labels=None, body="", reactions=10):
    return {
        "number": 123,
        "title": title,
        "reactions": reactions,
        "comments": 2,
        "labels": labels or [],
        "body": body,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-07-01T00:00:00Z",
    }


def _should_exclude(gs, issue):
    """Apply all exclusion filters from the module to one issue. Returns True if excluded."""
    tokens, labels, phrases = gs.load_exclusion_patterns()
    title_lower = issue["title"].lower()
    body_lower = (issue.get("body") or "").lower()
    issue_labels = issue.get("labels", [])

    if any(tok in title_lower for tok in tokens):
        return True
    if any(lbl.lower() in labels for lbl in issue_labels):
        return True
    if any(phrase in body_lower for phrase in phrases):
        return True
    if issue["reactions"] < 5:
        return True
    return False


class TestExclusionLoaderFromMarkdown(unittest.TestCase):

    def setUp(self):
        self.gs = _load_gap_scanner()
        self.gs._cached_exclusions = None

    def test_returns_three_collections(self):
        tokens, labels, phrases = self.gs.load_exclusion_patterns()
        self.assertIsInstance(tokens, list)
        self.assertIsInstance(labels, set)
        self.assertIsInstance(phrases, list)

    def test_title_tokens_non_empty(self):
        tokens, _, _ = self.gs.load_exclusion_patterns()
        self.assertGreater(len(tokens), 0)

    def test_labels_non_empty(self):
        _, labels, _ = self.gs.load_exclusion_patterns()
        self.assertGreater(len(labels), 0)

    def test_body_phrases_non_empty(self):
        _, _, phrases = self.gs.load_exclusion_patterns()
        self.assertGreater(len(phrases), 0)

    def test_fallback_when_file_missing(self):
        self.gs._cached_exclusions = None
        original = self.gs.EXCLUSION_PATTERNS_FILE
        self.gs.EXCLUSION_PATTERNS_FILE = Path("/nonexistent/exclusions.md")
        try:
            tokens, labels, phrases = self.gs.load_exclusion_patterns()
            # Should return hardcoded defaults
            self.assertIn("[rfc]", tokens)
            self.assertIn("wontfix", labels)
            self.assertIn("tracking issue", phrases)
        finally:
            self.gs.EXCLUSION_PATTERNS_FILE = original
            self.gs._cached_exclusions = None

    def test_cache_returns_same_object(self):
        self.gs._cached_exclusions = None
        first = self.gs.load_exclusion_patterns()
        second = self.gs.load_exclusion_patterns()
        self.assertIs(first, second, "Second call should return cached result")


class TestExclusionFiltersApplied(unittest.TestCase):

    def setUp(self):
        self.gs = _load_gap_scanner()
        self.gs._cached_exclusions = None

    def test_rfc_title_excluded(self):
        issue = _make_issue(title="[RFC] New provider architecture")
        self.assertTrue(_should_exclude(self.gs, issue))

    def test_tracking_title_excluded(self):
        issue = _make_issue(title="[Tracking] Roadmap for v3")
        self.assertTrue(_should_exclude(self.gs, issue))

    def test_epic_label_excluded(self):
        issue = _make_issue(title="Good bug fix", labels=["epic"])
        self.assertTrue(_should_exclude(self.gs, issue))

    def test_wontfix_label_excluded(self):
        issue = _make_issue(title="Good bug fix", labels=["wontfix"])
        self.assertTrue(_should_exclude(self.gs, issue))

    def test_tracking_body_excluded(self):
        issue = _make_issue(
            title="Normal title",
            body="This is a tracking issue for multiple items."
        )
        self.assertTrue(_should_exclude(self.gs, issue))

    def test_umbrella_body_excluded(self):
        issue = _make_issue(
            title="Normal title",
            body="Umbrella issue for all Groq-related work."
        )
        self.assertTrue(_should_exclude(self.gs, issue))

    def test_low_reactions_excluded(self):
        issue = _make_issue(title="Fix broken auth", reactions=3)
        self.assertTrue(_should_exclude(self.gs, issue))

    def test_clean_issue_not_excluded(self):
        """A normal bug report with enough reactions passes all filters."""
        issue = _make_issue(
            title="Bedrock strict tools mode returns error",
            labels=["bug"],
            body="Steps to reproduce:\n```python\nlitellm.completion(...)\n```",
            reactions=15,
        )
        self.assertFalse(_should_exclude(self.gs, issue))

    def test_roadmap_title_excluded(self):
        issue = _make_issue(title="Q3 Roadmap discussion")
        self.assertTrue(_should_exclude(self.gs, issue))

    def test_duplicate_label_excluded(self):
        issue = _make_issue(title="Normal bug", labels=["duplicate"])
        self.assertTrue(_should_exclude(self.gs, issue))


if __name__ == "__main__":
    unittest.main()
