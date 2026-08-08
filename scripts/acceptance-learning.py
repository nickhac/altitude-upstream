#!/usr/bin/env python3
"""
acceptance-learning.py — altitude-upstream

Called after pr-monitor.py detects a merge or close event for a PR submitted
by nickhac. Updates scores, manages the circuit breaker, escalates to Nick when
thresholds are crossed.

Usage:
    python3 scripts/acceptance-learning.py --pr-id <id> --event merged
    python3 scripts/acceptance-learning.py --pr-id <id> --event closed
    python3 scripts/acceptance-learning.py --recalibrate

Tables used:
    prs, repos, wedge_hypotheses, repo_intelligence, circuit_breaker,
    system_state, rejection_reasons
"""

import sys
import os
import argparse
import subprocess
import json
import math
import logging
from datetime import datetime, timezone, timedelta

# Add scripts/ dir to path so we can import db.py
sys.path.insert(0, os.path.dirname(__file__))
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("acceptance-learning")


# ---------------------------------------------------------------------------
# Helpers — Secrets / Telegram
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


def send_telegram(message: str) -> None:
    """Send a Telegram message to Nick via the Founder Channel bot."""
    try:
        bot_token = _get_secret(os.environ["TELEGRAM_TOKEN_SECRET"])
        chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not chat_id_raw:
            # Fall back: fetch from .hermes/.env if env not set
            env_file = os.path.expanduser("~/.hermes/.env")
            if os.path.exists(env_file):
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("TELEGRAM_CHAT_ID="):
                            chat_id_raw = line.split("=", 1)[1].strip()
                            break

        if not chat_id_raw:
            log.warning("TELEGRAM_CHAT_ID not set — skipping Telegram alert")
            return

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id_raw, "text": message})
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", url,
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=15,
        )
        resp = json.loads(r.stdout) if r.stdout else {}
        if not resp.get("ok"):
            log.warning("Telegram send failed: %s", r.stdout[:200])
        else:
            log.info("Telegram alert sent")
    except Exception as e:
        log.warning("Telegram error (non-fatal): %s", e)


