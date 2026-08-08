#!/usr/bin/env python3
"""
pr-monitor.py — altitude-upstream

Polls GitHub for status changes on all open PRs tracked in the `prs` Postgres
table.  Fires immediate Telegram alerts on merge/close/changes-requested and
records every event in the pr_events table.

Usage:
    python3 scripts/pr-monitor.py
"""

import json
import subprocess
import sys
import re
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# DB connection (reuse the shared helper)
# ---------------------------------------------------------------------------
sys.path.insert(0, __file__.rsplit("/", 1)[0])  # scripts/ on path
from db import get_connection  # noqa: E402


# ---------------------------------------------------------------------------
# GitHub helper
# ---------------------------------------------------------------------------

def gh_api(path: str, *, extra_args: list[str] | None = None) -> dict | list | None:
    """
    Call `gh api <path>` and return parsed JSON, or None on error.
    extra_args is appended to the gh invocation (e.g. ['--paginate']).
    """
    cmd = ["gh", "api", path]
    if extra_args:
        cmd.extend(extra_args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [github] gh api {path} failed: {r.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        print(f"  [github] JSON parse error for {path}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Telegram helper
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
    """Fire an immediate Telegram alert.  Failures are logged but not fatal."""
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
# Rejection reason classifier
# ---------------------------------------------------------------------------

REJECTION_PATTERNS: list[tuple[str, str]] = [
    (r"out.of.scope|not.in.scope|doesn.t fit|doesn.t belong|outside.+scope", "out_of_scope"),
    (r"wrong.approach|different.approach|not.the.right.way|alternative.approach", "wrong_approach"),
    (r"need.+test|missing.+test|add.+test|test coverage|tests are required", "needs_tests"),
    (r"\bspam\b|self.promotion|advertisement|unrelated", "spam"),
    (r"\bduplicate\b|already.exists|duplicate.of|closes.+#\d+|fixed.in", "duplicate"),
    (r"\bcla\b|contributor.license|sign.+cla|cla.+required", "cla_required"),
    (r"policy|guideline|code.of.conduct|maintainer|our.standard", "maintainer_policy"),
]


def classify_rejection(comments: list[dict]) -> str:
    """
    Look at the last ≤5 comments and return the most likely reason_code.
    Returns 'unknown' if nothing matches.
    """
    recent = comments[-5:] if len(comments) > 5 else comments
    combined = " ".join(c.get("body", "") or "" for c in recent).lower()
    for pattern, code in REJECTION_PATTERNS:
        if re.search(pattern, combined):
            return code
    return "unknown"


def get_last_maintainer_comment(comments: list[dict]) -> str:
    """Return a short snippet from the last comment body (≤200 chars)."""
    if not comments:
        return "(no comments)"
    last_body = comments[-1].get("body") or ""
    snippet = last_body.replace("\n", " ").strip()
    return snippet[:200] + ("…" if len(snippet) > 200 else "")


# ---------------------------------------------------------------------------
# Core monitor logic
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fetch_open_prs(conn) -> list[dict]:
    """Return rows from prs where status is open or changes_requested."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, r.full_name AS repo, p.pr_number, p.title,
                   p.pr_url AS url, p.status, p.wedge_type
            FROM prs p
            JOIN repos r ON r.id = p.repo_id
            WHERE p.status IN ('open', 'changes_requested', 'conflict')
            ORDER BY p.submitted_at ASC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def update_pr_status(conn, pr_id: int, new_status: str) -> None:
    resolved_at = now_utc() if new_status in ("merged", "closed") else None
    with conn.cursor() as cur:
        if resolved_at:
            cur.execute(
                "UPDATE prs SET status=%s, resolved_at=%s, last_checked_at=%s WHERE id=%s",
                (new_status, resolved_at, now_utc(), pr_id),
            )
        else:
            cur.execute(
                "UPDATE prs SET status=%s, last_checked_at=%s WHERE id=%s",
                (new_status, now_utc(), pr_id),
            )
    conn.commit()


def touch_last_checked(conn, pr_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE prs SET last_checked_at=%s WHERE id=%s",
            (now_utc(), pr_id),
        )
    conn.commit()


def insert_pr_event(conn, pr_id: int, event_type: str, actor: str | None, body: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pr_events (pr_id, event_type, actor, body, detected_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (pr_id, event_type, actor, body, now_utc()),
        )
    conn.commit()


def insert_rejection_reason(conn, pr_id: int, reason_code: str, raw_comments: str, repo_full_name: str = "", wedge_type: str = "") -> None:
    with conn.cursor() as cur:
        # Check if row exists (no unique constraint on pr_id, so manual upsert)
        cur.execute("SELECT id FROM rejection_reasons WHERE pr_id = %s LIMIT 1", (pr_id,))
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """
                UPDATE rejection_reasons
                SET reason_code = %s, reason_detail = %s, created_at = %s
                WHERE id = %s
                """,
                (reason_code, raw_comments[:4000] if raw_comments else None, now_utc(), existing[0]),
            )
        else:
            cur.execute(
                """
                INSERT INTO rejection_reasons (pr_id, repo_full_name, wedge_type, reason_code, reason_detail, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (pr_id, repo_full_name, wedge_type, reason_code, raw_comments[:4000] if raw_comments else None, now_utc()),
            )
    conn.commit()


def check_changes_requested(repo: str, pr_number: int) -> tuple[bool, str | None, str | None]:
    """
    Check PR reviews for 'CHANGES_REQUESTED' state.
    Returns (has_changes_requested, reviewer_login, comment_snippet).
    """
    reviews = gh_api(f"repos/{repo}/pulls/{pr_number}/reviews")
    if not reviews or not isinstance(reviews, list):
        return False, None, None

    # Find most recent CHANGES_REQUESTED review
    for review in reversed(reviews):
        if review.get("state") == "CHANGES_REQUESTED":
            actor = review.get("user", {}).get("login")
            body = review.get("body") or ""
            snippet = body.replace("\n", " ").strip()[:200]
            return True, actor, snippet or None

    return False, None, None


def process_pr(conn, pr: dict) -> bool:
    """
    Check one PR against GitHub and handle any state transition.
    Returns True if a state change was detected and handled.
    """
    repo = pr["repo"]
    number = pr["pr_number"]
    pr_id = pr["id"]
    old_status = pr["status"]
    title = pr["title"] or f"PR #{number}"
    url = pr["url"] or f"https://github.com/{repo}/pull/{number}"
    wedge_type = pr.get("wedge_type") or "unknown"

    print(f"  Checking {repo}#{number} ({old_status}) …", end=" ", flush=True)

    gh_data = gh_api(f"repos/{repo}/pulls/{number}")
    if gh_data is None or not isinstance(gh_data, dict):
        print("SKIP (API error)")
        return False

    gh_state: str = gh_data.get("state", "open")
    merged_at = gh_data.get("merged_at")
    merged_by = (gh_data.get("merged_by") or {}).get("login")

    # -----------------------------------------------------------------------
    # Determine new status
    # -----------------------------------------------------------------------
    if merged_at:
        new_status = "merged"
    elif gh_state == "closed":
        new_status = "closed"
    else:
        # Still open — check reviews for changes_requested
        cr, reviewer, cr_snippet = check_changes_requested(repo, number)
        if cr and old_status != "changes_requested":
            new_status = "changes_requested"
            print(f"CHANGES_REQUESTED (reviewer={reviewer})")
            update_pr_status(conn, pr_id, new_status)
            insert_pr_event(
                conn, pr_id,
                event_type="changes_requested",
                actor=reviewer,
                body=cr_snippet,
            )
            msg = (
                f"🔄 Changes requested on PR: {title}\n"
                f"{url}\n"
                f"{cr_snippet or ''}"
            )
            send_telegram(msg)
            return True

        # ------------------------------------------------------------------
        # Detect merge conflicts (mergeable_state == "dirty")
        # ------------------------------------------------------------------
        mergeable_state = gh_data.get("mergeable_state", "")
        if mergeable_state == "dirty" and old_status != "conflict":
            update_pr_status(conn, pr_id, "conflict")
            insert_pr_event(
                conn, pr_id,
                event_type="conflict",
                actor=None,
                body="mergeable_state=dirty: branch has conflicts with base branch",
            )
            msg = (
                f"⚠️ Merge conflict on PR: {title}\n"
                f"{url}\n"
                f"Branch has conflicts that must be resolved before merge."
            )
            send_telegram(msg)
            print("CONFLICT")
            return True

        # Conflict resolved — branch is clean again
        if old_status == "conflict" and mergeable_state in ("clean", "has_hooks", "unstable"):
            update_pr_status(conn, pr_id, "open")
            insert_pr_event(
                conn, pr_id,
                event_type="conflict_resolved",
                actor=None,
                body=f"mergeable_state={mergeable_state}: conflict resolved",
            )
            msg = (
                f"✅ Conflict resolved on PR: {title}\n"
                f"{url}\n"
                f"Branch is now mergeable."
            )
            send_telegram(msg)
            print("CONFLICT_RESOLVED")
            return True

        else:
            # No change
            touch_last_checked(conn, pr_id)
            print("no change")
            return False

    # -----------------------------------------------------------------------
    # State changed (merged or closed)
    # -----------------------------------------------------------------------
    if new_status == old_status:
        # Shouldn't happen (we only fetch open/changes_requested), but guard it
        touch_last_checked(conn, pr_id)
        print("no change")
        return False

    print(f"{new_status.upper()}")

    # Update DB
    update_pr_status(conn, pr_id, new_status)

    # -----------------------------------------------------------------------
    # Handle MERGE
    # -----------------------------------------------------------------------
    if new_status == "merged":
        # Fetch merge commit message
        merge_commit_sha = gh_data.get("merge_commit_sha")
        merge_commit_body = None
        if merge_commit_sha:
            commit_data = gh_api(f"repos/{repo}/commits/{merge_commit_sha}")
            if commit_data and isinstance(commit_data, dict):
                merge_commit_body = (
                    commit_data.get("commit", {}).get("message", "")
                )

        insert_pr_event(
            conn, pr_id,
            event_type="merged",
            actor=merged_by,
            body=merge_commit_body,
        )

        msg = (
            f"✅ PR MERGED: {title}\n"
            f"{url}\n"
            f"Repo: {repo} | Wedge: {wedge_type}"
        )
        send_telegram(msg)

    # -----------------------------------------------------------------------
    # Handle CLOSE (unmerged)
    # -----------------------------------------------------------------------
    elif new_status == "closed":
        # Fetch last 5 comments
        comments_data = gh_api(
            f"repos/{repo}/issues/{number}/comments",
            extra_args=["--paginate"],
        )
        comments: list[dict] = []
        if isinstance(comments_data, list):
            comments = comments_data

        last_comment_snippet = get_last_maintainer_comment(comments)
        reason_code = classify_rejection(comments)
        raw_comments_text = json.dumps([
            {"author": c.get("user", {}).get("login"), "body": (c.get("body") or "")[:500]}
            for c in comments[-5:]
        ])

        closer = gh_data.get("closed_by", {}) or {}
        closer_login = closer.get("login") if isinstance(closer, dict) else None

        insert_pr_event(
            conn, pr_id,
            event_type="closed",
            actor=closer_login,
            body=last_comment_snippet,
        )
        insert_rejection_reason(conn, pr_id, reason_code, raw_comments_text,
                                repo_full_name=repo, wedge_type=wedge_type)

        msg = (
            f"❌ PR CLOSED (not merged): {title}\n"
            f"{url}\n"
            f"Repo: {repo}\n"
            f"Last maintainer comment: {last_comment_snippet}"
        )
        send_telegram(msg)

    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    conn = get_connection()
    try:
        open_prs = fetch_open_prs(conn)
        total = len(open_prs)
        changes = 0

        print(f"MONITOR: found {total} open/pending PRs to check")

        for pr in open_prs:
            try:
                changed = process_pr(conn, pr)
                if changed:
                    changes += 1
            except Exception as exc:
                print(f"  [error] PR id={pr['id']} repo={pr['repo']}#{pr['pr_number']}: {exc}")
                # Ensure we still touch last_checked_at even on partial failures
                try:
                    touch_last_checked(conn, pr["id"])
                except Exception:
                    pass

        print(f"MONITOR: checked {total} PRs, {changes} changes detected")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
