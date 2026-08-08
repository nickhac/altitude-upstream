# openai/openai-python: Proven Learnings

**Confidence:** low | **Last updated:** 2026-08-05

## Status
- Tier 1 but LOW PRIORITY: 0/5 historical external contributor PRs merged
- The client SDK is largely auto-generated from the OpenAPI spec
- Most hand-written code is internal

## What might work
- missing_documentation on public API methods that are hand-written (not generated)
- Target: src/openai/resources/ — check if file is generated before modifying

## Infrastructure
- Default branch: main
- Worktree: $WORKTREE_BASE/openai-python (created on first run)

## Warning
- Check if target file contains "# File generated from our OpenAPI spec" comment
- If yes: do not submit a PR — changes will be overwritten on next generation

## Open questions
- Have not successfully submitted a PR here yet — all signal is theoretical
