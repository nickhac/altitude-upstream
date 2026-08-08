#!/usr/bin/env python3
"""
pr-followup.py — altitude-upstream

Reads unprocessed events from pr_events and takes follow-up actions:

  - merged   → call acceptance-learning.py --pr-id N --event merged
  - closed   → call acceptance-learning.py --pr-id N --event closed
  - changes_requested → send Telegram alert (no auto-edit; human decision required)

Tracks progress with system_state key 'followup_last_processed_event_id' so each
event is processed exactly once even if the script is run repeatedly.

Usage:
    python3 scripts/pr-followup.py
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from db import get_connection  # noqa: E402


# ---------------------------------------------------------------------------
# Telegram helper (inline copy — avoids cross-script import)
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
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Failed to fetch secret {name}: {r.stderr.strip()}")
    return r.stdout.strip()


_telegram_token: str | None = None


def _get_telegram_token() -> str:
    global _telegram_token
    if _telegram_token is None:
        raw = _get_secret(os.environ["TELEGRAM_TOKEN_SECRET"])
        data = json.loads(raw)
        _telegram_token = data["token"]
    return _telegram_token  # type: ignore[return-value]


TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(text: str) -> None:
    """Fire an immediate Telegram alert. Failures are logged but not fatal."""
    try:
        token = _get_telegram_token()
    except Exception as exc:
        print(f"  [telegram] could not fetch token: {exc}")
        return

    r = subprocess.run(
        [
            "curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            "-d", f"chat_id={TELEGRAM_CHAT_ID}",
            "-d", f"text={text}",
            "-d", "parse_mode=HTML",
        ],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  [telegram] curl failed: {r.stderr.strip()[:200]}")
        return
    try:
        resp = json.loads(r.stdout)
        if not resp.get("ok"):
            print(f"  [telegram] API error: {resp.get('description', r.stdout[:200])}")
    except json.JSONDecodeError:
        print(f"  [telegram] unexpected response: {r.stdout[:200]}")


# ---------------------------------------------------------------------------
# system_state helpers
# ---------------------------------------------------------------------------

def get_last_processed_event_id(conn) -> int:
    """Return the id of the last pr_events row that was processed, or 0."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM system_state WHERE key = 'followup_last_processed_event_id'"
        )
        row = cur.fetchone()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0


def set_last_processed_event_id(conn, event_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES ('followup_last_processed_event_id', %s, NOW())
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (str(event_id),),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Event fetching
# ---------------------------------------------------------------------------

def fetch_unprocessed_events(conn, after_id: int) -> list[dict]:
    """Return all pr_events with id > after_id, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                e.id,
                e.pr_id,
                e.event_type,
                e.actor,
                e.body,
                e.detected_at,
                p.pr_url,
                p.pr_number,
                p.title,
                p.wedge_type,
                r.full_name AS repo
            FROM pr_events e
            JOIN prs p ON p.id = e.pr_id
            JOIN repos r ON r.id = p.repo_id
            WHERE e.id > %s
            ORDER BY e.id ASC
            """,
            (after_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Acceptance-learning invoker
# ---------------------------------------------------------------------------

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def invoke_acceptance_learning(pr_id: int, event: str) -> bool:
    """
    Call acceptance-learning.py for the given pr_id + event.
    Returns True on success (exit 0), False on failure.
    """
    cmd = [
        sys.executable,
        os.path.join(SCRIPTS_DIR, "acceptance-learning.py"),
        "--pr-id", str(pr_id),
        "--event", event,
    ]
    print(f"  [acceptance-learning] running: {' '.join(cmd[2:])}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.stdout.strip():
        for line in r.stdout.strip().splitlines():
            print(f"    {line}")
    if r.returncode != 0:
        print(f"  [acceptance-learning] FAILED (exit {r.returncode}): {r.stderr.strip()[:400]}")
        return False
    return True


# ---------------------------------------------------------------------------
# Per-event handlers
# ---------------------------------------------------------------------------

def handle_merged(conn, event: dict) -> None:
    pr_id = event["pr_id"]
    repo = event["repo"]
    title = event["title"] or event["pr_url"]
    print(f"  → MERGED: {repo}#{event['pr_number']} — {title[:60]}")

    ok = invoke_acceptance_learning(pr_id, "merged")
    if not ok:
        # Non-fatal: log and alert Nick, don't crash the loop
        send_telegram(
            f"⚠️ acceptance-learning failed for merged PR #{event['pr_number']} ({repo}).\n"
            f"Manual intervention may be needed."
        )


def handle_closed(conn, event: dict) -> None:
    pr_id = event["pr_id"]
    repo = event["repo"]
    title = event["title"] or event["pr_url"]
    print(f"  → CLOSED (unmerged): {repo}#{event['pr_number']} — {title[:60]}")

    ok = invoke_acceptance_learning(pr_id, "closed")
    if not ok:
        send_telegram(
            f"⚠️ acceptance-learning failed for closed PR #{event['pr_number']} ({repo}).\n"
            f"Manual intervention may be needed."
        )


def handle_changes_requested(conn, event: dict) -> None:
    """
    Changes requested: alert Nick.  No auto-edit — human decides whether to revise.
    """
    repo = event["repo"]
    number = event["pr_number"]
    url = event["pr_url"]
    title = event["title"] or url
    actor = event["actor"] or "maintainer"
    body = event["body"] or "(no comment)"

    print(f"  → CHANGES_REQUESTED: {repo}#{number} by {actor}")

    msg = (
        f"🔄 Changes requested — PR needs your review:\n"
        f"<b>{title[:80]}</b>\n"
        f"{url}\n"
        f"Reviewer: {actor}\n"
        f"Comment: {body[:200]}"
    )
    send_telegram(msg)


def handle_event(conn, event: dict) -> None:
    etype = event["event_type"]
    if etype == "merged":
        handle_merged(conn, event)
    elif etype == "closed":
        handle_closed(conn, event)
    elif etype == "changes_requested":
        handle_changes_requested(conn, event)
    else:
        # Unknown type — log and skip
        print(f"  → SKIP unknown event_type={etype!r} (event id={event['id']})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    conn = get_connection()
    try:
        last_id = get_last_processed_event_id(conn)
        events = fetch_unprocessed_events(conn, after_id=last_id)
        total = len(events)

        print(f"FOLLOWUP: {total} unprocessed event(s) since event id={last_id}")

        processed = 0
        max_processed_id = last_id

        for event in events:
            try:
                print(f"\n  Processing event id={event['id']} type={event['event_type']} "
                      f"pr_id={event['pr_id']} detected_at={event['detected_at']}")
                handle_event(conn, event)
                max_processed_id = max(max_processed_id, event["id"])
                processed += 1
            except Exception as exc:
                print(f"  [error] event id={event['id']}: {exc}")
                # Mark as processed anyway to avoid infinite retry on broken events
                max_processed_id = max(max_processed_id, event["id"])

        if max_processed_id > last_id:
            set_last_processed_event_id(conn, max_processed_id)
            print(f"\nFOLLOWUP: advanced cursor to event id={max_processed_id}")

        print(f"FOLLOWUP: processed {processed}/{total} events")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
