# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `SESREPORT_APP_DKIM_MX` now defaults to empty. It used to default to one
  operator's inbound MX host; set it in `.env` if your sending app signs DKIM.
- Tests and docstrings use neutral example domains instead of real ones.
- CI: every checkout sets `persist-credentials: false`; a tag-triggered release
  workflow builds the sdist and wheel and attaches SLSA provenance
  (`*.intoto.jsonl`); an OSS compliance workflow runs the readiness audits and
  zizmor; issue templates added.

### Added

- **VDM-equivalent deliverability, with VDM off** (spec `docs/prd/vdm-coverage.md`,
  TDD artifact `tdd/vdm-coverage.tdd.yaml`):
  - The ledger Lambda now counts Delivery, Open and Click events into daily
    counters per recipient domain, sending identity and config set. It stores no
    addresses and dedupes per SES message ID for 30 days.
  - A new **Mailbox providers** section shows complaint, bounce, open and click
    rates per provider, identity and config set. Providers are classified by
    domain map plus MX lookup. Rates appear from 1,000 deliveries and follow the
    Range filter.
  - A built-in **Advisor** checks DKIM, custom MAIL FROM/SPF and DMARC, complaint
    and bounce thresholds, and Delivery/Open/Click publishing gaps. It feeds
    Needs attention and a new Authentication grid.
  - Every **Fix** button copies an agent-ready Markdown brief.
  - No VDM API is called (`BatchGetMetricData` and `ListRecommendations` are
    never used). The only new dependency is `dnspython`.

- **Dashboard redesign** (Ocean Depths theme, light and dark modes with a
  toggle): a verdict panel with the rolling rate drawn against the alert and
  AWS review lines; KPI tiles with mini-charts and meters; a severity-ranked
  "Needs attention" list with copy-paste fixes; the rolling rate and daily
  complaints on one shared time axis with a keyboard-accessible crosshair; a
  bar view of attribution; a pipeline diagram and a config-set × event-type
  coverage grid; and a timeline of when complaints age out.
  The per-config-set filter no longer shows a scaled, estimated rate. The rate
  is labelled account-wide, because that is the only rate AWS publishes.

- **UI end-to-end tests** (`tests/ui`, Playwright/Chromium): boot the app in
  demo mode and verify the report tables' layout contracts — no horizontal
  overflow at desktop widths, a pinned address column with edge fade and
  keyboard scrolling when narrow, labelled cards on phone widths, keyboard
  sorting with `aria-sort`, and CSV export completeness. Wired into PR
  validation as a separate `ui-tests` job.
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

