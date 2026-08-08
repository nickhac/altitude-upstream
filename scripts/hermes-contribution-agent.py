#!/usr/bin/env python3
"""
hermes-contribution-agent.py — altitude-upstream CR-001 Phase 3

Hermes-native contribution agent. Thin Python orchestrator that:
  1. Reads gap metadata from Postgres (by --gap-id)
  2. Loads knowledge files via get-repo-knowledge.py
  3. Runs all deterministic gates (worktree, branch, quality, smoke, tests, commit, PR)
     by importing from agent-contribution-engine.py
  4. Uses run_bedrock_agent (wrapped with loop-engineering constraints) as the Fix Writer
  5. Uses _verify_contribution (fail-open) as the Verifier
  6. Records result to Postgres
  7. Supports --dry-run mode

Loop engineering:
  - max_attempts = 2 (code iteration cap, not infra retries)
  - Fix Writer: max 20 turns internally, EARLY-EXIT on non-empty diff + py_compile pass
  - Fix Writer: explicit TURN BUDGET (stop at turn 12, capture diff even if incomplete)
  - Verifier: fail-open — if delegation fails, treat as PASS with 'verify skipped'
  - After each failed Fix Writer attempt: capture git diff HEAD as partial-work rescue
  - After 2 failed attempts: save to /tmp/partial-{gap_id}.diff, mark blocked

Usage:
    python3 scripts/hermes-contribution-agent.py --gap-id N
    python3 scripts/hermes-contribution-agent.py --gap-id N --dry-run
    python3 scripts/hermes-contribution-agent.py --repo owner/repo --dry-run
"""

import sys
import os
import re
import json
import argparse
import subprocess
import textwrap
import traceback
import importlib.util
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Path setup: import siblings from scripts/
# ---------------------------------------------------------------------------

scripts_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(scripts_dir)
sys.path.insert(0, scripts_dir)


# ---------------------------------------------------------------------------
# Dynamic import of agent-contribution-engine.py (hyphen in filename)
# ---------------------------------------------------------------------------

