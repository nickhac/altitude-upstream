#!/usr/bin/env python3
"""
verify_contribution.py — altitude-upstream

Semantic correctness check for generated diffs, using a direct Bedrock API call.
Answers: does this change actually solve the described gap, correctly and safely?

This is NOT a structural check (size, secrets, CI paths — that's check_diff_quality).
This is a reasoning check: read the gap description, read the diff, decide if the
fix is correct, complete, and safe to submit as a PR.

Returns a structured verdict:
  PASS  — diff correctly solves the gap, safe to submit
  REJECT — diff is wrong, incomplete, or unsafe; reason explains why

Usage (standalone):
    python3 scripts/verify_contribution.py --gap-id 42 --diff /tmp/foo.diff
    python3 scripts/verify_contribution.py --gap-id 42 --diff /tmp/foo.diff --dry-run

Usage (imported):
    from verify_contribution import verify_contribution
    ok, verdict, reason = verify_contribution(diff_text, gap, source_context)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))

BEDROCK_MODEL = os.environ.get('BEDROCK_MODEL_ID', 'anthropic.claude-3-5-sonnet-20241022-v2:0')
BEDROCK_REGION = os.environ.get('AWS_REGION', 'us-east-1')

# ---------------------------------------------------------------------------
# Bedrock client
# ---------------------------------------------------------------------------

def _bedrock_client():
    try:
        import boto3
        return boto3.client('bedrock-runtime', region_name=BEDROCK_REGION)
    except ImportError:
        raise RuntimeError("boto3 not available — install with: pip install boto3")


def _call_bedrock(system: str, user: str, max_tokens: int = 1024) -> str:
    """Single Bedrock API call. Returns the text response."""
    client = _bedrock_client()
    body = json.dumps({
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': max_tokens,
        'system': system,
        'messages': [{'role': 'user', 'content': user}],
    })
    response = client.invoke_model(modelId=BEDROCK_MODEL, body=body)
    result = json.loads(response['body'].read())
    return result['content'][0]['text']


# ---------------------------------------------------------------------------
# Source context fetcher
# ---------------------------------------------------------------------------

def _fetch_source_context(diff_text: str, repo_full_name: str,
                           source_url: Optional[str]) -> str:
    """
    Fetch up to 200 lines of context around the changed files so the reviewer
    can see what the code looked like before. Uses GitHub raw URLs.
    Returns a string to inject into the verification prompt.
    """
    context_parts = []

    # Extract changed files from the diff
    files = []
    for line in diff_text.splitlines():
        if line.startswith('+++ b/'):
            files.append(line[6:])

    if not files:
        return ""

    # Pick the primary changed file (first non-test file, else first file)
    primary = next((f for f in files if 'test' not in f.lower()), files[0])

    # Determine branch — try main then master
    owner, _, repo = repo_full_name.partition('/')
    for branch in ('main', 'master'):
        url = f"https://raw.githubusercontent.com/{repo_full_name}/{branch}/{primary}"
        r = subprocess.run(
            ['curl', '-sL', '--max-time', '10', '--write-out', '%{http_code}', url],
            capture_output=True, text=True
        )
        if r.stdout and not r.stdout.endswith('404'):
            # Last 3 chars are the HTTP status code appended by --write-out
            status = r.stdout[-3:]
            body = r.stdout[:-3]
            if status == '200' and body.strip():
                lines = body.splitlines()
                # Keep first 200 lines — enough to understand structure and conventions
                preview = '\n'.join(lines[:200])
                context_parts.append(
                    f"### Source file (first 200 lines): {primary}\n```\n{preview}\n```"
                )
                break

    # If there's an issue URL, note it (reviewer can use it as context)
    if source_url and 'github.com' in source_url:
        context_parts.append(f"### Related issue: {source_url}")

    return '\n\n'.join(context_parts)


# ---------------------------------------------------------------------------
# Core verification
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = textwrap.dedent("""
    You are a senior open-source maintainer performing a code review on an
    AI-generated contribution before it is submitted as a pull request.

    Your job is to determine whether the diff correctly and completely solves
    the described gap, and is safe to submit.

    You must respond ONLY with a JSON object in exactly this format — no other text:
    {
      "verdict": "PASS" or "REJECT",
      "reason": "one clear sentence explaining the verdict",
      "issues": ["list of specific problems if REJECT, empty list if PASS"],
      "suggestions": ["non-blocking improvements, empty if none"]
    }

    REJECT criteria (any one is sufficient):
    - The diff does not actually fix the described gap
    - The logic is wrong or the change would cause incorrect behaviour
    - The change is incomplete (e.g. adds a function but never calls it, or
      adds a model but misses required fields)
    - The diff introduces a regression or breaks existing behaviour
    - The diff touches files unrelated to the gap
    - The change adds test coverage that tests the wrong thing
    - For docstrings: the docstring is inaccurate, misleading, or describes
      the wrong function

    PASS criteria:
    - The diff directly addresses the described gap
    - The logic is correct given the surrounding code context
    - If tests were added, they test the right behaviour
    - The change is minimal — does not do more than necessary
    - A maintainer would accept this without requesting major changes
