# langchain-ai/langchain: Proven Learnings

**Confidence:** medium | **Last updated:** 2026-08-05

## What works
- missing_documentation (XS): docstrings on partner integration methods
- Target: libs/partners/ subdirectory — cleaner, smaller files, faster CI

## Infrastructure
- Default branch: **master** (NOT main — this will break PR creation if wrong)
- Worktree: $WORKTREE_BASE/langchain
- Branch base: upstream/master
- PR base: master

## What doesn't work
- 71 false-positive model_registry_staleness gaps were generated — langchain is not a
  model registry. These have been deprioritised. Do not re-scan this repo for V1 gaps.
- M/L effort gaps: architecture-level, not automatable
- **PR #39251 declined 2026-08-05**: auto-closed by bot — langchain requires issue-first approval.
  CONTRIBUTING.md rule: open issue → wait for maintainer assignment → THEN open PR.
  We did not follow this. All future langchain PRs MUST link a pre-approved issue.

## Hard requirement: issue-first workflow
langchain-ai enforces strict issue-first review. Before submitting ANY PR:
1. Open or find an existing issue describing the change
2. Wait for a maintainer to comment approval + assign
3. Only then open the PR with the issue number in the description

**Do NOT submit PRs to langchain-ai/langchain without a linked approved issue.**
Skip langchain gaps in the daily contribution run until this gate is built into the pipeline.

## Open questions
- Build issue-pre-flight gate: auto-open an issue, wait N days for approval, then submit PR
