#!/usr/bin/env python3
"""
self_improve.py — altitude-upstream self-improvement engine

Runs daily (after the contribution loop). Reads the system's own state
from Postgres, diagnoses what's working and what isn't, generates and
applies targeted DB/gap-scan fixes, commits changes, and reports to Nick
via Telegram.

Usage:
    python3 scripts/self_improve.py
    python3 scripts/self_improve.py --dry-run     # print actions, don't execute
    python3 scripts/self_improve.py --verbose     # also print Bedrock diagnosis
"""

import sys
import os
import json
import argparse
import subprocess
import traceback
from datetime import datetime, timezone, date

# ---------------------------------------------------------------------------
# Path bootstrap — allow running from any cwd
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as db_module

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NICK_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")
BEDROCK_REGION = os.environ.get("AWS_REGION", "us-east-1")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPTS_DIR)

# Repos we track for queue health
TRACKED_REPOS = ["litellm", "vllm", "langchain", "llama_index"]


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="altitude-upstream self-improvement engine")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--verbose", action="store_true", help="Print Bedrock diagnosis output")
    return parser.parse_args()


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
# Step 1: Collect evidence
# ---------------------------------------------------------------------------

def collect_evidence(conn) -> dict:
    """Collect system state evidence from Postgres."""
    cur = conn.cursor()
    evidence = {}

    # --- PRs submitted last 7 days ---
    if table_exists(cur, "prs"):
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'open')   AS open_7d,
                COUNT(*) FILTER (WHERE status = 'merged') AS merged_7d,
                COUNT(*) FILTER (WHERE status = 'closed') AS closed_7d,
                COUNT(*) AS total_7d
            FROM prs
            WHERE submitted_at >= NOW() - INTERVAL '7 days'
        """)
        row = cur.fetchone()
        open_7d, merged_7d, closed_7d, total_7d = row
        resolved_7d = merged_7d + closed_7d
        evidence["prs_7d"] = {
            "open": open_7d,
            "merged": merged_7d,
            "closed": closed_7d,
            "total": total_7d,
            "acceptance_rate_pct": (
                round(merged_7d / resolved_7d * 100) if resolved_7d > 0 else None
            ),
        }

        # --- Acceptance rate by repo ---
        has_repos = table_exists(cur, "repos")
        if has_repos:
            cur.execute("""
                SELECT
                    r.full_name,
                    COUNT(*) FILTER (WHERE p.status = 'merged') AS merged,
                    COUNT(*) FILTER (WHERE p.status IN ('merged', 'closed')) AS resolved
                FROM prs p
                LEFT JOIN repos r ON r.id = p.repo_id
                WHERE p.submitted_at >= NOW() - INTERVAL '7 days'
                GROUP BY r.full_name
                ORDER BY r.full_name
            """)
            rows = cur.fetchall()
            repo_rates = {}
            for full_name, merged, resolved in rows:
                repo_rates[full_name or "unknown"] = {
                    "merged": merged,
                    "resolved": resolved,
                    "acceptance_rate_pct": (
                        round(merged / resolved * 100) if resolved > 0 else None
                    ),
                }
            evidence["acceptance_by_repo"] = repo_rates

        # --- Acceptance rate by wedge_type ---
        has_wedge = column_exists(cur, "prs", "wedge_type")
        if has_wedge:
            cur.execute("""
                SELECT
                    wedge_type,
                    COUNT(*) FILTER (WHERE status = 'merged') AS merged,
                    COUNT(*) FILTER (WHERE status IN ('merged', 'closed')) AS resolved
                FROM prs
                WHERE submitted_at >= NOW() - INTERVAL '7 days'
                GROUP BY wedge_type
                ORDER BY wedge_type
            """)
            rows = cur.fetchall()
            wedge_rates = {}
            for wedge_type, merged, resolved in rows:
                wedge_rates[wedge_type or "unknown"] = {
                    "merged": merged,
                    "resolved": resolved,
                    "acceptance_rate_pct": (
                        round(merged / resolved * 100) if resolved > 0 else None
                    ),
                }
            evidence["acceptance_by_wedge_type"] = wedge_rates
    else:
        evidence["prs_7d"] = {"note": "prs table not yet created"}

    # --- Blocked gaps last 7 days ---
    if table_exists(cur, "gaps"):
        has_block_reason = column_exists(cur, "gaps", "block_reason")
        has_blocked_at = column_exists(cur, "gaps", "blocked_at")

        if has_block_reason and has_blocked_at:
            cur.execute("""
                SELECT block_reason, COUNT(*) AS cnt
                FROM gaps
                WHERE status = 'blocked'
                  AND blocked_at >= NOW() - INTERVAL '7 days'
                GROUP BY block_reason
                ORDER BY cnt DESC
            """)
        elif has_block_reason:
            cur.execute("""
                SELECT block_reason, COUNT(*) AS cnt
                FROM gaps
                WHERE status = 'blocked'
                GROUP BY block_reason
                ORDER BY cnt DESC
            """)
        else:
            cur.execute("""
                SELECT 'unknown' AS block_reason, COUNT(*) AS cnt
                FROM gaps
                WHERE status = 'blocked'
            """)

        rows = cur.fetchall()
        evidence["blocked_gaps_7d"] = [
            {"block_reason": r[0] or "unspecified", "count": r[1]} for r in rows
        ]

        # --- Gap queue health by repo (XS/S open gaps) ---
        has_effort = column_exists(cur, "gaps", "effort")
        has_repos_col = column_exists(cur, "gaps", "repo_id")
        has_repos_table = table_exists(cur, "repos")

        if has_effort and has_repos_col and has_repos_table:
            cur.execute("""
                SELECT
                    r.full_name,
                    COUNT(*) FILTER (WHERE g.effort IN ('XS', 'S')) AS xs_s_open,
                    COUNT(*) AS total_open
                FROM gaps g
                LEFT JOIN repos r ON r.id = g.repo_id
                WHERE g.status = 'open'
                GROUP BY r.full_name
                ORDER BY xs_s_open DESC
            """)
        elif has_effort:
            cur.execute("""
                SELECT
                    'unknown' AS full_name,
                    COUNT(*) FILTER (WHERE effort IN ('XS', 'S')) AS xs_s_open,
                    COUNT(*) AS total_open
                FROM gaps
                WHERE status = 'open'
            """)
        else:
            cur.execute("""
                SELECT
                    'unknown' AS full_name,
                    0 AS xs_s_open,
                    COUNT(*) AS total_open
                FROM gaps
                WHERE status = 'open'
            """)

        rows = cur.fetchall()
        queue_health = {}
        for full_name, xs_s_open, total_open in rows:
            # Normalise repo name to short form
            short_name = (full_name or "unknown").split("/")[-1].replace("-", "_").lower()
            queue_health[short_name] = {
                "xs_s_open": xs_s_open,
                "total_open": total_open,
            }
        evidence["gap_queue_health"] = queue_health

        # Total open XS/S gaps
        if has_effort:
            cur.execute("""
                SELECT COUNT(*) FROM gaps
                WHERE status = 'open' AND effort IN ('XS', 'S')
            """)
            evidence["total_open_xs_s"] = cur.fetchone()[0]
        else:
            cur.execute("SELECT COUNT(*) FROM gaps WHERE status = 'open'")
            evidence["total_open_xs_s"] = cur.fetchone()[0]
    else:
        evidence["gap_queue_health"] = {}
        evidence["total_open_xs_s"] = 0
        evidence["blocked_gaps_7d"] = []

    # --- System state ---
    if table_exists(cur, "system_state"):
        cur.execute("SELECT key, value FROM system_state")
        evidence["system_state"] = {k: v for k, v in cur.fetchall()}
    else:
        evidence["system_state"] = {}

    # --- Today's submission progress ---
    if table_exists(cur, "ramp_state"):
        cur.execute("""
            SELECT submitted_today, cap
            FROM ramp_state
            WHERE date = CURRENT_DATE
            ORDER BY id DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            evidence["today_submitted"] = row[0]
            evidence["today_cap"] = row[1]
        else:
            evidence["today_submitted"] = 0
            evidence["today_cap"] = 5

    cur.close()
    return evidence