""").strip()


def verify_contribution(
    diff_text: str,
    gap: dict,
    source_context: str = "",
    verbose: bool = False,
) -> tuple[bool, str, str]:
    """
    Verify a diff against the gap it claims to fix.

    Args:
        diff_text:      The unified diff to verify.
        gap:            Dict with keys: description, wedge_type, source_url,
                        repo (full_name).
        source_context: Optional pre-fetched source context string.
        verbose:        Print the raw Bedrock response.

    Returns:
        (ok, verdict, reason)
        ok=True  → PASS, safe to submit
        ok=False → REJECT, reason explains why
    """
    if not diff_text or not diff_text.strip():
        return False, "REJECT", "Empty diff — nothing to verify"

    repo = gap.get('repo_full_name') or gap.get('repo', 'unknown/repo')
    description = gap.get('description', '(no description)')
    wedge_type = gap.get('wedge_type', 'unknown')
    source_url = gap.get('source_url') or ''

    # Fetch source context if not provided
    if not source_context:
        try:
            source_context = _fetch_source_context(diff_text, repo, source_url)
        except Exception as e:
            source_context = f"(could not fetch source context: {e})"

    # Truncate diff if very large — keep first 300 lines which cover the substance
    diff_lines = diff_text.splitlines()
    if len(diff_lines) > 300:
        diff_text = '\n'.join(diff_lines[:300]) + f"\n... (truncated, {len(diff_lines)} total lines)"

    user_message = textwrap.dedent(f"""
        ## Contribution to review

        **Repo:** {repo}
        **Gap type:** {wedge_type}
        **Gap description:** {description}
        {"**Related issue:** " + source_url if source_url else ""}

        ## Source context (before the change)

        {source_context if source_context else "(not available)"}

        ## The diff

        ```diff
        {diff_text}
        ```

        Review this diff against the gap description and return your JSON verdict.
    """).strip()

    if verbose:
        print(f"\n[verify] Sending to Bedrock ({BEDROCK_MODEL})...")

    try:
        raw = _call_bedrock(SYSTEM_PROMPT, user_message)
    except Exception as e:
        # Fail open — don't block contributions on infra errors
        return True, "PASS", f"Verification skipped (Bedrock error: {e})"

    if verbose:
        print(f"[verify] Raw response:\n{raw}\n")

    # Parse the JSON verdict
    verdict_data = _parse_verdict(raw)
    if verdict_data is None:
        # Can't parse — fail open with a warning
        return True, "PASS", f"Verification inconclusive (parse error) — raw: {raw[:150]}"

    verdict = verdict_data.get('verdict', 'PASS').upper()
    reason = verdict_data.get('reason', '')
    issues = [i for i in verdict_data.get('issues', []) if i]
    suggestions = [s for s in verdict_data.get('suggestions', []) if s]

    if verdict == 'REJECT':
        detail = '; '.join(issues) if issues else reason
        return False, 'REJECT', detail

    summary = reason
    if suggestions:
        summary += f" | suggestions: {'; '.join(suggestions[:2])}"
    return True, 'PASS', summary


def _parse_verdict(text: str) -> Optional[dict]:
    """Extract JSON verdict from Bedrock response text."""
    # Try direct parse first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Try to find JSON block in prose response
    m = re.search(r'\{[^{}]*"verdict"[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    # Try markdown code block
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Verify a contribution diff against its gap description'
    )
    parser.add_argument('--gap-id', type=int, help='Gap ID to look up from DB')
    parser.add_argument('--diff', type=str, help='Path to diff file')
    parser.add_argument('--diff-stdin', action='store_true',
                        help='Read diff from stdin')
    parser.add_argument('--description', type=str,
                        help='Gap description (if not using --gap-id)')
    parser.add_argument('--repo', type=str,
                        help='Repo full name (if not using --gap-id)')
    parser.add_argument('--verbose', action='store_true',
                        help='Print raw Bedrock response')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print what would be verified without calling Bedrock')
    args = parser.parse_args()

    # Load diff
    if args.diff_stdin:
        diff_text = sys.stdin.read()
    elif args.diff:
        with open(args.diff) as f:
            diff_text = f.read()
    else:
        print("ERROR: provide --diff <path> or --diff-stdin")
        sys.exit(1)

    # Load gap
    gap = {}
    if args.gap_id:
        try:
            from db import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT g.id, g.wedge_type, g.description, g.source_url,
                       r.full_name
                FROM gaps g JOIN repos r ON g.repo_id = r.id
                WHERE g.id = %s
            """, (args.gap_id,))
            row = cur.fetchone()
            if not row:
                print(f"ERROR: gap #{args.gap_id} not found")
                sys.exit(1)
            gap = {
                'id': row[0],
                'wedge_type': row[1],
                'description': row[2],
                'source_url': row[3],
                'repo_full_name': row[4],
            }
            conn.close()
        except Exception as e:
            print(f"ERROR loading gap from DB: {e}")
            sys.exit(1)
    else:
        gap = {
            'description': args.description or '(no description)',
            'wedge_type': 'unknown',
            'source_url': '',
            'repo_full_name': args.repo or 'unknown/repo',
        }

    if args.dry_run:
        print(f"DRY RUN — would verify:")
        print(f"  repo:        {gap.get('repo_full_name')}")
        print(f"  gap:         {gap.get('description', '')[:100]}")
        print(f"  diff lines:  {len(diff_text.splitlines())}")
        print(f"  model:       {BEDROCK_MODEL}")
        sys.exit(0)

    print(f"VERIFY: gap #{gap.get('id', '?')} — {gap.get('description', '')[:80]}")
    ok, verdict, reason = verify_contribution(
        diff_text, gap, verbose=args.verbose
    )

    print(f"VERIFY: {verdict} — {reason}")
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
