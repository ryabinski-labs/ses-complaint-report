# Contributing

Thanks for taking a look. This is a small, focused tool — bug reports and
targeted pull requests are both welcome.

## Development setup

Requirements: Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest                    # smoke tests (no AWS calls)
uv run python -m sesreport.collect   # exercise the collector against your account
```

You do not need AWS to develop the UI: run in demo mode with synthetic data.

```sh
SESREPORT_DEMO=1 uv run sesreport
```

## Making changes

1. Fork and branch from `main`.
2. Keep changes small and focused; one concern per PR.
3. Add or update tests under `tests/`.
4. Run the checks that CI runs: `uv run ruff check . && uv run pytest`.
5. Open a PR using the template and describe what changed and how you verified it.

## Ground rules

- Never commit credentials, `.env`, account IDs, or any recipient email address.
  Collector output lives in `src/sesreport/data/` and is gitignored — keep it that way.
- AWS-facing code must be read-only by default and must degrade gracefully when a
  source is unavailable (see the per-section error handling in `collect.py`).
- Match the existing style; keep the dependency set small.

## Reporting bugs

Open an issue with what you ran, what you expected, and what happened, including
the relevant collector warning (the report's "Collector warnings" box lists them).
