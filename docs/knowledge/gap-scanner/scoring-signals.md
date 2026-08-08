# Gap Scanner: Scoring Signals

**Confidence:** medium | **Last updated:** 2026-08-05

## Score formula (0.0 – 1.0)

Base scores by wedge type:
- model_registry_staleness: 0.80 (most reliable, deterministic fix)
- missing_documentation: 0.75 (reliable, XS effort, consistent format)
- broken_integration: 0.65 (higher value but harder to fix correctly)

Modifiers:
- +0.05 if issue has "PR welcome" comment from maintainer
- +0.03 if reaction count > 10
- +0.03 if issue updated within 30 days
- -0.10 if effort = M
- -0.20 if effort = L
- +0.02 per merged PR from external contributor in same repo (max +0.10)

## Wedge type definitions
- model_registry_staleness: model exists on provider API but missing from repo's registry
- missing_documentation: public function/class has no docstring or < 10 char docstring
- broken_integration: issue reports a provider/tool integration that doesn't work correctly

## repo score (0.0 – 1.0) — used to prioritise which repo to target first
Current scores: vllm=0.82, litellm=0.80, llama_index=0.80, langchain=0.78, openai-python=0.80
These will be updated by the self-improvement agent as merge signal accumulates.

## When to update this file
- Weekly, after smart-gap-scorer.py runs and updates wedge_hypotheses table
- When first merges arrive — update base scores to reflect real acceptance data