def gbrain_write(note: str) -> None:
    """Write a qualitative note to Gbrain."""
    try:
        r = subprocess.run(
            ["gbrain", "write", note],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            log.warning("gbrain write failed (exit %d): %s", r.returncode, r.stderr[:200])
        else:
            log.info("Gbrain note written")
    except FileNotFoundError:
        log.warning("gbrain CLI not found — skipping Gbrain write")
    except Exception as e:
        log.warning("gbrain error (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# system_state helpers
# ---------------------------------------------------------------------------

def get_system_state(cur) -> dict:
    """Return all system_state rows as a dict keyed by key."""
    cur.execute("SELECT key, value FROM system_state")
    rows = cur.fetchall()
    return {row[0]: row[1] for row in rows}


def set_system_state(cur, key: str, value: str) -> None:
    cur.execute(
        """
        INSERT INTO system_state (key, value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (key, value),
    )


def get_ss_int(state: dict, key: str, default: int) -> int:
    try:
        return int(state.get(key, default))
    except (TypeError, ValueError):
        return default


def get_ss_float(state: dict, key: str, default: float) -> float:
    try:
        return float(state.get(key, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# circuit_breaker helpers
# ---------------------------------------------------------------------------

def upsert_circuit_breaker(cur, scope: str) -> None:
    """Ensure a circuit_breaker row exists for the given scope."""
    cur.execute(
        """
        INSERT INTO circuit_breaker (scope, state, consecutive_declines)
        VALUES (%s, 'closed', 0)
        ON CONFLICT (scope) DO NOTHING
        """,
        (scope,),
    )


def get_circuit_breaker(cur, scope: str) -> dict | None:
    cur.execute(
        "SELECT scope, state, consecutive_declines, tripped_reason, tripped_at "
        "FROM circuit_breaker WHERE scope = %s",
        (scope,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "scope": row[0],
        "state": row[1],
        "consecutive_declines": row[2],
        "tripped_reason": row[3],
        "tripped_at": row[4],
    }


def reset_circuit_breaker(cur, scope: str) -> None:
    """Reset consecutive_declines to 0 and set state=closed."""
    upsert_circuit_breaker(cur, scope)
    cur.execute(
        """
        UPDATE circuit_breaker
        SET state = 'closed', consecutive_declines = 0, tripped_reason = NULL, tripped_at = NULL
        WHERE scope = %s
        """,
        (scope,),
    )
    log.info("Circuit breaker reset: %s", scope)


def increment_circuit_breaker(cur, scope: str) -> int:
    """Increment consecutive_declines and return the new value."""
    upsert_circuit_breaker(cur, scope)
    cur.execute(
        """
        UPDATE circuit_breaker
        SET consecutive_declines = consecutive_declines + 1
        WHERE scope = %s
        RETURNING consecutive_declines
        """,
        (scope,),
    )
    row = cur.fetchone()
    new_val = row[0] if row else 1
    log.info("Circuit breaker incremented: %s → %d", scope, new_val)
    return new_val


def trip_circuit_breaker(cur, scope: str, reason: str) -> None:
    """Trip (open) the circuit breaker for a scope."""
    upsert_circuit_breaker(cur, scope)
    cur.execute(
        """
        UPDATE circuit_breaker
        SET state = 'open', tripped_reason = %s, tripped_at = NOW()
        WHERE scope = %s
        """,
        (reason, scope),
    )
    log.warning("Circuit breaker TRIPPED: %s — %s", scope, reason)


# ---------------------------------------------------------------------------
# wedge_hypotheses helpers
# ---------------------------------------------------------------------------

def upsert_wedge_hypothesis(cur, wedge_type: str) -> None:
    cur.execute(
        """
        INSERT INTO wedge_hypotheses (wedge_type, submitted_count, accepted_count, acceptance_rate)
        VALUES (%s, 0, 0, 0.0)
        ON CONFLICT (wedge_type) DO NOTHING
        """,
        (wedge_type,),
    )


def get_wedge_hypothesis(cur, wedge_type: str) -> dict | None:
    cur.execute(
        "SELECT wedge_type, submitted_count, accepted_count, acceptance_rate "
        "FROM wedge_hypotheses WHERE wedge_type = %s",
        (wedge_type,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "wedge_type": row[0],
        "submitted_count": row[1],
        "accepted_count": row[2],
        "acceptance_rate": row[3],
    }


# ---------------------------------------------------------------------------
# repo_intelligence helpers
# ---------------------------------------------------------------------------

def update_merge_rate(cur, repo_full_name: str, wedge_type: str, was_merged: bool) -> None:
    """
    Rolling average merge rate for (repo, wedge_type) stored as
    repo_intelligence.merge_rate_{wedge_type} keyed by repo_full_name.
    Uses EMA (alpha=0.3) so recent events have more weight.
    """
    col_name = f"merge_rate_{wedge_type}"
    cur.execute(
        "SELECT value FROM repo_intelligence WHERE repo_full_name = %s AND key = %s",
        (repo_full_name, col_name),
    )
    row = cur.fetchone()
    current_rate = float(row[0]) if row else 0.5  # start neutral
    alpha = 0.3
    new_val_float = 1.0 if was_merged else 0.0
    new_rate = alpha * new_val_float + (1.0 - alpha) * current_rate

    cur.execute(
        """
        INSERT INTO repo_intelligence (repo_full_name, key, value, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (repo_full_name, key)
        DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (repo_full_name, col_name, str(new_rate)),
    )


def set_repo_banned(cur, repo_full_name: str) -> None:
    cur.execute(
        """
        INSERT INTO repo_intelligence (repo_full_name, key, value, updated_at)
        VALUES (%s, 'banned', 'true', NOW())
        ON CONFLICT (repo_full_name, key)
        DO UPDATE SET value = 'true', updated_at = NOW()
        """,
        (repo_full_name,),
    )


# ---------------------------------------------------------------------------
# Acceptance rate 7-day recalibration
# ---------------------------------------------------------------------------

def recalculate_acceptance_rate_7d(cur) -> tuple[float, int]:
    """
    Read prs table for last 7 days and return (rate, count).
    'merged' counts as accepted; 'closed' counts as declined.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    cur.execute(
        """
        SELECT status FROM prs
        WHERE resolved_at >= %s AND status IN ('merged', 'closed')
        """,
        (cutoff,),
    )
    rows = cur.fetchall()
    total = len(rows)
    if total == 0:
        return 0.0, 0
    merged = sum(1 for r in rows if r[0] == "merged")
    rate = merged / total
    return rate, total


def store_acceptance_rate_7d(cur) -> tuple[float, int]:
    rate, count = recalculate_acceptance_rate_7d(cur)
    set_system_state(cur, "acceptance_rate_7d", str(rate))
    set_system_state(cur, "submitted_count_7d", str(count))
    log.info("acceptance_rate_7d = %.2f (%d PRs)", rate, count)
    return rate, count


# ---------------------------------------------------------------------------
# Knowledge file write-back
# ---------------------------------------------------------------------------

def update_knowledge_on_pr_resolution(
    repo_full_name: str,
    pr_url: str,
    outcome: str,
    reason: str,
    wedge_type: str,
    knowledge_root: str = "",
) -> None:
    """Append PR resolution learnings to docs/knowledge/ markdown files and git commit them.

    outcome: 'merged' → appends to '## What works'
             'closed'  → appends to '## What doesn't work'
    knowledge_root: override for testing (defaults to docs/knowledge/ in repo root).
    Fails silently — never raises exceptions to the caller.
    """
    try:
        from datetime import date as _date
        today = _date.today().isoformat()
        repo_slug = repo_full_name.replace("/", "-")
        if not knowledge_root:
            knowledge_root = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "docs", "knowledge"
            )

        # ---- 1. Repo knowledge file ----------------------------------------
        repo_md = os.path.join(knowledge_root, "repos", f"{repo_slug}.md")
        if os.path.exists(repo_md):
            with open(repo_md, "r", encoding="utf-8") as fh:
                content = fh.read()

            if outcome == "merged":
                entry = f"- PR {pr_url}: {wedge_type} accepted ✓ ({today})\n"
                section = "## What works"
            else:
                truncated_reason = reason[:120] if reason else "unknown"
                entry = f"- PR {pr_url}: {wedge_type} declined — {truncated_reason} ({today})\n"
                section = "## What doesn't work"

            if section in content:
                # Insert the entry right after the section heading line
                content = content.replace(section + "\n", section + "\n" + entry, 1)
            else:
                # Section not found — append it at end of file
                content = content.rstrip("\n") + f"\n\n{section}\n{entry}"

            with open(repo_md, "w", encoding="utf-8") as fh:
                fh.write(content)
            log.info("Updated repo knowledge: %s (%s)", repo_md, outcome)
        else:
            log.warning("Repo knowledge file not found (skipping): %s", repo_md)

        # ---- 2. Wedge-type knowledge file -----------------------------------
        wedge_md = os.path.join(knowledge_root, "wedge-types", f"{wedge_type}.md")
        if os.path.exists(wedge_md):
            with open(wedge_md, "r", encoding="utf-8") as fh:
                wt_content = fh.read()

            signal_section = "## Acceptance signal"
            if outcome == "merged":
                signal_entry = f"- {today}: accepted by {repo_full_name} ({pr_url})\n"
            else:
                signal_entry = f"- {today}: declined by {repo_full_name} — {reason[:80] if reason else 'unknown'} ({pr_url})\n"

            if signal_section in wt_content:
                wt_content = wt_content.replace(
                    signal_section + "\n", signal_section + "\n" + signal_entry, 1
                )
            else:
                wt_content = wt_content.rstrip("\n") + f"\n\n{signal_section}\n{signal_entry}"

            with open(wedge_md, "w", encoding="utf-8") as fh:
                fh.write(wt_content)
            log.info("Updated wedge-type knowledge: %s (%s)", wedge_md, outcome)
        else:
            log.warning("Wedge-type knowledge file not found (skipping): %s", wedge_md)

        # ---- 3. Git commit --------------------------------------------------
        repo_root = os.path.join(os.path.dirname(os.path.dirname(__file__)))
        short_repo = repo_full_name.split("/")[-1] if "/" in repo_full_name else repo_full_name
        commit_msg = f"learning: {short_repo} PR {outcome} ({wedge_type})"
        subprocess.run(
            ["git", "-C", repo_root, "add", "docs/knowledge/"],
            capture_output=True, text=True, timeout=30,
        )
        result = subprocess.run(
            ["git", "-C", repo_root, "commit", "-m", commit_msg],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            log.info("Knowledge commit: %s", commit_msg)
        else:
            # Nothing staged (files unchanged or already committed) is not an error
            log.debug("git commit output: %s", result.stdout.strip() or result.stderr.strip())

    except Exception as exc:
        log.warning("update_knowledge_on_pr_resolution failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# ACCEPTANCE (on merge)
# ---------------------------------------------------------------------------

def handle_acceptance(cur, pr: dict) -> None:
    """Process a merged PR: update scores, reset circuit breakers, write Gbrain."""
    pr_id = pr["id"]
    repo_id = pr["repo_id"]
    repo_full_name = pr["full_name"]
    wedge_type = pr["wedge_type"]
    title = pr.get("title") or pr.get("pr_url", "")
    submitted_at = pr.get("submitted_at")
    resolved_at = pr.get("resolved_at") or datetime.now(timezone.utc)

    log.info("ACCEPTANCE: PR #%d | %s | %s", pr_id, repo_full_name, wedge_type)

    # --- 1. Update wedge_hypotheses ---
    upsert_wedge_hypothesis(cur, wedge_type)
    cur.execute(
        """
        UPDATE wedge_hypotheses
        SET
            submitted_count = submitted_count + 1,
            accepted_count  = accepted_count + 1,
            acceptance_rate = (accepted_count + 1)::FLOAT / (submitted_count + 1),
            updated_at      = NOW()
        WHERE wedge_type = %s
        """,
        (wedge_type,),
    )

    # --- 2. Update repos.score (cap at 1.0) ---
    cur.execute(
        """
        UPDATE repos
        SET score = LEAST(score + 0.05, 1.0)
        WHERE id = %s
        """,
        (repo_id,),
    )

    # --- 3. Update repo_intelligence.merge_rate_{wedge_type} ---
    update_merge_rate(cur, repo_full_name, wedge_type, was_merged=True)

    # --- 4. Reset circuit breakers ---
    reset_circuit_breaker(cur, "global")
    reset_circuit_breaker(cur, f"repo:{repo_full_name}")

    # --- 5. Reset system_state.consecutive_global_declines ---
    set_system_state(cur, "consecutive_global_declines", "0")

    # --- 6. Gbrain note ---
    # Calculate days to merge
    days_to_merge = "?"
    if submitted_at and resolved_at:
        try:
            if isinstance(submitted_at, str):
                submitted_at = datetime.fromisoformat(submitted_at)
            if isinstance(resolved_at, str):
                resolved_at = datetime.fromisoformat(resolved_at)
            delta = (resolved_at - submitted_at).days
            days_to_merge = str(max(delta, 0))
        except Exception:
            pass

    gbrain_note = (
        f"PR merged: {repo_full_name} accepted {wedge_type} contribution. "
        f"Model: {title}. Merged in {days_to_merge} days. Maintainer receptive."
    )
    gbrain_write(gbrain_note)

    # --- 7. Write-back to docs/knowledge/ markdown files ---
    pr_url_str = pr.get("pr_url") or f"https://github.com/{repo_full_name}/pull/{pr.get('pr_number', '')}"
    update_knowledge_on_pr_resolution(
        repo_full_name=repo_full_name,
        pr_url=pr_url_str,
        outcome="merged",
        reason="",
        wedge_type=wedge_type,
    )

    log.info("Acceptance processing complete for PR #%d", pr_id)


# ---------------------------------------------------------------------------
# DECLINE (on close-unmerged)
# ---------------------------------------------------------------------------

def handle_decline(cur, pr: dict) -> None:
    """Process a closed (unmerged) PR: update scores, increment circuit breakers, write Gbrain."""
    pr_id = pr["id"]
    repo_id = pr["repo_id"]
    repo_full_name = pr["full_name"]
    wedge_type = pr["wedge_type"]
    title = pr.get("title") or pr.get("pr_url", "")

    log.info("DECLINE: PR #%d | %s | %s", pr_id, repo_full_name, wedge_type)

    # --- 1. Look up rejection reason ---
    cur.execute(
        "SELECT reason_code, reason_detail FROM rejection_reasons WHERE pr_id = %s",
        (pr_id,),
    )
    rej_row = cur.fetchone()
    reason_code = rej_row[0] if rej_row else "unknown"
    reason_detail = rej_row[1] if rej_row else ""

    # --- 2. Update wedge_hypotheses (submitted_count only) ---
    upsert_wedge_hypothesis(cur, wedge_type)
    cur.execute(
        """
        UPDATE wedge_hypotheses
        SET
            submitted_count = submitted_count + 1,
            acceptance_rate = CASE
                WHEN (submitted_count + 1) > 0
                THEN accepted_count::FLOAT / (submitted_count + 1)
                ELSE 0.0
            END,
            updated_at = NOW()
        WHERE wedge_type = %s
        """,
        (wedge_type,),
    )

    # --- 3. Update repos.score (floor at 0.0) ---
    cur.execute(
        """
        UPDATE repos
        SET score = GREATEST(score - 0.1, 0.0)
        WHERE id = %s
        """,
        (repo_id,),
    )

    # --- 4. Increment circuit breakers ---
    global_declines = increment_circuit_breaker(cur, "global")
    increment_circuit_breaker(cur, f"repo:{repo_full_name}")
    increment_circuit_breaker(cur, f"wedge:{wedge_type}")

    # --- 5. Update system_state.consecutive_global_declines ---
    cur.execute(
        "SELECT value FROM system_state WHERE key = 'consecutive_global_declines'"
    )
    row = cur.fetchone()
    current_consecutive = int(row[0]) if row else 0
    new_consecutive = current_consecutive + 1
    set_system_state(cur, "consecutive_global_declines", str(new_consecutive))

    # --- 6. Recalculate acceptance_rate_7d ---
    rate_7d, count_7d = store_acceptance_rate_7d(cur)

    # --- 7. Gbrain note ---
    gbrain_note = (
        f"PR declined: {repo_full_name} closed '{title}' without merging. "
        f"Wedge: {wedge_type}. Reason: {reason_code}."
    )
    gbrain_write(gbrain_note)

    # --- 8. Auto-write structured lesson from decline reason ---
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from lessons import learn_from_decline
        pr_url = pr.get('url') or f"https://github.com/{repo_full_name}/pull/{pr.get('pr_number','')}"
        lesson_id = learn_from_decline(
            repo_full_name=repo_full_name,
            pr_title=title,
            reason_code=reason_code,
            comment_snippet=reason_detail or '',
            wedge_type=wedge_type,
            source_url=pr_url,
        )
        if lesson_id:
            log.info("Auto-lesson written: id=%s reason=%s repo=%s", lesson_id, reason_code, repo_full_name)
    except Exception as e:
        log.warning("learn_from_decline failed (non-fatal): %s", e)

    # --- 9. Write-back to docs/knowledge/ markdown files ---
    pr_url_str = pr.get("pr_url") or f"https://github.com/{repo_full_name}/pull/{pr.get('pr_number', '')}"
    decline_reason_text = f"{reason_code}: {reason_detail}" if reason_detail else reason_code
    update_knowledge_on_pr_resolution(
        repo_full_name=repo_full_name,
        pr_url=pr_url_str,
        outcome="closed",
        reason=decline_reason_text,
        wedge_type=wedge_type,
    )

    # --- 10. Circuit breaker checks ---
    check_circuit_breakers(
        cur,
        repo_full_name=repo_full_name,
        wedge_type=wedge_type,
        reason_code=reason_code,
        reason_detail=reason_detail,
        new_consecutive=new_consecutive,
        rate_7d=rate_7d,
        count_7d=count_7d,
    )

    log.info("Decline processing complete for PR #%d", pr_id)


# ---------------------------------------------------------------------------
# CIRCUIT BREAKER CHECKS
# ---------------------------------------------------------------------------

def check_circuit_breakers(
    cur,
    repo_full_name: str,
    wedge_type: str,
    reason_code: str,
    reason_detail: str,
    new_consecutive: int,
    rate_7d: float,
    count_7d: int,
) -> None:
    """Evaluate all trip conditions and send alerts if any are triggered."""

    # Read thresholds from system_state
    cur.execute("SELECT key, value FROM system_state")
    state = {row[0]: row[1] for row in cur.fetchall()}
    threshold_consecutive = get_ss_int(state, "circuit_breaker_threshold_consecutive", 3)
    threshold_rate_7d = get_ss_float(state, "circuit_breaker_threshold_rate_7d", 0.30)

    # -----------------------------------------------------------------------
    # (a) Consecutive global declines
    # -----------------------------------------------------------------------
    if new_consecutive >= threshold_consecutive:
        trip_circuit_breaker(
            cur, "global",
            f"{new_consecutive} consecutive declines, last: {repo_full_name}"
        )
        set_system_state(cur, "submission_paused", "true")
        set_system_state(
            cur, "pause_reason",
            f"Circuit breaker: {new_consecutive} consecutive declines"
        )
        alert = (
            f"🚨 CIRCUIT BREAKER TRIPPED: {new_consecutive} consecutive declines.\n"
            f"Submission PAUSED.\n"
            f"Last decline: {repo_full_name} — {reason_code}: {reason_detail}\n"
            f"Reply YES to resume after reviewing."
        )
        send_telegram(alert)
        log.warning("CIRCUIT BREAKER (a): consecutive global declines = %d", new_consecutive)

    # -----------------------------------------------------------------------
    # (b) Wedge type has 2+ declines with no merges
    # -----------------------------------------------------------------------
    wh = get_wedge_hypothesis(cur, wedge_type)
    if wh:
        wedge_declines = wh["submitted_count"] - wh["accepted_count"]
        if wedge_declines >= 2 and wh["accepted_count"] == 0:
            trip_circuit_breaker(
                cur, f"wedge:{wedge_type}",
                f"{wedge_declines} declines, 0 merges"
            )
            alert = (
                f"⚠️ Wedge type paused: {wedge_type} has {wedge_declines} declines, 0 merges.\n"
                f"Removing from queue pending review."
            )
            send_telegram(alert)
            log.warning("CIRCUIT BREAKER (b): wedge %s has %d declines, 0 merges", wedge_type, wedge_declines)

    # -----------------------------------------------------------------------
    # (c) Acceptance rate 7d below threshold and enough data
    # -----------------------------------------------------------------------
    if count_7d >= 3 and rate_7d < threshold_rate_7d:
        trip_circuit_breaker(
            cur, "global",
            f"acceptance_rate_7d={rate_7d:.2f} < threshold={threshold_rate_7d:.2f} over {count_7d} PRs"
        )
        set_system_state(cur, "submission_paused", "true")
        set_system_state(
            cur, "pause_reason",
            f"Low 7-day acceptance rate: {rate_7d:.0%} over {count_7d} PRs"
        )
        rate_pct = round(rate_7d * 100)
        alert = (
            f"🚨 Acceptance rate dropped to {rate_pct}% over 7 days ({count_7d} PRs).\n"
            f"Submission PAUSED.\n"
            f"Reply YES to resume."
        )
        send_telegram(alert)
        log.warning("CIRCUIT BREAKER (c): rate_7d=%.2f count_7d=%d", rate_7d, count_7d)

    # -----------------------------------------------------------------------
    # (d) Spam flag
    # -----------------------------------------------------------------------
    if reason_code and reason_code.lower() == "spam":
        set_repo_banned(cur, repo_full_name)
        set_system_state(cur, "submission_paused", "true")
        set_system_state(
            cur, "pause_reason",
            f"Spam flag from {repo_full_name} — reviewing"
        )
        alert = (
            f"🚨 SPAM FLAG: {repo_full_name} marked our PR as spam.\n"
            f"Repo BANNED permanently.\n"
            f"No further contributions to this repo."
        )
        send_telegram(alert)
        log.warning("CIRCUIT BREAKER (d): SPAM flag from %s", repo_full_name)


# ---------------------------------------------------------------------------
# PR lookup
# ---------------------------------------------------------------------------

def load_pr(cur, pr_id: int) -> dict | None:
    """Load a PR row joined with repo full_name."""
    cur.execute(
        """
        SELECT
            p.id,
            p.repo_id,
            p.pr_url,
            p.pr_number,
            p.status,
            p.wedge_type,
            p.submitted_at,
            p.resolved_at,
            r.full_name,
            p.pr_url AS title
        FROM prs p
        JOIN repos r ON r.id = p.repo_id
        WHERE p.id = %s
        """,
        (pr_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "repo_id": row[1],
        "pr_url": row[2],
        "pr_number": row[3],
        "status": row[4],
        "wedge_type": row[5],
        "submitted_at": row[6],
        "resolved_at": row[7],
        "full_name": row[8],
        "title": row[9],  # fallback: use pr_url as title if no separate title column
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_event(pr_id: int, event: str) -> int:
    """Process a single merged or closed event for a PR."""
    if event not in ("merged", "closed"):
        log.error("Unknown event: %s (must be 'merged' or 'closed')", event)
        return 1

    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                pr = load_pr(cur, pr_id)
                if pr is None:
                    log.error("PR #%d not found in database", pr_id)
                    return 1

                log.info(
                    "Processing event=%s for PR #%d (%s) repo=%s wedge=%s",
                    event, pr_id, pr["pr_url"], pr["full_name"], pr["wedge_type"],
                )

                if event == "merged":
                    handle_acceptance(cur, pr)
                else:
                    handle_decline(cur, pr)

                # Always recalculate acceptance_rate_7d at end of every run
                store_acceptance_rate_7d(cur)

        log.info("Done: event=%s pr_id=%d", event, pr_id)
        return 0
    finally:
        conn.close()


def run_recalibrate() -> int:
    """Recalculate all rates without processing any event."""
    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                log.info("Recalibrating all rates…")

                # Acceptance rate 7d
                rate, count = store_acceptance_rate_7d(cur)
                log.info("acceptance_rate_7d recalibrated: %.2f (%d PRs)", rate, count)

                # Recalculate acceptance_rate per wedge type from actual data
                cur.execute("SELECT wedge_type FROM wedge_hypotheses")
                wedge_types = [r[0] for r in cur.fetchall()]
                for wt in wedge_types:
                    cur.execute(
                        """
                        UPDATE wedge_hypotheses
                        SET
                            acceptance_rate = CASE
                                WHEN submitted_count > 0
                                THEN accepted_count::FLOAT / submitted_count
                                ELSE 0.0
                            END,
                            updated_at = NOW()
                        WHERE wedge_type = %s
                        """,
                        (wt,),
                    )
                log.info("Recalibrated %d wedge hypothesis acceptance rates", len(wedge_types))

        log.info("Recalibration complete")
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acceptance learning loop for altitude-upstream"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--pr-id", type=int, metavar="ID",
        help="Internal Postgres ID of the PR row",
    )
    group.add_argument(
        "--recalibrate", action="store_true",
        help="Recalculate all rates without processing an event",
    )
    parser.add_argument(
        "--event", choices=["merged", "closed"],
        help="Event type (required with --pr-id)",
    )

    args = parser.parse_args()

    if args.recalibrate:
        return run_recalibrate()

    if args.pr_id is not None:
        if not args.event:
            parser.error("--event is required when --pr-id is specified")
        return run_event(args.pr_id, args.event)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
