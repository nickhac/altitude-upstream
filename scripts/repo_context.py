#!/usr/bin/env python3
"""
repo_context.py — altitude-upstream

Fetches and caches per-repo contribution context for use in agent prompts.
Cache TTL: 24 hours in repo_context_cache table.

Public API:
    get_repo_context(repo_full_name: str, classic_pat: str) -> dict
"""
import json
import datetime
import urllib.request
import urllib.error
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from db import get_connection

CACHE_TTL_HOURS = 24
MAX_CONTRIBUTING = 3000
MAX_PR_TEMPLATE = 1000
MAX_MERGED_PR_DIFF = 800
MAX_NAMING_CONVENTIONS = 400


def _github_get(url: str, pat: str, accept: str = 'application/vnd.github.v3+json') -> bytes:
    """Make a GitHub API GET. Raises urllib.error.HTTPError on non-2xx."""
    req = urllib.request.Request(
        url,
        headers={
            'Authorization': f'token {pat}',
            'Accept': accept,
            'User-Agent': 'altitude-upstream/1.0',
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def _fetch_contributing(repo_full_name: str, pat: str) -> str:
    """Fetch CONTRIBUTING.md content, truncated. Returns '' if not found."""
    for path in ('CONTRIBUTING.md', '.github/CONTRIBUTING.md', 'docs/CONTRIBUTING.md'):
        try:
            raw = _github_get(
                f'https://api.github.com/repos/{repo_full_name}/contents/{path}',
                pat,
                accept='application/vnd.github.v3.raw',
            )
            return raw.decode('utf-8', errors='replace')[:MAX_CONTRIBUTING]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            break
        except Exception:
            break
    return ''


def _fetch_pr_template(repo_full_name: str, pat: str) -> str:
    """Fetch PR template. Returns '' if not found."""
    for path in (
        '.github/PULL_REQUEST_TEMPLATE.md',
        '.github/pull_request_template.md',
        'PULL_REQUEST_TEMPLATE.md',
    ):
        try:
            raw = _github_get(
                f'https://api.github.com/repos/{repo_full_name}/contents/{path}',
                pat,
                accept='application/vnd.github.v3.raw',
            )
            return raw.decode('utf-8', errors='replace')[:MAX_PR_TEMPLATE]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            break
        except Exception:
            break
    return ''


def _fetch_merged_external_prs(repo_full_name: str, pat: str) -> list:
    """Fetch up to 3 recently merged PRs from external contributors."""
    try:
        data = json.loads(_github_get(
            f'https://api.github.com/repos/{repo_full_name}/pulls'
            f'?state=closed&sort=updated&direction=desc&per_page=30',
            pat
        ))
    except Exception:
        return []

    results = []
    for pr in data:
        if len(results) >= 3:
            break
        # External contributor: not OWNER or MEMBER
        assoc = pr.get('author_association', '')
        if assoc in ('OWNER', 'MEMBER'):
            continue
        if pr.get('user', {}).get('type') == 'Bot':
            continue
        if not pr.get('merged_at'):
            continue

        # Fetch the diff excerpt
        diff_excerpt = ''
        try:
            diff_bytes = _github_get(
                f'https://api.github.com/repos/{repo_full_name}/pulls/{pr["number"]}',
                pat,
                accept='application/vnd.github.v3.diff'
            )
            diff_excerpt = diff_bytes.decode('utf-8', errors='replace')[:MAX_MERGED_PR_DIFF]
        except Exception:
            pass

        results.append({
            'title': pr.get('title', '')[:120],
            'body': (pr.get('body') or '')[:300],
            'diff_excerpt': diff_excerpt,
        })

    return results


def _extract_naming_conventions(repo_full_name: str, file_path: str, pat: str) -> str:
    """Extract first ~400 chars of target file to infer naming style."""
    if not file_path:
        return ''
    try:
        raw = _github_get(
            f'https://api.github.com/repos/{repo_full_name}/contents/{file_path}',
            pat,
            accept='application/vnd.github.v3.raw',
        )
        content = raw.decode('utf-8', errors='replace')
        # First non-empty lines up to MAX_NAMING_CONVENTIONS chars
        return content[:MAX_NAMING_CONVENTIONS]
    except Exception:
        return ''


def _fetch_fresh(repo_full_name: str, pat: str, target_file: str = '') -> dict:
    """Fetch all context from GitHub. Returns dict."""
    contributing = _fetch_contributing(repo_full_name, pat)
    pr_template = _fetch_pr_template(repo_full_name, pat)
    merged_prs = _fetch_merged_external_prs(repo_full_name, pat)
    naming = _extract_naming_conventions(repo_full_name, target_file, pat)
    return {
        'repo_full_name': repo_full_name,
        'contributing_md': contributing,
        'pr_template': pr_template,
        'merged_prs': merged_prs,
        'naming_conventions': naming,
    }


def _save_cache(conn, cur, repo_full_name: str, ctx: dict) -> None:
    cur.execute("""
        INSERT INTO repo_context_cache
            (repo_full_name, contributing_md, pr_template, merged_prs_json,
             naming_conventions, fetched_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (repo_full_name) DO UPDATE SET
            contributing_md   = EXCLUDED.contributing_md,
            pr_template       = EXCLUDED.pr_template,
            merged_prs_json   = EXCLUDED.merged_prs_json,
            naming_conventions = EXCLUDED.naming_conventions,
            fetched_at        = NOW()
    """, (
        repo_full_name,
        ctx['contributing_md'],
        ctx['pr_template'],
        json.dumps(ctx['merged_prs']),
        ctx['naming_conventions'],
    ))
    conn.commit()


def get_repo_context(repo_full_name: str, classic_pat: str, target_file: str = '') -> dict:
    """
    Return per-repo contribution context dict. Uses 24h Postgres cache.

    Args:
        repo_full_name: e.g. 'BerriAI/litellm'
        classic_pat: GitHub classic PAT for API calls
        target_file: optional path within repo to extract naming conventions from

    Returns:
        {
            'contributing_md': str,
            'pr_template': str,
            'merged_prs': list[dict],
            'naming_conventions': str,
            'repo_full_name': str,
        }
    """
    empty = {
        'repo_full_name': repo_full_name,
        'contributing_md': '',
        'pr_template': '',
        'merged_prs': [],
        'naming_conventions': '',
    }

    try:
        conn = get_connection()
        cur = conn.cursor()

        # Check cache
        cur.execute("""
            SELECT repo_full_name, contributing_md, pr_template,
                   merged_prs_json, naming_conventions, fetched_at
            FROM repo_context_cache
            WHERE repo_full_name = %s
        """, (repo_full_name,))
        row = cur.fetchone()

        if row:
            fetched_at = row[5]
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=datetime.timezone.utc)
            age_hours = (
                datetime.datetime.now(datetime.timezone.utc) - fetched_at
            ).total_seconds() / 3600

            if age_hours < CACHE_TTL_HOURS:
                conn.close()
                return {
                    'repo_full_name': row[0],
                    'contributing_md': row[1],
                    'pr_template': row[2],
                    'merged_prs': json.loads(row[3]),
                    'naming_conventions': row[4],
                }

        # Cache miss or stale — fetch fresh
        ctx = _fetch_fresh(repo_full_name, classic_pat, target_file)
        _save_cache(conn, cur, repo_full_name, ctx)
        conn.close()
        return ctx

    except Exception as e:
        print(f"  [repo_context] Warning: {e} — proceeding without context")
        return empty
