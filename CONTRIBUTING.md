# Contributing

This is a personal production system — it's not designed for external contributors. But if you find a bug or improvement, a PR is welcome.

## What belongs here

- Bug fixes in the pipeline scripts
- New wedge-type handlers
- Improvements to the gap scorer
- Additional test coverage

## What doesn't

- New target repos (those are added through the knowledge base, not code changes)
- Changes to the daily cap or ramp logic (operational decisions)

## Running tests

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -v
```
