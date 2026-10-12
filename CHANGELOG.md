# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CodeQL now also runs on pull requests, so code-scanning results attach to PR
  head SHAs (which the OpenSSF Scorecard SAST check measures). The audit gate
  allow-lists exactly this workflow's `security-events: write`.
- Fuzzing: an Atheris harness (`fuzz/fuzz_targets.py`) covering the history
  JSON-lines parser and the environment config loader, a deterministic
  regression corpus in the test suite, and a bounded `Fuzz` CI workflow.
- `parse_history` is now a pure, unit-testable function used by `/api/history`.
- **Complaint attribution**: a unified, recipient-level complaint-record table
  across *all* configured app stores (not just the account table), with AWS
  **mailbox-simulator** addresses flagged as tests and an attribution summary
  reconciling the CloudWatch count against what the stores retained.
- **Identity-level feedback routing**: the report now reads
  `GetIdentityNotificationAttributes`, so identities that deliver
  `BOUNCE`/`COMPLAINT` through their own SNS topic are shown — feedback for sends
  that carried no configuration set, which the per-config-set map cannot see.
- **Self-owned feedback ledger** (`infra/ledger-stack.yaml` + `deploy_ledger.sh`):
  a DynamoDB table and ingest Lambda that retain deduped, attributed
  bounce/complaint rows past CloudWatch's 455-day window, keyed on a SHA-256 of
  the address (redacted plaintext, 13-month TTL) — no VDM required. The report
  reads it read-only when `SESREPORT_LEDGER_TABLE` is set.

### Fixed

- **Attribution double-count**: once the ledger was deployed, a complaint the
  ledger *and* an application store both retained was counted twice, because the
  ledger stores a redacted address while a store keeps the full one. The merge
  now joins on a source-independent identity (the ledger's `recipient_hash`, or a
  SHA-256 of an unredacted store address) and treats ledger rows as
  authoritative, so `records`, `real_records`, and the ledger/store subtotals
  agree.
- **Ingest Lambda robustness**: a shared feedback/ops topic can carry a JSON
  payload that is not an object (a bare array or string). The handler now ignores
  those and isolates each record, so one malformed message can no longer fail the
  whole invocation (and trigger SNS retries).
- **Dashboard accessibility**: added a `<main>` landmark, accessible names on the
  table search/filter controls, keyboard-focusable scroll regions, and a valid
  heading order — `a11y_audit` now reports zero violations.
- `.gitignore` now excludes `dist/` and `build/` so `uv build` artefacts cannot
  be committed.

## [1.0.0] - 2026-10-11

### Added

- Interactive, refreshable SES complaint / deliverability report served over
  localhost, replacing the static one-shot HTML audit.
- Collector for SES v2 (account posture, identities, config sets + event
  destinations via the SES v1 API), CloudWatch (`Complaint`, `Send`, `Bounce`,
  `Reputation.ComplaintRate`, per-config-set complaints), SNS, SQS, DynamoDB,
  and CloudWatch Logs Insights.
- Manual **Refresh** (live re-query) plus an hourly background refresh, cached to
  `src/sesreport/data/latest.json`.
- Search, sort, and category filters on every table; time-range picker and
  per-config-set toggle on the charts.
- Retention &amp; expiry section: CloudWatch 455-day aging, permanent SES
  suppression-list entries, and the adaptive reputation-rate window, with
  per-complaint expiry dates.
- `./start.sh` service script (`start` / `fg` / `stop` / `status` / `logs`).
- Demo mode (`SESREPORT_DEMO=1`) serving a synthetic dataset with no AWS calls.
- Email redaction (`SESREPORT_REDACT_EMAILS`).
- **Suppression coverage** section: every configuration set flagged as routing or
  not routing `BOUNCE`/`COMPLAINT`, risk-ranked, with a copy-paste AWS CLI fix command.
- **Change tracking**: refresh snapshots persisted to `data/history.jsonl`; the
  report now shows deltas (new complaints, rate movement, new complainant
  addresses, new complaint days) and a rolling-rate sparkline across refreshes.
- **Export**: full-report JSON download, per-table CSV export, and copy-as-Markdown summary.
- `GET /api/history` endpoint; `SESREPORT_HISTORY_MAX` config.
- `GOVERNANCE.md`, `NOTICE` (third-party attribution), `MAINTAINERS.md`, and
  `examples/` ready-made configs.
- README dashboard screenshot and SVG favicon.
- SPDX `MIT` headers across source files.
- Frontend contract test asserting the demo model has every field the UI reads.
- Security CI: CodeQL code scanning and OpenSSF Scorecard supply-chain analysis.
- CI: `oss_audit_gate.py` fails when the OSS readiness audits regress
  (vendored `opensource_audit` / `readme_audit` / `license_metadata_audit` / `workflow_audit`).

[Unreleased]: https://github.com/ryabinski-labs/ses-complaint-report/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ryabinski-labs/ses-complaint-report/releases/tag/v1.0.0
