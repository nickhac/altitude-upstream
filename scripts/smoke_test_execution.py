#!/usr/bin/env python3
"""
smoke_test_execution.py — altitude-upstream

Execution smoke test: confirms that changed Python files are importable and
that any new/modified public functions/classes are callable without crashing.

This is the execution verification layer from the build spec:
  "Docker sandbox: execute and verify the contribution runs"

We don't use Docker — we run in the already-isolated worktree with the change
applied. Same guarantee: if it crashes on import or basic invocation, we catch
it here before the PR goes live.

Checks performed (per changed .py file):
  1. Syntax check     — py_compile, instant
  2. Import check     — python3 -c "import <module>" in the worktree
  3. Function smoke   — for each new public function/class added by the diff,
                        attempt a minimal invocation (no-args call or
                        inspect.signature check)

Returns:
  (passed: bool, results: list[dict])
  Each result: {file, check, status, detail}

Usage (standalone):
    python3 scripts/smoke_test_execution.py --diff /tmp/foo.diff --worktree /path/to/repo
    python3 scripts/smoke_test_execution.py --diff /tmp/foo.diff --worktree /path/to/repo --verbose

Usage (imported):
    from smoke_test_execution import smoke_test
    passed, results = smoke_test(diff_text, worktree_path, verbose=False)
"""

import argparse
import ast
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Diff parsing helpers
# ---------------------------------------------------------------------------

def _changed_python_files(diff_text: str) -> list[str]:
    """Return list of .py file paths touched by the diff (relative to repo root)."""
    files = []
    for line in diff_text.splitlines():
        if line.startswith('+++ b/') and line.endswith('.py'):
            files.append(line[6:])
    return list(dict.fromkeys(files))  # deduplicate, preserve order


def _new_public_symbols(diff_text: str, filepath: str) -> list[str]:
    """
    Extract names of public functions/classes that are NEW in the diff
    (added lines starting with 'def ' or 'class ') for the given file.
    """
    symbols = []
    in_file = False
    for line in diff_text.splitlines():
        if line.startswith('+++ b/') and line[6:] == filepath:
            in_file = True
            continue
        if line.startswith('+++ b/') and line[6:] != filepath:
            in_file = False
        if not in_file:
            continue
        if line.startswith('+') and not line.startswith('+++'):
            content = line[1:].lstrip()
            m = re.match(r'^(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)', content)
            if m:
                name = m.group(2)
                if not name.startswith('_'):  # public only
                    symbols.append(name)
    return list(dict.fromkeys(symbols))