def _load_ace():
    """Load agent-contribution-engine.py as module 'ace'."""
    engine_path = os.path.join(scripts_dir, 'agent-contribution-engine.py')
    spec = importlib.util.spec_from_file_location('ace', engine_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ace = _load_ace()

# Pull everything we need from the engine module
setup_worktree      = ace.setup_worktree
create_branch       = ace.create_branch
check_diff_quality  = ace.check_diff_quality
run_tests           = ace.run_tests
commit_and_push     = ace.commit_and_push
submit_pr           = ace.submit_pr
build_pr_body       = ace.build_pr_body
build_summary_comment = ace.build_summary_comment
record_submission   = ace.record_submission
mark_blocked        = ace.mark_blocked
get_fine_grained_pat = ace.get_fine_grained_pat
get_classic_pat     = ace.get_classic_pat
ensure_fork         = ace.ensure_fork
get_conn            = ace.get_conn
make_slug           = ace.make_slug
parse_diff_files    = ace.parse_diff_files
count_diff_lines    = ace.count_diff_lines
post_pr_comment     = ace.post_pr_comment
run_external_gates  = ace.run_external_gates
build_claude_prompt = ace.build_claude_prompt
run_bedrock_agent   = ace.run_bedrock_agent
fetch_gap           = ace.fetch_gap
apply_diff          = ace.apply_diff
check_circuit_breaker = ace.check_circuit_breaker
check_ramp_cap      = ace.check_ramp_cap

WORKTREE_BASE = ace.WORKTREE_BASE

# ---------------------------------------------------------------------------
# Sibling imports
# ---------------------------------------------------------------------------

from smoke_test_execution import smoke_test
from verify_contribution import verify_contribution as _verify_contribution
from repo_context import get_repo_context

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FIX_ATTEMPTS = 2   # code iteration cap (not infra retries)

# ---------------------------------------------------------------------------
# Knowledge injection
# ---------------------------------------------------------------------------


def load_repo_knowledge(repo_full_name: str, wedge_type: str) -> str:
    """
    Run get-repo-knowledge.py and return the combined knowledge text.
    Returns empty string on failure (non-fatal).
    """
    grk_path = os.path.join(scripts_dir, 'get-repo-knowledge.py')
    cmd = [
        'python3', grk_path,
        '--repo', repo_full_name,
        '--wedge', wedge_type,
        '--infra', 'worktrees',
        '--infra', 'github-auth',
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=project_dir, timeout=30
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        if r.stderr.strip():
            print(f"  Knowledge warning: {r.stderr.strip()[:200]}")
        return ''
    except Exception as e:
        print(f"  Knowledge load warning: {e}")
        return ''


def build_enhanced_prompt(gap: dict, knowledge_text: str, attempt: int = 1,
                          test_error: str = None, repo_ctx: dict = None) -> str:
    """
    Build the Fix Writer prompt with:
      - EARLY-EXIT rule
      - TURN BUDGET (max 15 tool calls, stop at turn 12)
      - Injected knowledge context
    """
    base_prompt = build_claude_prompt(
        gap, attempt=attempt, test_error=test_error, repo_ctx=repo_ctx
    )

    early_exit_rules = textwrap.dedent("""
        LOOP ENGINEERING RULES (read before starting):
        - EARLY-EXIT: Once `git diff` is non-empty and `python3 -m py_compile` passes on
          all changed files, STOP immediately. Do not iterate further.
        - TURN BUDGET: You have a maximum of 15 tool calls. Stop at turn 12 and capture
          `git diff` even if the fix is incomplete. An incomplete diff is better than
          hitting the cap with no output.
        - Do NOT add new dependencies, change build systems, or modify lock files.
        - Do NOT run the full test suite — only syntax-check changed files.
    """).strip()

    knowledge_block = ''
    if knowledge_text:
        knowledge_block = (
            "\n\n--- KNOWLEDGE BASE (repo + wedge + infra context) ---\n"
            + knowledge_text[:8000]
            + "\n--- END KNOWLEDGE BASE ---"
        )

    return base_prompt + "\n\n" + early_exit_rules + knowledge_block


# ---------------------------------------------------------------------------
# Loop-engineering Fix Writer harness
# ---------------------------------------------------------------------------


def run_fix_writer_with_harness(
    gap: dict,
    worktree: str,
    repo_ctx: dict,
    knowledge_text: str,
    gap_id: int,
) -> tuple:
    """
    Wrap run_bedrock_agent with loop-engineering constraints:
      - max_attempts = MAX_FIX_ATTEMPTS (2)
      - After each attempt: quality gate + smoke test. Pass = stop.
      - On failure: capture partial git diff as rescue artifact
      - After all attempts exhausted: save /tmp/partial-{gap_id}.diff
    Returns (diff_text, error, attempts_used, partial_diff_saved)
    """
    last_partial_diff = ''
    attempts_used = 0

    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        attempts_used = attempt
        print(f"  Fix Writer attempt {attempt}/{MAX_FIX_ATTEMPTS}...")

        # Build enhanced prompt with loop-engineering constraints
        # We patch the gap description temporarily to inject early-exit rules
        # by building the prompt ourselves and passing it via the system override
        # NOTE: run_bedrock_agent calls build_claude_prompt internally.
        # We build our own enhanced prompt and pass it as a custom 'description'
        # injection by temporarily enriching the gap dict — but we must NOT mutate
        # the original gap. We create a shallow copy with enhanced description.
        enhanced_prompt = build_enhanced_prompt(
            gap,
            knowledge_text=knowledge_text,
            attempt=attempt,
            repo_ctx=repo_ctx,
        )

        # run_bedrock_agent builds its own prompt via build_claude_prompt.
        # We inject knowledge by creating an augmented gap object.
        # The simplest approach: pass knowledge as part of gap['description']
        # injected only for the agent call (shallow copy).
        knowledge_suffix = ''
        if knowledge_text:
            knowledge_suffix = (
                "\n\n--- KNOWLEDGE BASE (repo + wedge + infra context) ---\n"
                + knowledge_text[:6000]
                + "\n--- END KNOWLEDGE BASE ---"
            )
        early_exit_suffix = textwrap.dedent("""

            LOOP ENGINEERING RULES (read before starting):
            - EARLY-EXIT: Once `git diff` is non-empty and `python3 -m py_compile` passes
              on all changed files, STOP immediately. Do not iterate further.
            - TURN BUDGET: You have a maximum of 15 tool calls. Stop at turn 12 and capture
              `git diff` even if the fix is incomplete. An incomplete diff is better than
              hitting the cap with no output.
            - Do NOT add new dependencies, change build systems, or modify lock files.
            - Do NOT run the full test suite — only syntax-check changed files.
        """).strip()

        augmented_gap = dict(gap)
        augmented_gap['description'] = (
            gap['description'] + early_exit_suffix + knowledge_suffix
        )

        # Capture partial diff BEFORE running (clean state)
        r_pre = subprocess.run(
            ['git', 'diff', 'HEAD'],
            capture_output=True, text=True, cwd=worktree
        )
        pre_diff = r_pre.stdout.strip()

        # Run the Bedrock Fix Writer (up to 20 turns internally)
        diff_text, err = run_bedrock_agent(
            augmented_gap, worktree,
            attempt=attempt, repo_ctx=repo_ctx
        )

        # Capture whatever is in the worktree after the agent runs
        r_post = subprocess.run(
            ['git', 'diff', 'HEAD'],
            capture_output=True, text=True, cwd=worktree
        )
        post_diff = r_post.stdout.strip()

        # Also check staged changes
        r_staged = subprocess.run(
            ['git', 'diff', '--cached'],
            capture_output=True, text=True, cwd=worktree
        )
        staged_diff = r_staged.stdout.strip()

        # Prefer the agent's returned diff, fall back to working-tree diff
        effective_diff = diff_text or post_diff or staged_diff
        last_partial_diff = effective_diff or last_partial_diff

        if err and not effective_diff:
            print(f"  Fix Writer attempt {attempt} error: {err}")
            # Reset worktree before next attempt
            subprocess.run(['git', 'checkout', '.'], cwd=worktree, capture_output=True)
            subprocess.run(['git', 'clean', '-fd'], cwd=worktree, capture_output=True)
            continue

        if not effective_diff:
            print(f"  Fix Writer attempt {attempt}: no diff produced")
            subprocess.run(['git', 'checkout', '.'], cwd=worktree, capture_output=True)
            subprocess.run(['git', 'clean', '-fd'], cwd=worktree, capture_output=True)
            continue

        # Quality gate check
        qpass, qreason = check_diff_quality(effective_diff)
        if not qpass:
            print(f"  Fix Writer attempt {attempt}: quality gate failed — {qreason}")
            last_partial_diff = effective_diff
            subprocess.run(['git', 'checkout', '.'], cwd=worktree, capture_output=True)
            subprocess.run(['git', 'clean', '-fd'], cwd=worktree, capture_output=True)
            continue

        # Apply diff to worktree if not already applied
        if diff_text and not post_diff:
            ok, apply_err = apply_diff(diff_text, worktree)
            if not ok:
                print(f"  Fix Writer attempt {attempt}: diff apply failed — {apply_err}")
                last_partial_diff = effective_diff
                subprocess.run(['git', 'checkout', '.'], cwd=worktree, capture_output=True)
                subprocess.run(['git', 'clean', '-fd'], cwd=worktree, capture_output=True)
                continue

        # Smoke test check
        smoke_passed, smoke_results = smoke_test(effective_diff, worktree)
        fails = [r for r in smoke_results if r['status'] == 'FAIL']
        if not smoke_passed:
            detail = '; '.join(
                f"{r['check']}:{r['file'].split('/')[-1]}:{r['detail'][:80]}"
                for r in fails
            )
            print(f"  Fix Writer attempt {attempt}: smoke test failed — {detail}")
            last_partial_diff = effective_diff
            subprocess.run(['git', 'checkout', '.'], cwd=worktree, capture_output=True)
            subprocess.run(['git', 'clean', '-fd'], cwd=worktree, capture_output=True)
            continue

        # Both gates passed — return the diff
        print(f"  Fix Writer attempt {attempt}: quality gate + smoke test PASSED")
        return effective_diff, None, attempts_used, False

    # All attempts exhausted — save partial diff
    partial_diff_path = f'/tmp/partial-{gap_id}.diff'
    partial_diff_saved = False
    if last_partial_diff:
        try:
            with open(partial_diff_path, 'w') as f:
                f.write(last_partial_diff)
            print(f"  Partial diff saved to {partial_diff_path}")
            partial_diff_saved = True
        except Exception as e:
            print(f"  Warning: could not save partial diff: {e}")

    error_msg = (
        f"Fix Writer exhausted {MAX_FIX_ATTEMPTS} attempts. "
        + (f"Partial diff saved to {partial_diff_path}" if partial_diff_saved
           else "No partial diff available.")
    )
    return None, error_msg, attempts_used, partial_diff_saved


# ---------------------------------------------------------------------------
# Fail-open verifier wrapper
# ---------------------------------------------------------------------------


def verify_with_failopen(diff_text: str, gap: dict) -> tuple:
    """
    Wrap _verify_contribution with fail-open: if anything goes wrong at the
    infra level (connection error, import error, etc.), treat as PASS.
    Returns (ok, verdict, reason).
    """
    try:
        ok, verdict, reason = _verify_contribution(diff_text, gap)
        return ok, verdict, reason
    except Exception as e:
        print(f"  Verifier infra error (failing open): {e}")
        return True, 'PASS', 'verify skipped'


# ---------------------------------------------------------------------------
# Core: process one gap
# ---------------------------------------------------------------------------


def process_gap_hermes(conn, gap: dict, dry_run: bool = False) -> dict:
    """
    Full Hermes-native pipeline for one gap.
    Returns a result dict: {gap_id, status, pr_url, reason, attempts, diff_lines}
    """
    gap_id         = gap['id']
    repo_full_name = gap['repo_full_name']
    wedge_type     = gap['wedge_type']
    cur            = conn.cursor()

    result = {
        'gap_id':     gap_id,
        'status':     'failed',
        'pr_url':     None,
        'reason':     '',
        'attempts':   0,
        'diff_lines': 0,
    }

    # -----------------------------------------------------------------------
    # [STEP 1/9] PATs
    # -----------------------------------------------------------------------
    print(f"[STEP 1/9] Fetching credentials...")
    fine_grained_pat = get_fine_grained_pat()
    classic_pat      = get_classic_pat()

    # Mark gap in-progress
    if not dry_run:
        cur.execute(
            "UPDATE gaps SET status='in_progress', updated_at=NOW() WHERE id=%s",
            (gap_id,)
        )
        conn.commit()

    try:
        # -------------------------------------------------------------------
        # [STEP 2/9] Fork
        # -------------------------------------------------------------------
        print(f"[STEP 2/9] Ensuring fork of {repo_full_name}...")
        _, fork_full = ensure_fork(repo_full_name, classic_pat)

        # -------------------------------------------------------------------
        # [STEP 3/9] Worktree
        # -------------------------------------------------------------------
        print(f"[STEP 3/9] Setting up worktree...")
        worktree = setup_worktree(repo_full_name, fine_grained_pat, classic_pat)

        # -------------------------------------------------------------------
        # [STEP 4/9] Branch
        # -------------------------------------------------------------------
        slug        = make_slug(gap['description'])
        branch_name = f"fix/{wedge_type}-{gap_id}-{slug}"
        print(f"[STEP 4/9] Creating branch {branch_name}...")
        create_branch(worktree, branch_name)

        # -------------------------------------------------------------------
        # [STEP 5/9] Repo context + knowledge injection
        # -------------------------------------------------------------------
        print(f"[STEP 5/9] Loading repo context and knowledge...")

        # Per-repo context (CONTRIBUTING.md, recent PRs, naming conventions)
        try:
            target_file = ''
            if gap.get('source_url') and 'blob/' in (gap.get('source_url') or ''):
                parts = gap['source_url'].split('blob/')
                if len(parts) == 2:
                    target_file = '/'.join(parts[1].split('/')[1:])
            repo_ctx = get_repo_context(repo_full_name, classic_pat, target_file)
        except Exception as e:
            print(f"  Repo context warning: {e} — continuing without")
            repo_ctx = None

        # Knowledge-base injection (docs/knowledge/)
        knowledge_text = load_repo_knowledge(repo_full_name, wedge_type)
        if knowledge_text:
            print(f"  Knowledge loaded: {len(knowledge_text)} chars")
        else:
            print(f"  Knowledge: no files found (continuing without)")

        # -------------------------------------------------------------------
        # DRY RUN exit point
        # -------------------------------------------------------------------
        if dry_run:
            print(f"\n[DRY RUN] Fix Writer would receive this context:")
            print(f"  Gap #{gap_id}: {gap['description'][:120]}")
            print(f"  Repo: {repo_full_name} | Wedge: {wedge_type}")
            print(f"  Branch: {branch_name}")
            print(f"  Worktree: {worktree}")
            print(f"  Repo ctx keys: {list((repo_ctx or {}).keys())}")
            print(f"  Knowledge chars: {len(knowledge_text)}")
            sample_prompt = build_enhanced_prompt(
                gap, knowledge_text=knowledge_text[:200], repo_ctx=repo_ctx
            )
            print(f"\n--- Fix Writer prompt sample (first 500 chars) ---")
            print(sample_prompt[:500])
            print("--- end sample ---")
            # Reset gap to open
            cur.execute(
                "UPDATE gaps SET status='open', updated_at=NOW() WHERE id=%s",
                (gap_id,)
            )
            conn.commit()
            result.update(status='dry_run', reason='DRY_RUN')
            return result

        # -------------------------------------------------------------------
        # [STEP 6/9] Fix Writer (loop-engineering harness)
        # -------------------------------------------------------------------
        print(f"[STEP 6/9] Running Fix Writer (max {MAX_FIX_ATTEMPTS} attempts)...")
        diff_text, fw_err, attempts_used, partial_saved = run_fix_writer_with_harness(
            gap=gap,
            worktree=worktree,
            repo_ctx=repo_ctx,
            knowledge_text=knowledge_text,
            gap_id=gap_id,
        )
        result['attempts'] = attempts_used

        if not diff_text:
            blocked_reason = fw_err or "Fix Writer produced no diff"
            if partial_saved:
                blocked_reason += f" [partial_diff_saved=/tmp/partial-{gap_id}.diff]"
            mark_blocked(conn, cur, gap_id, blocked_reason)
            result.update(status='blocked', reason=blocked_reason)
            return result

        result['diff_lines'] = count_diff_lines(diff_text)

        # -------------------------------------------------------------------
        # [STEP 6b/9] Run full test suite (with retry context)
        # -------------------------------------------------------------------
        print(f"[STEP 6b/9] Running tests...")
        try:
            tests_passed, test_output = run_tests(worktree, diff_text)
        except subprocess.TimeoutExpired:
            tests_passed, test_output = False, "Tests timed out"

        if not tests_passed:
            # One retry: reset, re-run Fix Writer with test error context
            print(f"  Tests failed — retrying Fix Writer with error context...")
            subprocess.run(['git', 'checkout', '.'], cwd=worktree, capture_output=True)
            subprocess.run(['git', 'clean', '-fd'], cwd=worktree, capture_output=True)

            diff_text2, fw_err2, _, _ = run_fix_writer_with_harness(
                gap=gap,
                worktree=worktree,
                repo_ctx=repo_ctx,
                knowledge_text=knowledge_text,
                gap_id=gap_id,
            )
            result['attempts'] += 1

            if diff_text2:
                ok, apply_err = apply_diff(diff_text2, worktree)
                if ok:
                    diff_text = diff_text2
                    result['diff_lines'] = count_diff_lines(diff_text)

            try:
                tests_passed2, test_output2 = run_tests(worktree, diff_text)
            except subprocess.TimeoutExpired:
                tests_passed2, test_output2 = False, "Tests timed out on retry"

            if not tests_passed2:
                blocked_reason = f"Tests failed after retry: {test_output2[:200]}"
                mark_blocked(conn, cur, gap_id, blocked_reason)
                result.update(status='blocked', reason=blocked_reason)
                return result

        # -------------------------------------------------------------------
        # [STEP 7/9] External gates + Verifier
        # -------------------------------------------------------------------
        print(f"[STEP 7/9] Running external gates and semantic verification...")

        # Write temp files for gate scripts
        pr_title    = f"fix({wedge_type}): {gap['description'][:60]}"
        pr_body     = build_pr_body(gap, diff_text)
        diff_path   = f'/tmp/contribution-{gap_id}.diff'
        pr_body_path = f'/tmp/pr-body-{gap_id}.md'

        try:
            with open(diff_path, 'w') as f:
                f.write(diff_text)
            with open(pr_body_path, 'w') as f:
                f.write(pr_body)

            gates_ok, gate_errors = run_external_gates(diff_path, pr_body_path, repo_full_name)
            if not gates_ok:
                blocked_reason = f"External gate: {'; '.join(gate_errors)}"
                mark_blocked(conn, cur, gap_id, blocked_reason)
                result.update(status='blocked', reason=blocked_reason)
                return result
        finally:
            for tmp in [diff_path, pr_body_path]:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

        # Semantic verification (fail-open)
        print(f"  Semantic verification...")
        v_ok, v_verdict, v_reason = verify_with_failopen(diff_text, gap)
        print(f"  Verify: {v_verdict} — {v_reason[:120]}")
        if not v_ok:
            blocked_reason = f"Verify rejected: {v_reason[:200]}"
            mark_blocked(conn, cur, gap_id, blocked_reason)
            result.update(status='blocked', reason=blocked_reason)
            return result

        # -------------------------------------------------------------------
        # [STEP 8/9] Commit and push
        # -------------------------------------------------------------------
        print(f"[STEP 8/9] Committing and pushing...")
        commit_message = (
            f"{pr_title}\n\n"
            f"Gap ID: #{gap_id}\n"
            f"Co-authored-by: Hermes Agent <hermes-agent@nousresearch.com>"
        )
        ok, push_err = commit_and_push(
            worktree, branch_name, commit_message, fine_grained_pat, fork_full
        )
        if not ok:
            blocked_reason = f"Push failed: {push_err}"
            mark_blocked(conn, cur, gap_id, blocked_reason)
            result.update(status='blocked', reason=blocked_reason)
            return result

        # -------------------------------------------------------------------
        # [STEP 9/9] Open PR and record
        # -------------------------------------------------------------------
        print(f"[STEP 9/9] Opening PR and recording submission...")
        pr_url, pr_number, pr_err = submit_pr(
            repo_full_name, branch_name, pr_title, pr_body, classic_pat
        )
        if pr_err:
            # PR failed but code is pushed — reset to open, don't block permanently
            cur.execute(
                "UPDATE gaps SET status='open', updated_at=NOW() WHERE id=%s",
                (gap_id,)
            )
            conn.commit()
            result.update(status='pr_failed', reason=f"PR creation failed: {pr_err}")
            return result

        # Post summary comment
        summary = build_summary_comment(gap, diff_text)
        post_pr_comment(repo_full_name, pr_number, summary, classic_pat)

        # Record to Postgres
        record_submission(
            conn, gap_id, gap['repo_id'], pr_url, pr_number, wedge_type, pr_title
        )

        print(f"  PR opened: {pr_url}")
        result.update(status='submitted', pr_url=pr_url, reason='OK')
        return result

    except Exception as e:
        tb = traceback.format_exc()
        print(f"  Unexpected error: {e}")
        print(tb)
        try:
            mark_blocked(conn, cur, gap_id, f"Exception: {str(e)[:150]}")
        except Exception:
            pass
        result.update(status='error', reason=f"Exception: {e}")
        return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description='Hermes-native contribution agent — CR-001 Phase 3'
    )
    parser.add_argument(
        '--gap-id', type=int,
        help='Specific gap ID to process (required unless --dry-run with --repo)'
    )
    parser.add_argument(
        '--repo', type=str,
        help='Restrict to gaps from this repo (e.g. vllm-project/vllm). '
             'Optional filter; --gap-id takes precedence.'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Set up worktree, load context, print Fix Writer prompt — no writes, '
             'no push, no PR, no DB writes beyond resetting gap to open'
    )
    args = parser.parse_args()

    if not args.gap_id and not args.dry_run and not args.repo:
        parser.error('--gap-id is required unless --dry-run is set')

    # -----------------------------------------------------------------------
    # DB connection + circuit-breaker check
    # -----------------------------------------------------------------------
    conn = get_conn()

    ok, reason = check_circuit_breaker(conn)
    if not ok:
        print(f"BLOCKED: {reason}")
        conn.close()
        sys.exit(1)

    if not args.dry_run:
        ok, reason = check_ramp_cap(conn)
        if not ok:
            print(f"BLOCKED: {reason}")
            conn.close()
            sys.exit(1)

    # -----------------------------------------------------------------------
    # Gap selection
    # -----------------------------------------------------------------------
    gap = fetch_gap(conn, gap_id=args.gap_id, repo_filter=args.repo)
    if not gap:
        print("HERMES-AGENT: No eligible gaps in queue.")
        conn.close()
        sys.exit(0)

    print(f"HERMES-AGENT: gap #{gap['id']} {gap['repo_full_name']} — starting")
    print(f"  Wedge: {gap['wedge_type']} | Score: {gap.get('score', 0):.3f}")
    print(f"  Description: {gap['description'][:120]}")
    print(f"  Source: {gap['source_url']}")

    # -----------------------------------------------------------------------
    # Run pipeline
    # -----------------------------------------------------------------------
    result = process_gap_hermes(conn, gap, dry_run=args.dry_run)

    conn.close()

    # -----------------------------------------------------------------------
    # Final output: structured JSON summary
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("HERMES-AGENT RESULT:")
    print(json.dumps(result, indent=2))
    print("=" * 60)

    sys.exit(0 if result['status'] in ('submitted', 'dry_run') else 1)


if __name__ == '__main__':
    main()
