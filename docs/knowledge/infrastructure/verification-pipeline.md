# Verification Pipeline: Gate Order and Rationale

**Confidence:** high | **Last updated:** 2026-08-05

## Gate sequence (all must pass before PR submission)

1. **Quality gate** (deterministic)
   - Max 5 files changed, max 200 lines
   - No secrets (regex: ghp_, sk-ant-, AKIA, etc.)
   - No CI/CD file modifications (*.yml in .github/, Dockerfile, requirements*.txt)

2. **Execution smoke test** (deterministic)
   - Syntax: python3 -m py_compile {file}
   - Import: python3 -c "import {module}" in worktree
   - Symbol check: new public functions/classes are accessible after import
   - Skips non-Python files silently (Go, YAML, JSON changes don't need this)

3. **Test runner** (deterministic)
   - pytest on test_*.py and tests/* files ONLY
   - 120s timeout per run
   - One retry with error context if first attempt fails
   - Passes automatically if no test files were modified

4. **Semantic verifier** (agentic — Hermes agent in new architecture)
   - Reads: diff + original source file (first 200 lines) + gap description
   - Answers: does this change correctly fix the described gap?
   - Catches: fabricated claims, wrong logic, incomplete fixes, attribution in source code
   - Fails open: infra failures never block a good PR

5. **Disclosure check** (deterministic)
   - PR body must contain: "Co-authored-by: Hermes Agent"
   - Source code must NOT contain any Co-authored-by text

## Attribution rules
- Co-authored-by goes in: git commit message body, PR description
- Co-authored-by goes NOWHERE in: source code, docstrings, comments, JSON values