# ---------------------------------------------------------------------------
# Step 2: Diagnose via Bedrock
# ---------------------------------------------------------------------------

def call_bedrock(evidence: dict, verbose: bool) -> dict:
    """Call Bedrock with the evidence and return the parsed action plan."""
    import boto3

    evidence_json = json.dumps(evidence, indent=2, default=str)

    prompt = f"""You are the self-improvement engine for altitude-upstream, an autonomous OSS contribution system.

Your goal: submit 5 high-quality PRs per day that get merged.

## Valid DB schema (ONLY use these tables and columns in SQL):
- gaps: id, repo_id, wedge_type, description, effort, status, score, source_url
  status values: 'open', 'in_progress', 'submitted', 'blocked', 'deprioritised', 'invalid'
  effort values: 'XS', 'S', 'M', 'L'
- repos: id, full_name, tier, score
  tier=1 is active, tier=2 is inactive
- prs: id, gap_id, repo_id, pr_url, pr_number, status, wedge_type, submitted_at
  status values: 'open', 'merged', 'closed'
- system_state: key, value, updated_at (key-value store)
- wedge_hypotheses: id, wedge_type, submitted_count, accepted_count, acceptance_rate
- repo_intelligence: id, repo_full_name, merge_rate_model_registry, merge_rate_broken_integration
- lessons: id, scope, trigger_phase, title, description, invalidated_at
- circuit_breaker: id, scope, state, tripped_reason
  state values: 'closed' (ok), 'open' (tripped/blocked)

## Important context:
- The system was built on 2026-08-03. All PRs are 1-3 days old — 0 merges is NORMAL, not a failure.
- PRs take 1-7 days to merge in typical OSS repos. Do not diagnose "0 merges" as a problem.
- The PAT permission issue was FIXED on 2026-08-04. Ignore any DB state suggesting it's still broken.
- missing_documentation PRs ARE working and should NOT be deprioritised — they are our most reliable wedge type.
- Focus diagnosis on: queue runway, blocked gap patterns, wedge type distribution, whether submissions are happening.

## Current system state:
{evidence_json}

Diagnose what is working and what isn't. Then output a JSON action plan:
{{
  "diagnosis": "2-3 sentence summary",
  "actions": [
    {{
      "type": "db_update",
      "description": "what to do",
      "sql": "VALID SQL using only the schema above",
      "priority": 1
    }}
  ]
}}

Action types:
- db_update: SQL against gaps/repos/system_state/wedge_hypotheses only
- gap_scan: trigger gap-scanner.py --vector all (use when XS/S queue < 20 per repo)
- deprioritise: UPDATE gaps SET status='deprioritised' WHERE effort IN (...) AND ...
- replenish: run gap-scanner.py --vector 3 (doc gaps, fast)

Constraints:
- Max 5 actions per run
- No script modifications — DB and gap queue only
- Never set submission_paused=true unless circuit_breaker is actually tripped
- Never change daily_submission_cap above 5
- Only use table/column names from the schema above
- Output ONLY valid JSON with no markdown fences or extra text
"""

    client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
    })

    response = client.invoke_model(
        modelId=BEDROCK_MODEL,
        body=body,
        contentType="application/json",
        accept="application/json",
    )

    result = json.loads(response["body"].read())
    raw_text = result["content"][0]["text"].strip()

    if verbose:
        print("\n--- Bedrock raw response ---")
        print(raw_text)
        print("----------------------------\n")

    # Strip markdown fences if present
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        # Drop first and last fence lines
        inner = []
        in_block = False
        for line in lines:
            if line.startswith("```") and not in_block:
                in_block = True
                continue
            if line.startswith("```") and in_block:
                break
            if in_block:
                inner.append(line)
        raw_text = "\n".join(inner)

    plan = json.loads(raw_text)
    return plan


