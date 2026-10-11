# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Suppression coverage** section: every configuration set flagged as routing or
  not routing `BOUNCE`/`COMPLAINT`, risk-ranked, with a copy-paste AWS CLI fix command.
- **Change tracking**: refresh snapshots persisted to `data/history.jsonl`; the
  report now shows deltas (new complaints, rate movement, new complainant
  addresses, new complaint days) and a rolling-rate sparkline across refreshes.
- **Export**: full-report JSON download, per-table CSV export, and copy-as-Markdown summary.
- `GET /api/history` endpoint; `SESREPORT_HISTORY_MAX` config.
- Frontend contract test asserting the demo model has every field the UI reads.
- SVG favicon.
- `GOVERNANCE.md`, `NOTICE` (third-party attribution), and `examples/` ready-made configs.
- README dashboard screenshot.
- SPDX `MIT` headers across source files.
- `oss_audit_gate.py` wired into CI: fails when the OSS readiness audits regress
  (vendored `opensource_audit` / `readme_audit` / `license_metadata_audit` / `workflow_audit`).

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

[Unreleased]: https://github.com/ryabinski-labs/ses-complaint-report/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ryabinski-labs/ses-complaint-report/releases/tag/v1.0.0
