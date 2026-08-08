#!/usr/bin/env python3
"""
lessons.py — altitude-upstream

Shared helper for reading and writing structured lessons.
Called by contribution-writer (pre-flight) and acceptance-learning (post-outcome).

Usage:
  from lessons import run_preflight, add_lesson, confirm_lesson

Lesson lifecycle:
  1. add_lesson() — record a new lesson from an observed failure or pattern
  2. run_preflight() — query lessons for a given phase and repo, execute checks
  3. confirm_lesson() — increment confirmed_count when a lesson proved correct again
  4. invalidate_lesson() — mark a lesson as no longer applicable (pattern changed)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from db import get_connection


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def add_lesson(scope, trigger_phase, check_type, title, description,
               action=None, source=None):
    """
    Insert or update a lesson. On conflict (scope+trigger_phase+title),
    increments confirmed_count and updates description/action.

    scope:         'global' | 'repo:BerriAI/litellm' | 'wedge:model_registry_staleness'
    trigger_phase: 'pre_worktree' | 'pre_commit' | 'pre_push' | 'pre_pr'
                   | 'post_merge' | 'post_decline'
    check_type:    'assertion' | 'procedure' | 'warning' | 'ban'
    action:        optional machine-executable check, e.g.:
                   'check_db: SELECT cla_signed FROM repo_intelligence WHERE ...'
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO lessons
            (scope, trigger_phase, check_type, title, description, action, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (scope, trigger_phase, title) DO UPDATE SET
            description     = EXCLUDED.description,
            action          = COALESCE(EXCLUDED.action, lessons.action),
            source          = COALESCE(EXCLUDED.source, lessons.source),
            confirmed_count = lessons.confirmed_count + 1,
            invalidated_at  = NULL   -- re-activate if it was invalidated
    """, (scope, trigger_phase, check_type, title, description, action, source))
    conn.commit()
    cur.execute("SELECT id FROM lessons WHERE scope=%s AND trigger_phase=%s AND title=%s",
                (scope, trigger_phase, title))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def confirm_lesson(lesson_id):
    """Increment confirmed_count — call when a lesson correctly predicted a problem."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE lessons SET confirmed_count = confirmed_count + 1 WHERE id = %s",
                (lesson_id,))
    conn.commit()
    conn.close()


