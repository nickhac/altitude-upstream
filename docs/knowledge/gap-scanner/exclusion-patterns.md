# Gap Scanner: Exclusion Patterns

**Confidence:** high | **Last updated:** 2026-08-05

## Issue title tokens to exclude (V2 scanner)
These indicate architectural discussions, RFCs, or tracking issues — not fixable gaps:
- [rfc], [tracking], [meta], [epic]
- roadmap, discussion, architecture
- rust rewrite, v2 plan, v3 plan, v4 plan
- dark mode, tracking issue

## Issue labels to exclude
- epic, rfc, design, wont-fix, wontfix, invalid, duplicate, question

## Issue body phrases to exclude (indicates umbrella/tracking issues)
- "tracking issue", "this issue tracks", "umbrella issue"

## Effort classification
- XS: docstring additions, single-line fixes, config key additions
- S: multi-line additions, new JSON entries, small function additions
- M: new provider integrations, multi-file features — DEPRIORITISED (agent cannot reliably complete)
- L: architectural changes — DEPRIORITISED

## Repo-specific exclusions
- langchain-ai/langchain: do NOT run V1 (model registry) scan — false positives
- langchain-ai/langchain: **SKIP in daily contribution run** — requires issue-first approval before PR; no automated path to obtain maintainer assignment exists yet
- ggerganov/llama.cpp: tier=2, skip entirely (C/C++)
- ollama/ollama: tier=2, skip entirely (Go)

## When to update this file
- When a batch of queued gaps turns out to be noise (feature requests masquerading as bugs)
- When a new label pattern emerges that indicates out-of-scope issues
- Update the Python constants in gap-scanner.py to match after updating this file
