#!/usr/bin/env python3
"""
pre-flight-gate.py — altitude-upstream Phase 1

Checks a target repo before any contribution attempt:
  1. CONTRIBUTING.md exists and is parseable
  2. No explicit AI contribution ban
  3. Repo is actively maintained (commit in last 90 days)

Exit 0 = green. Exit 1 = blocked (reason printed to stdout as GATE_FAIL: ...).

Usage:
    python3 scripts/pre-flight-gate.py BerriAI/litellm
"""

import sys
import json
import subprocess
import re
from datetime import datetime, timezone, timedelta


def gh_api(path: str) -> dict | list | None:
    r = subprocess.run(
        ["gh", "api", path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def get_file_content(repo: str, filepath: str) -> str | None:
    """Fetch raw file content from GitHub via gh api."""
    data = gh_api(f"repos/{repo}/contents/{filepath}")
    if data is None or not isinstance(data, dict):
        return None
    if data.get("encoding") == "base64":
        import base64
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return data.get("content")


AI_BAN_PATTERNS = [
    r"no\s+ai[- ]generated",
    r"do not\s+use\s+ai",
    r"ai\s+contributions?\s+(are\s+)?not\s+accepted",
    r"prohibit\s+ai",
    r"ban\s+ai",
    r"no\s+llm",
    r"no\s+copilot",
    r"human[- ]only\s+contributions?",
]


def check_ai_ban(text: str) -> tuple[bool, str]:
    """Returns (banned, reason). True = banned."""
    lower = text.lower()
    for pattern in AI_BAN_PATTERNS:
        m = re.search(pattern, lower)
        if m:
            # Extract surrounding context for the reason
            start = max(0, m.start() - 40)
            end = min(len(lower), m.end() + 40)
            snippet = text[start:end].replace("\n", " ").strip()
            return True, f"AI ban detected: '...{snippet}...'"
    return False, ""


def run(repo: str) -> int:
    print(f"PRE-FLIGHT: {repo}")
    owner, name = repo.split("/", 1)

    # --- 1. Repo exists and get metadata ---
    repo_data = gh_api(f"repos/{repo}")
    if repo_data is None:
        print(f"GATE_FAIL: repo {repo} not found or not accessible")
        return 1

    stars = repo_data.get("stargazers_count", 0)
    archived = repo_data.get("archived", False)
    if archived:
        print(f"GATE_FAIL: repo is archived")
        return 1
    print(f"  repo: {stars} stars, archived={archived} ✓")

    # --- 2. Active maintenance check (commit in last 90 days) ---
    commits = gh_api(f"repos/{repo}/commits?per_page=1")
    if not commits or not isinstance(commits, list):
        print("GATE_FAIL: cannot read commit history")
        return 1

    first = commits[0]
    assert isinstance(first, dict)
    last_commit_str = first["commit"]["committer"]["date"]
    last_commit = datetime.fromisoformat(last_commit_str.replace("Z", "+00:00"))
    age_days = (datetime.now(timezone.utc) - last_commit).days
    if age_days > 90:
        print(f"GATE_FAIL: last commit {age_days} days ago (threshold: 90 days)")
        return 1
    print(f"  last commit: {last_commit_str[:10]} ({age_days} days ago) ✓")

    # --- 3. CONTRIBUTING.md --- 
    contributing = None
    for path in ["CONTRIBUTING.md", "CONTRIBUTING", ".github/CONTRIBUTING.md"]:
        contributing = get_file_content(repo, path)
        if contributing:
            print(f"  CONTRIBUTING.md: found at {path} ({len(contributing)} chars) ✓")
            break
    if contributing is None:
        # Not a hard block — many good repos don't have one
        print("  CONTRIBUTING.md: not found (WARN — proceeding)")

    # --- 4. AI ban check (CONTRIBUTING + README + AI_POLICY if exists) ---
    texts_to_check = []
    if contributing:
        texts_to_check.append(("CONTRIBUTING.md", contributing))

    for ai_policy_path in ["AI_POLICY.md", ".github/AI_POLICY.md", "AI_CONTRIBUTIONS.md"]:
        ai_policy = get_file_content(repo, ai_policy_path)
        if ai_policy:
            texts_to_check.append((ai_policy_path, ai_policy))
            print(f"  {ai_policy_path}: found, checking for AI ban...")
            break

    readme = get_file_content(repo, "README.md")
    if readme:
        texts_to_check.append(("README.md", readme[:3000]))  # only scan top of README

    for source, text in texts_to_check:
        banned, reason = check_ai_ban(text)
        if banned:
            print(f"GATE_FAIL: [{source}] {reason}")
            return 1

    print(f"  AI policy: no ban detected across {len(texts_to_check)} file(s) ✓")

    # --- 5. PR template ---
    pr_template = None
    for path in [".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md",
                 "PULL_REQUEST_TEMPLATE.md"]:
        pr_template = get_file_content(repo, path)
        if pr_template:
            print(f"  PR template: {path} ✓")
            break
    if pr_template is None:
        print("  PR template: not found (will use standard body)")

    print(f"GATE_PASS: {repo} — cleared for contribution")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/pre-flight-gate.py <owner>/<repo>")
        sys.exit(1)
    sys.exit(run(sys.argv[1]))
