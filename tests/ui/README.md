# UI end-to-end tests

Playwright (Chromium) suite for the dashboard's interactive surface. It boots
the app itself in **demo mode** (`SESREPORT_DEMO=1` — synthetic data, no AWS
calls) on port 8799 and drives it headlessly.

```sh
cd tests/ui
npm ci
npx playwright test          # add --headed for a visible browser
```

The suite covers the §4.2 recipient-level complaint records table: fit at
desktop widths, the pinned address column / edge fade / keyboard scrolling when
a narrow viewport must scroll, labelled cards and toolbar sorting on phone
widths, keyboard sorting with `aria-sort`, and CSV export completeness. It runs
as a separate `ui-tests` job in PR validation; the pytest suite
(`tests/test_frontend_table_ux.py`) guards the same contracts structurally.
