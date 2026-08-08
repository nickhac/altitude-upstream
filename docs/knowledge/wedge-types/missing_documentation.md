# Wedge Type: missing_documentation

**Confidence:** medium | **Last updated:** 2026-08-05

## What it is
A public function or class has no docstring, or a docstring shorter than 10 characters.
The fix is to add a correct, accurate docstring.

## What a good docstring looks like
Match the style of the surrounding file. Check 3 adjacent functions first.

Google style (common in litellm, langchain):
```python
def function(param: str, count: int = 0) -> list:
    """Brief one-line description.

    Args:
        param: Description of param.
        count: Description of count. Defaults to 0.

    Returns:
        Description of return value.

    Example:
        >>> result = function("hello", count=1)
    """
```

NumPy style (common in vllm, llama_index):
```python
def function(param: str, count: int = 0) -> list:
    """Brief one-line description.

    Parameters
    ----------
    param : str
        Description of param.
    count : int, optional
        Description of count, by default 0.

    Returns
    -------
    list
        Description of return value.
    """
```

## What to avoid (learned from rejected PRs)
- Do NOT invent deprecation notices unless the function is actually deprecated in the source
- Do NOT claim a function is a "shim" or "wrapper" unless you can see that in the code
- Do NOT add Co-authored-by anywhere in the docstring or source code
- Do NOT add information that isn't verifiable from reading the function itself
- Keep it accurate and minimal — do not over-document

## Acceptance signal
- No merges yet — first signal expected within 1 week of 2026-08-05
- Will update this file with real acceptance patterns when data arrives
