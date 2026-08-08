# altitude-upstream

**Autonomous OSS contribution agent.** Discovers real gaps in high-traffic AI/LLM repos, writes quality-gated code fixes, and submits verified PRs — daily, unattended.

Built by [nickhac](https://github.com/nickhac) as a production agentic system, not a demo.

---

## What it does

```
Gap Scanner ──► Score & Queue ──► Fix Writer ──► Quality Gate ──► PR Submission
     │                                                                    │
     └──────── Postgres state ◄──── Acceptance Learning ◄────────────────┘
```

1. **Scans** 5 Tier-1 LLM repos daily for contribution gaps: missing model registry entries, broken integrations, missing documentation
2. **Scores** each gap on user pain, maintainer receptivity, freshness, and feasibility — re-scored weekly as new signals arrive
3. **Writes** the fix using a Hermes-native agent with loop-engineering constraints (turn budgets, early-exit on success, partial-work rescue)
4. **Gates** every contribution through: diff quality check → smoke test → semantic verifier → CONTRIBUTING.md compliance
5. **Submits** PRs via the GitHub API with full AI disclosure, conventional commits, and repo-specific formatting
6. **Learns** from every merge/decline — acceptance signals written back into the knowledge base as structured markdown, committed to git

---

## Target repos

| Repo | Domain | Daily cap |
|---|---|---|
| [BerriAI/litellm](https://github.com/BerriAI/litellm) | LLM gateway, model registry | 3/day |
| [vllm-project/vllm](https://github.com/vllm-project/vllm) | LLM inference engine | 1/day |
| [run-llama/llama_index](https://github.com/run-llama/llama_index) | Document agents | 1/day |
| [openai/openai-python](https://github.com/openai/openai-python) | OpenAI Python SDK | 1/day |

Global cap: **5 PRs/day**, ramping.

---

## Architecture

### Core pipeline (`scripts/`)

| Script | Role |
|---|---|
| `gap-scanner.py` | GitHub issues + code analysis → gap candidates |
| `smart-gap-scorer.py` | Scores gaps: user_pain × maintainer_receptivity × feasibility × freshness |
| `get-eligible-gaps.py` | Daily cap enforcement, per-repo limits, SKIP_REPOS gate |
| `agent-contribution-engine.py` | Orchestrates fix writing via Bedrock agents |
| `hermes-contribution-agent.py` | Hermes-native agent with loop engineering (MAX_FIX_ATTEMPTS=2, EARLY-EXIT, TURN BUDGET) |
| `smoke_test_execution.py` | Runs `python3 -m py_compile` + repo-specific smoke tests |
| `verify_contribution.py` | Semantic review: does this diff actually fix what it claims? |
| `scope-check.py` | Pre-submission: scope guard (max 3 files, no CI/lockfile touches) |
| `pr-monitor.py` | Polls open PRs every 2h: merges, declines, conflicts, review requests |
| `acceptance-learning.py` | Writes merge/decline signals back to `docs/knowledge/` markdown |
| `record-contribution-result.py` | DB writes: submitted/blocked/rejected/skipped |
| `get-repo-knowledge.py` | Loads repo-specific knowledge files for agent context injection |
| `daily-brief.py` | Daily Telegram summary: PRs submitted, queue depth, acceptance rate |

### Knowledge base (`docs/knowledge/`)

Every proven fact about each repo lives as versioned markdown — not hardcoded Python constants. The agent reads this before writing any fix. The learning agent writes back to it after every PR closes.

```
docs/knowledge/
├── repos/               # Per-repo: what works, what doesn't, CI quirks, branch conventions
├── wedge-types/         # Per-gap-type: acceptance signals, common failure modes
├── gap-scanner/         # Exclusion patterns, scoring signals
├── infrastructure/      # Worktree setup, GitHub auth, verification pipeline
└── agent-prompts/       # Canonical Fix Writer + Verifier prompt templates with loop contracts
```

### Loop engineering

Every agent runs under explicit constraints to prevent runaway tool loops:

```
Fix Writer:
  MAX_FIX_ATTEMPTS = 2          # code iteration cap (separate from 3-attempt infra cap)
  TURN BUDGET: stop at 15 tool calls
  EARLY-EXIT: stop the moment git diff is non-empty + py_compile passes
  Partial rescue: save /tmp/partial-{gap_id}.diff before marking BLOCKED

Verifier:
  Single pass, read-only
  Fail-open: infra errors → PASS (never block a good diff on infra noise)
  Strict reject: fabricated claims, wrong file, broken logic
```

### State (Postgres)

```sql
gaps        -- discovered gaps with scores, effort, status
repos       -- tier-1 repos with metadata
prs         -- submitted PRs with status lifecycle
ramp_state  -- daily submission counter + cap
pr_events   -- merge/decline/conflict/review events timeline
```

---

## Quality gates (in order)

Every contribution passes all of these before a PR is opened:

1. **Diff non-empty** — something was actually changed
2. **Scope check** — ≤3 files, no CI/Dockerfile/lockfile touches
3. **Smoke test** — `py_compile` on all changed `.py` files
4. **Repo tests** — any existing tests for the changed module
5. **Semantic verifier** — LLM review: does the diff plausibly fix the described gap?
6. **CONTRIBUTING.md parse** — PR format, branch target, required sections
7. **AI disclosure** — every PR includes `Co-authored-by: Hermes Agent`

---

## Wedge types

**model_registry_staleness** — New models available via API but missing from `model_prices_and_context_window.json`. System fetches provider pricing pages, cross-references against registry, generates entries with correct token limits and pricing.

**broken_integration** — Provider-specific flags or parameters that cause runtime errors for real users. Identified from GitHub issues with user-reported stack traces. System finds the flag, patches the registry or handler code.

**missing_documentation** — Public API surface (classes, functions) with no docstrings. System generates accurate docstrings from type signatures, usage examples in tests, and source context — never fabricated.

---

## Tech stack

```
Language      Python 3.11
Database      PostgreSQL (AWS RDS) via psycopg2
Agents        Hermes Agent (Nous Research) + Amazon Bedrock (Claude Sonnet)
Scheduling    Hermes cron (daily contribution at 09:00 UTC, monitor every 2h)
Auth          AWS Secrets Manager (PATs, DB credentials, Telegram token)
VCS           git worktrees — one per target repo, hard-reset before each contribution
Notifications Telegram bot (PR events, daily brief, conflict alerts)
Testing       pytest, 94 tests
```

---

## Configuration

All secrets are pulled from **AWS Secrets Manager** at runtime. No credentials in code or config files.

Required secrets (configure in AWS Secrets Manager with your own naming convention):
```
<your-project>/github/pat           # Fine-grained PAT: push/fork on your forks
<your-project>/github/classic-pat   # Classic PAT: PR creation against upstream repos
<your-project>/db/database_url      # Postgres connection URL
<your-project>/db/password          # Postgres password (separate — URL-encoding safe)
<your-project>/notifications/telegram_bot_token  # Telegram alerts
```

Required environment variables:
```bash
# AWS
AWS_REGION=<your-region>
BEDROCK_MODEL_ID=<your-bedrock-model-id>

# Secret names (tell the scripts where to find secrets in Secrets Manager)
NICKHAC_PAT_SECRET=<your-project>/github/pat
NICKHAC_CLASSIC_PAT_SECRET=<your-project>/github/classic-pat
DB_URL_SECRET=<your-project>/db/database_url
DB_PASSWORD_SECRET=<your-project>/db/password
TELEGRAM_TOKEN_SECRET=<your-project>/notifications/telegram_bot_token
TELEGRAM_CHAT_ID=<your-telegram-user-id>
```

---

## Running locally

```bash
# Install deps
pip install psycopg2-binary requests boto3 pytest

# Check the gap queue
python3 scripts/get-eligible-gaps.py --limit 5 --pretty

# Inspect repo knowledge
python3 scripts/get-repo-knowledge.py --repo BerriAI/litellm --wedge model_registry_staleness

# Dry-run the agent on a specific gap (no push, no PR)
python3 scripts/hermes-contribution-agent.py --gap-id 264 --dry-run

# Run the test suite
python3 -m pytest tests/ -v
```

---

## Test suite

```
tests/
├── test_knowledge_base.py           # 30 tests: file existence, structure, CLI
├── test_gap_scanner_exclusions.py   # 16 tests: exclusion loader, filter logic
├── test_get_eligible_gaps.py        #  9 tests: cap enforcement, output shape
├── test_record_result.py            # 10 tests: DB writes, status variants
├── test_hermes_contribution_agent.py # 17 tests: loop engineering contracts
└── test_acceptance_learning_knowledge.py # 8 tests: knowledge writeback
```

94 tests, 0 failures.

---

## Open PRs

| PR | Repo | Type | Status |
|---|---|---|---|
| [#35617](https://github.com/BerriAI/litellm/pull/35617) | litellm | fix(bedrock): strict tools for Sonnet 4.6/Haiku 4.5 | Open |
| [#35776](https://github.com/BerriAI/litellm/pull/35776) | litellm | feat(model_registry): Kimi-K2.5 | Open |
| [#36067](https://github.com/BerriAI/litellm/pull/36067) | litellm | feat(model_registry): DeepInfra gemma-4 | Open — CI green |
| [#36068](https://github.com/BerriAI/litellm/pull/36068) | litellm | feat(model_registry): groq/compound-beta-mini | Open — CI green |

---

## AI disclosure

All PRs submitted by this system include:
```
Co-authored-by: Hermes Agent <hermes-agent@nousresearch.com>
```
and a PR description note disclosing AI assistance. No exceptions — this is enforced by the quality gate.

---

## License

MIT
