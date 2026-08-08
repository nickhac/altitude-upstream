# BerriAI/litellm: Proven Learnings

**Confidence:** high | **Last updated:** 2026-08-05

## What works
- model_registry_staleness (JSON additions): reliable pattern, agent gets it right
- Both JSON files must be updated in every PR:
  - `litellm/model_prices_and_context_window.json`
  - `litellm/model_prices_and_context_window_backup.json`
- CLA signed: nickhac signed at cla-assistant.io/BerriAI/litellm on 2026-08-03 ✓
- Daily cap: 3 PRs/day (litellm has the most gaps and is the most reliable)

## Infrastructure
- Default branch: main
- Worktree: $WORKTREE_BASE/litellm (dedicated, not $WORKTREE_BASE/litellm)
- PAT for push/fork: <NICKHAC_PAT_SECRET> (fine-grained, admin+workflow on nickhac/*)
- PAT for PR creation: <NICKHAC_CLASSIC_PAT_SECRET> (classic, repo+workflow scopes)

## CI / Review process
- Greptile + Claude Code review: [litellm-maintainer] runs automated review, score >= 4/5 required
- Fork CI failures on Block-fork-dependency-changes and Verify-PR-source-branch are EXPECTED
  and non-blocking — do not treat as real failures
- PR diff must show ONLY our changed files — if it shows upstream infra changes, branch is stale

## What doesn't work
- M/L effort gaps (feature requests, architecture changes): agent cannot complete in reasonable turns
- Stale fork main as branch base: always base on upstream/main directly

## Open questions
- Docstring acceptance rate: unknown — no merges yet on missing_documentation wedge
