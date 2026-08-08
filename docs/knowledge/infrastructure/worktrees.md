# Worktree Management: Proven Patterns

**Confidence:** high | **Last updated:** 2026-08-05

## Hard reset before every contribution (mandatory)

Before creating any branch:
```bash
git clean -fdx          # remove all untracked/ignored files
git checkout -- .       # revert tracked modifications
git fetch upstream --depth=200
git checkout main       # or master
git reset --hard upstream/main   # or upstream/master
```

Without this: stale branch files, leftover diffs from prior runs, and uncommitted
changes from failed attempts silently corrupt the new PR's diff.

## Worktree locations
- litellm: $WORKTREE_BASE/litellm (dedicated path, not $WORKTREE_BASE/)
- All other repos: $WORKTREE_BASE/{repo_name}

## First-time clone
```bash
git clone --depth=1 https://{fine_grained_pat}@github.com/nickhac/{repo}.git {path}
git remote add upstream https://github.com/{owner}/{repo}.git
git config user.email "<your-github-noreply-email>"
git config user.name "nickhac"
```

## Test runner rules
- ONLY run tests in: files matching test_*.py or paths containing /tests/
- NEVER run pytest on source files — complex repos (vllm, litellm) have import
  dependencies and custom plugins that break when run outside the installed package
- NEVER run make test, pants, or bazel — exotic build tools not installed on this box
