#!/usr/bin/env python3
"""
daily-brief.py — altitude-upstream

Sends a formatted Telegram daily brief to Nick (chat_id=NICK_CHAT_ID).
Pulls all data from Postgres. Graceful if tables are missing (first run).

Usage:
    python3 scripts/daily-brief.py
    python3 scripts/daily-brief.py --dry-run
"""

import sys
import subprocess
import json
import traceback
from datetime import datetime, timezone, date

# ---------------------------------------------------------------------------
# Path bootstrap — allow running from any cwd
# ---------------------------------------------------------------------------
import os
sys.path.insert(0, os.path.dirname(__file__))
import db as db_module

DRY_RUN = "--dry-run" in sys.argv
NICK_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
SCORE_THRESHOLD = 0.6  # gaps scored above this are "ready to submit"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_secret(name: str) -> str:
    r = subprocess.run(
        [
            "aws", "secretsmanager", "get-secret-value",
            "--secret-id", name,
            "--region", os.environ.get("AWS_REGION", "us-east-1"),
            "--query", "SecretString",
            "--output", "text",
        ],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def table_exists(cur, table_name: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s)",
        (table_name,),
    )
    return cur.fetchone()[0]


def column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s)",
        (table_name, column_name),
    )
    return cur.fetchone()[0]


