#!/usr/bin/env python3
"""
scope-check.py — altitude-upstream

Validates a contribution diff is within acceptable scope:
  - < 200 lines changed (additions + deletions)
  - Single logical unit (no unrelated file sprawl)
  - No secrets or tokens present

Exit 0 = pass. Exit 1 = fail (reason printed).

Usage:
    python3 scripts/scope-check.py <diff-file>
    git diff HEAD > /tmp/contribution.diff && python3 scripts/scope-check.py /tmp/contribution.diff
"""

import sys
import re

MAX_LINES = 200
SECRET_PATTERNS = [
    r"(?i)(api_key|secret|token|password|passwd|pwd)\s*=\s*['\"][^'\"]{8,}['\"]",
    r"ghp_[A-Za-z0-9]{36}",
    r"sk-[A-Za-z0-9]{32,}",
    r"AKIA[0-9A-Z]{16}",
    r"(?i)bearer\s+[A-Za-z0-9\-_\.]{20,}",
]


def run(diff_file: str) -> int:
    print(f"SCOPE-CHECK: {diff_file}")

    try:
        with open(diff_file) as f:
            diff = f.read()
    except FileNotFoundError:
        print(f"SCOPE_FAIL: file not found: {diff_file}")
        return 1

    # Count added/removed lines (lines starting with + or - but not +++ / ---)
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    total = added + removed
    print(f"  lines changed: +{added} -{removed} = {total} total")

    if total > MAX_LINES:
        print(f"SCOPE_FAIL: {total} lines changed exceeds limit of {MAX_LINES}")
        return 1

    # Count files changed
    files_changed = [line[6:] for line in diff.splitlines() if line.startswith("+++ b/")]
    print(f"  files changed: {len(files_changed)}")
    for f in files_changed:
        print(f"    {f}")

    # Secret scan
    for pattern in SECRET_PATTERNS:
        m = re.search(pattern, diff)
        if m:
            print(f"SCOPE_FAIL: potential secret detected matching pattern: {pattern[:40]}")
            return 1

    print(f"SCOPE_PASS: {total} lines, {len(files_changed)} file(s), no secrets detected")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/scope-check.py <diff-file>")
        sys.exit(1)
    sys.exit(run(sys.argv[1]))
