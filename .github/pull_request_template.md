## What changed

<!-- One or two sentences. Link the issue if there is one. -->

## How I verified it

<!-- Commands you ran and what you saw. e.g. `uv run pytest`, `SESREPORT_DEMO=1 uv run sesreport` -->

## Checklist

- [ ] `uv run ruff check .` passes
- [ ] `uv run pytest` passes
- [ ] No credentials, `.env`, account IDs, or recipient email addresses are committed
- [ ] The collector still degrades gracefully when a source is unavailable