# ---------------------------------------------------------------------------
# Step 3: Execute actions
# ---------------------------------------------------------------------------

def execute_action(action: dict, conn, dry_run: bool, verbose: bool) -> str:
    """Execute a single action and return a human-readable result string."""
    action_type = action.get("type", "").lower()
    description = action.get("description", "")

    print(f"  [{action_type}] {description}")

    if action_type == "db_update":
        sql = action.get("sql", "").strip()
        if not sql:
            return f"⚠️ db_update: no SQL provided — skipped"

        # Safety guardrails
        sql_upper = sql.upper()
        if "DELETE FROM PRS" in sql_upper or "DROP TABLE" in sql_upper:
            return f"⛔ db_update: unsafe SQL blocked — skipped"
        if "DAILY_SUBMISSION_CAP" in sql_upper and ">" in sql_upper:
            # Naive check: don't allow cap increases
            return f"⛔ db_update: daily_submission_cap increase blocked"

        if dry_run:
            return f"[dry-run] db_update would run: {sql}"

        try:
            cur = conn.cursor()
            cur.execute(sql)
            conn.commit()
            cur.close()
            return f"✅ db_update executed: {description}"
        except Exception as e:
            conn.rollback()
            return f"❌ db_update failed: {e}"

    elif action_type == "gap_scan":
        if dry_run:
            return f"[dry-run] gap_scan: would run gap-scanner.py --vector all"

        cmd = [sys.executable, os.path.join(SCRIPTS_DIR, "gap-scanner.py"), "--vector", "all"]
        if verbose:
            print(f"    Running: {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                return f"✅ gap_scan completed successfully"
            else:
                return f"❌ gap_scan failed (rc={r.returncode}): {r.stderr[:200]}"
        except subprocess.TimeoutExpired:
            return f"⏱️ gap_scan timed out after 5 minutes"
        except Exception as e:
            return f"❌ gap_scan error: {e}"

    elif action_type == "deprioritise":
        sql = action.get("sql", "").strip()
        if sql:
            # Use the provided SQL if it's a safe UPDATE/deprioritise
            if not sql.upper().startswith("UPDATE"):
                return f"⛔ deprioritise: only UPDATE statements allowed — skipped"
            if dry_run:
                return f"[dry-run] deprioritise would run: {sql}"
            try:
                cur = conn.cursor()
                cur.execute(sql)
                conn.commit()
                cur.close()
                return f"✅ deprioritise executed: {description}"
            except Exception as e:
                conn.rollback()
                return f"❌ deprioritise failed: {e}"
        else:
            # Build deprioritise SQL from description keywords
            wedge_type = action.get("wedge_type", "")
            effort = action.get("effort", "")
            if not wedge_type and not effort:
                return f"⚠️ deprioritise: no wedge_type or effort specified — skipped"

            conditions = []
            params = []
            if wedge_type:
                conditions.append("wedge_type = %s")
                params.append(wedge_type)
            if effort:
                conditions.append("effort = %s")
                params.append(effort)

            where_clause = " AND ".join(conditions)
            built_sql = f"UPDATE gaps SET status='deprioritised' WHERE {where_clause} AND status='open'"

            if dry_run:
                return f"[dry-run] deprioritise would run: {built_sql}"
            try:
                cur = conn.cursor()
                cur.execute(built_sql, params)
                conn.commit()
                cur.close()
                return f"✅ deprioritise executed: {description}"
            except Exception as e:
                conn.rollback()
                return f"❌ deprioritise failed: {e}"

    elif action_type == "replenish":
        if dry_run:
            return f"[dry-run] replenish: would run gap-scanner.py --vector 3"

        cmd = [sys.executable, os.path.join(SCRIPTS_DIR, "gap-scanner.py"), "--vector", "3"]
        if verbose:
            print(f"    Running: {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                return f"✅ replenish (doc gaps scan) completed"
            else:
                return f"❌ replenish failed (rc={r.returncode}): {r.stderr[:200]}"
        except subprocess.TimeoutExpired:
            return f"⏱️ replenish timed out after 5 minutes"
        except Exception as e:
            return f"❌ replenish error: {e}"

    else:
        return f"⚠️ Unknown action type '{action_type}' — skipped"


def execute_plan(plan: dict, conn, dry_run: bool, verbose: bool) -> list:
    """Execute all actions in the plan, ordered by priority. Returns list of result strings."""
    actions = plan.get("actions", [])
    # Sort by priority ascending (1 = highest)
    actions_sorted = sorted(actions, key=lambda a: a.get("priority", 99))
    # Cap at 5
    actions_sorted = actions_sorted[:5]

    results = []
    for i, action in enumerate(actions_sorted, 1):
        print(f"\nAction {i}/{len(actions_sorted)}:")
        result = execute_action(action, conn, dry_run, verbose)
        print(f"    → {result}")
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Step 4: Git commit & push
# ---------------------------------------------------------------------------

def git_commit_and_push(today: date, dry_run: bool) -> str:
    """Commit and push any changes in the altitude-upstream repo."""
    commit_msg = f"chore(auto): self-improvement run {today.isoformat()}"

    if dry_run:
        print(f"\n[dry-run] git add -A && git commit -m '{commit_msg}' && git push origin main")
        return "git commit skipped (dry-run)"

    try:
        r_status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=PROJECT_DIR,
        )
        if not r_status.stdout.strip():
            print("\nNo git changes to commit.")
            return "no changes to commit"

        subprocess.run(
            ["git", "add", "-A"],
            check=True, cwd=PROJECT_DIR,
        )
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            check=True, cwd=PROJECT_DIR,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            check=True, cwd=PROJECT_DIR,
        )
        return f"✅ committed and pushed: {commit_msg}"
    except subprocess.CalledProcessError as e:
        return f"❌ git operation failed: {e}"


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_queue_health_lines(evidence: dict) -> list:
    """Build per-repo queue health lines for the Telegram report."""
    queue_health = evidence.get("gap_queue_health", {})
    lines = []
    for repo_key in TRACKED_REPOS:
        # Try exact match and common variants
        candidates = [
            repo_key,
            repo_key.replace("_", "-"),
            f"nickhac/{repo_key}",
            f"BerriAI/{repo_key}",
        ]
        # Also search for partial key matches
        data = None
        for c in candidates:
            if c in queue_health:
                data = queue_health[c]
                break
        if data is None:
            # partial match
            for k, v in queue_health.items():
                if repo_key in k.lower():
                    data = v
                    break

        n = data["xs_s_open"] if data else "?"
        lines.append(f"- {repo_key}: {n} XS/S gaps")

    return lines