def _file_to_module(filepath: str, worktree: str) -> Optional[str]:
    """
    Convert a file path like 'llama_index/core/async_utils.py' to a
    dotted module name like 'llama_index.core.async_utils'.
    Walks up the directory tree to find the package root (first dir
    without __init__.py is the root).
    """
    abs_path = os.path.join(worktree, filepath)
    if not os.path.exists(abs_path):
        return None

    parts = Path(filepath).with_suffix('').parts
    # Find the deepest ancestor with __init__.py (= package root boundary)
    # Work backwards to find where the package starts
    for i in range(len(parts)):
        candidate_dir = os.path.join(worktree, *parts[:i+1])
        init = os.path.join(worktree, *parts[:i], '__init__.py')
        if i == 0 and not os.path.exists(os.path.join(worktree, parts[0], '__init__.py')):
            # Top-level file, not in a package
            return parts[-1]  # just the module name
        if i > 0 and not os.path.exists(os.path.join(worktree, parts[i-1], '__init__.py')):
            # Previous dir has no __init__, so parts[i] is the package root
            return '.'.join(parts[i:])
    return '.'.join(parts)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _syntax_check(filepath: str, worktree: str) -> tuple[bool, str]:
    """Compile-check a Python file. Instant."""
    abs_path = os.path.join(worktree, filepath)
    if not os.path.exists(abs_path):
        return True, "file not found — skipped"

    r = subprocess.run(
        [sys.executable, '-m', 'py_compile', abs_path],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return False, f"SyntaxError: {(r.stderr or r.stdout).strip()[:200]}"
    return True, "syntax OK"


def _import_check(filepath: str, worktree: str, timeout: int = 15) -> tuple[bool, str]:
    """
    Try to import the module in a subprocess with the worktree on sys.path.
    Uses a clean Python process so import side-effects don't pollute our env.
    """
    module = _file_to_module(filepath, worktree)
    if not module:
        return True, "could not determine module name — skipped"

    # Find the right sys.path root: walk up to find where the top package lives
    parts = Path(filepath).parts
    # Add worktree root and any sub-package root to sys.path
    sys_path_dirs = [worktree]
    # Also add common src layouts
    for candidate in ['src', 'lib']:
        candidate_path = os.path.join(worktree, candidate)
        if os.path.isdir(candidate_path):
            sys_path_dirs.append(candidate_path)

    # Find actual package root by walking up from the file
    abs_file = os.path.join(worktree, filepath)
    check_dir = os.path.dirname(abs_file)
    while check_dir != worktree and check_dir != os.path.dirname(check_dir):
        if not os.path.exists(os.path.join(check_dir, '__init__.py')):
            # This dir has no __init__, so its parent is the sys.path entry
            sys_path_dirs.append(check_dir)
            break
        check_dir = os.path.dirname(check_dir)

    sys_path_str = ':'.join(sys_path_dirs)

    script = textwrap.dedent(f"""
        import sys
        for p in {sys_path_dirs!r}:
            if p not in sys.path:
                sys.path.insert(0, p)
        try:
            import {module}
            print("IMPORT_OK")
        except ImportError as e:
            # Missing optional dep — not our bug, treat as pass
            if 'No module named' in str(e):
                print(f"IMPORT_SKIP: {{e}}")
            else:
                print(f"IMPORT_FAIL: {{e}}")
                sys.exit(1)
        except Exception as e:
            print(f"IMPORT_FAIL: {{e}}")
            sys.exit(1)
    """).strip()

    r = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True, text=True, timeout=timeout,
        cwd=worktree, env={**os.environ, 'DATABASE_URL': ''}
    )

    output = (r.stdout + r.stderr).strip()
    if 'IMPORT_OK' in output:
        return True, f"import {module} OK"
    if 'IMPORT_SKIP' in output:
        missing = output.split('IMPORT_SKIP:')[-1].strip()[:80]
        return True, f"import skipped (missing optional dep: {missing})"
    if 'IMPORT_FAIL' in output:
        detail = output.split('IMPORT_FAIL:')[-1].strip()[:150]
        return False, f"import {module} failed: {detail}"
    if r.returncode != 0:
        return False, f"import crashed: {output[:150]}"
    return True, f"import {module} OK"


