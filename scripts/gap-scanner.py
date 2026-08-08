#!/usr/bin/env python3
"""
gap-scanner.py — altitude-upstream Vector 1 + Vector 2 + Vector 3

Vector 1: Compares live provider model lists against litellm's
          model_prices_and_context_window.json. Finds missing/stale entries.

Vector 2: Mines GitHub issues on target repos for well-scoped, high-pain bugs
          with no open PR.

Vector 3: Scans target repo files for public functions/classes with missing
          or minimal docstrings. Flags documentation gaps.

Writes scored gap rows to Postgres. Called by the daily/weekly cron loop.

Usage:
    python3 scripts/gap-scanner.py --vector 1         # model registry scan
    python3 scripts/gap-scanner.py --vector 2         # issue mining
    python3 scripts/gap-scanner.py --vector 3         # documentation gaps
    python3 scripts/gap-scanner.py --vector all       # all three (default)
    python3 scripts/gap-scanner.py --dry-run          # print gaps, don't write
"""

import sys
import json
import argparse
import subprocess
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import psycopg2
from pathlib import Path


# ---------------------------------------------------------------------------
# Knowledge-file exclusion loader
# ---------------------------------------------------------------------------

EXCLUSION_PATTERNS_FILE = Path(__file__).parent.parent / \
    'docs/knowledge/gap-scanner/exclusion-patterns.md'

_cached_exclusions = None  # cache after first load


def load_exclusion_patterns():
    """
    Load exclusion patterns from docs/knowledge/gap-scanner/exclusion-patterns.md
    at runtime.  Falls back to hardcoded defaults if the file is missing.

    Returns a tuple:
        (title_tokens: list[str], labels: set[str], body_phrases: list[str])
    """
    global _cached_exclusions
    if _cached_exclusions is not None:
        return _cached_exclusions

    # Hardcoded defaults — kept in sync with the markdown file
    default_title_tokens = [
        '[rfc]', '[tracking]', '[meta]', 'roadmap', 'discussion',
        'architecture', 'rust', 'rewrite', 'v2 plan', 'v3 plan', 'v4 plan',
        '[epic]', 'dark mode', 'tracking issue',
    ]
    default_labels = {
        'epic', 'rfc', 'design', 'wont-fix', 'wontfix', 'invalid',
        'duplicate', 'question',
    }
    default_body_phrases = ['tracking issue', 'this issue tracks', 'umbrella issue']

    if not EXCLUSION_PATTERNS_FILE.exists():
        print(f"  [gap-scanner] exclusion-patterns.md not found at "
              f"{EXCLUSION_PATTERNS_FILE}, using hardcoded defaults")
        _cached_exclusions = (default_title_tokens, default_labels, default_body_phrases)
        return _cached_exclusions

    try:
        text = EXCLUSION_PATTERNS_FILE.read_text(encoding='utf-8')
        title_tokens = list(default_title_tokens)  # start from defaults
        labels = set(default_labels)
        body_phrases = list(default_body_phrases)

        current_section = None
        for line in text.splitlines():
            line_stripped = line.strip()

            # Detect section headers
            if 'title tokens' in line_stripped.lower():
                current_section = 'title'
                continue
            elif 'labels to exclude' in line_stripped.lower():
                current_section = 'labels'
                continue
            elif 'body phrases' in line_stripped.lower():
                current_section = 'body'
                continue
            elif line_stripped.startswith('#'):
                current_section = None
                continue

            # Parse bullet list items
            if line_stripped.startswith('- ') and current_section:
                value = line_stripped[2:].strip()
                # Strip inline comments (everything after '—' or '#')
                for delim in (' — ', ' # '):
                    if delim in value:
                        value = value.split(delim)[0].strip()
                # Handle comma-separated entries on one line
                entries = [v.strip() for v in value.split(',') if v.strip()]
                for entry in entries:
                    entry_lower = entry.lower()
                    if current_section == 'title':
                        if entry_lower not in title_tokens:
                            title_tokens.append(entry_lower)
                    elif current_section == 'labels':
                        labels.add(entry_lower)
                    elif current_section == 'body':
                        # Strip surrounding quotes
                        entry_clean = entry.strip('"\'')
                        if entry_clean not in body_phrases:
                            body_phrases.append(entry_clean)

        print(f"  [gap-scanner] loaded exclusions from markdown: "
              f"{len(title_tokens)} title tokens, {len(labels)} labels, "
              f"{len(body_phrases)} body phrases")
        _cached_exclusions = (title_tokens, labels, body_phrases)

    except Exception as e:
        print(f"  [gap-scanner] error loading exclusion-patterns.md ({e}), "
              f"using hardcoded defaults")
        _cached_exclusions = (default_title_tokens, default_labels, default_body_phrases)

    return _cached_exclusions


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def get_conn():
    r1 = subprocess.run(
        ['aws', 'secretsmanager', 'get-secret-value',
         '--secret-id', os.environ['DB_URL_SECRET'],
         '--region', os.environ.get('AWS_REGION', 'us-east-1'), '--query', 'SecretString', '--output', 'text'],
        capture_output=True, text=True
    )
    r2 = subprocess.run(
        ['aws', 'secretsmanager', 'get-secret-value',
         '--secret-id', os.environ['DB_PASSWORD_SECRET'],
         '--region', os.environ.get('AWS_REGION', 'us-east-1'), '--query', 'SecretString', '--output', 'text'],
        capture_output=True, text=True
    )
    url = r1.stdout.strip()
    db_pass = r2.stdout.strip()
    p = urlparse(url)
    return psycopg2.connect(
        host=p.hostname, port=p.port or 5432, dbname=p.path.lstrip('/'),
        user=p.username, password=db_pass, sslmode='require'
    )


# ---------------------------------------------------------------------------
# Scoring formula
# ---------------------------------------------------------------------------

def score_gap(user_pain, maintainer_receptivity, merge_speed, narrative_fit, freshness):
    return (user_pain * 0.35
            + maintainer_receptivity * 0.25
            + merge_speed * 0.20
            + narrative_fit * 0.10
            + freshness * 0.10)


# ---------------------------------------------------------------------------
# Vector 1 — model registry staleness
# ---------------------------------------------------------------------------

