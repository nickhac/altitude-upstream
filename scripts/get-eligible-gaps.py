#!/usr/bin/env python3
"""
get-eligible-gaps.py — altitude-upstream

Query Postgres for the top N open gaps eligible for contribution.
Returns a JSON array of gap objects, highest score first.

Eligibility criteria:
  - status = 'open'
  - effort IN ('XS', 'S')  — agents cannot reliably complete M/L gaps
  - score >= 0.62           — minimum quality threshold
  - repo tier = 1           — only active Tier-1 repos
  - not daily-capped        — repo not at its per-repo daily limit

Usage:
    python3 scripts/get-eligible-gaps.py --limit 5
    python3 scripts/get-eligible-gaps.py --limit 5 --repo BerriAI/litellm
    python3 scripts/get-eligible-gaps.py --limit 5 --wedge missing_documentation
    python3 scripts/get-eligible-gaps.py --limit 5 --dry-run
"""

import sys
import json
import argparse
import subprocess
from datetime import date
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from db import get_connection


# ---------------------------------------------------------------------------
# Per-repo daily cap
# ---------------------------------------------------------------------------

REPO_DAILY_CAPS = {
    'BerriAI/litellm': 3,    # most reliable — highest cap
    'vllm-project/vllm': 1,
    'langchain-ai/langchain': 1,
    'run-llama/llama_index': 1,
    'openai/openai-python': 1,
}
GLOBAL_DAILY_CAP = 5

# Repos skipped entirely in the daily contribution run.
# Reason is logged when a gap from these repos is encountered.
SKIP_REPOS = {
    'langchain-ai/langchain': 'requires issue-first maintainer approval before PR — no automated path',
    'ggerganov/llama.cpp': 'tier-2: C/C++, out of scope',
    'ollama/ollama': 'tier-2: Go, out of scope',
}


def get_today_submissions(conn):
    """Return dict of {repo_full_name: count} for PRs submitted today."""
    cur = conn.cursor()
    cur.execute("""
        SELECT r.full_name, COUNT(p.id)
        FROM prs p
        JOIN repos r ON r.id = p.repo_id
        WHERE p.submitted_at::date = CURRENT_DATE
          AND p.status != 'abandoned'
        GROUP BY r.full_name
    """)
    return {row[0]: row[1] for row in cur.fetchall()}


def get_global_today_count(conn):
    """Return total PRs submitted today across all repos."""
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(submitted_today, 0)
        FROM ramp_state
        WHERE date = CURRENT_DATE
    """)
    row = cur.fetchone()
    return row[0] if row else 0


def get_eligible_gaps(conn, limit: int, repo_filter: 'str | None' = None,
                      wedge_filter: 'str | None' = None) -> list:
    """
    Query eligible gaps from Postgres.

    Returns a list of gap dicts ordered by score DESC.
    Applies per-repo and global daily caps.
    """
    today_by_repo = get_today_submissions(conn)
    global_count = get_global_today_count(conn)

    if global_count >= GLOBAL_DAILY_CAP:
        return []  # Global cap hit — nothing eligible

    # Build base query — joins repos to filter tier=1
    query = """
        SELECT
            g.id,
            r.full_name AS repo,
            g.wedge_type,
            g.description,
            g.effort,
            g.score,
            g.source_url,
            g.contribution_level,
            g.user_pain,
            g.freshness,
            g.provider
        FROM gaps g
        JOIN repos r ON r.id = g.repo_id
        WHERE g.status = 'open'
          AND g.effort IN ('XS', 'S')
          AND COALESCE(g.score, 0) >= 0.62
          AND r.tier = 1
    """
    params = []

    if repo_filter:
        query += " AND r.full_name = %s"
        params.append(repo_filter)

    if wedge_filter:
        query += " AND g.wedge_type = %s"
        params.append(wedge_filter)

    query += " ORDER BY g.score DESC"

    # Fetch more than limit so we can apply per-repo caps
    fetch_limit = max(limit * 5, 50)
    query += f" LIMIT {fetch_limit}"

    cur = conn.cursor()
    cur.execute(query, params)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]

    eligible = []
    for row in rows:
        gap = dict(zip(cols, row))
        repo = gap['repo']

        # Skip repos with a hard policy block
        if repo in SKIP_REPOS:
            continue  # Issue-first or tier-2 repo — not eligible for automated PR

        # Apply per-repo cap
        repo_cap = REPO_DAILY_CAPS.get(repo, 1)
        repo_today = today_by_repo.get(repo, 0)
        if repo_today >= repo_cap:
            continue  # This repo is at its daily limit

        eligible.append(gap)

        # Update in-memory counter to prevent selecting more than cap from same repo
        today_by_repo[repo] = repo_today + 1

        if len(eligible) >= limit:
            break

    return eligible


def format_gap(gap: dict) -> dict:
    """Return a clean serialisable gap dict for agent consumption."""
    return {
        'id': gap['id'],
        'repo': gap['repo'],
        'wedge_type': gap['wedge_type'],
        'description': gap['description'],
        'effort': gap['effort'],
        'score': round(float(gap['score'] or 0), 4),
        'source_url': gap.get('source_url') or '',
        'contribution_level': gap.get('contribution_level') or 1,
        'user_pain': round(float(gap.get('user_pain') or 0), 3),
        'provider': gap.get('provider') or '',
    }


def main():
    parser = argparse.ArgumentParser(
        description='Get top eligible gaps for contribution'
    )
    parser.add_argument('--limit', type=int, default=5,
                        help='Maximum number of gaps to return (default: 5)')
    parser.add_argument('--repo', type=str, default=None,
                        help='Filter to a specific repo (e.g. BerriAI/litellm)')
    parser.add_argument('--wedge', type=str, default=None,
                        help='Filter to a specific wedge type')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print gaps without writing; skips daily cap check')
    parser.add_argument('--pretty', action='store_true',
                        help='Pretty-print JSON output')
    args = parser.parse_args()

    try:
        conn = get_connection()
    except Exception as e:
        print(json.dumps({'error': f'DB connection failed: {e}'}))
        sys.exit(1)

    try:
        gaps = get_eligible_gaps(
            conn,
            limit=args.limit,
            repo_filter=args.repo,
            wedge_filter=args.wedge,
        )
        results = [format_gap(g) for g in gaps]
    finally:
        conn.close()

    indent = 2 if args.pretty else None
    print(json.dumps(results, indent=indent))


if __name__ == '__main__':
    main()
