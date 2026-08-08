#!/usr/bin/env python3
"""
smart-gap-scorer.py — altitude-upstream 10x smarter gap re-scorer

Re-scores all open gaps in the DB with deep signal analysis:

  Signal 1: Issue depth analysis — fetch full body + comments, extract
            has_reproduction, has_error_message, has_maintainer_engagement,
            has_pr_welcome, is_confirmed_bug signals.

  Signal 2: Fix feasibility — how many files does the fix touch? ≤3 = feasible,
            ≤20 lines = XS effort.

  Signal 3: Cross-repo amplification — same provider/model broken in 2+ repos
            boosts user_pain by 1.3x.

  Signal 4: Recency scoring — fetch updated_at from GitHub and map to freshness.

  Signal 5: Maintainer merge velocity — check repo_intelligence for historical
            merge rate on this wedge_type; boost merge_speed if >3 PRs merged.

Usage:
    python3 scripts/smart-gap-scorer.py                  # re-score all open gaps
    python3 scripts/smart-gap-scorer.py --dry-run        # print changes, don't write
    python3 scripts/smart-gap-scorer.py --gap-id 42      # re-score one gap
"""

import sys
import os
import json
import re
import time
import argparse
import subprocess
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ---------------------------------------------------------------------------
# DB import
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))
from db import get_connection


# ---------------------------------------------------------------------------
# Scoring formula (same weights as gap-scanner.py)
# ---------------------------------------------------------------------------

def score_gap(user_pain: float, maintainer_receptivity: float,
              merge_speed: float, narrative_fit: float, freshness: float) -> float:
    return (user_pain * 0.35
            + maintainer_receptivity * 0.25
            + merge_speed * 0.20
            + narrative_fit * 0.10
            + freshness * 0.10)


# ---------------------------------------------------------------------------
# GitHub API helper
# ---------------------------------------------------------------------------

_GH_TOKEN: str | None = None


