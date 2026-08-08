#!/usr/bin/env python3
"""
disclosure-check.py — altitude-upstream

Verifies a PR body contains the required AI disclosure line.

Required: Co-authored-by: Hermes Agent
Also checks: AI assistance note present

Exit 0 = pass. Exit 1 = fail.

Usage:
    python3 scripts/disclosure-check.py <pr-body-file>
"""

import sys
import re

REQUIRED_COAUTHORED = "Co-authored-by: Hermes Agent"
AI_NOTE_PATTERNS = [
    r"ai[- ]assisted",
    r"generated with",
    r"hermes agent",
    r"co-authored-by:\s*hermes",
]


def run(pr_body_file: str) -> int:
    print(f"DISCLOSURE-CHECK: {pr_body_file}")

    try:
        with open(pr_body_file) as f:
            body = f.read()
    except FileNotFoundError:
        print(f"DISCLOSURE_FAIL: file not found: {pr_body_file}")
        return 1

    # Check Co-authored-by line
    if REQUIRED_COAUTHORED not in body:
        print(f"DISCLOSURE_FAIL: missing required line: '{REQUIRED_COAUTHORED}'")
        return 1
    print(f"  Co-authored-by: Hermes Agent ✓")

    # Check AI assistance note
    lower = body.lower()
    found_note = any(re.search(p, lower) for p in AI_NOTE_PATTERNS)
    if not found_note:
        print("DISCLOSURE_FAIL: no AI assistance note found in PR body")
        return 1
    print(f"  AI assistance note present ✓")

    print("DISCLOSURE_PASS: all required disclosures present")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/disclosure-check.py <pr-body-file>")
        sys.exit(1)
    sys.exit(run(sys.argv[1]))