BEDROCK_PREFIXES = (
    'anthropic.', 'au.anthropic.', 'eu.anthropic.', 'global.anthropic.',
    'jp.anthropic.', 'us.anthropic.', 'apac.anthropic.', 'bedrock/',
)

PROVIDER_DOCS = {
    # provider_key: (litellm_prefix, docs_url, api_endpoint_or_None)
    'groq':      ('groq/',       'https://console.groq.com/docs/models', None),
    'deepinfra': ('deepinfra/',  'https://api.deepinfra.com/v1/openai/models', 'https://api.deepinfra.com/v1/openai/models'),
    'cerebras':  ('cerebras/',   'https://inference-docs.cerebras.ai/introduction', None),
    'together':  ('together_ai/', 'https://docs.together.ai/docs/serverless-models', None),
    'fireworks': ('fireworks_ai/', 'https://fireworks.ai/models', None),
    'mistral':   ('mistral/',    'https://docs.mistral.ai/getting-started/models/', None),
}


def fetch_litellm_prices():
    """Fetch litellm's model_prices_and_context_window.json from GitHub."""
    r = subprocess.run(
        ['curl', '-sL',
         'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json'],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(r.stdout)


def fetch_deepinfra_models():
    """DeepInfra has a public unauthenticated model list."""
    r = subprocess.run(
        ['curl', '-s', '--max-time', '15',
         'https://api.deepinfra.com/v1/openai/models'],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
        return [m['id'] for m in data.get('data', [])]
    except Exception:
        return []


def fetch_groq_models_from_sdk():
    """Parse Groq model list from their Python SDK on GitHub."""
    r = subprocess.run(
        ['gh', 'api',
         'repos/groq/groq-python/contents/src/groq/types/chat/completion_create_params.py'],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return []
    import base64
    data = json.loads(r.stdout)
    content = base64.b64decode(data['content']).decode('utf-8', errors='replace')
    # Extract model literals from the Union type
    models = re.findall(r'"((?:llama|gemma|mixtral|whisper|qwen|kimi|compound|guard)[^"]+)"', content)
    return list(set(models))


def fetch_vllm_supported_models():
    """Fetch vllm's supported_models.md and extract HuggingFace model IDs."""
    r = subprocess.run(
        ['curl', '-sL', '--max-time', '20',
         'https://raw.githubusercontent.com/vllm-project/vllm/main/docs/models/supported_models.md'],
        capture_output=True, text=True
    )
    if r.returncode != 0 or not r.stdout.strip():
        return set(), {}

    model_ids = set()
    # arch_map: model_id -> architecture string (first column of same table row)
    arch_map = {}

    # Parse markdown tables — look for rows with backtick-wrapped org/model entries
    # Table rows look like: | LlamaForCausalLM | ... | `meta-llama/Llama-3.1-8B` |
    current_arch = None
    for line in r.stdout.splitlines():
        # Detect architecture cell at start of row (first column)
        # Rows start with | Architecture | ...
        cells = [c.strip() for c in line.split('|')]
        if len(cells) < 3:
            continue
        # First non-empty cell may be architecture name
        first = cells[1] if len(cells) > 1 else ''
        if first and not first.startswith('-') and not first.lower().startswith('arch'):
            # Check if it looks like an architecture name (CamelCase or has "For")
            if re.match(r'^[A-Z][a-zA-Z0-9]+', first):
                current_arch = first

        # Extract all `org/model` backtick patterns from entire row
        hits = re.findall(r'`([A-Za-z0-9_.-]+/[A-Za-z0-9_.:/-]+)`', line)
        for h in hits:
            # Filter out obvious non-model entries (paths, version strings)
            if '/' in h and not h.startswith('http') and '.' not in h.split('/')[0]:
                model_ids.add(h)
                if current_arch:
                    arch_map[h] = current_arch

    return model_ids, arch_map


def fetch_vllm_actual_models():
    """Fetch trending HuggingFace models. Falls back to known popular models if API unavailable."""
    results = []
    seen = set()
    for tag in ('text-generation', 'image-text-to-text'):
        r = subprocess.run(
            ['curl', '-sL', '--max-time', '20',
             f'https://huggingface.co/api/models?sort=likes&limit=50&pipeline_tag={tag}'],
            capture_output=True, text=True
        )
        if r.returncode != 0 or not r.stdout.strip():
            continue
        try:
            models = json.loads(r.stdout)
        except Exception:
            continue
        if not isinstance(models, list):
            print(f"  vllm: HuggingFace API error for {tag}: {r.stdout[:100]}")
            continue
        for m in models:
            mid = m.get('id', '') or m.get('modelId', '')
            if mid and mid not in seen:
                seen.add(mid)
                results.append({
                    'id': mid,
                    'downloads': m.get('downloads', 0) or 0,
                    'likes': m.get('likes', 0) or 0,
                })

    # Fallback: well-known popular models if HF API is rate-limited
    if not results:
        print("  vllm: HuggingFace API unavailable, using fallback model list")
        fallback = [
            ('Qwen/Qwen3-235B-A22B', 8000, 2000000),
            ('Qwen/Qwen3-30B-A3B', 5000, 1500000),
            ('deepseek-ai/DeepSeek-V3-0324', 12000, 3000000),
            ('deepseek-ai/DeepSeek-R2', 9000, 2500000),
            ('meta-llama/Llama-3.3-70B-Instruct', 15000, 5000000),
            ('meta-llama/Llama-4-Scout-17B-16E-Instruct', 6000, 1000000),
            ('google/gemma-3-27b-it', 4000, 800000),
            ('mistralai/Mistral-Small-3.2-24B-Instruct-2506', 3000, 600000),
        ]
        for mid, likes, downloads in fallback:
            if mid not in seen:
                seen.add(mid)
                results.append({'id': mid, 'downloads': downloads, 'likes': likes})

    return results


def vector1_vllm_scan():
    """Find popular HuggingFace models whose arch is supported by vllm but aren't listed."""
    print("Vector 1: scanning vllm supported_models.md vs HuggingFace trending...")
    supported_ids, arch_map = fetch_vllm_supported_models()
    if not supported_ids:
        print("  vllm: could not fetch supported_models.md, skipping")
        return []

    hf_models = fetch_vllm_actual_models()
    if not hf_models:
        print("  vllm: could not fetch HuggingFace trending models, skipping")
        return []

    # Build set of known architecture families from supported models
    known_archs = set(arch_map.values())

    # Normalise supported IDs to lowercase for comparison
    supported_lower = {m.lower() for m in supported_ids}

    gaps_found = []
    for model in hf_models:
        mid = model['id']
        likes = model['likes']
        downloads = model['downloads']

        # Must be popular
        if likes < 1000 and downloads < 100_000:
            continue

        # Must NOT already be in vllm's list
        if mid.lower() in supported_lower:
            continue

        # Try to find a matching architecture from the org or model name tokens
        # e.g. "meta-llama/Llama-3.3-70B" -> tokens ['meta', 'llama', 'Llama', '3', '3', '70B']
        mid_tokens_lower = set(re.split(r'[^a-zA-Z0-9]+', mid.lower()))
        matched_arch = None
        for arch in known_archs:
            arch_lower = arch.lower()
            # Simple heuristic: arch name tokens appear in model id
            arch_tokens = set(re.split(r'[^a-zA-Z0-9]+', arch_lower))
            # Remove short noise tokens
            arch_tokens = {t for t in arch_tokens if len(t) > 3}
            if arch_tokens and arch_tokens.issubset(mid_tokens_lower):
                matched_arch = arch
                break

        if not matched_arch:
            continue

        # Score: normalise likes/downloads to 0-1
        likes_score = min(1.0, likes / 50_000)
        dl_score = min(1.0, downloads / 5_000_000)
        freshness = max(likes_score, dl_score)

        gap = {
            'wedge_type': 'model_registry_staleness',
            'description': (
                f'vllm does not list {mid} in supported_models.md — '
                f'architecture {matched_arch} is supported but model is missing'
            ),
            'effort': 'S',
            'provider': 'vllm',
            'repo': 'vllm-project/vllm',
            'source_url': f'https://huggingface.co/{mid}',
            'contribution_level': 1,
            'user_pain': 0.70,
            'maintainer_receptivity': 0.85,
            'merge_speed': 0.88,
            'narrative_fit': 0.80,
            'freshness': freshness,
            'model_id': mid,
        }
        gap['score'] = score_gap(
            gap['user_pain'], gap['maintainer_receptivity'],
            gap['merge_speed'], gap['narrative_fit'], gap['freshness']
        )
        gaps_found.append(gap)

    gaps_found.sort(key=lambda x: -x['score'])
    print(f"  vllm: {len(gaps_found)} missing popular models found, queuing top {min(10, len(gaps_found))}")
    return gaps_found[:10]


def fetch_langchain_openai_models():
    """Extract model name strings from langchain-openai's chat_models/base.py."""
    r = subprocess.run(
        ['curl', '-sL', '--max-time', '20',
         'https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/partners/openai/langchain_openai/chat_models/base.py'],
        capture_output=True, text=True
    )
    if r.returncode != 0 or not r.stdout.strip():
        return set()
    # Extract quoted strings matching gpt-*, o1-*, o3-*, o4-*
    hits = re.findall(r'["\']((gpt|o1|o3|o4)-[^"\']+)["\']', r.stdout)
    return {h[0] for h in hits}


def fetch_openai_sdk_models():
    """Extract model names from openai-python's chat_model.py Literal type."""
    r = subprocess.run(
        ['curl', '-sL', '--max-time', '20',
         'https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/shared/chat_model.py'],
        capture_output=True, text=True
    )
    if r.returncode != 0 or not r.stdout.strip():
        return set()
    # All quoted strings in a Literal type definition
    hits = re.findall(r'["\']([^"\']+)["\']', r.stdout)
    # Keep only model-like strings (contain - and aren't imports/comments)
    return {h for h in hits if '-' in h and not h.startswith('#')}


def vector1_langchain_scan():
    """Find OpenAI models present in openai-python SDK but missing from langchain-openai."""
    print("Vector 1: scanning langchain-openai vs openai-python SDK models...")
    langchain_models = fetch_langchain_openai_models()
    sdk_models = fetch_openai_sdk_models()

    if not sdk_models:
        print("  langchain: could not fetch openai-python chat_model.py, skipping")
        return []

    langchain_lower = {m.lower() for m in langchain_models}
    gaps_found = []

    for model_id in sdk_models:
        if model_id.lower() in langchain_lower:
            continue
        # Only flag models that look like chat models (gpt-*, o1-*, o3-*, o4-*)
        if not re.match(r'^(gpt|o1|o3|o4)-', model_id):
            continue

        # Freshness heuristic: newer series score higher
        freshness = 1.0 if re.match(r'^(o3|o4)-', model_id) else (
            0.85 if re.match(r'^o1-', model_id) else 0.65
        )
        gap = {
            'wedge_type': 'model_registry_staleness',
            'description': (
                f'langchain-openai does not list {model_id} — '
                f'present in openai-python SDK but missing from langchain_openai chat_models'
            ),
            'effort': 'S',
            'provider': 'langchain',
            'repo': 'langchain-ai/langchain',
            'source_url': 'https://github.com/openai/openai-python/blob/main/src/openai/types/chat_model.py',
            'contribution_level': 1,
            'user_pain': 0.72,
            'maintainer_receptivity': 0.88,
            'merge_speed': 0.90,
            'narrative_fit': 0.80,
            'freshness': freshness,
            'model_id': model_id,
        }
        gap['score'] = score_gap(
            gap['user_pain'], gap['maintainer_receptivity'],
            gap['merge_speed'], gap['narrative_fit'], gap['freshness']
        )
        gaps_found.append(gap)

    gaps_found.sort(key=lambda x: -x['score'])
    print(f"  langchain: {len(gaps_found)} models in openai SDK missing from langchain-openai")
    return gaps_found


def fetch_llamacpp_supported_archs() -> set:
    """Parse llama.cpp README.md for its supported model/architecture names."""
    r = subprocess.run(
        ['curl', '-sL', '--max-time', '30',
         'https://raw.githubusercontent.com/ggerganov/llama.cpp/master/README.md'],
        capture_output=True, text=True
    )
    if r.returncode != 0 or not r.stdout.strip():
        return set()

    archs: set[str] = set()
    # Look for lines in the supported-models table region — rows with | and model names
    # Also collect architecture tokens from lines that look like table rows
    in_table = False
    for line in r.stdout.splitlines():
        stripped = line.strip()
        # Table rows start/end with |
        if stripped.startswith('|') and stripped.endswith('|'):
            in_table = True
            cells = [c.strip() for c in stripped.split('|')]
            for cell in cells:
                # Skip header/divider rows
                if not cell or cell.startswith('-') or cell.lower() in ('model', 'status', 'type', 'notes', ''):
                    continue
                # Tokenise cell into word parts and collect alpha tokens ≥ 4 chars
                tokens = re.findall(r'[A-Za-z][A-Za-z0-9]{2,}', cell)
                for tok in tokens:
                    if len(tok) >= 3:
                        archs.add(tok.lower())
        elif in_table and not stripped.startswith('|'):
            # Left the table section — keep scanning (multiple tables in README)
            in_table = False

    return archs


def fetch_hf_gguf_models() -> list[dict]:
    """Fetch top GGUF models by likes from HuggingFace API."""
    r = subprocess.run(
        ['curl', '-sL', '--max-time', '30',
         'https://huggingface.co/api/models?search=GGUF&sort=likes&limit=30'],
        capture_output=True, text=True
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        data = json.loads(r.stdout)
        if not isinstance(data, list):
            return []
        return [
            {
                'id': m.get('id', '') or m.get('modelId', ''),
                'likes': m.get('likes', 0) or 0,
            }
            for m in data
            if (m.get('id') or m.get('modelId'))
        ]
    except Exception:
        return []


def vector1_llamacpp_scan() -> list[dict]:
    """Find GGUF models on HuggingFace whose architecture is NOT in llama.cpp's supported list."""
    print("Vector 1: scanning llama.cpp supported archs vs HuggingFace GGUF models...")
    supported_archs = fetch_llamacpp_supported_archs()
    if not supported_archs:
        print("  llama.cpp: could not parse README, skipping")
        return []

    hf_models = fetch_hf_gguf_models()
    if not hf_models:
        print("  llama.cpp: could not fetch HuggingFace GGUF models, skipping")
        return []

    gaps_found = []
    seen_archs: set[str] = set()

    for model in hf_models:
        mid = model['id']
        likes = model['likes']

        if likes < 500:
            continue

        # Extract the probable base architecture from the model id (org/ModelName)
        # e.g. "bartowski/Qwen2.5-72B-Instruct-GGUF" -> arch tokens from "Qwen2.5-72B-Instruct"
        parts = mid.split('/')
        model_name = parts[-1] if len(parts) > 1 else mid
        name_tokens_lower = set(re.findall(r'[A-Za-z][A-Za-z0-9]*', model_name.lower()))

        # Guess the architecture family — the longest token NOT in a stop-list
        STOP_TOKENS = {
            'gguf', 'instruct', 'chat', 'it', 'base', 'merged',
            'quantized', 'unsloth', 'the', 'model', 'ggml',
        }
        arch_candidates = [
            t for t in name_tokens_lower
            if t not in STOP_TOKENS and len(t) >= 3
        ]
        if not arch_candidates:
            continue

        # Pick the most likely arch: shortest token that looks like a model family name
        arch_candidates.sort(key=lambda t: (len(t), t))
        arch = arch_candidates[0]

        # Skip if this architecture IS already in llama.cpp's supported list
        if arch in supported_archs:
            continue

        # Deduplicate per architecture (one gap per missing arch)
        if arch in seen_archs:
            continue
        seen_archs.add(arch)

        freshness = min(1.0, likes / 5000)

        gap = {
            'wedge_type': 'model_registry_staleness',
            'description': (
                f'llama.cpp does not support {arch} architecture — '
                f'{mid} has {likes} likes on HuggingFace'
            ),
            'effort': 'M',
            'provider': 'ggerganov',
            'repo': 'ggerganov/llama.cpp',
            'source_url': f'https://huggingface.co/{mid}',
            'contribution_level': 2,
            'user_pain': 0.75,
            'maintainer_receptivity': 0.72,
            'merge_speed': 0.65,
            'narrative_fit': 0.80,
            'freshness': freshness,
        }
        gap['score'] = score_gap(
            gap['user_pain'], gap['maintainer_receptivity'],
            gap['merge_speed'], gap['narrative_fit'], gap['freshness']
        )
        gaps_found.append(gap)

    gaps_found.sort(key=lambda x: -x['score'])
    print(f"  llama.cpp: {len(gaps_found)} unsupported architectures found in popular GGUF models")
    return gaps_found[:10]


def vector1_scan(litellm_prices, dry_run=False):
    """Find missing model entries across providers."""
    gaps_found = []
    litellm_keys_lower = {k.lower() for k in litellm_prices.keys()}

    # DeepInfra — fully automated (public API)
    print("Vector 1: scanning DeepInfra...")
    deepinfra_models = fetch_deepinfra_models()
    missing_deepinfra = []
    for model_id in deepinfra_models:
        litellm_key = f'deepinfra/{model_id}'
        if litellm_key.lower() not in litellm_keys_lower:
            missing_deepinfra.append(model_id)

    if missing_deepinfra:
        # Score and rank — prioritise newer, higher-profile models
        for model_id in missing_deepinfra[:10]:  # cap at 10 per scan
            # Freshness heuristic: newer model families score higher
            freshness = 1.0 if any(x in model_id.lower() for x in
                                    ['v4', 'v3.5', 'v3.6', 'v3.7', 'gemma-4', 'kimi-k2']) else 0.6
            gap = {
                'wedge_type': 'model_registry_staleness',
                'description': f'Model deepinfra/{model_id} exists on DeepInfra but is missing from litellm model_prices_and_context_window.json',
                'effort': 'S',
                'provider': 'deepinfra',
                'source_url': 'https://api.deepinfra.com/v1/openai/models',
                'contribution_level': 1,
                'user_pain': 0.70,
                'maintainer_receptivity': 0.90,
                'merge_speed': 0.90,
                'narrative_fit': 0.75,
                'freshness': freshness,
                'model_id': model_id,
            }
            gap['score'] = score_gap(
                gap['user_pain'], gap['maintainer_receptivity'],
                gap['merge_speed'], gap['narrative_fit'], gap['freshness']
            )
            gaps_found.append(gap)
        print(f"  DeepInfra: {len(missing_deepinfra)} missing models, queuing top {min(10, len(missing_deepinfra))}")

    # Groq — from SDK
    print("Vector 1: scanning Groq (SDK)...")
    groq_models = fetch_groq_models_from_sdk()
    for model_id in groq_models:
        litellm_key = f'groq/{model_id}'
        if litellm_key.lower() not in litellm_keys_lower:
            gap = {
                'wedge_type': 'model_registry_staleness',
                'description': f'Model groq/{model_id} listed in Groq SDK but missing from litellm pricing file',
                'effort': 'S',
                'provider': 'groq',
                'source_url': 'https://github.com/groq/groq-python',
                'contribution_level': 1,
                'user_pain': 0.70,
                'maintainer_receptivity': 0.90,
                'merge_speed': 0.90,
                'narrative_fit': 0.75,
                'freshness': 0.8,
                'model_id': model_id,
            }
            gap['score'] = score_gap(
                gap['user_pain'], gap['maintainer_receptivity'],
                gap['merge_speed'], gap['narrative_fit'], gap['freshness']
            )
            gaps_found.append(gap)
    if groq_models:
        groq_missing = [m for m in groq_models if f'groq/{m}'.lower() not in litellm_keys_lower]
        print(f"  Groq: {len(groq_missing)} missing models")

    # vllm — HuggingFace trending vs supported_models.md
    gaps_found.extend(vector1_vllm_scan())

    # langchain-openai — openai-python SDK vs langchain chat_models
    gaps_found.extend(vector1_langchain_scan())

    return gaps_found


# ---------------------------------------------------------------------------
# Vector 2 — issue pain mining
# ---------------------------------------------------------------------------

ISSUE_CLASSIFIERS = {
    'model': ('model_registry_staleness', 1),
    'price': ('pricing_staleness', 1),
    'context': ('stale_context_window', 1),
    'window': ('stale_context_window', 1),
    'strict': ('broken_integration', 2),
    'error': ('broken_integration', 2),
    'fail': ('broken_integration', 2),
    'broken': ('broken_integration', 2),
    'missing': ('missing_capability', 3),
    'support': ('missing_capability', 3),
}


def vector2_scan(dry_run=False):
    """Mine GitHub issues for high-pain, unaddressed bugs."""
    gaps_found = []

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT full_name FROM repos WHERE tier = 1 ORDER BY score DESC")
    target_repos = [r[0] for r in cur.fetchall()]

    # Also check existing open PRs to avoid duplication
    cur.execute("SELECT source_url FROM gaps WHERE status IN ('open', 'in_progress')")
    existing_issue_urls = {r[0] for r in cur.fetchall() if r[0]}
    conn.close()

    # ---------------------------------------------------------------------------
    # Exclusion patterns — loaded from markdown at runtime
    # ---------------------------------------------------------------------------
    EXCLUDE_TITLE_TOKENS, EXCLUDE_LABELS, EXCLUDE_BODY_PHRASES = load_exclusion_patterns()

    now = datetime.now(timezone.utc)

    for repo in target_repos:
        print(f"Vector 2: mining issues in {repo}...")
        r = subprocess.run(
            ['gh', 'api',
             f'repos/{repo}/issues?state=open&sort=reactions&direction=desc&per_page=30',
             '--jq', '[.[] | select(.pull_request == null) | '
                     '{number:.number, title:.title, '
                     'reactions:.reactions.total_count, comments:.comments, '
                     'labels:[.labels[].name], body:.body, '
                     'created_at:.created_at, updated_at:.updated_at}]'],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            print(f"  API error for {repo}: {r.stderr[:100]}")
            continue

        try:
            issues = json.loads(r.stdout)
        except Exception:
            continue

        for issue in issues:
            issue_url = f'https://github.com/{repo}/issues/{issue["number"]}'
            if issue_url in existing_issue_urls:
                continue

            title = issue['title']
            title_lower = title.lower()
            body = issue.get('body') or ''
            body_lower = body.lower()
            labels = issue.get('labels', [])
            reactions = issue['reactions']

            # ------------------------------------------------------------------
            # EXCLUSION FILTERS
            # ------------------------------------------------------------------

            # 1. Title tokens
            if any(tok in title_lower for tok in EXCLUDE_TITLE_TOKENS):
                continue

            # 2. Excluded labels
            if any(lbl.lower() in EXCLUDE_LABELS for lbl in labels):
                continue

            # 3. Body phrases
            if any(phrase in body_lower for phrase in EXCLUDE_BODY_PHRASES):
                continue

            # 4. Minimum reactions threshold
            if reactions < 5:
                continue

            # 5. Stale: older than 180 days with no comments in last 60 days
            try:
                created_at = datetime.fromisoformat(issue['created_at'].replace('Z', '+00:00'))
                updated_at = datetime.fromisoformat(issue['updated_at'].replace('Z', '+00:00'))
                age_days = (now - created_at).days
                days_since_update = (now - updated_at).days
                if age_days > 180 and days_since_update > 60:
                    continue
            except Exception:
                pass

            # 6. Feature requests without a code snippet are not actionable
            has_code_block = '```' in body
            if '[feature]' in title_lower and not has_code_block:
                continue

            # ------------------------------------------------------------------
            # Check for existing open PR via timeline cross-references
            # ------------------------------------------------------------------
            pr_check = subprocess.run(
                ['gh', 'api',
                 f'repos/{repo}/issues/{issue["number"]}/timeline',
                 '--jq',
                 '[.[] | select(.event=="cross-referenced") '
                 '| .source.issue.pull_request.url] | length'],
                capture_output=True, text=True
            )
            try:
                if pr_check.returncode == 0 and int(pr_check.stdout.strip()) > 0:
                    continue  # Someone already has a PR open for this issue
            except (ValueError, TypeError):
                pass

            # ------------------------------------------------------------------
            # Classify
            # ------------------------------------------------------------------
            wedge_type = 'broken_integration'
            contribution_level = 2
            for keyword, (wtype, level) in ISSUE_CLASSIFIERS.items():
                if keyword in title_lower:
                    wedge_type = wtype
                    contribution_level = level
                    break

            # ------------------------------------------------------------------
            # Scoring — base values
            # ------------------------------------------------------------------
            is_bug = 'bug' in labels
            has_maintainer_label = any(
                lbl in labels for lbl in ['help wanted', 'good first issue', 'confirmed']
            )

            user_pain = min(1.0, 0.4 + (reactions / 30) * 0.6)
            maintainer_receptivity = 0.7 if is_bug else 0.5
            merge_speed = 0.8 if contribution_level == 1 else (0.6 if contribution_level == 2 else 0.4)

            # ------------------------------------------------------------------
            # INCLUSION BOOSTS
            # ------------------------------------------------------------------

            # Boost 1: good first issue / help wanted labels
            if any(lbl in labels for lbl in ['good first issue', 'help wanted']):
                user_pain = min(1.0, user_pain + 0.15)
                maintainer_receptivity = min(1.0, maintainer_receptivity + 0.2)

            # Boost 2: bug with stack trace / error message in body
            if is_bug:
                has_stacktrace = any(
                    marker in body_lower
                    for marker in ['traceback', 'stack trace', 'exception:', 'error:', 'at line']
                )
                if has_stacktrace:
                    user_pain = min(1.0, user_pain + 0.1)

            # Boost 3: maintainer comment inviting PR
            # Fetch issue comments to check for maintainer invitation
            comments_r = subprocess.run(
                ['gh', 'api',
                 f'repos/{repo}/issues/{issue["number"]}/comments',
                 '--jq',
                 '[.[] | select((.body | ascii_downcase | '
                 'contains("pr welcome")) or '
                 '(.body | ascii_downcase | contains("happy to review")))'
                 '] | length'],
                capture_output=True, text=True
            )
            try:
                if comments_r.returncode == 0 and int(comments_r.stdout.strip()) > 0:
                    maintainer_receptivity = min(1.0, maintainer_receptivity + 0.25)
            except (ValueError, TypeError):
                pass

            # Boost 4: clear reproduction in body
            has_reproduction = (
                has_code_block
                or any(phrase in body_lower for phrase in [
                    'reproduce', 'steps to reproduce', 'minimal example',
                ])
            )
            if has_reproduction:
                merge_speed = min(1.0, merge_speed + 0.1)

            # Existing label-based boost (kept from original)
            if has_maintainer_label:
                maintainer_receptivity = min(1.0, maintainer_receptivity + 0.2)

            score = score_gap(user_pain, maintainer_receptivity, merge_speed, 0.85, 0.7)

            # Only queue if score above threshold
            if score >= 0.62:
                gap = {
                    'wedge_type': wedge_type,
                    'description': f'GitHub issue #{issue["number"]}: {issue["title"][:120]}',
                    'effort': 'S' if contribution_level == 1 else ('M' if contribution_level == 2 else 'L'),
                    'provider': repo.split('/')[0].lower(),
                    'source_url': issue_url,
                    'contribution_level': contribution_level,
                    'user_pain': round(user_pain, 2),
                    'maintainer_receptivity': round(maintainer_receptivity, 2),
                    'merge_speed': round(merge_speed, 2),
                    'narrative_fit': 0.85,
                    'freshness': 0.7,
                    'score': round(score, 3),
                    'issue_number': issue['number'],
                    'repo': repo,
                }
                gaps_found.append(gap)

        print(f"  {repo}: {len([g for g in gaps_found if g.get('repo') == repo])} gaps found")

    return sorted(gaps_found, key=lambda x: -x['score'])


# ---------------------------------------------------------------------------
# Vector 3 — documentation gap scanner
# ---------------------------------------------------------------------------

# Files to scan: (repo, filepath, raw_url, branch)
DOC_SCAN_TARGETS = [
    (
        'BerriAI/litellm',
        'litellm/main.py',
        'https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/main.py',
        'main',
        'python',
    ),
    (
        'vllm-project/vllm',
        'vllm/sampling_params.py',
        'https://raw.githubusercontent.com/vllm-project/vllm/main/vllm/sampling_params.py',
        'main',
        'python',
    ),
    (
        'langchain-ai/langchain',
        'libs/partners/openai/langchain_openai/chat_models/base.py',
        'https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/partners/openai/langchain_openai/chat_models/base.py',
        'master',
        'python',
    ),
    (
        'openai/openai-python',
        'src/openai/resources/chat/completions/completions.py',
        'https://raw.githubusercontent.com/openai/openai-python/main/src/openai/resources/chat/completions/completions.py',
        'main',
        'python',
    ),
    (
        'run-llama/llama_index',
        'llama-index-core/llama_index/core/async_utils.py',
        'https://raw.githubusercontent.com/run-llama/llama_index/main/llama-index-core/llama_index/core/async_utils.py',
        'main',
        'python',
    ),
]

# Caps
_V3_GAPS_PER_FILE = 5
_V3_GAPS_PER_REPO = 10
# Minimum docstring length to be considered "useful"
_MIN_DOCSTRING_CHARS = 30


def _fetch_raw(url: str) -> str | None:
    """Fetch raw text from a URL using curl; return None on failure."""
    r = subprocess.run(
        ['curl', '-sL', '--max-time', '30', url],
        capture_output=True, text=True
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout


def _score_doc_gap(func_name: str, signature: str, filepath: str) -> float:
    """
    Compute a documentation-gap score in [0, 1].

    Boosts:
    - Function has parameters (signature contains '(...)' with content)
    - Signature has a return type hint (' -> ')
    - File is a high-importance entry point (main.py)
    """
    base = 0.60
    # Has non-trivial parameters?
    params_match = re.search(r'\(([^)]+)\)', signature)
    if params_match and params_match.group(1).strip() not in ('', 'self', 'cls'):
        base += 0.10
    # Has return type annotation?
    if ' -> ' in signature:
        base += 0.10
    # High-importance file?
    if filepath.endswith('main.py'):
        base += 0.10
    return min(1.0, base)


def _parse_doc_gaps(source: str, repo: str, filepath: str, branch: str) -> list[dict]:
    """
    Parse Python source for public functions/classes missing adequate docstrings.

    Returns a list of gap dicts (unscored — caller must add 'score').
    """
    lines = source.splitlines()
    gaps: list[dict] = []
    file_gaps = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        # Match public def or class (not starting with _)
        m = re.match(r'^(\s*)(def|class)\s+([A-Za-z][A-Za-z0-9_]*)\s*', line)
        if m and not m.group(3).startswith('_'):
            func_name = m.group(3)
            definition_kind = m.group(2)
            line_number = i + 1  # 1-indexed

            # Collect the full signature (may span multiple lines until ':')
            sig_lines = [line.rstrip()]
            j = i + 1
            while j < len(lines) and ':' not in sig_lines[-1]:
                sig_lines.append(lines[j].rstrip())
                j += 1
            signature = ' '.join(sig_lines)

            # Find the next non-empty line after the signature
            k = j  # first line after the colon line
            while k < len(lines) and not lines[k].strip():
                k += 1

            # Check for docstring
            has_good_docstring = False
            if k < len(lines):
                next_stripped = lines[k].strip()
                if next_stripped.startswith('"""') or next_stripped.startswith("'''"):
                    # Count total docstring content length
                    quote = '"""' if next_stripped.startswith('"""') else "'''"
                    # Gather docstring body
                    doc_content = next_stripped[3:]  # strip opening triple-quote
                    end_k = k
                    if quote not in doc_content:
                        # Multi-line docstring
                        end_k = k + 1
                        while end_k < len(lines) and quote not in lines[end_k]:
                            doc_content += ' ' + lines[end_k].strip()
                            end_k += 1
                        if end_k < len(lines):
                            doc_content += ' ' + lines[end_k].split(quote)[0].strip()
                    else:
                        doc_content = doc_content.split(quote)[0]
                    if len(doc_content.strip()) >= _MIN_DOCSTRING_CHARS:
                        has_good_docstring = True

            if not has_good_docstring:
                gap_score = _score_doc_gap(func_name, signature, filepath)
                source_url = (
                    f'https://github.com/{repo}/blob/{branch}/{filepath}#L{line_number}'
                )
                gap = {
                    'wedge_type': 'missing_documentation',
                    'description': (
                        f'{definition_kind.capitalize()} `{func_name}` in {filepath} '
                        f'has no docstring — add parameter docs, return type, and example'
                    ),
                    'effort': 'XS',
                    'provider': repo.split('/')[0].lower(),
                    'source_url': source_url,
                    'contribution_level': 1,
                    'user_pain': 0.45,
                    'maintainer_receptivity': 0.80,
                    'merge_speed': 0.85,
                    'narrative_fit': 0.70,
                    'freshness': 0.90,
                    'repo': repo,
                    'score': round(gap_score, 3),
                }
                gaps.append(gap)
                file_gaps += 1
                if file_gaps >= _V3_GAPS_PER_FILE:
                    break

        i += 1

    return gaps


def _parse_doc_gaps_c(source: str, repo: str, filepath: str, branch: str) -> list[dict]:
    """
    Parse C/C++ header for LLAMA_API functions missing a preceding // or /* comment.
    Returns a list of gap dicts (unscored — caller must add 'score').
    """
    lines = source.splitlines()
    gaps: list[dict] = []
    file_gaps = 0

    for i, line in enumerate(lines):
        # Match LLAMA_API function declarations (may span multiple lines but first token is key)
        if not re.search(r'\bLLAMA_API\b', line):
            continue
        # Skip macros and typedefs
        if re.search(r'^\s*#', line) or 'typedef' in line:
            continue
        # Look for a function name in the declaration
        func_m = re.search(r'\bLLAMA_API\b[^;{(]*?\b([a-z_][a-z0-9_]*)\s*\(', line)
        if not func_m:
            continue

        func_name = func_m.group(1)
        line_number = i + 1

        # Check for a preceding comment on the line immediately before (skip blank lines)
        has_comment = False
        for prev_i in range(i - 1, max(i - 4, -1), -1):
            prev = lines[prev_i].strip()
            if not prev:
                continue
            if prev.startswith('//') or prev.startswith('/*') or prev.startswith('*'):
                has_comment = True
            break

        if not has_comment:
            source_url = f'https://github.com/{repo}/blob/{branch}/{filepath}#L{line_number}'
            gap = {
                'wedge_type': 'missing_documentation',
                'description': (
                    f'C API function `{func_name}` in {filepath} '
                    f'has no doxygen comment — add @brief, @param, @return'
                ),
                'effort': 'XS',
                'provider': repo.split('/')[0].lower(),
                'source_url': source_url,
                'contribution_level': 1,
                'user_pain': 0.50,
                'maintainer_receptivity': 0.78,
                'merge_speed': 0.82,
                'narrative_fit': 0.70,
                'freshness': 0.85,
                'repo': repo,
                'score': 0.0,
            }
            gap['score'] = round(_score_doc_gap(func_name, line, filepath), 3)
            gaps.append(gap)
            file_gaps += 1
            if file_gaps >= _V3_GAPS_PER_FILE:
                break

    return gaps


def _parse_doc_gaps_go(source: str, repo: str, filepath: str, branch: str) -> list[dict]:
    """
    Parse Go source for public Client methods and New* functions missing a preceding // comment.
    Returns a list of gap dicts (unscored — caller must add 'score').
    """
    lines = source.splitlines()
    gaps: list[dict] = []
    file_gaps = 0

    for i, line in enumerate(lines):
        # Match `func (c *Client) MethodName(` or `func NewSomething(`
        if not re.match(r'\s*func\s+(?:\([^)]+\)\s+)?(?:New|[A-Z])', line):
            continue
        func_m = re.search(r'\bfunc\s+(?:\([^)]+\)\s+)?([A-Z][A-Za-z0-9_]*)\s*\(', line)
        if not func_m:
            continue

        func_name = func_m.group(1)
        line_number = i + 1

        # Check for a preceding // comment
        has_comment = False
        for prev_i in range(i - 1, max(i - 4, -1), -1):
            prev = lines[prev_i].strip()
            if not prev:
                continue
            if prev.startswith('//'):
                has_comment = True
            break

        if not has_comment:
            source_url = f'https://github.com/{repo}/blob/{branch}/{filepath}#L{line_number}'
            gap = {
                'wedge_type': 'missing_documentation',
                'description': (
                    f'Go function `{func_name}` in {filepath} '
                    f'has no doc comment — add a // {func_name} ... comment'
                ),
                'effort': 'XS',
                'provider': repo.split('/')[0].lower(),
                'source_url': source_url,
                'contribution_level': 1,
                'user_pain': 0.48,
                'maintainer_receptivity': 0.76,
                'merge_speed': 0.82,
                'narrative_fit': 0.68,
                'freshness': 0.85,
                'repo': repo,
                'score': 0.0,
            }
            gap['score'] = round(_score_doc_gap(func_name, line, filepath), 3)
            gaps.append(gap)
            file_gaps += 1
            if file_gaps >= _V3_GAPS_PER_FILE:
                break

    return gaps


def vector3_scan(dry_run=False) -> list[dict]:
    """
    Scan target repo files for public functions/classes with missing or
    minimal docstrings and return scored gap dicts.

    Caps: 5 gaps per file, 10 gaps per repo.
    """
    all_gaps: list[dict] = []
    repo_counts: dict[str, int] = {}

    for repo, filepath, raw_url, branch, lang in DOC_SCAN_TARGETS:
        repo_counts.setdefault(repo, 0)
        if repo_counts[repo] >= _V3_GAPS_PER_REPO:
            continue

        print(f"  Vector 3: fetching {repo}/{filepath} ...")
        source = _fetch_raw(raw_url)
        if source is None:
            print(f"    WARNING: could not fetch {raw_url}")
            continue

        if lang == 'c':
            file_gaps = _parse_doc_gaps_c(source, repo, filepath, branch)
        elif lang == 'go':
            file_gaps = _parse_doc_gaps_go(source, repo, filepath, branch)
        else:
            file_gaps = _parse_doc_gaps(source, repo, filepath, branch)

        # Respect per-repo cap
        remaining = _V3_GAPS_PER_REPO - repo_counts[repo]
        file_gaps = file_gaps[:remaining]

        repo_counts[repo] = repo_counts.get(repo, 0) + len(file_gaps)
        all_gaps.extend(file_gaps)
        print(f"    {repo}/{filepath}: {len(file_gaps)} doc gaps")

    # Summary per repo
    for repo, count in repo_counts.items():
        print(f"  Vector 3 — {repo}: {count} total doc gaps queued")

    return all_gaps


# ---------------------------------------------------------------------------
# Write gaps to Postgres
# ---------------------------------------------------------------------------

def write_gaps(gaps, dry_run=False):
    if dry_run:
        print(f"\nDRY RUN — {len(gaps)} gaps found:")
        for g in sorted(gaps, key=lambda x: -x['score'])[:10]:
            print(f"  [{g['score']:.3f}] {g['wedge_type']} | {g['provider']} | {g['description'][:80]}")
        return

    conn = get_conn()
    cur = conn.cursor()

    # Get or create repo_id for each gap
    written = 0
    for gap in gaps:
        repo_full = gap.get('repo') or f"BerriAI/litellm"

        # Check if this exact gap already exists (by description)
        cur.execute(
            "SELECT id FROM gaps WHERE description = %s AND status NOT IN ('abandoned')",
            (gap['description'],)
        )
        if cur.fetchone():
            continue  # Already queued

        # Get repo_id
        cur.execute("SELECT id FROM repos WHERE full_name = %s", (repo_full,))
        row = cur.fetchone()
        if row:
            repo_id = row[0]
        else:
            cur.execute(
                "INSERT INTO repos (owner, name, full_name, tier) VALUES (%s, %s, %s, 2) RETURNING id",
                (repo_full.split('/')[0], repo_full.split('/')[-1], repo_full)
            )
            repo_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO gaps (
                repo_id, wedge_type, description, effort, status,
                score, contribution_level, user_pain, maintainer_receptivity,
                freshness, source_url, provider
            ) VALUES (%s, %s, %s, %s, 'open', %s, %s, %s, %s, %s, %s, %s)
        """, (
            repo_id,
            gap['wedge_type'],
            gap['description'],
            gap['effort'],
            gap['score'],
            gap['contribution_level'],
            gap['user_pain'],
            gap['maintainer_receptivity'],
            gap['freshness'],
            gap.get('source_url', ''),
            gap['provider'],
        ))
        written += 1

    conn.commit()
    conn.close()
    print(f"SCANNER: wrote {written} new gaps to Postgres")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vector', default='all', choices=['1', '2', '3', 'all'])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    all_gaps = []

    if args.vector in ('1', 'all'):
        print("=== Vector 1: Model registry staleness ===")
        try:
            prices = fetch_litellm_prices()
            print(f"Loaded {len(prices)} litellm model keys")
            gaps = vector1_scan(prices, dry_run=args.dry_run)
            all_gaps.extend(gaps)
        except Exception as e:
            print(f"Vector 1 error: {e}")

    if args.vector in ('2', 'all'):
        print("\n=== Vector 2: Issue pain mining ===")
        try:
            gaps = vector2_scan(dry_run=args.dry_run)
            all_gaps.extend(gaps)
        except Exception as e:
            print(f"Vector 2 error: {e}")

    if args.vector in ('3', 'all'):
        print("\n=== Vector 3: Documentation gaps ===")
        try:
            gaps = vector3_scan(dry_run=args.dry_run)
            all_gaps.extend(gaps)
        except Exception as e:
            print(f"Vector 3 error: {e}")

    write_gaps(all_gaps, dry_run=args.dry_run)
    print(f"\nSCANNER COMPLETE: {len(all_gaps)} total gaps found")


if __name__ == '__main__':
    main()