def _get_gh_token() -> str:
    global _GH_TOKEN
    if _GH_TOKEN:
        return _GH_TOKEN
    # Try classic PAT first (preferred for issue/comment reads)
    for secret_id in (
        os.environ["NICKHAC_CLASSIC_PAT_SECRET"],
        os.environ["NICKHAC_PAT_SECRET"],
    ):
        r = subprocess.run(
            [
                "aws", "secretsmanager", "get-secret-value",
                "--secret-id", secret_id,
                "--region", os.environ.get("AWS_REGION", "us-east-1"),
                "--query", "SecretString",
                "--output", "text",
            ],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            _GH_TOKEN = r.stdout.strip()
            return _GH_TOKEN
    # Fall back to ambient GH_TOKEN env var (CI / gh CLI auth)
    _GH_TOKEN = os.environ.get("GH_TOKEN", "")
    return _GH_TOKEN


def gh_api(path: str, retries: int = 3) -> dict | list | None:
    """
    Call the GitHub API via `gh api <path>`.
    Handles 403/429 rate limits with exponential back-off.
    Returns parsed JSON or None on failure.
    """
    token = _get_gh_token()
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token

    for attempt in range(retries):
        time.sleep(1)  # polite 1s between all calls
        r = subprocess.run(
            ["gh", "api", path],
            capture_output=True, text=True, env=env,
        )
        if r.returncode == 0:
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                return None
        # Rate-limit handling
        stderr_lower = r.stderr.lower()
        if "rate limit" in stderr_lower or "403" in r.stderr or "429" in r.stderr:
            wait = 60 * (attempt + 1)
            print(f"  [rate-limit] sleeping {wait}s before retry {attempt + 1}/{retries}")
            time.sleep(wait)
        else:
            # Hard failure, no retry
            return None
    return None


def parse_github_issue_url(url: str) -> tuple[str, str] | None:
    """
    Parse 'https://github.com/owner/repo/issues/123'
    Returns ('owner/repo', '123') or None.
    """
    m = re.match(
        r"https?://github\.com/([^/]+/[^/]+)/issues/(\d+)",
        url or "",
    )
    if m:
        return m.group(1), m.group(2)
    return None


# ---------------------------------------------------------------------------
# Signal 1 — Issue depth analysis
# ---------------------------------------------------------------------------

def analyse_issue_depth(repo_full: str, issue_number: str) -> dict:
    """
    Fetch the full issue + all comments from GitHub and extract quality signals.

    Returns a dict with boolean flags:
        has_reproduction, has_error_message, has_maintainer_engagement,
        has_pr_welcome, is_confirmed_bug, updated_at (ISO string or None)
    """
    result = {
        "has_reproduction": False,
        "has_error_message": False,
        "has_maintainer_engagement": False,
        "has_pr_welcome": False,
        "is_confirmed_bug": False,
        "updated_at": None,
        "labels": [],
    }

    # 1. Fetch the issue itself
    issue_data = gh_api(f"repos/{repo_full}/issues/{issue_number}")
    if not issue_data or not isinstance(issue_data, dict):
        return result

    result["updated_at"] = issue_data.get("updated_at")
    result["labels"] = [lbl["name"].lower() for lbl in issue_data.get("labels", [])]

    body = issue_data.get("body") or ""
    body_lower = body.lower()

    # Reproduction: code block present or explicit reproduction steps
    if "```" in body or re.search(
        r"(reproduce|steps to reproduce|minimal.*example|repro)", body_lower
    ):
        result["has_reproduction"] = True

    # Error message: traceback / exception keywords
    if re.search(
        r"(traceback|exception:|error:|stacktrace|stack trace|at line \d|raise \w)",
        body_lower,
    ):
        result["has_error_message"] = True

    # 2. Fetch comments (paginated — first 100 is typically enough)
    comments_data = gh_api(
        f"repos/{repo_full}/issues/{issue_number}/comments?per_page=100"
    )
    if not isinstance(comments_data, list):
        comments_data = []

    # Fetch repo members/collaborators once (for maintainer check)
    # We use author_association as a cheaper signal: OWNER, MEMBER, COLLABORATOR
    MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

    for comment in comments_data:
        comment_body = (comment.get("body") or "").lower()
        association = (comment.get("author_association") or "").upper()

        if association in MAINTAINER_ASSOCIATIONS:
            result["has_maintainer_engagement"] = True

            # "PR welcome" signal from a maintainer
            if any(
                phrase in comment_body
                for phrase in (
                    "pr welcome",
                    "happy to review",
                    "would accept a pr",
                    "welcome a pr",
                    "feel free to open a pr",
                )
            ):
                result["has_pr_welcome"] = True

            # Confirmed bug: label 'bug' + maintainer saying it's a bug
            if "bug" in result["labels"] and any(
                phrase in comment_body
                for phrase in ("confirmed", "can reproduce", "yes this is a bug",
                               "this is indeed a bug", "i can reproduce")
            ):
                result["is_confirmed_bug"] = True

    # Also check body for code blocks in comments
    for comment in comments_data:
        comment_body = comment.get("body") or ""
        comment_lower = comment_body.lower()
        if "```" in comment_body or re.search(
            r"(reproduce|steps to reproduce|minimal.*example)", comment_lower
        ):
            result["has_reproduction"] = True
        if re.search(
            r"(traceback|exception:|error:|stacktrace|stack trace|at line \d)",
            comment_lower,
        ):
            result["has_error_message"] = True

    return result


# ---------------------------------------------------------------------------
# Signal 2 — Fix feasibility
# ---------------------------------------------------------------------------

_FILE_PATTERN = re.compile(
    r"[`'\"]?([\w./\-]+\.(?:py|js|ts|go|rs|java|rb|cpp|c|h|yaml|yml|json|toml))[`'\"]?"
)


def assess_fix_feasibility(issue_body: str, description: str) -> dict:
    """
    Estimate how many files a fix would touch based on issue body + gap description.
    Returns {'feasible': bool, 'file_count': int, 'small_fix': bool}.
    """
    combined = (issue_body or "") + "\n" + (description or "")
    files_mentioned = set(_FILE_PATTERN.findall(combined))

    # Filter noise: skip very short paths that look like extensions not paths
    files_mentioned = {
        f for f in files_mentioned
        if "/" in f or (len(f) > 5 and "." in f)
    }

    file_count = len(files_mentioned)

    # If no files explicitly mentioned, assume 1–2 files (scoped)
    if file_count == 0:
        file_count = 1

    feasible = file_count <= 3

    # Small-fix heuristic: doc gaps, model registry entries, or single-file fixes
    small_fix_keywords = [
        "docstring", "missing model", "add model", "update model",
        "one-line", "single file", "registry", "config entry",
        "xs", "x-small", "trivial",
    ]
    desc_lower = description.lower() if description else ""
    small_fix = feasible and (
        file_count == 1
        or any(kw in desc_lower for kw in small_fix_keywords)
    )

    return {"feasible": feasible, "file_count": file_count, "small_fix": small_fix}


# ---------------------------------------------------------------------------
# Signal 4 — Recency scoring
# ---------------------------------------------------------------------------

def freshness_from_updated_at(updated_at_str: str | None) -> float:
    """Map issue updated_at to a freshness score."""
    if not updated_at_str:
        return 0.5  # unknown — neutral

    try:
        updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
    except ValueError:
        return 0.5

    now = datetime.now(timezone.utc)
    days_ago = (now - updated_at).days

    if days_ago <= 7:
        return 1.0
    elif days_ago <= 30:
        return 0.8
    elif days_ago <= 90:
        return 0.6
    else:
        return 0.3


# ---------------------------------------------------------------------------
# Signal 5 — Maintainer merge velocity
# ---------------------------------------------------------------------------

def get_merge_velocity(cur, repo_full: str, wedge_type: str) -> dict:
    """
    Check repo_intelligence for historical merge rate for (repo, wedge_type).
    Also query the prs table for actual merged PR count.

    Returns {'merge_rate': float | None, 'merged_count': int}
    """
    col_name = f"merge_rate_{wedge_type}"
    cur.execute(
        "SELECT value FROM repo_intelligence WHERE repo_full_name = %s AND key = %s",
        (repo_full, col_name),
    )
    row = cur.fetchone()
    merge_rate = float(row[0]) if row else None

    # Count merged PRs for this repo+wedge_type
    cur.execute(
        """
        SELECT COUNT(*) FROM prs p
        JOIN repos r ON p.repo_id = r.id
        WHERE r.full_name = %s
          AND p.wedge_type = %s
          AND p.status = 'merged'
        """,
        (repo_full, wedge_type),
    )
    merged_count_row = cur.fetchone()
    merged_count = merged_count_row[0] if merged_count_row else 0

    return {"merge_rate": merge_rate, "merged_count": merged_count}


# ---------------------------------------------------------------------------
# Signal 3 — Cross-repo signal amplification
# ---------------------------------------------------------------------------

def build_cross_repo_map(gaps: list[dict]) -> dict[str, int]:
    """
    Build a map of provider/model key -> count of repos mentioning it.
    Used to apply 1.3x multiplier to user_pain when count >= 2.
    """
    # Extract a normalised "model/provider key" from description + provider
    provider_repo_count: dict[str, set] = defaultdict(set)

    for gap in gaps:
        desc = (gap.get("description") or "").lower()
        provider = (gap.get("provider") or "").lower()
        repo_id = gap.get("repo_id")
        gap_id = gap.get("id")

        # Try to extract known model/provider names
        # e.g. "deepseek-v3", "llama-3.3", "gpt-4o", "gemini-2.0"
        model_hits = re.findall(
            r"(deepseek[-\s]?v\d|llama[-\s]?\d|gpt[-\s]?\d|gemini[-\s]?\d|"
            r"claude[-\s]?\d|mistral|qwen\d?|groq|together|fireworks|"
            r"deepinfra|cerebras)",
            desc,
        )
        for hit in model_hits:
            key = re.sub(r"\s+", "-", hit.strip())
            provider_repo_count[key].add(repo_id or gap_id)

        # Also key by bare provider name
        if provider:
            provider_repo_count[provider].add(repo_id or gap_id)

    return {k: len(v) for k, v in provider_repo_count.items()}


def cross_repo_multiplier(gap: dict, cross_repo_map: dict[str, int]) -> float:
    """
    Return 1.3 if this gap's provider/model appears in 2+ repos, else 1.0.
    """
    desc = (gap.get("description") or "").lower()
    provider = (gap.get("provider") or "").lower()

    candidates = set()
    model_hits = re.findall(
        r"(deepseek[-\s]?v\d|llama[-\s]?\d|gpt[-\s]?\d|gemini[-\s]?\d|"
        r"claude[-\s]?\d|mistral|qwen\d?|groq|together|fireworks|"
        r"deepinfra|cerebras)",
        desc,
    )
    for hit in model_hits:
        candidates.add(re.sub(r"\s+", "-", hit.strip()))
    if provider:
        candidates.add(provider)

    for candidate in candidates:
        if cross_repo_map.get(candidate, 0) >= 2:
            return 1.3

    return 1.0


# ---------------------------------------------------------------------------
# Core re-scorer
# ---------------------------------------------------------------------------

def rescore_gap(gap: dict, cur, cross_repo_map: dict[str, int]) -> dict:
    """
    Apply all 5 signals to a gap row and return updated field values.

    Returns a dict with:
        gap_id, old_score, new_score, new_user_pain, new_maintainer_receptivity,
        new_freshness, new_effort, new_contribution_level, changed (bool)
    """
    gap_id = gap["id"]
    repo_id = gap["repo_id"]
    wedge_type = gap.get("wedge_type") or ""
    description = gap.get("description") or ""
    source_url = gap.get("source_url") or ""
    provider = gap.get("provider") or ""

    # Current values (DB) — clamp to [0, 1]
    user_pain = float(gap.get("user_pain") or 0.5)
    maintainer_receptivity = float(gap.get("maintainer_receptivity") or 0.5)
    merge_speed = float(gap.get("merge_speed") or 0.5)
    narrative_fit = float(gap.get("narrative_fit") or 0.7)
    freshness = float(gap.get("freshness") or 0.5)
    old_score = float(gap.get("score") or 0.0)
    effort = gap.get("effort") or "M"
    contribution_level = int(gap.get("contribution_level") or 2)

    # Fetch repo full_name
    cur.execute("SELECT full_name FROM repos WHERE id = %s", (repo_id,))
    repo_row = cur.fetchone()
    repo_full = repo_row[0] if repo_row else ""

    # ----------------------------------------------------------------
    # Signal 1 + 4: issue depth + recency (GitHub issue only)
    # ----------------------------------------------------------------
    issue_ref = parse_github_issue_url(source_url)
    depth_signals = {}

    if issue_ref:
        repo_from_url, issue_number = issue_ref
        print(f"  gap #{gap_id}: fetching issue {repo_from_url}#{issue_number} …")
        depth_signals = analyse_issue_depth(repo_from_url, issue_number)

        # Signal 1: adjust pain + receptivity
        if depth_signals.get("has_reproduction"):
            user_pain = min(1.0, user_pain + 0.10)
        if depth_signals.get("has_error_message"):
            user_pain = min(1.0, user_pain + 0.05)
        if depth_signals.get("has_maintainer_engagement"):
            maintainer_receptivity = min(1.0, maintainer_receptivity + 0.15)
        if depth_signals.get("has_pr_welcome"):
            maintainer_receptivity = min(1.0, maintainer_receptivity + 0.25)
        if depth_signals.get("is_confirmed_bug"):
            user_pain = min(1.0, user_pain + 0.10)
            maintainer_receptivity = min(1.0, maintainer_receptivity + 0.10)

        # Signal 4: recency freshness
        updated_at = depth_signals.get("updated_at")
        if updated_at:
            freshness = freshness_from_updated_at(updated_at)

    # ----------------------------------------------------------------
    # Signal 2: fix feasibility
    # ----------------------------------------------------------------
    issue_body = ""
    if issue_ref:
        # We already have the issue data implicitly — re-parse description
        issue_body = description  # best proxy without re-fetching

    feasibility = assess_fix_feasibility(issue_body, description)

    if not feasibility["feasible"]:
        contribution_level = max(contribution_level, 3)
        old_score -= 0.0  # penalty applied later via score formula recalc
        merge_speed = max(0.0, merge_speed - 0.15)  # encode infeasibility in merge_speed
    elif feasibility["small_fix"]:
        effort = "XS"
        merge_speed = min(1.0, merge_speed + 0.10)

    # ----------------------------------------------------------------
    # Signal 3: cross-repo amplification
    # ----------------------------------------------------------------
    multiplier = cross_repo_multiplier(gap, cross_repo_map)
    if multiplier > 1.0:
        user_pain = min(1.0, user_pain * multiplier)

    # ----------------------------------------------------------------
    # Signal 5: maintainer merge velocity
    # ----------------------------------------------------------------
    if repo_full and wedge_type:
        velocity = get_merge_velocity(cur, repo_full, wedge_type)
        if velocity["merged_count"] > 3:
            merge_speed = min(1.0, merge_speed + 0.10)

    # ----------------------------------------------------------------
    # Recalculate final score
    # ----------------------------------------------------------------
    new_score = score_gap(user_pain, maintainer_receptivity, merge_speed,
                          narrative_fit, freshness)
    new_score = round(new_score, 4)
    user_pain = round(user_pain, 4)
    maintainer_receptivity = round(maintainer_receptivity, 4)
    freshness = round(freshness, 4)
    old_score = round(old_score, 4)

    changed = (
        abs(new_score - old_score) > 1e-4
        or effort != (gap.get("effort") or "M")
        or contribution_level != int(gap.get("contribution_level") or 2)
    )

    return {
        "gap_id": gap_id,
        "old_score": old_score,
        "new_score": new_score,
        "new_user_pain": user_pain,
        "new_maintainer_receptivity": maintainer_receptivity,
        "new_freshness": freshness,
        "new_effort": effort,
        "new_contribution_level": contribution_level,
        "changed": changed,
        "signals": depth_signals,
    }


# ---------------------------------------------------------------------------
# Load gaps from DB
# ---------------------------------------------------------------------------

def load_open_gaps(cur, gap_id: int | None = None) -> list[dict]:
    """Load open gaps from the DB, optionally filtered to a single gap_id."""
    base_sql = """
        SELECT
            g.id,
            g.repo_id,
            g.wedge_type,
            g.description,
            g.effort,
            g.status,
            g.score,
            g.contribution_level,
            g.user_pain,
            g.maintainer_receptivity,
            g.freshness,
            g.source_url,
            g.provider
        FROM gaps g
        WHERE g.status IN ('open', 'in_progress')
    """
    if gap_id is not None:
        cur.execute(base_sql + " AND g.id = %s", (gap_id,))
    else:
        cur.execute(base_sql + " ORDER BY g.score DESC")

    cols = [
        "id", "repo_id", "wedge_type", "description", "effort",
        "status", "score", "contribution_level", "user_pain",
        "maintainer_receptivity", "freshness", "source_url", "provider",
    ]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Re-score open gaps with 10x smarter GitHub signals."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print proposed changes without writing to the DB.",
    )
    parser.add_argument(
        "--gap-id",
        type=int,
        default=None,
        metavar="N",
        help="Re-score only this gap ID.",
    )
    args = parser.parse_args()

    conn = get_connection()
    cur = conn.cursor()

    # Load gaps
    gaps = load_open_gaps(cur, gap_id=args.gap_id)
    if not gaps:
        print("SCORER: no open gaps found.")
        cur.close()
        conn.close()
        return

    print(f"SCORER: loaded {len(gaps)} open gap(s) to re-score")

    # Build cross-repo map once (no API calls needed)
    cross_repo_map = build_cross_repo_map(gaps)
    if cross_repo_map:
        top = sorted(cross_repo_map.items(), key=lambda x: -x[1])[:5]
        print(f"  cross-repo signals: {top}")

    # Re-score each gap
    results = []
    for gap in gaps:
        try:
            result = rescore_gap(gap, cur, cross_repo_map)
            results.append(result)
        except Exception as exc:
            print(f"  ERROR scoring gap #{gap['id']}: {exc}")

    # Summarise
    improved = sum(1 for r in results if r["new_score"] > r["old_score"] + 1e-4)
    degraded = sum(1 for r in results if r["new_score"] < r["old_score"] - 1e-4)
    total = len(results)

    if args.dry_run:
        print(f"\nDRY RUN — proposed changes ({total} gaps, {improved} improved, {degraded} degraded):")
        print(f"{'ID':>6}  {'OLD':>7}  {'NEW':>7}  {'ΔPAIN':>7}  {'ΔREC':>7}  {'FRESH':>6}  EFFORT  DESC")
        print("-" * 100)
        for r in sorted(results, key=lambda x: -(x["new_score"] - x["old_score"])):
            gap = next(g for g in gaps if g["id"] == r["gap_id"])
            delta_score = r["new_score"] - r["old_score"]
            print(
                f"{r['gap_id']:>6}  {r['old_score']:>7.4f}  {r['new_score']:>7.4f}  "
                f"{r['new_user_pain']:>7.4f}  {r['new_maintainer_receptivity']:>7.4f}  "
                f"{r['new_freshness']:>6.2f}  {r['new_effort']:<6}  "
                f"{gap.get('description', '')[:60]}"
            )
            signals = r.get("signals", {})
            if any(signals.get(k) for k in (
                "has_reproduction", "has_error_message",
                "has_maintainer_engagement", "has_pr_welcome", "is_confirmed_bug"
            )):
                print(f"         signals: {[k for k, v in signals.items() if v and k != 'updated_at' and k != 'labels']}")
    else:
        # Write updates to DB
        written = 0
        for r in results:
            if not r["changed"]:
                continue
            cur.execute(
                """
                UPDATE gaps
                SET score                  = %s,
                    user_pain              = %s,
                    maintainer_receptivity = %s,
                    freshness              = %s,
                    effort                 = %s,
                    contribution_level     = %s,
                    updated_at             = NOW()
                WHERE id = %s
                """,
                (
                    r["new_score"],
                    r["new_user_pain"],
                    r["new_maintainer_receptivity"],
                    r["new_freshness"],
                    r["new_effort"],
                    r["new_contribution_level"],
                    r["gap_id"],
                ),
            )
            written += 1

        conn.commit()
        print(f"SCORER: re-scored {total} gaps, {improved} improved, {degraded} degraded")
        print(f"SCORER: wrote {written} updated rows to Postgres")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
