#!/usr/bin/env python3
"""
record-contribution-result.py — altitude-upstream

Write the result of a contribution attempt to Postgres.
Called by contribution agents after fix-writer + verifier have run.

Usage:
    # Successful PR submission:
    python3 scripts/record-contribution-result.py \
        --gap-id 42 \
        --result '{"status":"submitted","pr_url":"https://github.com/BerriAI/litellm/pull/99999","pr_number":99999}'

    # Blocked (could not produce a valid fix):
    python3 scripts/record-contribution-result.py \
        --gap-id 42 \
        --result '{"status":"blocked","reason":"import errors in smoke test"}'

    # Skipped (verifier rejected the diff):
    python3 scripts/record-contribution-result.py \
        --gap-id 42 \
        --result '{"status":"rejected","reason":"diff does not address the described gap"}'

Result JSON schema:
    {
        "status": "submitted" | "blocked" | "rejected" | "skipped",
        "pr_url": "https://github.com/...",   # required when status=submitted
        "pr_number": 99999,                    # required when status=submitted
        "reason": "..."                        # required when status=blocked/rejected
    }

On submitted:  gap.status -> 'in_progress', inserts prs row, increments ramp_state
On blocked:    gap.status -> 'blocked'
On rejected:   gap.status -> 'blocked'
On skipped:    gap.status remains 'open' (no-op on gap, just logs)
"""

import sys
import json
import argparse
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from db import get_connection


def record_submitted(conn, gap_id: int, pr_url: str, pr_number: int,
                     wedge_type: str, repo_id: int) -> None:
    """Record a successful PR submission."""
    cur = conn.cursor()

    # Mark gap as in_progress
    cur.execute("""
        UPDATE gaps SET status = 'in_progress', updated_at = NOW()
        WHERE id = %s
    """, (gap_id,))

    # Insert PR row (upsert in case of retry)
    cur.execute("""
        INSERT INTO prs (gap_id, repo_id, pr_url, pr_number, status, wedge_type,
                         submitted_at, last_checked_at)
        VALUES (%s, %s, %s, %s, 'open', %s, NOW(), NOW())
        ON CONFLICT (pr_url) DO UPDATE
            SET last_checked_at = NOW()
    """, (gap_id, repo_id, pr_url, pr_number, wedge_type))

    # Increment today's ramp_state counter
    cur.execute("""
        INSERT INTO ramp_state (date, cap, submitted_today)
        VALUES (CURRENT_DATE, 5, 1)
        ON CONFLICT (date) DO UPDATE
            SET submitted_today = ramp_state.submitted_today + 1,
                updated_at = NOW()
    """)

    conn.commit()
    print(f"RECORDED: gap {gap_id} -> in_progress, PR {pr_url}")


def record_blocked(conn, gap_id: int, reason: str) -> None:
    """Mark a gap as blocked with a reason."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE gaps SET status = 'blocked', updated_at = NOW()
        WHERE id = %s
    """, (gap_id,))
    conn.commit()
    print(f"RECORDED: gap {gap_id} -> blocked ({reason[:120]})")


def main():
    parser = argparse.ArgumentParser(
        description='Record a contribution result to Postgres'
    )
    parser.add_argument('--gap-id', type=int, required=True,
                        help='Gap ID from the gaps table')
    parser.add_argument('--result', type=str, required=True,
                        help='JSON result object (see module docstring)')
    args = parser.parse_args()

    try:
        result = json.loads(args.result)
    except json.JSONDecodeError as e:
        print(f"ERROR: --result is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    status = result.get('status')
    if status not in ('submitted', 'blocked', 'rejected', 'skipped'):
        print(
            f"ERROR: status must be submitted|blocked|rejected|skipped, got: {status!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        conn = get_connection()
    except Exception as e:
        print(f"ERROR: DB connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if status == 'submitted':
            pr_url = result.get('pr_url', '')
            pr_number = int(result.get('pr_number', 0))
            if not pr_url:
                print("ERROR: pr_url required when status=submitted", file=sys.stderr)
                sys.exit(1)

            # Fetch wedge_type and repo_id for the gap
            cur = conn.cursor()
            cur.execute("""
                SELECT g.wedge_type, g.repo_id
                FROM gaps g
                WHERE g.id = %s
            """, (args.gap_id,))
            row = cur.fetchone()
            if not row:
                print(f"ERROR: gap {args.gap_id} not found", file=sys.stderr)
                sys.exit(1)
            wedge_type, repo_id = row

            record_submitted(conn, args.gap_id, pr_url, pr_number,
                             wedge_type, repo_id)

        elif status in ('blocked', 'rejected'):
            reason = result.get('reason', 'no reason provided')
            record_blocked(conn, args.gap_id, reason)

        elif status == 'skipped':
            # No-op on the gap — agent decided not to work on it
            print(f"RECORDED: gap {args.gap_id} skipped (no DB change)")

    finally:
        conn.close()


if __name__ == '__main__':
    main()
