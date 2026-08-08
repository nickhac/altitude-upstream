# GitHub Authentication: Proven Patterns

**Confidence:** high | **Last updated:** 2026-08-05

## Two-Token Pattern (mandatory)

### Token 1: Fine-grained PAT
- Secret: `<NICKHAC_PAT_SECRET>`
- Scopes: admin + workflow on nickhac/* repos
- Use for: git push, git clone (authenticated), fork operations
- Cannot: create PRs on repos you don't own (403 on both gh CLI and REST API)

### Token 2: Classic PAT
- Secret: `<NICKHAC_CLASSIC_PAT_SECRET>`
- Scopes: repo, workflow, admin:org
- Use for: PR creation via REST API ONLY
- Pattern: POST https://api.github.com/repos/{upstream}/pulls
- Never use gh pr create — GraphQL returns "Resource not accessible by personal access token"

## Branch creation rules
- Always base new branches on upstream/main (or upstream/master) — never fork main
- Fork main can be thousands of commits behind — stale base = contaminated diff
- Before branching: sync fork via POST /repos/{fork}/merge-upstream {"branch":"main"}
- Then: git fetch upstream && git checkout upstream/main -b fix/your-branch-name

## Default branch detection
- Do not hardcode 'main' — fetch from GitHub API:
  GET /repos/{owner}/{repo} → .default_branch
- Known exceptions: langchain-ai/langchain uses 'master'

## Environment
- Unset DATABASE_URL before any subprocess that might inherit it (git, gh CLI)
  RDS URL in env causes unexpected tools to try connecting to Postgres
