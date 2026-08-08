#!/usr/bin/env python3
"""
get-repo-knowledge.py — altitude-upstream

Return knowledge file content for a given repo and/or wedge type.
Reads from docs/knowledge/ markdown files.

Used by contribution agents to load context before writing a fix.
Returns the file content as plain text (stdout), or JSON with --json flag.

Usage:
    # Get repo-specific knowledge
    python3 scripts/get-repo-knowledge.py --repo BerriAI/litellm

    # Get wedge-type knowledge
    python3 scripts/get-repo-knowledge.py --wedge model_registry_staleness

    # Get infrastructure knowledge
    python3 scripts/get-repo-knowledge.py --infra github-auth

    # Get all relevant context for a contribution (repo + wedge + infra)
    python3 scripts/get-repo-knowledge.py \
        --repo BerriAI/litellm \
        --wedge model_registry_staleness \
        --infra github-auth \
        --infra worktrees \
        --infra verification-pipeline

    # Return as JSON dict (key -> content)
    python3 scripts/get-repo-knowledge.py --repo BerriAI/litellm --json

Knowledge directory: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'docs', 'knowledge')/

Repo slug mapping (GitHub full_name -> filename):
    BerriAI/litellm       -> repos/BerriAI-litellm.md
    vllm-project/vllm     -> repos/vllm-project-vllm.md
    langchain-ai/langchain -> repos/langchain-ai-langchain.md
    run-llama/llama_index  -> repos/run-llama-llama_index.md
    openai/openai-python   -> repos/openai-openai-python.md
"""

import sys
import os
import json
import argparse
from pathlib import Path

# ---------------------------------------------------------------------------
# Knowledge base root
# ---------------------------------------------------------------------------

KNOWLEDGE_ROOT = Path('os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'docs', 'knowledge')')

# Map GitHub full_name -> knowledge file path relative to KNOWLEDGE_ROOT
REPO_SLUG_MAP = {
    'BerriAI/litellm':        'repos/BerriAI-litellm.md',
    'vllm-project/vllm':      'repos/vllm-project-vllm.md',
    'langchain-ai/langchain': 'repos/langchain-ai-langchain.md',
    'run-llama/llama_index':  'repos/run-llama-llama_index.md',
    'openai/openai-python':   'repos/openai-openai-python.md',
}


def read_knowledge_file(rel_path: str) -> 'tuple[str, str | None]':
    """
    Read a knowledge file by relative path.

    Returns (content, error_message). error_message is None on success.
    """
    full_path = KNOWLEDGE_ROOT / rel_path
    if not full_path.exists():
        return '', f"Knowledge file not found: {full_path}"
    try:
        return full_path.read_text(encoding='utf-8'), None
    except Exception as e:
        return '', f"Error reading {full_path}: {e}"


def get_repo_knowledge(repo_full_name: str) -> 'tuple[str, str | None]':
    """Load repo knowledge by GitHub full_name (e.g. 'BerriAI/litellm')."""
    rel = REPO_SLUG_MAP.get(repo_full_name)
    if not rel:
        # Try auto-deriving: replace / with - and use repos/ dir
        slug = repo_full_name.replace('/', '-')
        rel = f'repos/{slug}.md'
    return read_knowledge_file(rel)


def get_wedge_knowledge(wedge_type: str) -> 'tuple[str, str | None]':
    """Load wedge-type knowledge by wedge_type string."""
    rel = f'wedge-types/{wedge_type}.md'
    return read_knowledge_file(rel)


def get_infra_knowledge(infra_name: str) -> 'tuple[str, str | None]':
    """Load infrastructure knowledge by name (e.g. 'github-auth')."""
    rel = f'infrastructure/{infra_name}.md'
    return read_knowledge_file(rel)


def list_available() -> dict:
    """Return a dict of available knowledge files by category."""
    result = {}
    for subdir in ('repos', 'wedge-types', 'infrastructure', 'gap-scanner'):
        d = KNOWLEDGE_ROOT / subdir
        if d.exists():
            files = sorted(f.name for f in d.glob('*.md'))
            result[subdir] = files
    return result


def main():
    parser = argparse.ArgumentParser(
        description='Load knowledge files for agent context'
    )
    parser.add_argument('--repo', type=str, default=None,
                        help='GitHub full_name (e.g. BerriAI/litellm)')
    parser.add_argument('--wedge', type=str, default=None,
                        help='Wedge type (e.g. model_registry_staleness)')
    parser.add_argument('--infra', type=str, action='append', default=[],
                        help='Infrastructure file name(s) (repeatable, e.g. --infra github-auth)')
    parser.add_argument('--gap-scanner', type=str, default=None,
                        help='Gap-scanner file (e.g. exclusion-patterns)')
    parser.add_argument('--list', action='store_true',
                        help='List all available knowledge files and exit')
    parser.add_argument('--json', action='store_true',
                        help='Return output as JSON dict {name: content}')
    args = parser.parse_args()

    if args.list:
        available = list_available()
        print(json.dumps(available, indent=2))
        return

    sections = {}  # ordered: name -> content

    if args.repo:
        content, err = get_repo_knowledge(args.repo)
        if err:
            print(f"WARNING: {err}", file=sys.stderr)
        else:
            sections[f'repo:{args.repo}'] = content

    if args.wedge:
        content, err = get_wedge_knowledge(args.wedge)
        if err:
            print(f"WARNING: {err}", file=sys.stderr)
        else:
            sections[f'wedge:{args.wedge}'] = content

    for infra_name in args.infra:
        content, err = get_infra_knowledge(infra_name)
        if err:
            print(f"WARNING: {err}", file=sys.stderr)
        else:
            sections[f'infra:{infra_name}'] = content

    if args.gap_scanner:
        rel = f'gap-scanner/{args.gap_scanner}.md'
        content, err = read_knowledge_file(rel)
        if err:
            print(f"WARNING: {err}", file=sys.stderr)
        else:
            sections[f'gap-scanner:{args.gap_scanner}'] = content

    if not sections:
        print("No knowledge files loaded. Use --list to see available files.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(sections, indent=2))
    else:
        # Plain text: concatenate with separators for easy reading by agents
        parts = []
        for name, content in sections.items():
            parts.append(f"=== {name} ===\n{content.strip()}")
        print('\n\n'.join(parts))


if __name__ == '__main__':
    main()