- **Fidelity audit of every finding and number** (2026-10-24; four read-only
  audits against live AWS, DNS and the sending apps' code). False positives
  removed: SES's DKIM status only says whether *SES* signs, so identities whose
  app signs (Project tag or MX on `SESREPORT_APP_DKIM_MX`) pass instead of
  raising 11 highs; unverified identities get one housekeeping note and no auth
  findings (they cannot send); a missing MAIL FROM or weak SPF is a note when
  DKIM already aligns; a disabled configuration set is never a gap; a set whose
  only plausible senders route feedback through identity-level topics, or that
  sent nothing in 14 days, is a note; OPEN/CLICK publishing is off by policy and
  no longer a finding; the identity "Sending" column read `no` for every
  identity (`GetEmailIdentity` has no such field). False negatives added: SES's
  own `Reputation.ComplaintRate`/`BounceRate` against the same lines as the
  ledger rows (a rate above the 0.08% line showed nothing); a DMARC
  record with no valid `p=` (truncated at the first `;` by some DNS hosts), more
  than one record, `pct<100`, and inherited policies read through `sp=`, with
  the DMARCbis tree walk instead of a last-two-labels guess; a strict SPF check
  (one record, whole-token include, a real `all`); configuration sets that
  route feedback to a handler the ledger never sees, and CloudWatch-only
  destinations no longer count as routing; identity topics with no handler or
  ledger (email-only); the share of sends that carried no configuration set;
  unconfirmed SNS subscriptions; 1024-bit DKIM keys; address identities that
  cannot align DMARC; tracking that is switched on (as a note).
- **Numbers.** Days are UTC, like CloudWatch, the ledger and the SES console
  (local-midnight alignment labelled every winter day one day early). The
  "overall complaint rate" divided five months of complaints by 455 days of
  sends; it now uses the sends from the same window and is labelled "complaints
  per send", not SES's enforcement rate. Per-config-set complaints and sends are
  read for every set by name (`ListMetrics` hid anything older than 14 days:
  8 complaints on one set showed as 0), with SES's reputation metrics as the
  usage fallback. Ledger breakdowns count numerators only from the first day
  deliveries were counted, bounce rates divide by sends (deliveries + bounces)
  as SES does, and "30 days" is 30 days. Only COMPLAINT ledger rows appear as
  complainants (20 bounce rows were listed as "real complainants"). The
  unattributed-feedback Logs Insights query parses the handler's plain-text
  lines (it matched nothing before). The simulator note counts events, not
  addresses. The SQS row is labelled as the SNS delivery DLQ it is.
- **Ledger ingest.** The sort key is `<eventType>#<messageId>`, so the
  config-set and identity copies of one event (timestamps a second apart)
  write one row, and the copy that knows the configuration set wins. Sender
  display names are parsed (`"Name" <x@mail.example.net>` was stored as
  `mail.example.net>`). The reader dedupes rows written by the old key.
- **Fix briefs** no longer propose adding DELIVERY to an ops/alert topic or to
  an email-only identity topic; they create a dedicated ledger-subscribed
  destination instead, and routing briefs create an SNS destination (a
  CloudWatch destination hears nothing).
- **Needs attention** gains account-posture rules (sending paused, suppression
  reasons, quota, a DLQ with messages, verified identities with sending
  disabled, a region with suppression off) and treats an unreadable enforcement
  status as unknown rather than "At risk".

- README IAM list used a non-existent `sesv2:` prefix; SES v2 actions are authorised as `ses:` (#23).
- **Security (from QA of the redesign):**
  - Table cells now render API values as text, so values such as a crafted recipient address or ARF string can no longer inject markup or script into the page. Attribute values and date fallbacks are escaped too.
  - CSV exports prefix cells that start with `= + - @` so spreadsheets keep them as text.
  - The copied AWS CLI fix command shell-quotes the config-set name.
  - `POST /api/refresh` now requires a same-origin `X-Requested-With` header and rejects cross-site `Origin` / `Sec-Fetch-Site`, so other websites can no longer start billed collection runs.
  - The server answers only loopback Host headers plus `SESREPORT_ALLOWED_HOSTS` (a DNS-rebinding guard).
- **Demo data isolation**: demo mode now writes to `data/demo/`. Before this it overwrote the live `latest.json` and pushed real snapshots out of the capped history.
- **No horizontal scrolling**:
  - Tables wrap long values instead of scrolling sideways.
  - The coverage grid turns into cards on narrow screens.
  - The section nav wraps onto more lines.
  - Charts never draw wider than their card, including at 320px.
  - The complainant and unattributed tables are stacked full width.
- **Visual and accessibility fixes**:
  - Printing from a dark theme now uses light colours.
  - Focus rings reach 3:1 contrast.
  - Light-mode status text meets 4.5:1.
  - The pipeline list has proper list semantics, and keyboard chart readings go to a live region.
  - The threshold rail and the latest-rate label no longer clip.
  - Number headers align with their numbers.
  - Short ranges show weekly ticks.
  - Overlapping timeline dots merge into one labelled cluster.
  - The `NaN days ago` text is gone when there are no complaints.
  - Store breakdowns read `bounce: 42` instead of a Python dict.

- **Recipient-level complaint table overflow**: the §4.2 table's 10 columns
  were wider than the report's 1134 px content column, forcing horizontal
  scrolling even at a 1440 px viewport and hiding the recipient address when
  scrolled. Related columns now share a row (store + source, ARF diagnostic +
  event type, first/last seen) with no data loss, so the table fits down to
  ~1024 px. Where a narrow viewport still needs to scroll, the address column
  stays pinned with a scroll shadow and edge fade, and below 640 px each row
  becomes a labelled card. Sortable headers are now real buttons with
  `aria-sort`, so sorting works from the keyboard too.
- **Phone sorting and region semantics**: below 640 px the tables render as
  cards and the sortable column headers are hidden, so the toolbar now carries
  a sort control (column select + direction toggle) in that layout. Table
  scroll regions only take tab focus and announce themselves as scrollable
  when they actually overflow.
- **Alert-text contrast on cards**: `.bad` text (red) measured 4.41:1 against
  the card background used by the phone card layout — under the WCAG AA 4.5:1
  minimum. Red text now uses a `--redtx` token that clears AA on the page,
  card, and hover backgrounds; the decorative `--red` remains for borders and
  charts.
- **Mobile page overflow**: raw report tables and filter `<select>` elements
  sized to their longest option could push the whole page wider than the
  viewport; both are now contained (no overflow at 375/390/430 px widths).
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
