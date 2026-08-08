# Wedge Type: model_registry_staleness

**Confidence:** high | **Last updated:** 2026-08-05

## What it is
A model exists on a provider's live API but is missing from the repo's model registry.
The fix is to add the model entry in the correct JSON or Python file.

## litellm pattern (most common)
Two files MUST be updated in every PR:
1. `litellm/model_prices_and_context_window.json` — main registry
2. `litellm/model_prices_and_context_window_backup.json` — backup (same content)

Entry format:
```json
"deepinfra/provider/model-name": {
  "max_tokens": 131072,
  "max_input_tokens": 131072,
  "max_output_tokens": 131072,
  "input_cost_per_token": 0.0000008,
  "output_cost_per_token": 0.0000008,
  "litellm_provider": "deepinfra",
  "mode": "chat",
  "supports_function_calling": true,
  "supports_vision": false
}
```

Fetch current pricing from: https://api.deepinfra.com/v1/openai/models

## vllm pattern
vllm issues categorised as model_registry_staleness are usually feature requests
for architectural support (new model architectures, quantisation methods).
These are M/L effort — skip, mark deprioritised.

## What makes a good PR
- Entry matches an actually available model (verify via provider API)
- Pricing matches provider's published rates
- All required fields present (check adjacent entries in the file for the pattern)
- No extraneous fields added