def send_telegram(token: str, chat_id: int, text: str) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = subprocess.run(
        [
            "curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(payload),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"curl failed: {r.stderr}")
    resp = json.loads(r.stdout)
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram API error: {resp.get('description', resp)}")


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_pr_queue(cur):
    """Returns (open_count, merged_count_all_time, acceptance_rate_7d)."""
    # Open count
    cur.execute("SELECT COUNT(*) FROM prs WHERE status = 'open'")
    open_count = cur.fetchone()[0]

    # Merged all time
    cur.execute("SELECT COUNT(*) FROM prs WHERE status = 'merged'")
    merged_count = cur.fetchone()[0]

    # 7-day acceptance rate: merged / (merged + closed) in last 7 days
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'merged') AS merged_7d,
            COUNT(*) FILTER (WHERE status IN ('merged', 'closed')) AS resolved_7d
        FROM prs
        WHERE resolved_at >= NOW() - INTERVAL '7 days'
    """)
    row = cur.fetchone()
    merged_7d, resolved_7d = row[0], row[1]
    if resolved_7d > 0:
        acceptance_rate_7d = round(merged_7d / resolved_7d * 100)
    else:
        acceptance_rate_7d = None  # no data

    return open_count, merged_count, acceptance_rate_7d


def fetch_active_prs(cur):
    """Returns list of dicts for open PRs."""
    # Check if repos table exists for JOIN
    has_repos = table_exists(cur, "repos")
    if has_repos:
        cur.execute("""
            SELECT
                p.pr_number,
                r.full_name AS repo,
                p.status,
                p.pr_url,
                p.submitted_at
            FROM prs p
            LEFT JOIN repos r ON r.id = p.repo_id
            WHERE p.status = 'open'
            ORDER BY p.submitted_at ASC
        """)
    else:
        cur.execute("""
            SELECT
                pr_number,
                NULL AS repo,
                status,
                pr_url,
                submitted_at
            FROM prs
            WHERE status = 'open'
            ORDER BY submitted_at ASC
        """)
    rows = cur.fetchall()
    now = datetime.now(timezone.utc)
    result = []
    for pr_number, repo, status, pr_url, submitted_at in rows:
        if submitted_at and submitted_at.tzinfo:
            age_days = (now - submitted_at).days
        elif submitted_at:
            age_days = (now - submitted_at.replace(tzinfo=timezone.utc)).days
        else:
            age_days = "?"
        result.append({
            "number": pr_number or "?",
            "repo": repo or "unknown",
            "status": status,
            "url": pr_url,
            "age": age_days,
        })
    return result


def fetch_recent_outcomes(cur):
    """Returns list of dicts for PRs resolved in last 7 days."""
    has_repos = table_exists(cur, "repos")
    has_gaps = table_exists(cur, "gaps")
    has_rejection_reasons = table_exists(cur, "rejection_reasons")

    # Build title source: prefer gaps.description if available
    if has_gaps and has_repos:
        title_expr = "COALESCE(g.description, p.wedge_type)"
        from_clause = """
            FROM prs p
            LEFT JOIN repos r ON r.id = p.repo_id
            LEFT JOIN gaps g ON g.id = p.gap_id
        """
    elif has_repos:
        title_expr = "p.wedge_type"
        from_clause = "FROM prs p LEFT JOIN repos r ON r.id = p.repo_id"
    else:
        title_expr = "p.wedge_type"
        from_clause = "FROM prs p"

    repo_expr = "r.full_name AS repo" if has_repos else "NULL AS repo"

    # Rejection reason
    if has_rejection_reasons:
        cur.execute("""
            SELECT
                p.pr_number,
                {repo_expr},
                p.status,
                {title_expr} AS title,
                rr.reason_code
            {from_clause}
            LEFT JOIN rejection_reasons rr ON rr.pr_id = p.id
            WHERE p.resolved_at >= NOW() - INTERVAL '7 days'
              AND p.status IN ('merged', 'closed')
            ORDER BY p.resolved_at DESC
        """.format(
            repo_expr=repo_expr,
            title_expr=title_expr,
            from_clause=from_clause,
        ))
    else:
        cur.execute("""
            SELECT
                p.pr_number,
                {repo_expr},
                p.status,
                {title_expr} AS title,
                NULL AS reason_code
            {from_clause}
            WHERE p.resolved_at >= NOW() - INTERVAL '7 days'
              AND p.status IN ('merged', 'closed')
            ORDER BY p.resolved_at DESC
        """.format(
            repo_expr=repo_expr,
            title_expr=title_expr,
            from_clause=from_clause,
        ))

    rows = cur.fetchall()
    result = []
    for pr_number, repo, status, title, reason_code in rows:
        result.append({
            "number": pr_number or "?",
            "repo": repo or "unknown",
            "status": status,
            "title": (title or "")[:40],
            "reason_code": reason_code,
        })
    return result


def fetch_gap_queue(cur):
    """Returns (ready_count, top_candidate_or_None)."""
    has_gaps = table_exists(cur, "gaps")
    has_repos = table_exists(cur, "repos")

    if not has_gaps:
        return 0, None

    # Check if gaps has a score column
    has_score = column_exists(cur, "gaps", "score")

    if has_score and has_repos:
        cur.execute("""
            SELECT COUNT(*) FROM gaps g
            WHERE g.status = 'open' AND g.score > %s
        """, (SCORE_THRESHOLD,))
        ready_count = cur.fetchone()[0]

        cur.execute("""
            SELECT g.wedge_type, r.full_name, g.score
            FROM gaps g
            LEFT JOIN repos r ON r.id = g.repo_id
            WHERE g.status = 'open' AND g.score > %s
            ORDER BY g.score DESC
            LIMIT 1
        """, (SCORE_THRESHOLD,))
        top = cur.fetchone()
    elif has_score:
        cur.execute("""
            SELECT COUNT(*) FROM gaps WHERE status = 'open' AND score > %s
        """, (SCORE_THRESHOLD,))
        ready_count = cur.fetchone()[0]

        cur.execute("""
            SELECT wedge_type, NULL, score FROM gaps
            WHERE status = 'open' AND score > %s
            ORDER BY score DESC LIMIT 1
        """, (SCORE_THRESHOLD,))
        top = cur.fetchone()
    else:
        # No score column — count all open gaps
        cur.execute("SELECT COUNT(*) FROM gaps WHERE status = 'open'")
        ready_count = cur.fetchone()[0]

        if has_repos:
            cur.execute("""
                SELECT g.wedge_type, r.full_name, NULL
                FROM gaps g
                LEFT JOIN repos r ON r.id = g.repo_id
                WHERE g.status = 'open'
                LIMIT 1
            """)
        else:
            cur.execute("""
                SELECT wedge_type, NULL, NULL FROM gaps
                WHERE status = 'open' LIMIT 1
            """)
        top = cur.fetchone()

    if top:
        top_candidate = {"wedge_type": top[0], "repo": top[1] or "unknown", "score": top[2]}
    else:
        top_candidate = None

    return ready_count, top_candidate


def fetch_wedge_performance(cur):
    """Returns (best_wedge_or_None, worst_wedge_or_None)."""
    has_wh = table_exists(cur, "wedge_hypotheses")
    if not has_wh:
        return None, None

    cur.execute("""
        SELECT wedge_type, submitted_count, accepted_count, acceptance_rate
        FROM wedge_hypotheses
        WHERE submitted_count > 0
        ORDER BY acceptance_rate DESC
    """)
    rows = cur.fetchall()
    if not rows:
        return None, None

    best = rows[0]
    worst = rows[-1]

    def make_wedge(row):
        wedge_type, submitted_count, accepted_count, acceptance_rate = row
        return {
            "wedge_type": wedge_type,
            "submitted_count": submitted_count,
            "accepted_count": accepted_count,
            "acceptance_rate": round(acceptance_rate * 100) if acceptance_rate <= 1.0 else round(acceptance_rate),
        }

    best_d = make_wedge(best)
    worst_d = make_wedge(worst) if worst != best else None

    return best_d, worst_d


def fetch_circuit_breaker(cur):
    """Returns dict with state, or None if table missing."""
    if not table_exists(cur, "circuit_breaker"):
        return None

    cur.execute("""
        SELECT state, failure_count, opened_at, reason
        FROM circuit_breaker
        ORDER BY id DESC LIMIT 1
    """)
    row = cur.fetchone()
    if not row:
        return {"state": "closed", "failure_count": 0, "reason": None}

    state, failure_count, opened_at, reason = row
    return {"state": state or "closed", "failure_count": failure_count or 0, "reason": reason}


def fetch_ramp_state(cur):
    """Returns (submitted_today, cap) for today."""
    cur.execute("""
        SELECT submitted_today, cap
        FROM ramp_state
        WHERE date = CURRENT_DATE
        ORDER BY id DESC LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        return row[0], row[1]
    return 0, 5


def fetch_system_state(cur):
    """Returns dict or None if table missing."""
    if not table_exists(cur, "system_state"):
        return None

    # system_state is a key-value table: (key TEXT PRIMARY KEY, value TEXT)
    cur.execute("SELECT key, value FROM system_state WHERE key IN ('submission_paused', 'pause_reason')")
    rows = {k: v for k, v in cur.fetchall()}
    return {
        "paused": rows.get("submission_paused", "false").lower() == "true",
        "pause_reason": rows.get("pause_reason", None),
    }


def update_last_brief_at(cur, conn):
    """Update system_state.last_daily_brief_at if the table exists."""
    if not table_exists(cur, "system_state"):
        return

    # key-value table — upsert the value
    cur.execute("""
        INSERT INTO system_state (key, value, updated_at)
        VALUES ('last_daily_brief_at', NOW()::TEXT, NOW())
        ON CONFLICT (key) DO UPDATE SET value = NOW()::TEXT, updated_at = NOW()
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def build_message(
    today: date,
    open_count: int,
    merged_count: int,
    acceptance_rate_7d,
    active_prs: list,
    recent_outcomes: list,
    ready_gaps: int,
    top_candidate,
    best_wedge,
    worst_wedge,
    circuit_breaker,
    submitted_today: int,
    cap: int,
    system_state,
    no_prs_yet: bool,
) -> str:
    lines = []

    # Header
    lines.append("<b>🤖 altitude-upstream daily brief</b>")
    lines.append(f"<i>{today.strftime('%A, %B %-d %Y')}</i>")
    lines.append("")

    # ---- PR Queue ----
    lines.append("<b>📊 PR Queue</b>")
    if no_prs_yet:
        lines.append("No PRs submitted yet")
    else:
        rate_str = f"{acceptance_rate_7d}%" if acceptance_rate_7d is not None else "n/a"
        lines.append(
            f"Open: {open_count} | Merged: {merged_count} (all time) | "
            f"Acceptance rate (7d): {rate_str}"
        )
    lines.append("")

    # ---- Active PRs ----
    lines.append("<b>🔄 Active PRs</b>")
    if not active_prs:
        lines.append("• No open PRs")
    else:
        for pr in active_prs:
            lines.append(
                f"• #{pr['number']} {pr['repo']} — {pr['status']} — {pr['age']}d old"
            )
            lines.append(f"  {pr['url']}")
    lines.append("")

    # ---- Recent outcomes ----
    lines.append("<b>⏳ Recent outcomes</b> (last 7 days)")
    if not recent_outcomes:
        lines.append("• No resolved PRs this week")
    else:
        for pr in recent_outcomes:
            if pr["status"] == "merged":
                lines.append(f"• ✅ MERGED: #{pr['number']} {pr['repo']} — {pr['title']}")
            else:
                reason = pr["reason_code"] or "unspecified"
                lines.append(
                    f"• ❌ CLOSED: #{pr['number']} {pr['repo']} — {pr['title']} "
                    f"(reason: {reason})"
                )
    lines.append("")

    # ---- Gap Queue ----
    lines.append("<b>📋 Gap Queue</b>")
    lines.append(f"Ready to submit: {ready_gaps} gaps scored above threshold")
    if top_candidate:
        score_str = f"{top_candidate['score']:.2f}" if top_candidate["score"] is not None else "n/a"
        lines.append(
            f"Top candidate: {top_candidate['wedge_type']} | "
            f"{top_candidate['repo']} | score={score_str}"
        )
    else:
        lines.append("Top candidate: none")
    lines.append("")

    # ---- What's working ----
    lines.append("<b>🧠 What's working</b>")
    if best_wedge:
        lines.append(
            f"{best_wedge['wedge_type']}: {best_wedge['acceptance_rate']}% acceptance "
            f"({best_wedge['accepted_count']}/{best_wedge['submitted_count']})"
        )
    else:
        lines.append("Not enough data yet")
    lines.append("")

    # ---- What's not working ----
    lines.append("<b>⚠️ What's not working</b>")
    if worst_wedge and worst_wedge["acceptance_rate"] < 50 and worst_wedge["submitted_count"] >= 2:
        lines.append(
            f"{worst_wedge['wedge_type']}: {worst_wedge['acceptance_rate']}% acceptance "
            f"— Consider pausing this wedge type"
        )
    else:
        lines.append("All wedge types healthy")
    lines.append("")

    # ---- System health ----
    cb_state = circuit_breaker["state"] if circuit_breaker else "unknown"
    is_open = cb_state.lower() in ("open", "tripped", "triggered")

    if is_open:
        lines.append("🔴 <b>⚠️ CIRCUIT BREAKER OPEN ⚠️</b> 🔴")
        cb_reason = circuit_breaker.get("reason") or "no reason recorded"
        lines.append(f"<b>Reason: {cb_reason}</b>")
        lines.append("")

    lines.append("<b>🔋 System health</b>")
    lines.append(f"Circuit breaker: {cb_state} | Ramp: {submitted_today}/{cap}/day")

    if system_state and system_state.get("paused"):
        pause_reason = system_state.get("pause_reason") or "no reason"
        lines.append(f"⛔ SUBMISSION PAUSED: {pause_reason}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        conn = db_module.get_connection()
        cur = conn.cursor()

        # Verify prs table exists at all
        has_prs = table_exists(cur, "prs")

        if not has_prs:
            no_prs_yet = True
            open_count = merged_count = 0
            acceptance_rate_7d = None
            active_prs = []
            recent_outcomes = []
        else:
            no_prs_yet = False
            open_count, merged_count, acceptance_rate_7d = fetch_pr_queue(cur)
            # If total submissions is 0, treat as first run
            cur.execute("SELECT COUNT(*) FROM prs")
            _count_row = cur.fetchone()
            total_prs = _count_row[0] if _count_row else 0
            if total_prs == 0:
                no_prs_yet = True
            active_prs = fetch_active_prs(cur)
            recent_outcomes = fetch_recent_outcomes(cur)

        ready_gaps, top_candidate = fetch_gap_queue(cur)
        best_wedge, worst_wedge = fetch_wedge_performance(cur)
        circuit_breaker = fetch_circuit_breaker(cur)
        submitted_today, cap = fetch_ramp_state(cur)
        system_state = fetch_system_state(cur)

        today = date.today()
        message = build_message(
            today=today,
            open_count=open_count,
            merged_count=merged_count,
            acceptance_rate_7d=acceptance_rate_7d,
            active_prs=active_prs,
            recent_outcomes=recent_outcomes,
            ready_gaps=ready_gaps,
            top_candidate=top_candidate,
            best_wedge=best_wedge,
            worst_wedge=worst_wedge,
            circuit_breaker=circuit_breaker if circuit_breaker else {"state": "unknown"},
            submitted_today=submitted_today,
            cap=cap,
            system_state=system_state,
            no_prs_yet=no_prs_yet,
        )

        if DRY_RUN:
            print("=== DRY RUN — message that would be sent ===")
            print(message)
            print("=== END DRY RUN ===")
            # Still update the timestamp on dry run so we track the run
            update_last_brief_at(cur, conn)
            cur.close()
            conn.close()
            print("BRIEF_SENT")
            return 0

        # Send via Telegram
        token = _get_secret(os.environ["TELEGRAM_TOKEN_SECRET"])
        send_telegram(token, NICK_CHAT_ID, message)

        # Update last_daily_brief_at
        update_last_brief_at(cur, conn)

        cur.close()
        conn.close()

        print("BRIEF_SENT")
        return 0

    except Exception as e:
        reason = str(e).replace("\n", " ")[:200]
        print(f"BRIEF_FAILED: {reason}", file=sys.stderr)
        # Also print to stdout so cron/scheduler captures it
        print(f"BRIEF_FAILED: {reason}")
        if os.environ.get("BRIEF_DEBUG"):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
