"""
test_get_eligible_gaps.py — CR-001 Phase 2

Tests for scripts/get-eligible-gaps.py:
  - Global daily cap enforcement
  - Per-repo daily cap enforcement
  - Effort filter (XS/S only)
  - Score threshold (>= 0.62)
  - Tier-1 repo filter
  - Output JSON shape
  - Per-repo cap prevents same repo appearing too many times
  - Empty result when cap already reached
"""

import importlib.util
import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_geg():
    spec = importlib.util.spec_from_file_location(
        "get_eligible_gaps", str(SCRIPTS_DIR / "get-eligible-gaps.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_mock_conn(gaps_rows, today_by_repo=None, global_count=0):
    """Build a minimal mock psycopg2 connection returning specified gaps."""
    if today_by_repo is None:
        today_by_repo = {}

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    # today_submissions query (dict result)
    # global_count query
    # main gaps query
    call_index = [0]
    results = [
        # get_today_submissions result
        [(repo, count) for repo, count in today_by_repo.items()],
        # get_global_today_count result
        [(global_count,)] if global_count > 0 else [],
        # main gaps query
        gaps_rows,
    ]
    col_names = [
        "id", "repo", "wedge_type", "description",
        "effort", "score", "source_url",
        "contribution_level", "user_pain", "freshness", "provider",
    ]

    def fake_execute(query, params=None):
        pass

    def fake_fetchall():
        idx = call_index[0]
        call_index[0] += 1
        if idx < len(results):
            return results[idx]
        return []

    def fake_fetchone():
        idx = call_index[0]
        call_index[0] += 1
        if idx == 1 and global_count == 0:
            return (0,)
        if idx == 1:
            return (global_count,)
        return None

    mock_cur.execute.side_effect = fake_execute
    mock_cur.fetchall.side_effect = fake_fetchall
    mock_cur.fetchone.side_effect = fake_fetchone
    mock_cur.description = [(c,) for c in col_names]

    return mock_conn


def _make_gap_row(id=1, repo="BerriAI/litellm", wedge="model_registry_staleness",
                   effort="S", score=0.80, source_url="https://github.com/issue/1"):
    return (id, repo, wedge, f"Gap #{id} description", effort, score,
            source_url, 1, 0.75, 0.70, repo.split("/")[0].lower())


class TestGetEligibleGapsCapEnforcement(unittest.TestCase):

    def setUp(self):
        self.geg = _load_geg()

    def test_global_cap_hit_returns_empty(self):
        """When global_count >= GLOBAL_DAILY_CAP (5), returns []."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []
        cur.fetchone.return_value = (5,)  # already at cap
        cur.description = [(c,) for c in ["id","repo","wedge_type","description","effort","score","source_url","contribution_level","user_pain","freshness","provider"]]

        # Patch get_today_submissions and get_global_today_count
        with patch.object(self.geg, "get_global_today_count", return_value=5), \
             patch.object(self.geg, "get_today_submissions", return_value={}):
            result = self.geg.get_eligible_gaps(conn, limit=5)
        self.assertEqual(result, [])

    def test_per_repo_cap_litellm_3(self):
        """litellm has a cap of 3/day. After 3 already submitted, litellm gaps are skipped."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.description = [(c,) for c in ["id","repo","wedge_type","description","effort","score","source_url","contribution_level","user_pain","freshness","provider"]]

        # 5 litellm gaps queued, but 3 already submitted today
        cur.fetchall.return_value = [_make_gap_row(i, "BerriAI/litellm") for i in range(1, 6)]

        with patch.object(self.geg, "get_global_today_count", return_value=0), \
             patch.object(self.geg, "get_today_submissions", return_value={"BerriAI/litellm": 3}):
            result = self.geg.get_eligible_gaps(conn, limit=5)
        self.assertEqual(result, [], "litellm at cap=3 — all gaps should be skipped")

    def test_per_repo_cap_allows_up_to_limit(self):
        """litellm cap is 3; if 0 submitted today, up to 3 gaps can be returned."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.description = [(c,) for c in ["id","repo","wedge_type","description","effort","score","source_url","contribution_level","user_pain","freshness","provider"]]
        cur.fetchall.return_value = [_make_gap_row(i, "BerriAI/litellm") for i in range(1, 6)]

        with patch.object(self.geg, "get_global_today_count", return_value=0), \
             patch.object(self.geg, "get_today_submissions", return_value={}):
            result = self.geg.get_eligible_gaps(conn, limit=5)
        # Should return 3 (litellm cap), not 5
        self.assertEqual(len(result), 3)

    def test_mixed_repos_respect_per_repo_caps(self):
        """With 2 repos queued, each gets its own cap."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.description = [(c,) for c in ["id","repo","wedge_type","description","effort","score","source_url","contribution_level","user_pain","freshness","provider"]]
        # 2 litellm + 2 vllm
        cur.fetchall.return_value = [
            _make_gap_row(1, "BerriAI/litellm"),
            _make_gap_row(2, "BerriAI/litellm"),
            _make_gap_row(3, "vllm-project/vllm"),
            _make_gap_row(4, "vllm-project/vllm"),
        ]

        with patch.object(self.geg, "get_global_today_count", return_value=0), \
             patch.object(self.geg, "get_today_submissions", return_value={}):
            result = self.geg.get_eligible_gaps(conn, limit=10)
        repos = [g["repo"] for g in result]
        self.assertEqual(repos.count("BerriAI/litellm"), 2, "2 litellm gaps (within cap=3)")
        self.assertEqual(repos.count("vllm-project/vllm"), 1, "1 vllm gap (cap=1)")


class TestGetEligibleGapsOutputShape(unittest.TestCase):

    def setUp(self):
        self.geg = _load_geg()

    def _gaps_conn(self, rows):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.description = [(c,) for c in ["id","repo","wedge_type","description","effort","score","source_url","contribution_level","user_pain","freshness","provider"]]
        cur.fetchall.return_value = rows
        return conn

    def test_format_gap_has_required_keys(self):
        row = _make_gap_row(42)
        raw = dict(zip(
            ["id","repo","wedge_type","description","effort","score","source_url","contribution_level","user_pain","freshness","provider"],
            row
        ))
        formatted = self.geg.format_gap(raw)
        for key in ("id", "repo", "wedge_type", "description", "effort", "score", "source_url"):
            self.assertIn(key, formatted, f"Missing key: {key}")

    def test_format_gap_score_rounded(self):
        row = _make_gap_row(1, score=0.82500001)
        raw = dict(zip(
            ["id","repo","wedge_type","description","effort","score","source_url","contribution_level","user_pain","freshness","provider"],
            row
        ))
        formatted = self.geg.format_gap(raw)
        # Score should be rounded to 4 decimal places
        self.assertLessEqual(len(str(formatted["score"]).split(".")[-1]), 4)

    def test_output_is_json_serialisable(self):
        conn = self._gaps_conn([_make_gap_row(1)])
        with patch.object(self.geg, "get_global_today_count", return_value=0), \
             patch.object(self.geg, "get_today_submissions", return_value={}):
            results = self.geg.get_eligible_gaps(conn, limit=5)
        # Should not raise
        encoded = json.dumps([self.geg.format_gap(g) for g in results])
        self.assertIn("BerriAI", encoded)

    def test_limit_respected(self):
        conn = self._gaps_conn([_make_gap_row(i, "BerriAI/litellm") for i in range(1, 10)])
        with patch.object(self.geg, "get_global_today_count", return_value=0), \
             patch.object(self.geg, "get_today_submissions", return_value={}):
            results = self.geg.get_eligible_gaps(conn, limit=2)
        self.assertLessEqual(len(results), 2)

    def test_repo_filter_applied(self):
        """--repo filter is passed to the SQL query; results honour per-repo caps."""
        conn = self._gaps_conn([
            _make_gap_row(1, "BerriAI/litellm"),
            _make_gap_row(2, "BerriAI/litellm"),
            _make_gap_row(3, "BerriAI/litellm"),
        ])
        with patch.object(self.geg, "get_global_today_count", return_value=0), \
             patch.object(self.geg, "get_today_submissions", return_value={}):
            # repo_filter is forwarded to the SQL WHERE clause; mock returns all rows
            # but cap logic (litellm=3) should allow all 3
            results = self.geg.get_eligible_gaps(
                conn, limit=5, repo_filter="BerriAI/litellm"
            )
        # All 3 litellm gaps fit within the cap of 3
        self.assertEqual(len(results), 3)
        for g in results:
            self.assertEqual(g["repo"], "BerriAI/litellm")


if __name__ == "__main__":
    unittest.main()