def invalidate_lesson(lesson_id, reason=None):
    """Mark a lesson as no longer applicable."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE lessons SET invalidated_at = NOW(), description = description || %s WHERE id = %s",
        (f"\n\n[INVALIDATED: {reason}]" if reason else "\n\n[INVALIDATED]", lesson_id)
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Read + execute pre-flight
# ---------------------------------------------------------------------------

def run_preflight(repo_full_name, trigger_phase, gap_id=None, dry_run=False):
    """
    Query all active lessons for this phase + repo, execute checks, return failures.

    Returns: (passed: bool, failures: list[dict], warnings: list[dict])

    Failures are hard stops (check_type='ban' or failed assertion).
    Warnings are printed but do not abort.
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, scope, check_type, title, description, action
        FROM lessons
        WHERE invalidated_at IS NULL
          AND trigger_phase = %s
          AND (scope = 'global' OR scope = %s)
        ORDER BY check_type DESC, scope DESC  -- 'warning' before 'assertion', repo before global
    """, (trigger_phase, f'repo:{repo_full_name}'))

    lessons = cur.fetchall()

    failures = []
    warnings = []

    print(f"\n  📋 Pre-flight [{trigger_phase}] — {len(lessons)} lessons for {repo_full_name}")

    for (lid, scope, check_type, title, description, action) in lessons:
        result = 'pass'
        detail = None

        if check_type == 'ban':
            result = 'fail'
            detail = f"Repo or pattern banned: {description[:100]}"
            failures.append({'lesson_id': lid, 'title': title, 'detail': detail})
            print(f"  🚫 BAN: {title}")

        elif check_type == 'assertion' and action and action.startswith('check_db:'):
            sql = action[len('check_db:'):].strip()
            try:
                cur.execute(sql)
                row = cur.fetchone()
                val = row[0] if row else None
                if val is True or val == 't' or str(val).lower() == 'true':
                    result = 'pass'
                    print(f"  ✅ {title}")
                else:
                    result = 'fail'
                    detail = f"DB check returned {val!r}"
                    failures.append({'lesson_id': lid, 'title': title, 'detail': detail})
                    print(f"  ❌ FAIL: {title} → {detail}")
            except Exception as e:
                result = 'warn'
                detail = f"DB check error: {e}"
                warnings.append({'lesson_id': lid, 'title': title, 'detail': detail})
                print(f"  ⚠️  WARN: {title} → {detail}")

        elif check_type == 'procedure':
            result = 'pass'  # Procedures are informational — operator follows them in code
            print(f"  📌 PROCEDURE: {title}")
            print(f"     {description[:120]}")

        elif check_type == 'warning':
            result = 'warn'
            warnings.append({'lesson_id': lid, 'title': title, 'detail': description[:100]})
            print(f"  ⚠️  WARNING: {title}")

        else:
            result = 'pass'
            print(f"  ✅ {title}")

        # Record result
        if not dry_run:
            try:
                cur.execute("""
                    INSERT INTO pre_flight_results
                        (gap_id, repo_full_name, lesson_id, result, detail)
                    VALUES (%s, %s, %s, %s, %s)
                """, (gap_id, repo_full_name, lid, result, detail))
            except Exception:
                pass  # pre_flight_results is best-effort

    if not dry_run:
        conn.commit()
    conn.close()

    passed = len(failures) == 0
    return passed, failures, warnings


# ---------------------------------------------------------------------------
# Auto-learn from PR outcomes
# ---------------------------------------------------------------------------

def learn_from_decline(repo_full_name, pr_title, reason_code, comment_snippet,
                       wedge_type=None, source_url=None):
    """
    Called by acceptance-learning.py when a PR is declined.
    Writes a structured lesson based on the rejection pattern.
    """
    reason_to_lesson = {
        'out_of_scope': (
            'warning',
            f'pre_pr',
            f'Maintainer rejected as out-of-scope: check repo focus before submitting',
            f'PR "{pr_title[:60]}" was closed as out-of-scope. '
            f'Maintainer comment: {comment_snippet[:200]}. '
            f'Review repo CONTRIBUTING.md and recent merged PRs before submitting to this repo.'
        ),
        'needs_tests': (
            'procedure',
            'pre_commit',
            f'Add tests for any new functionality',
            f'PR "{pr_title[:60]}" was declined due to missing tests. '
            f'Always add unit tests covering the changed code paths before submitting.'
        ),
        'wrong_approach': (
            'warning',
            'pre_pr',
            f'Verify approach matches repo conventions before submitting',
            f'PR "{pr_title[:60]}" was declined due to wrong approach. '
            f'Maintainer comment: {comment_snippet[:200]}. '
            f'Check how similar contributions were handled in recent merged PRs.'
        ),
        'spam': (
            'ban',
            'pre_pr',
            f'Repo banned: marked PR as spam',
            f'PR "{pr_title[:60]}" was flagged as spam by maintainer. '
            f'Repo is permanently banned from contribution queue.'
        ),
        'duplicate': (
            'warning',
            'pre_pr',
            f'Check for existing open PRs covering the same change',
            f'PR "{pr_title[:60]}" was a duplicate. '
            f'Always search open PRs before submitting: '
            f'gh api repos/{repo_full_name}/pulls --jq \'[.[] | .title]\''
        ),
    }

    if reason_code not in reason_to_lesson:
        return None

    check_type, trigger_phase, title, description = reason_to_lesson[reason_code]
    scope = f'repo:{repo_full_name}'

    return add_lesson(
        scope=scope,
        trigger_phase=trigger_phase,
        check_type=check_type,
        title=title,
        description=description,
        source=source_url or f'declined PR: {pr_title[:60]}'
    )


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='lessons.py — view or test lessons')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--preflight', metavar='REPO')
    parser.add_argument('--phase', default='pre_pr')
    args = parser.parse_args()

    if args.list:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT scope, trigger_phase, check_type, title, confirmed_count
            FROM lessons WHERE invalidated_at IS NULL
            ORDER BY scope, trigger_phase, check_type
        """)
        for row in cur.fetchall():
            print(f"[{row[1]}] {row[0]} ({row[2]}, confirmed={row[4]}x): {row[3][:60]}")
        conn.close()

    elif args.preflight:
        passed, failures, warnings = run_preflight(args.preflight, args.phase, dry_run=True)
        print(f"\nResult: {'PASS' if passed else 'FAIL'}")
        if failures:
            print("Hard failures:")
            for f in failures:
                print(f"  - {f['title']}: {f['detail']}")
        if warnings:
            print("Warnings:")
            for w in warnings:
                print(f"  - {w['title']}")
