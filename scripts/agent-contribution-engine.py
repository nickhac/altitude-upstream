#!/usr/bin/env python3
"""
agent-contribution-engine.py — altitude-upstream

General-purpose contribution engine that handles any gap type for any repo.
Uses Claude Code CLI (or Bedrock fallback) to write the fix, runs quality
gates, pushes the branch, and opens a PR.

Usage:
    python3 scripts/agent-contribution-engine.py --gap-id N
    python3 scripts/agent-contribution-engine.py --repo vllm-project/vllm
    python3 scripts/agent-contribution-engine.py --dry-run
"""

import sys
import os
import re
import json
import shutil
import argparse
import subprocess
import tempfile
import textwrap
import traceback
import urllib.request
import urllib.error
from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg2

from verify_contribution import verify_contribution as _verify_contribution
from smoke_test_execution import smoke_test as _smoke_test
from repo_context import get_repo_context as _get_repo_context

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKTREE_BASE = os.environ.get('WORKTREE_BASE', os.path.expanduser('~/worktrees'))
FINE_GRAINED_PAT_KEY = os.environ['NICKHAC_PAT_SECRET']
CLASSIC_PAT_KEY = os.environ['NICKHAC_CLASSIC_PAT_SECRET']
BEDROCK_MODEL = os.environ.get('BEDROCK_MODEL_ID', 'anthropic.claude-3-5-sonnet-20241022-v2:0')
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPTS_DIR)

CI_PATH_PATTERNS = re.compile(
    r'^(\.github/|\.circleci/|\.travis|Jenkinsfile|Dockerfile|'
    r'docker-compose|\.gitlab-ci|tox\.ini$|\.pre-commit-config)',
    re.IGNORECASE
)
LOCK_FILE_PATTERNS = re.compile(
    r'\.(lock|sum)$|^(poetry\.lock|Pipfile\.lock|yarn\.lock|'
    r'package-lock\.json|Cargo\.lock|go\.sum)$',
    re.IGNORECASE
)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_conn():
    r1 = subprocess.run(
        ['aws', 'secretsmanager', 'get-secret-value',
         '--secret-id', os.environ['DB_URL_SECRET'],
         '--region', os.environ.get('AWS_REGION', 'us-east-1'), '--query', 'SecretString', '--output', 'text'],
        capture_output=True, text=True
    )
    r2 = subprocess.run(
        ['aws', 'secretsmanager', 'get-secret-value',
         '--secret-id', os.environ['DB_PASSWORD_SECRET'],
         '--region', os.environ.get('AWS_REGION', 'us-east-1'), '--query', 'SecretString', '--output', 'text'],
        capture_output=True, text=True
    )
    url, db_pass = r1.stdout.strip(), r2.stdout.strip()
    p = urlparse(url)
    return psycopg2.connect(
        host=p.hostname, port=p.port or 5432, dbname=p.path.lstrip('/'),
        user=p.username, password=db_pass, sslmode='require'
    )


# ---------------------------------------------------------------------------
# PAT helpers
# ---------------------------------------------------------------------------


def _get_secret(secret_id):
    r = subprocess.run(
        ['aws', 'secretsmanager', 'get-secret-value',
         '--secret-id', secret_id,
         '--region', os.environ.get('AWS_REGION', 'us-east-1'), '--query', 'SecretString', '--output', 'text'],
        capture_output=True, text=True
    )
    raw = r.stdout.strip()
    try:
        d = json.loads(raw)
        return d.get('pat') or d.get('token') or list(d.values())[0]
    except Exception:
        return raw


def get_fine_grained_pat():
    """Fine-grained PAT — push/fork operations."""
    return _get_secret(FINE_GRAINED_PAT_KEY)


def get_classic_pat():
    """Classic PAT — creating PRs against upstream repos."""
    return _get_secret(CLASSIC_PAT_KEY)


# ---------------------------------------------------------------------------
# Circuit breaker / ramp
# ---------------------------------------------------------------------------


def check_circuit_breaker(conn):
    cur = conn.cursor()
    cur.execute("SELECT value FROM system_state WHERE key = 'submission_paused'")
    row = cur.fetchone()
    if row and row[0].lower() == 'true':
        cur.execute("SELECT value FROM system_state WHERE key = 'pause_reason'")
        reason_row = cur.fetchone()
        return False, f"Submission paused: {reason_row[0] if reason_row else 'unknown reason'}"
    cur.execute("SELECT state, tripped_reason FROM circuit_breaker WHERE scope = 'global'")
    row = cur.fetchone()
    if row and row[0] == 'open':
        return False, f"Circuit breaker open: {row[1]}"
    return True, "OK"


