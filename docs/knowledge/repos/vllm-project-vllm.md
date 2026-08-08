# vllm-project/vllm: Proven Learnings

**Confidence:** medium | **Last updated:** 2026-08-05

## What works
- missing_documentation (XS): docstrings on public classes/functions
- Do NOT run pytest on source files — vllm has custom pytest plugins that break on import
  outside the installed venv. Only run tests in tests/ directories.

## Infrastructure
- Default branch: main
- Worktree: $WORKTREE_BASE/vllm
- Test detection: only files matching test_*.py or in tests/ — never source files

## CI / Review process
- Large active community, strict review bar
- PRs from external contributors reviewed but can wait 1-2 weeks

## What doesn't work
- model_registry_staleness: vllm issues in this category are typically feature requests
  requiring architectural changes — not addressable in 1-3 files
- Running pytest on vllm/sampling_params.py or other source files: ImportError (see CI note)

## Open questions
- Docstring PR merge rate: PR #51019 was self-closed (quality issue), no clean data yet
