# run-llama/llama_index: Proven Learnings

**Confidence:** medium | **Last updated:** 2026-08-05

## What works
- missing_documentation (XS): docstrings on async utility functions
- Maintainers explicitly welcome external contributions (CONTRIBUTING.md states this)
- Target file: llama-index-core/llama_index/core/async_utils.py

## Infrastructure
- Default branch: main
- Worktree: $WORKTREE_BASE/llama_index
- Build system: pants — do NOT run make test or pants commands, they require pants installed
- Test detection: only tests/ directory files

## What doesn't work
- PRs containing fabricated information (invented deprecation notices): immediately rejected
  PR #22571 was self-closed for this reason
- Avoid claiming a function is deprecated unless the source code explicitly marks it so

## Open questions
- Merge rate: PRs #22584, #22585, #22590 open — awaiting first merge signal