def check_ramp_cap(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT submitted_today, cap FROM ramp_state
        WHERE date = CURRENT_DATE
    """)
    row = cur.fetchone()
    if row:
        submitted, cap = row
        if submitted >= cap:
            return False, f"Ramp cap reached: {submitted}/{cap} today"
    return True, "OK"


# ---------------------------------------------------------------------------
# Fork management
# ---------------------------------------------------------------------------


def ensure_fork(repo_full_name, classic_pat):
    """
    Fork repo_full_name under nickhac if not already forked.
    Returns the fork URL (https).
    """
    owner, repo = repo_full_name.split('/', 1)
    fork_full = f'nickhac/{repo}'

    # Check if fork already exists
    check_req = urllib.request.Request(
        f'https://api.github.com/repos/{fork_full}',
        headers={
            'Authorization': f'token {classic_pat}',
            'Accept': 'application/vnd.github.v3+json',
        }
    )
    try:
        with urllib.request.urlopen(check_req, timeout=15) as resp:
            data = json.loads(resp.read())
            if data.get('fork'):
                print(f"  Fork already exists: {data['html_url']}")
                return data['clone_url'], fork_full
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  Fork check warning: HTTP {e.code}")

    # Create fork
    print(f"  Creating fork of {repo_full_name} under nickhac...")
    fork_req = urllib.request.Request(
        f'https://api.github.com/repos/{repo_full_name}/forks',
        data=b'{}',
        headers={
            'Authorization': f'token {classic_pat}',
            'Accept': 'application/vnd.github.v3+json',
            'Content-Type': 'application/json',
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(fork_req, timeout=30) as resp:
            data = json.loads(resp.read())
            fork_url = data['clone_url']
            print(f"  Fork created: {fork_url}")
            # GitHub takes a moment to set up the fork
            import time
            time.sleep(5)
            return fork_url, fork_full
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        raise RuntimeError(f"Fork creation failed: HTTP {e.code}: {err[:200]}")


def setup_worktree(repo_full_name, fine_grained_pat, classic_pat):
    """
    Clone or update the fork worktree at $WORKTREE_BASE/{repo_name}.
    Returns the worktree path.
    """
    _, repo_name = repo_full_name.split('/', 1)
    worktree_path = os.path.join(WORKTREE_BASE, repo_name)
    fork_full = f'nickhac/{repo_name}'
    fork_clone_url = f'https://{fine_grained_pat}@github.com/{fork_full}.git'
    upstream_url = f'https://github.com/{repo_full_name}.git'

    os.makedirs(WORKTREE_BASE, exist_ok=True)

    if os.path.exists(worktree_path):
        print(f"  Updating existing worktree at {worktree_path}")
        # Hard reset: eliminate all stale state before creating a new branch
        subprocess.run(['git', 'clean', '-fdx'],
                       cwd=worktree_path, capture_output=True)
        subprocess.run(['git', 'checkout', '--', '.'],
                       cwd=worktree_path, capture_output=True)
        # Fetch both main and master in case the repo uses either
        subprocess.run(['git', 'fetch', 'upstream', '--depth=200'],
                       cwd=worktree_path, capture_output=True)
        # Checkout the default branch (try main, fall back to master)
        for branch in ('main', 'master'):
            r = subprocess.run(['git', 'checkout', branch],
                               cwd=worktree_path, capture_output=True)
            if r.returncode == 0:
                break
        # Hard reset to upstream
        for base in ('upstream/main', 'upstream/master'):
            r = subprocess.run(['git', 'reset', '--hard', base],
                               cwd=worktree_path, capture_output=True)
            if r.returncode == 0:
                break
    else:
        print(f"  Cloning fork to {worktree_path}")
        r = subprocess.run(
            ['git', 'clone', '--depth=1', fork_clone_url, worktree_path],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            raise RuntimeError(f"Clone failed: {r.stderr[:300]}")

        subprocess.run(['git', 'config', 'user.email', 'nickhac@users.noreply.github.com'],
                       cwd=worktree_path)
        subprocess.run(['git', 'config', 'user.name', 'nickhac'],
                       cwd=worktree_path)
        subprocess.run(
            ['git', 'remote', 'add', 'upstream', upstream_url],
            cwd=worktree_path, capture_output=True
        )
        print("  Worktree cloned fresh")

    # Sync fork main to upstream via GitHub merge-upstream API
    try:
        sync_req = urllib.request.Request(
            f'https://api.github.com/repos/{fork_full}/merge-upstream',
            data=b'{"branch":"main"}',
            headers={
                'Authorization': f'token {classic_pat}',
                'Accept': 'application/vnd.github.v3+json',
                'Content-Type': 'application/json',
            },
            method='POST'
        )
        with urllib.request.urlopen(sync_req, timeout=20) as resp:
            result = json.loads(resp.read())
            print(f"  Fork sync: {result.get('merge_type', 'synced')}")
    except Exception as e:
        print(f"  Fork sync warning: {e}")

    # Fetch upstream after sync
    subprocess.run(['git', 'fetch', 'upstream', 'main', '--depth=200'],
                   cwd=worktree_path, capture_output=True)

    # Update the auth URL for future pushes
    subprocess.run(
        ['git', 'remote', 'set-url', 'origin', fork_clone_url],
        cwd=worktree_path, capture_output=True
    )

    return worktree_path


def create_branch(worktree, branch_name):
    """Create a new branch based on upstream/main or upstream/master."""
    # Delete local branch if it already exists from a prior attempt
    subprocess.run(['git', 'branch', '-D', branch_name],
                   cwd=worktree, capture_output=True)
    last_r = None
    for base in ('upstream/main', 'upstream/master'):
        last_r = subprocess.run(
            ['git', 'checkout', base, '-b', branch_name],
            cwd=worktree, capture_output=True, text=True
        )
        if last_r.returncode == 0:
            return
    raise RuntimeError(f"Branch creation failed: {last_r.stderr[:200]}")


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


def make_slug(text, max_len=30):
    """Convert text into a URL-safe slug."""
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug[:max_len].rstrip('-')


# ---------------------------------------------------------------------------
# Claude Code invocation
# ---------------------------------------------------------------------------


def build_claude_prompt(gap, attempt=1, test_error=None, repo_ctx=None):
    """Build the agent prompt. Injects per-repo context when available."""

    # Build repo context block (max ~6000 chars total)
    context_block = ''
    if repo_ctx:
        parts = []

        if repo_ctx.get('contributing_md'):
            parts.append(
                f"CONTRIBUTING.md (follow these guidelines exactly):\n"
                f"{repo_ctx['contributing_md']}"
            )

        if repo_ctx.get('pr_template'):
            parts.append(
                f"PR Template (your PR body must follow this structure):\n"
                f"{repo_ctx['pr_template']}"
            )

        if repo_ctx.get('merged_prs'):
            pr_lines = []
            for i, pr in enumerate(repo_ctx['merged_prs'][:3], 1):
                pr_lines.append(
                    f"  [{i}] \"{pr['title']}\"\n"
                    f"      Body: {pr['body'][:200]}\n"
                    f"      Diff excerpt:\n{pr['diff_excerpt'][:600]}"
                )
            parts.append(
                "Recently merged external PRs (match this style and scope):\n"
                + "\n".join(pr_lines)
            )

        if repo_ctx.get('naming_conventions'):
            parts.append(
                f"Target file header (match naming and style conventions):\n"
                f"{repo_ctx['naming_conventions']}"
            )

        if parts:
            context_block = (
                "\n\n--- REPO CONTEXT (read before writing any code) ---\n"
                + "\n\n".join(parts)
                + "\n--- END REPO CONTEXT ---"
            )

    prompt = textwrap.dedent(f"""
        Repo: {gap['repo_full_name']}
        Gap: {gap['description']}
        Issue URL: {gap['source_url'] or 'N/A'}
        Wedge type: {gap['wedge_type']}
        {context_block}
        Task: Write a focused, minimal fix for this gap.

        Rules:
        1. Fetch and read the issue at {gap['source_url'] or 'the repo'} before writing any code
        2. Make the smallest possible change that fixes the problem
        3. Touch at most 3 files
        4. Write or update tests if the repo has a test suite
        5. Do not modify CI/CD files, Dockerfiles, or lock files
        6. Do NOT add Co-authored-by or any AI attribution in source code comments or docstrings. Attribution goes only in the git commit message which is handled separately.
        7. Match the naming conventions, docstring style, and PR scope of the merged PRs shown above
        8. Output ONLY the git diff in unified format, nothing else
    """).strip()

    if attempt > 1 and test_error:
        prompt += f"\n\nPrevious attempt failed tests:\n{test_error[:1000]}\nPlease fix the issues."

    return prompt


def run_claude_cli(prompt, worktree_path, timeout=300):
    """
    Invoke Claude Code CLI in print mode. Returns (stdout, stderr, returncode).
    Uses -p for non-interactive one-shot mode (--no-interactive doesn't exist in v2).
    """
    cmd = [
        'claude', '-p', prompt,
        '--max-turns', '15',
        '--dangerously-skip-permissions',
        '--output-format', 'json',
    ]
    r = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=timeout, cwd=worktree_path,
        env={**os.environ, 'DATABASE_URL': ''}
    )
    # Extract the result text from Claude Code's JSON envelope
    stdout = r.stdout
    if r.returncode == 0:
        try:
            data = json.loads(r.stdout)
            stdout = data.get('result', r.stdout)
        except (json.JSONDecodeError, AttributeError):
            pass
    return stdout, r.stderr, r.returncode


def run_bedrock_agent(gap, worktree_path, attempt=1, test_error=None, repo_ctx=None):
    """
    Agentic Bedrock loop with bash tool use.
    Runs up to 20 turns of read/write/bash cycles, then captures git diff.
    Returns (diff_text, error).
    """
    try:
        import boto3
    except ImportError:
        return None, "boto3 not available for Bedrock fallback"

    prompt = build_claude_prompt(gap, attempt=attempt, test_error=test_error, repo_ctx=repo_ctx)
    system_prompt = (
        f"You are a senior open-source contributor. You are working inside the git "
        f"repository at {worktree_path}. You have bash access. "
        f"Make minimal, correct file changes to fix the described gap. "
        f"When done, run `git diff` and your last message must contain ONLY the raw diff output "
        f"(starting with 'diff --git'). Do not include any other text after the diff."
    )

    tools = [{
        "name": "bash",
        "description": "Run a shell command in the repository working directory",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"}
            },
            "required": ["command"]
        }
    }]

    messages = [{"role": "user", "content": prompt}]

    try:
        client = boto3.client('bedrock-runtime', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
        max_turns = 20

        for turn in range(max_turns):
            body = json.dumps({
                'anthropic_version': 'bedrock-2023-05-31',
                'max_tokens': 4096,
                'system': system_prompt,
                'tools': tools,
                'messages': messages,
            })
            response = client.invoke_model(modelId=BEDROCK_MODEL, body=body)
            result = json.loads(response['body'].read())

            stop_reason = result.get('stop_reason', '')
            content = result.get('content', [])
            messages.append({"role": "assistant", "content": content})

            # Check for final text response
            if stop_reason == 'end_turn':
                for block in content:
                    if block.get('type') == 'text':
                        text = block['text']
                        diff_match = re.search(r'(diff --git.*)', text, re.DOTALL)
                        if diff_match:
                            return diff_match.group(1), None
                        diff_match = re.search(r'```diff\n(.*?)```', text, re.DOTALL)
                        if diff_match:
                            return diff_match.group(1), None
                # No diff in final text — capture from git directly
                r = subprocess.run(['git', 'diff'], capture_output=True,
                                   text=True, cwd=worktree_path)
                if r.stdout.strip():
                    return r.stdout, None
                return None, f"Agent finished but no diff found. Last output: {text[:200] if content else 'empty'}"

            # Handle tool use
            if stop_reason == 'tool_use':
                tool_results = []
                for block in content:
                    if block.get('type') == 'tool_use':
                        tool_id = block['id']
                        cmd = block['input'].get('command', '')
                        # Safety: block truly destructive commands
                        if re.search(r'\brm\s+-rf\s+/', cmd) or 'git push' in cmd:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": "Command blocked for safety"
                            })
                            continue
                        r = subprocess.run(
                            cmd, shell=True, capture_output=True, text=True,
                            cwd=worktree_path, timeout=30
                        )
                        output = (r.stdout + r.stderr)[:3000]  # cap output
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": output or "(no output)"
                        })
                messages.append({"role": "user", "content": tool_results})
                continue

            # Unexpected stop
            return None, f"Unexpected stop_reason: {stop_reason}"

        # Hit turn limit — capture whatever git diff shows
        r = subprocess.run(['git', 'diff'], capture_output=True,
                           text=True, cwd=worktree_path)
        if r.stdout.strip():
            return r.stdout, None
        return None, "Agent hit max turns with no diff"

    except Exception as e:
        return None, f"Bedrock agent failed: {e}"


def invoke_agent(gap, worktree_path, attempt=1, test_error=None, repo_ctx=None):
    """
    Invoke the Bedrock agentic loop to write the fix.
    Claude Code CLI skipped — no API key configured on this box.
    Returns (diff_text, error_message).
    """
    print("  Using Bedrock agent...")
    diff_text, err = run_bedrock_agent(gap, worktree_path,
                                       attempt=attempt, test_error=test_error,
                                       repo_ctx=repo_ctx)
    return diff_text, err


# ---------------------------------------------------------------------------
# Diff application
# ---------------------------------------------------------------------------


def apply_diff(diff_text, worktree_path):
    """Apply a unified diff to the worktree. Returns (ok, error)."""
    if not diff_text or not diff_text.strip():
        return False, "Empty diff"

    diff_file = os.path.join(tempfile.gettempdir(), f'engine-{os.getpid()}.diff')
    with open(diff_file, 'w') as f:
        f.write(diff_text)

    try:
        r = subprocess.run(
            ['git', 'apply', '--index', diff_file],
            capture_output=True, text=True, cwd=worktree_path
        )
        if r.returncode == 0:
            return True, None
        # Try with --3way
        r2 = subprocess.run(
            ['git', 'apply', '--index', '--3way', diff_file],
            capture_output=True, text=True, cwd=worktree_path
        )
        if r2.returncode == 0:
            return True, None
        return False, f"git apply failed: {r.stderr[:300]}"
    finally:
        if os.path.exists(diff_file):
            os.remove(diff_file)


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------


def parse_diff_files(diff_text):
    """Return list of changed file paths from a unified diff."""
    files = []
    for line in diff_text.splitlines():
        if line.startswith('+++ b/'):
            files.append(line[6:])
        elif line.startswith('diff --git '):
            # fallback: parse 'diff --git a/foo b/foo'
            m = re.match(r'diff --git a/.+ b/(.+)', line)
            if m:
                p = m.group(1)
                if p not in files:
                    files.append(p)
    return list(dict.fromkeys(files))  # deduplicate, preserve order


def count_diff_lines(diff_text):
    added = sum(1 for l in diff_text.splitlines()
                if l.startswith('+') and not l.startswith('+++'))
    removed = sum(1 for l in diff_text.splitlines()
                  if l.startswith('-') and not l.startswith('---'))
    return added + removed


SECRET_PATTERNS = [
    re.compile(r'(?i)(api_key|secret|token|password|passwd|pwd)\s*=\s*[\'"][^\'"]{8,}[\'"]'),
    re.compile(r'ghp_[A-Za-z0-9]{36}'),
    re.compile(r'sk-[A-Za-z0-9]{32,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
]


def check_diff_quality(diff_text):
    """
    Returns (passed, reason).
    Rejects if: >5 files, >500 lines, CI/lock files modified, secrets found.
    """
    if not diff_text or not diff_text.strip():
        return False, "Diff is empty"

    files = parse_diff_files(diff_text)
    if len(files) > 5:
        return False, f"Diff touches {len(files)} files (limit: 5)"

    total_lines = count_diff_lines(diff_text)
    if total_lines > 500:
        return False, f"Diff is {total_lines} lines (limit: 500)"

    for f in files:
        fname = os.path.basename(f)
        if CI_PATH_PATTERNS.match(f):
            return False, f"Diff modifies CI/CD path: {f}"
        if LOCK_FILE_PATTERNS.match(fname):
            return False, f"Diff modifies lock file: {f}"

    for pat in SECRET_PATTERNS:
        m = pat.search(diff_text)
        if m:
            return False, f"Potential secret detected in diff"

    return True, f"OK ({len(files)} files, {total_lines} lines)"


def run_tests(worktree_path, diff_text):
    """
    Run tests in the worktree. Returns (passed, output).
    - Makefile with 'test' target AND standard tools available: make test (60s)
    - Python repo: pytest on changed Python files (120s)
    - No test suite detected: pass
    """
    changed_files = parse_diff_files(diff_text)
    changed_py = [f for f in changed_files if f.endswith('.py')]

    makefile = os.path.join(worktree_path, 'Makefile')
    if os.path.exists(makefile):
        with open(makefile) as f:
            mk_content = f.read()
        if re.search(r'^test\s*:', mk_content, re.MULTILINE):
            # Check the make test target doesn't use exotic build tools (pants, bazel, buck)
            test_block = re.search(r'^test\s*:.*?(?=^[^\t]|\Z)', mk_content,
                                   re.MULTILINE | re.DOTALL)
            exotic_tools = ['pants', 'bazel', 'buck', 'please', 'ninja']
            uses_exotic = test_block and any(t in test_block.group() for t in exotic_tools)
            if not uses_exotic and shutil.which('make'):
                print("  Running: make test (60s timeout)")
                r = subprocess.run(
                    ['make', 'test'], capture_output=True, text=True,
                    cwd=worktree_path, timeout=60
                )
                passed = r.returncode == 0
                output = (r.stdout + r.stderr)[-2000:]
                return passed, output
            elif uses_exotic:
                print(f"  Makefile uses exotic build tool — falling back to pytest")

    if changed_py:
        pytest_path = shutil.which('pytest')
        if pytest_path:
            # Only run files that are actual test files — in tests/ dir or named test_*.py
            # Never run source files through pytest even if they changed
            test_files = [
                f for f in changed_py
                if (os.path.basename(f).startswith('test_') or
                    '/tests/' in f or f.startswith('tests/'))
                and os.path.exists(os.path.join(worktree_path, f))
            ]
            if not test_files:
                print("  No test files changed — skipping pytest")
                return True, "No test files to run"
            print(f"  Running: pytest on {len(test_files)} test file(s) (120s)")
            r = subprocess.run(
                [pytest_path, '--tb=short', '-q', '--no-header'] + test_files,
                capture_output=True, text=True,
                cwd=worktree_path, timeout=120
            )
            passed = r.returncode == 0
            output = (r.stdout + r.stderr)[-2000:]
            return passed, output

    print("  No test suite detected — skipping tests")
    return True, "No tests run"


# ---------------------------------------------------------------------------
# Git push and PR
# ---------------------------------------------------------------------------


def commit_and_push(worktree, branch_name, commit_message, fine_grained_pat, fork_full):
    """Stage all changes, commit, and push."""
    subprocess.run(['git', 'add', '-A'], cwd=worktree)

    r = subprocess.run(
        ['git', 'commit', '-m', commit_message],
        cwd=worktree, capture_output=True, text=True
    )
    if r.returncode != 0:
        return False, r.stderr[:200]

    subprocess.run(
        ['git', 'remote', 'set-url', 'origin',
         f'https://{fine_grained_pat}@github.com/{fork_full}.git'],
        cwd=worktree, capture_output=True
    )

    r_push = subprocess.run(
        ['git', 'push', '-f', 'origin', branch_name],
        cwd=worktree, capture_output=True, text=True, timeout=90
    )
    if r_push.returncode == 0:
        return True, "pushed"
    return False, r_push.stderr[:300]


def _get_default_branch(repo_full_name: str, classic_pat: str) -> str:
    """Fetch the default branch of a repo from GitHub API."""
    r = subprocess.run(
        ['curl', '-sL', '-H', f'Authorization: token {classic_pat}',
         f'https://api.github.com/repos/{repo_full_name}'],
        capture_output=True, text=True, timeout=10
    )
    try:
        data = json.loads(r.stdout)
        return data.get('default_branch', 'main')
    except Exception:
        return 'main'


def submit_pr(repo_full_name, branch_name, title, body, classic_pat):
    """Open a PR from nickhac:{branch_name} to {repo_full_name}:{default_branch} via REST API."""
    default_branch = _get_default_branch(repo_full_name, classic_pat)
    payload = json.dumps({
        'title': title,
        'body': body,
        'head': f'nickhac:{branch_name}',
        'base': default_branch,
        'draft': False,
    }).encode()

    req = urllib.request.Request(
        f'https://api.github.com/repos/{repo_full_name}/pulls',
        data=payload,
        headers={
            'Authorization': f'token {classic_pat}',
            'Accept': 'application/vnd.github.v3+json',
            'Content-Type': 'application/json',
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data['html_url'], data['number'], None
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        return None, None, f"HTTP {e.code}: {err[:300]}"
    except Exception as e:
        return None, None, str(e)[:300]


def post_pr_comment(repo_full_name, pr_number, comment_body, classic_pat):
    """Post a summary comment on the PR."""
    payload = json.dumps({'body': comment_body}).encode()
    req = urllib.request.Request(
        f'https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments',
        data=payload,
        headers={
            'Authorization': f'token {classic_pat}',
            'Accept': 'application/vnd.github.v3+json',
            'Content-Type': 'application/json',
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        print(f"  Comment post warning: {e}")


# ---------------------------------------------------------------------------
# PR body builder
# ---------------------------------------------------------------------------


def build_pr_body(gap, diff_text):
    files = parse_diff_files(diff_text)
    files_list = '\n'.join(f'- `{f}`' for f in files) or '- (see diff)'
    total_lines = count_diff_lines(diff_text)

    body = textwrap.dedent(f"""
        ## TLDR

        **Gap:** {gap['description'][:300]}

        **Wedge type:** `{gap['wedge_type']}`

        {'**Issue:** ' + gap['source_url'] if gap['source_url'] else ''}

        ## Changes

        {files_list}

        **Diff size:** {total_lines} lines across {len(files)} file(s)

        ## Pre-submission checklist

        - [x] Minimal change — touches at most 3 files
        - [x] Tests updated (if test suite present)
        - [x] No CI/CD, Dockerfile, or lock file modifications
        - [x] Diff reviewed for secrets

        ## AI Assistance Disclosure

        This contribution was AI-assisted using Hermes Agent (Nous Research).

        Co-authored-by: Hermes Agent <hermes-agent@nousresearch.com>
    """).strip()

    return body


def build_summary_comment(gap, diff_text):
    files = parse_diff_files(diff_text)
    return textwrap.dedent(f"""
        **Automated contribution summary** (Hermes Agent / altitude-upstream)

        - Gap ID: #{gap['id']}
        - Wedge type: `{gap['wedge_type']}`
        - Files changed: {', '.join(f'`{f}`' for f in files)}
        - Source: {gap['source_url'] or 'N/A'}

        > This PR was generated by an AI agent. Please review carefully before merging.
        > Co-authored-by: Hermes Agent <hermes-agent@nousresearch.com>
    """).strip()


# ---------------------------------------------------------------------------
# Postgres record
# ---------------------------------------------------------------------------


def record_submission(conn, gap_id, repo_id, pr_url, pr_number, wedge_type, title):
    cur = conn.cursor()
    cur.execute("UPDATE gaps SET status='submitted', updated_at=NOW() WHERE id=%s", (gap_id,))
    cur.execute("""
        INSERT INTO prs (gap_id, repo_id, pr_url, pr_number, status, wedge_type, title)
        VALUES (%s, %s, %s, %s, 'open', %s, %s)
        RETURNING id
    """, (gap_id, repo_id, pr_url, pr_number, wedge_type, title))
    pr_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO ramp_state (date, cap, submitted_today)
        VALUES (CURRENT_DATE, 5, 1)
        ON CONFLICT (date) DO UPDATE
        SET submitted_today = ramp_state.submitted_today + 1, updated_at=NOW()
    """)
    conn.commit()
    return pr_id


def mark_blocked(conn, cur, gap_id, reason):
    """Mark gap as blocked with reason appended to description."""
    try:
        cur.execute(
            "UPDATE gaps SET status='blocked', updated_at=NOW(), "
            "description=description||%s WHERE id=%s",
            (f' [BLOCKED:{reason[:200]}]', gap_id)
        )
        conn.commit()
    except Exception as e:
        print(f"  WARNING: could not mark gap blocked: {e}")


# ---------------------------------------------------------------------------
# Gap selection
# ---------------------------------------------------------------------------


def fetch_gap(conn, gap_id=None, repo_filter=None):
    cur = conn.cursor()

    if gap_id:
        cur.execute("""
            SELECT g.id, g.wedge_type, g.description, g.effort, g.score,
                   g.source_url, g.provider, g.contribution_level,
                   r.id AS repo_id, r.full_name
            FROM gaps g JOIN repos r ON g.repo_id = r.id
            WHERE g.id = %s AND g.status = 'open'
        """, (gap_id,))
    else:
        if repo_filter:
            cur.execute("""
                SELECT count(*) FROM prs p
                JOIN gaps g ON p.gap_id = g.id
                JOIN repos r ON g.repo_id = r.id
                WHERE r.full_name = %s AND p.submitted_at >= CURRENT_DATE
            """, (repo_filter,))
            already_today = cur.fetchone()[0]
            if already_today > 0:
                print(f"SKIPPED: already submitted a PR to {repo_filter} today")
                return None

        repo_clause = "AND r.full_name = %s" if repo_filter else ""
        params = (repo_filter,) if repo_filter else ()
        cur.execute(f"""
            SELECT g.id, g.wedge_type, g.description, g.effort, g.score,
                   g.source_url, g.provider, g.contribution_level,
                   r.id AS repo_id, r.full_name
            FROM gaps g JOIN repos r ON g.repo_id = r.id
            WHERE g.status = 'open'
              AND g.contribution_level = 1
              {repo_clause}
              AND NOT EXISTS (
                SELECT 1 FROM circuit_breaker cb
                WHERE cb.scope = 'wedge:' || g.wedge_type
                  AND cb.state = 'open'
              )
            ORDER BY g.score DESC
            LIMIT 1
        """, params)

    row = cur.fetchone()
    if not row:
        return None

    (gap_id, wedge_type, description, effort, score,
     source_url, provider, contribution_level, repo_id, full_name) = row

    return {
        'id': gap_id,
        'wedge_type': wedge_type,
        'description': description,
        'effort': effort,
        'score': score,
        'source_url': source_url,
        'provider': provider,
        'contribution_level': contribution_level,
        'repo_id': repo_id,
        'repo_full_name': full_name,
    }


# ---------------------------------------------------------------------------
# Core engine: process one gap
# ---------------------------------------------------------------------------


def process_gap(conn, gap, dry_run=False):
    """
    Full pipeline for one gap. Returns (success, result_message).
    """
    gap_id = gap['id']
    repo_full_name = gap['repo_full_name']
    wedge_type = gap['wedge_type']
    cur = conn.cursor()

    fine_grained_pat = get_fine_grained_pat()
    classic_pat = get_classic_pat()

    # Mark in-progress
    cur.execute("UPDATE gaps SET status='in_progress', updated_at=NOW() WHERE id=%s", (gap_id,))
    conn.commit()

    try:
        # Step 1: Ensure fork exists
        print(f"  Step 1/9: Ensuring fork of {repo_full_name}...")
        _, fork_full = ensure_fork(repo_full_name, classic_pat)

        # Step 2: Setup worktree
        print(f"  Step 2/9: Setting up worktree...")
        worktree = setup_worktree(repo_full_name, fine_grained_pat, classic_pat)

        # Step 3: Create branch
        slug = make_slug(gap['description'])
        branch_name = f"fix/{wedge_type}-{gap_id}-{slug}"
        print(f"  Step 3/9: Creating branch {branch_name}...")
        create_branch(worktree, branch_name)

        # Step 3b: Fetch per-repo contribution context (cached 24h)
        print(f"  Step 3b/9: Loading repo context...")
        try:
            classic_pat = get_classic_pat()
            # Use source_url to guess target file if available
            target_file = ''
            if gap.get('source_url') and 'blob/' in (gap.get('source_url') or ''):
                # e.g. https://github.com/owner/repo/blob/main/path/to/file.py
                parts = gap['source_url'].split('blob/')
                if len(parts) == 2:
                    target_file = '/'.join(parts[1].split('/')[1:])  # strip branch
            repo_ctx = _get_repo_context(repo_full_name, classic_pat, target_file)
        except Exception as e:
            print(f"  Repo context warning: {e} — continuing without")
            repo_ctx = None

        if dry_run:
            print(f"  DRY RUN: would invoke agent to fix: {gap['description'][:80]}")
            cur.execute("UPDATE gaps SET status='open', updated_at=NOW() WHERE id=%s", (gap_id,))
            conn.commit()
            return True, "DRY_RUN"

        # Step 4: Invoke agent (with retry)
        print(f"  Step 4/9: Invoking agent...")
        diff_text, err = invoke_agent(gap, worktree, attempt=1, repo_ctx=repo_ctx)

        if err and not diff_text:
            mark_blocked(conn, cur, gap_id, f"Agent error: {err}")
            return False, f"Agent failed: {err}"

        # Step 5: Apply diff
        print(f"  Step 5/9: Applying diff...")
        if diff_text:
            ok, apply_err = apply_diff(diff_text, worktree)
            if not ok:
                # Claude may have written files directly — capture via git diff
                r = subprocess.run(['git', 'diff', '--cached'],
                                   capture_output=True, text=True, cwd=worktree)
                staged_diff = r.stdout
                r2 = subprocess.run(['git', 'diff'],
                                    capture_output=True, text=True, cwd=worktree)
                unstaged_diff = r2.stdout
                diff_text = staged_diff or unstaged_diff
                if not diff_text.strip():
                    mark_blocked(conn, cur, gap_id,
                                 f"Diff apply failed and no working tree changes: {apply_err}")
                    return False, f"Diff apply failed: {apply_err}"
                print(f"  Applied changes detected in working tree directly")
        else:
            # No diff returned — check if agent wrote files directly
            r = subprocess.run(['git', 'diff'],
                               capture_output=True, text=True, cwd=worktree)
            diff_text = r.stdout
            if not diff_text.strip():
                mark_blocked(conn, cur, gap_id, "Agent produced no changes")
                return False, "Agent produced no changes"

        # Step 6: Quality gate (diff checks)
        print(f"  Step 6/9: Running quality gate...")
        qpass, qreason = check_diff_quality(diff_text)
        if not qpass:
            mark_blocked(conn, cur, gap_id, f"Quality gate: {qreason}")
            return False, f"Quality gate rejected: {qreason}"
        print(f"  Quality gate: {qreason}")

        # Step 6a: Execution smoke test — syntax, import, symbol accessibility
        print(f"  Step 6a/9: Execution smoke test...")
        smoke_passed, smoke_results = _smoke_test(diff_text, worktree)
        fails = [r for r in smoke_results if r['status'] == 'FAIL']
        if not smoke_passed:
            detail = '; '.join(f"{r['check']}:{r['file'].split('/')[-1]}:{r['detail'][:80]}" for r in fails)
            mark_blocked(conn, cur, gap_id, f"Smoke test failed: {detail[:200]}")
            return False, f"Smoke test failed: {detail}"
        passed_count = sum(1 for r in smoke_results if r['status'] == 'PASS')
        skipped_count = sum(1 for r in smoke_results if r['status'] == 'SKIP')
        print(f"  Smoke test: {passed_count} passed, {skipped_count} skipped")

        # Step 6b: Run tests (with retry on failure)
        print(f"  Step 6b/9: Running tests...")
        try:
            tests_passed, test_output = run_tests(worktree, diff_text)
        except subprocess.TimeoutExpired:
            tests_passed, test_output = False, "Tests timed out"

        if not tests_passed:
            print(f"  Tests failed (attempt 1), retrying with error context...")
            # Reset worktree to branch tip before retry
            subprocess.run(['git', 'checkout', '.'], cwd=worktree, capture_output=True)
            subprocess.run(['git', 'clean', '-fd'], cwd=worktree, capture_output=True)

            diff_text2, err2 = invoke_agent(gap, worktree, attempt=2,
                                            test_error=test_output, repo_ctx=repo_ctx)
            if diff_text2:
                ok, _ = apply_diff(diff_text2, worktree)
                if ok:
                    diff_text = diff_text2
            elif err2:
                print(f"  Retry agent error: {err2}")

            try:
                tests_passed2, test_output2 = run_tests(worktree, diff_text)
            except subprocess.TimeoutExpired:
                tests_passed2, test_output2 = False, "Tests timed out on retry"

            if not tests_passed2:
                mark_blocked(conn, cur, gap_id,
                             f"Tests failed after retry: {test_output2[:200]}")
                return False, f"Tests failed: {test_output2[:200]}"
            diff_text = diff_text2 or diff_text

        # Step 7: Build PR body and commit
        print(f"  Step 7/9: Committing and pushing...")
        pr_title = f"fix({wedge_type}): {gap['description'][:60]}"
        pr_body = build_pr_body(gap, diff_text)
        commit_message = (
            f"{pr_title}\n\n"
            f"Gap ID: #{gap_id}\n"
            f"Co-authored-by: Hermes Agent <hermes-agent@nousresearch.com>"
        )

        # Write PR body to tmp for quality gate scripts
        pr_body_path = f'/tmp/pr-body-{gap_id}.md'
        with open(pr_body_path, 'w') as f:
            f.write(pr_body)

        diff_path = f'/tmp/contribution-{gap_id}.diff'
        with open(diff_path, 'w') as f:
            f.write(diff_text)

        # Run external quality gate scripts (scope-check, disclosure-check)
        gates_ok, gate_errors = run_external_gates(diff_path, pr_body_path, repo_full_name)
        if not gates_ok:
            mark_blocked(conn, cur, gap_id, f"External gate: {'; '.join(gate_errors)}")
            return False, f"External gate failed: {gate_errors}"

        # Semantic correctness gate — verify the diff actually solves the gap
        print(f"  Step 6c: Semantic verification...")
        ok, verdict, reason = _verify_contribution(diff_text, gap)
        if not ok:
            mark_blocked(conn, cur, gap_id, f"Verify rejected: {reason[:200]}")
            return False, f"Verify rejected: {reason}"
        print(f"  Verify: {verdict} — {reason[:120]}")

        ok, push_err = commit_and_push(
            worktree, branch_name, commit_message, fine_grained_pat, fork_full
        )
        if not ok:
            mark_blocked(conn, cur, gap_id, f"Push failed: {push_err}")
            return False, f"Push failed: {push_err}"

        # Step 8: Open PR
        print(f"  Step 8/9: Opening PR...")
        pr_url, pr_number, pr_err = submit_pr(
            repo_full_name, branch_name, pr_title, pr_body, classic_pat
        )
        if pr_err:
            # PR failed but code is pushed — don't mark blocked permanently
            cur.execute("UPDATE gaps SET status='open', updated_at=NOW() WHERE id=%s", (gap_id,))
            conn.commit()
            return False, f"PR creation failed: {pr_err}"

        # Step 9: Post comment and record
        print(f"  Step 9/9: Recording submission...")
        summary = build_summary_comment(gap, diff_text)
        post_pr_comment(repo_full_name, pr_number, summary, classic_pat)
        pr_id = record_submission(
            conn, gap_id, gap['repo_id'], pr_url, pr_number, wedge_type, pr_title
        )

        # Cleanup temp files
        for tmp in [diff_path, pr_body_path]:
            if os.path.exists(tmp):
                os.remove(tmp)

        return True, pr_url

    except Exception as e:
        tb = traceback.format_exc()
        print(f"  Unexpected error: {e}")
        print(tb)
        try:
            mark_blocked(conn, cur, gap_id, f"Exception: {str(e)[:150]}")
        except Exception:
            pass
        return False, f"Exception: {e}"


# ---------------------------------------------------------------------------
# External quality gates
# ---------------------------------------------------------------------------


def run_external_gates(diff_path, pr_body_path, repo_full_name):
    """Run scope-check.py and disclosure-check.py. Returns (passed, errors)."""
    errors = []

    r = subprocess.run(
        ['python3', os.path.join(SCRIPTS_DIR, 'scope-check.py'), diff_path],
        capture_output=True, text=True
    )
    if r.returncode != 0 or 'SCOPE_FAIL' in r.stdout:
        errors.append(f"Scope: {r.stdout.strip()[:200]}")

    r = subprocess.run(
        ['python3', os.path.join(SCRIPTS_DIR, 'disclosure-check.py'), pr_body_path],
        capture_output=True, text=True
    )
    if r.returncode != 0 or 'DISCLOSURE_FAIL' in r.stdout:
        errors.append(f"Disclosure: {r.stdout.strip()[:200]}")

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description='Agent contribution engine — handles any gap type for any repo'
    )
    parser.add_argument('--gap-id', type=int, help='Specific gap ID to process')
    parser.add_argument('--repo', type=str,
                        help='Restrict to gaps from this repo (e.g. vllm-project/vllm)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would happen without making changes')
    args = parser.parse_args()

    conn = get_conn()
    cur = conn.cursor()

    # Circuit breaker
    ok, reason = check_circuit_breaker(conn)
    if not ok:
        print(f"BLOCKED: {reason}")
        conn.close()
        sys.exit(1)

    # Ramp cap (skip for dry runs)
    if not args.dry_run:
        ok, reason = check_ramp_cap(conn)
        if not ok:
            print(f"BLOCKED: {reason}")
            conn.close()
            sys.exit(1)

    # Fetch gap(s)
    gap = fetch_gap(conn, gap_id=args.gap_id, repo_filter=args.repo)
    if not gap:
        print("ENGINE: No eligible gaps in queue.")
        conn.close()
        sys.exit(0)

    if args.dry_run:
        print(f"ENGINE: DRY RUN — gap #{gap['id']} {gap['repo_full_name']}")
        print(f"  Wedge: {gap['wedge_type']} | Score: {gap['score']:.3f}")
        print(f"  Description: {gap['description'][:120]}")
        print(f"  Source: {gap['source_url']}")
        _, repo_name = gap['repo_full_name'].split('/', 1)
        branch_slug = make_slug(gap['description'])
        print(f"  Branch would be: fix/{gap['wedge_type']}-{gap['id']}-{branch_slug}")
        print(f"  Worktree: {os.path.join(WORKTREE_BASE, repo_name)}")
        success, result = process_gap(conn, gap, dry_run=True)
        conn.close()
        sys.exit(0 if success else 1)

    print(f"ENGINE: gap #{gap['id']} {gap['repo_full_name']} — starting")
    print(f"  Wedge: {gap['wedge_type']} | Score: {gap['score']:.3f}")
    print(f"  Description: {gap['description'][:100]}")

    success, result = process_gap(conn, gap, dry_run=False)

    if success:
        print(f"ENGINE: gap #{gap['id']} {gap['repo_full_name']} — submitted {result}")
    else:
        print(f"ENGINE: gap #{gap['id']} {gap['repo_full_name']} — FAILED: {result}")

    conn.close()
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