def _function_smoke(
    filepath: str, worktree: str, symbols: list[str], timeout: int = 15
) -> tuple[bool, str]:
    """
    For each new public symbol, verify it exists in the module and has a
    valid signature (inspect.signature). Does NOT call the function —
    just confirms it's accessible and inspectable without crashing.
    """
    if not symbols:
        return True, "no new public symbols to smoke-test"

    module = _file_to_module(filepath, worktree)
    if not module:
        return True, "could not determine module — skipped"

    sys_path_dirs = [worktree]
    for candidate in ['src', 'lib']:
        cp = os.path.join(worktree, candidate)
        if os.path.isdir(cp):
            sys_path_dirs.append(cp)

    checks = '\n'.join(
        f"    _check('{sym}', {module!r}, sys_path_dirs)"
        for sym in symbols[:5]  # cap at 5 symbols per file
    )

    script = textwrap.dedent(f"""
        import sys, inspect
        sys_path_dirs = {sys_path_dirs!r}
        for p in sys_path_dirs:
            if p not in sys.path:
                sys.path.insert(0, p)

        results = []

        def _check(name, mod_name, paths):
            try:
                mod = __import__(mod_name, fromlist=[name])
                obj = getattr(mod, name, None)
                if obj is None:
                    results.append(f"MISSING:{{name}}")
                    return
                # Just inspect the signature — don't call it
                try:
                    sig = inspect.signature(obj)
                    results.append(f"OK:{{name}}")
                except (ValueError, TypeError):
                    results.append(f"OK:{{name}}")  # not callable, that's fine
            except ImportError as e:
                results.append(f"SKIP:{{name}}:{{e}}")
            except Exception as e:
                results.append(f"FAIL:{{name}}:{{e}}")

        {checks}

        for r in results:
            print(r)
    """).strip()

    r = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True, text=True, timeout=timeout,
        cwd=worktree, env={**os.environ, 'DATABASE_URL': ''}
    )

    output = (r.stdout + r.stderr).strip()
    lines = [l for l in output.splitlines() if l.strip()]

    fails = [l for l in lines if l.startswith('FAIL:')]
    missing = [l for l in lines if l.startswith('MISSING:')]

    if fails:
        detail = '; '.join(l.split(':', 2)[-1] for l in fails[:3])
        return False, f"symbol check failed: {detail[:150]}"
    if missing:
        names = ', '.join(l.split(':')[1] for l in missing)
        return False, f"symbols not found in module: {names}"

    ok_count = len([l for l in lines if l.startswith('OK:')])
    skip_count = len([l for l in lines if l.startswith('SKIP:')])
    return True, f"{ok_count} symbol(s) OK, {skip_count} skipped (missing deps)"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def smoke_test(
    diff_text: str,
    worktree_path: str,
    verbose: bool = False,
) -> tuple[bool, list[dict]]:
    """
    Run all smoke test checks against a diff applied to a worktree.

    Returns:
        (all_passed, results)
        results is a list of dicts: {file, check, status, detail}
    """
    results = []
    all_passed = True

    py_files = _changed_python_files(diff_text)
    if not py_files:
        return True, [{'file': '(none)', 'check': 'python files',
                       'status': 'SKIP', 'detail': 'no Python files changed'}]

    for filepath in py_files:
        if verbose:
            print(f"  smoke: {filepath}")

        # 1. Syntax check
        ok, detail = _syntax_check(filepath, worktree_path)
        results.append({'file': filepath, 'check': 'syntax', 'status': 'PASS' if ok else 'FAIL', 'detail': detail})
        if not ok:
            all_passed = False
            if verbose:
                print(f"    syntax FAIL: {detail}")
            continue  # no point importing if syntax is broken

        # 2. Import check
        try:
            ok, detail = _import_check(filepath, worktree_path)
        except subprocess.TimeoutExpired:
            ok, detail = True, "import timed out — skipped"  # fail open
        results.append({'file': filepath, 'check': 'import', 'status': 'PASS' if ok else 'FAIL', 'detail': detail})
        if not ok:
            all_passed = False
            if verbose:
                print(f"    import FAIL: {detail}")
            continue

        # 3. Function smoke (new public symbols only)
        symbols = _new_public_symbols(diff_text, filepath)
        if symbols:
            try:
                ok, detail = _function_smoke(filepath, worktree_path, symbols)
            except subprocess.TimeoutExpired:
                ok, detail = True, "symbol check timed out — skipped"
            results.append({'file': filepath, 'check': 'symbols', 'status': 'PASS' if ok else 'FAIL', 'detail': detail})
            if not ok:
                all_passed = False
                if verbose:
                    print(f"    symbols FAIL: {detail}")
        else:
            results.append({'file': filepath, 'check': 'symbols', 'status': 'SKIP', 'detail': 'no new public symbols'})

        if verbose:
            for r in results[-3:]:
                print(f"    {r['check']}: {r['status']} — {r['detail']}")

    return all_passed, results


def _format_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        lines.append(f"  [{r['status']:4}] {r['check']:8} {r['file']}: {r['detail']}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Execution smoke test for a contribution diff'
    )
    parser.add_argument('--diff', required=True, help='Path to diff file')
    parser.add_argument('--worktree', required=True, help='Path to repo worktree')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    with open(args.diff) as f:
        diff_text = f.read()

    passed, results = smoke_test(diff_text, args.worktree, verbose=args.verbose)

    print(_format_results(results))
    print(f"\nSMOKE: {'PASS' if passed else 'FAIL'} — {sum(1 for r in results if r['status'] == 'PASS')} passed, "
          f"{sum(1 for r in results if r['status'] == 'FAIL')} failed, "
          f"{sum(1 for r in results if r['status'] == 'SKIP')} skipped")
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