def build_telegram_message(
    today: date,
    diagnosis: str,
    action_results: list,
    evidence: dict,
    git_result: str,
) -> str:
    lines = []
    lines.append(f"🔧 <b>Self-Improvement Run — {today.strftime('%Y-%m-%d')}</b>")
    lines.append("")
    lines.append(f"<b>Diagnosis:</b> {diagnosis}")
    lines.append("")
    lines.append("<b>Actions taken:</b>")
    if action_results:
        for r in action_results:
            lines.append(f"• {r}")
    else:
        lines.append("• No actions taken")
    lines.append("")
    lines.append("<b>Queue health:</b>")
    for ql in build_queue_health_lines(evidence):
        lines.append(ql)
    lines.append("")
    lines.append(f"<b>Git:</b> {git_result}")
    lines.append("")
    lines.append("Next run: tomorrow 10:00 UTC")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Update system_state
# ---------------------------------------------------------------------------

def record_run(conn, today: date, diagnosis: str, dry_run: bool) -> None:
    """Record this self-improvement run in system_state."""
    if dry_run:
        return

    try:
        cur = conn.cursor()
        if table_exists(cur, "system_state"):
            cur.execute("""
                INSERT INTO system_state (key, value, updated_at)
                VALUES ('last_self_improve_at', %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, (today.isoformat(),))
            cur.execute("""
                INSERT INTO system_state (key, value, updated_at)
                VALUES ('last_self_improve_diagnosis', %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, (diagnosis[:500],))
            conn.commit()
        cur.close()
    except Exception as e:
        print(f"  Warning: could not record run in system_state: {e}")
        conn.rollback()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    dry_run = args.dry_run
    verbose = args.verbose
    today = datetime.now(timezone.utc).date()

    if dry_run:
        print("🔍 DRY RUN — no changes will be made\n")

    print(f"🔧 altitude-upstream self-improvement engine — {today.isoformat()}")
    print("=" * 60)

    # -----------------------------------------------------------------------
    # Step 1: Collect evidence
    # -----------------------------------------------------------------------
    print("\n📊 Step 1: Collecting evidence from DB...")
    try:
        conn = db_module.get_connection()
        evidence = collect_evidence(conn)
        print(f"  Gathered: {list(evidence.keys())}")
    except Exception as e:
        print(f"❌ Failed to collect evidence: {e}")
        traceback.print_exc()
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Step 2: Diagnose via Bedrock
    # -----------------------------------------------------------------------
    print("\n🧠 Step 2: Calling Bedrock for diagnosis...")
    try:
        plan = call_bedrock(evidence, verbose)
        diagnosis = plan.get("diagnosis", "No diagnosis provided.")
        actions = plan.get("actions", [])
        print(f"  Diagnosis: {diagnosis}")
        print(f"  Actions proposed: {len(actions)}")
    except Exception as e:
        print(f"❌ Bedrock call failed: {e}")
        traceback.print_exc()
        # Graceful degradation: continue with a minimal plan
        diagnosis = f"Bedrock unavailable: {e}"
        plan = {
            "diagnosis": diagnosis,
            "actions": [],
        }

    # -----------------------------------------------------------------------
    # Step 3: Execute actions
    # -----------------------------------------------------------------------
    print("\n⚙️  Step 3: Executing actions...")
    action_results = execute_plan(plan, conn, dry_run, verbose)

    # -----------------------------------------------------------------------
    # Record run in system_state
    # -----------------------------------------------------------------------
    record_run(conn, today, diagnosis, dry_run)
    conn.close()

    # -----------------------------------------------------------------------
    # Step 4a: Git commit & push
    # -----------------------------------------------------------------------
    print("\n📤 Step 4a: Git commit & push...")
    git_result = git_commit_and_push(today, dry_run)
    print(f"  {git_result}")

    # -----------------------------------------------------------------------
    # Step 4b: Send Telegram report
    # -----------------------------------------------------------------------
    print("\n📱 Step 4b: Sending Telegram report...")
    message = build_telegram_message(today, diagnosis, action_results, evidence, git_result)

    if verbose:
        print("\n--- Telegram message ---")
        print(message)
        print("------------------------\n")

    if dry_run:
        print("[dry-run] Telegram message would be sent:")
        print(message)
    else:
        try:
            token = _get_secret(os.environ["TELEGRAM_TOKEN_SECRET"])
            send_telegram(token, NICK_CHAT_ID, message)
            print("  ✅ Telegram message sent")
        except Exception as e:
            print(f"  ❌ Telegram send failed: {e}")
            # Non-fatal — don't exit

    print("\n✅ Self-improvement run complete.")


if __name__ == "__main__":
    main()
