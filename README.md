# SES Complaint Report

**SES Complaint Report** is a self-hosted, refreshable dashboard for **Amazon SES
deliverability and complaint posture** in a single AWS account. Instead of a
one-shot HTML export, it is a page you can reopen, refresh against live AWS data, and search.

**Who it's for:** engineers and deliverability owners who operate SES sending
identities and need to watch bounce/complaint rates against AWS's enforcement
lines.

**The problem it solves:** SES complaint data is scattered across CloudWatch
metrics, the account-level suppression list, per-configuration-set event
destinations, SNS/SQS feedback plumbing, and whatever your application persisted.
This tool pulls it together into one view and keeps it current.

**Outcome:** a single localhost page showing complaint volume and rate vs the AWS
0.10% review line, who complained, what infra routes the feedback, and when the
data expires — refreshed on demand or hourly.

## Quickstart

```sh
git clone https://github.com/ryabinski-labs/ses-complaint-report
cd ses-complaint-report
cp .env.example .env        # edit for your profile/region
./start.sh                  # start in the background and open the browser
```

`./start.sh fg` runs in the foreground; `./start.sh stop`, `status`, and `logs`
manage a background instance.

## Install

Requirements: macOS or Linux, Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).
No AWS account is needed to explore the UI in demo mode.

```sh
uv sync
```

## Try it without AWS

Demo mode serves a synthetic dataset, so you can see the whole report before
pointing it at an account:

```sh
SESREPORT_DEMO=1 uv run sesreport
```

## What it shows

- **Enforcement & suppression posture** — enforcement status, production access,
  account suppression and its live entry counts, VDM, send quota, dedicated-IP
  warm-up (`sesv2:GetAccount`).
- **Complaint volume** — complaint / send / bounce counts and the rolling
  `Reputation.ComplaintRate` vs the AWS 0.10% review line and an internal alert line.
- **Complaints by configuration set** — from the `ses:configuration-set` metric dimension.
- **Individual complaint records** — complainants persisted by the app (address,
  ARF feedback provider, timestamp), plus per-store counts.
- **Infrastructure map** — sending identities and DKIM status, configuration sets
  and their event destinations, SNS topics/subscribers, the complaint DLQ.
- **Retention & expiry** — CloudWatch's 455-day aging of complaint data points,
  the non-expiring SES suppression list, and the adaptive reputation window, with
  per-complaint expiry dates.
- **Suppression coverage** — every configuration set flagged as routing or **not**
  routing `BOUNCE`/`COMPLAINT`, risk-ranked, with a copy-paste AWS CLI fix.
- **Change tracking** — each refresh is snapshotted, so the report shows deltas
  (new complaints, rate movement, new complainant addresses) and a rate sparkline.

Every table supports search, sort, category filters, and CSV export; the charts
have a time-range picker and a per-config-set toggle. The whole report exports as
JSON, and a Markdown summary copies to the clipboard.

## When to use it (and when not to)

**Use it** to audit or monitor a single SES account's complaint posture, to find
which configuration sets and templates attract complaints, and to understand what
AWS would see when it reviews your account.

**Don't use it** as a multi-tenant or fleet-wide dashboard (it audits one account
per instance), as an alerting system (it displays; it doesn't page), or as a
write tool — it is strictly read-only against AWS.

## Requirements

- macOS or Linux, Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).
- AWS credentials for the target account, via a profile in `~/.aws` (default: `default`).

### IAM permissions

Read-only:

```
sts:GetCallerIdentity
sesv2:GetAccount
sesv2:ListEmailIdentities, sesv2:GetEmailIdentity
sesv2:ListConfigurationSets
sesv2:ListSuppressedDestinations
ses:DescribeConfigurationSet
cloudwatch:GetMetricStatistics, cloudwatch:ListMetrics
logs:DescribeLogGroups, logs:StartQuery, logs:GetQueryResults
sns:ListTopics, sns:ListSubscriptionsByTopic
sqs:GetQueueUrl, sqs:GetQueueAttributes
dynamodb:Scan            # only on the app tables named in .env
```

## Configure

Copy `.env.example` to `.env` and edit:

| Variable | Meaning |
| --- | --- |
| `SESREPORT_PROFILE` | AWS profile to use |
| `SESREPORT_REGION` | Primary region being audited |
| `SESREPORT_EXTRA_REGIONS` | Extra regions summarised for identities |
| `SESREPORT_LOG_GROUPS` | Feedback log groups (blank = auto-discover) |
| `SESREPORT_LOG_GROUP_PREFIX` / `_MATCH` | Discovery prefix / substring |
| `SESREPORT_COMPLAINT_TABLE` | App table holding per-recipient complaint rows (blank = skip) |
| `SESREPORT_COMPLAINT_ATTR` / `_VALUE` | Filter attribute/value (default `bounce_type` = `Complaint`) |
| `SESREPORT_COMPLAINT_QUEUE` | Complaint DLQ name (blank = skip) |
| `SESREPORT_SUPPRESSION_TABLES` | Comma-separated app suppression tables to count |
| `SESREPORT_REDACT_EMAILS` | Mask recipient addresses in the API payload |
| `SESREPORT_DEMO` | Serve synthetic data instead of calling AWS |
| `SESREPORT_HOST`, `SESREPORT_PORT` | Local bind address (default `127.0.0.1:8787`) |
| `SESREPORT_REFRESH_SECONDS` | Background refresh interval (default 3600) |
| `SESREPORT_HISTORY_MAX` | Refresh snapshots kept for change tracking (default 500) |

The DynamoDB sections are app-specific; leave their variables blank and they are
skipped cleanly. The account ID is read from `sts:GetCallerIdentity`.

> The report reads recipient addresses from application stores. Those are kept
> out of the collector's output directory (`src/sesreport/data/` is gitignored)
> and are never sent anywhere — the dashboard is bound to localhost. Set
> `SESREPORT_REDACT_EMAILS=true` when screenshotting or sharing.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The interactive report |
| `GET` | `/api/report` | Latest collected model (JSON); `202` while collecting |
| `GET` | `/api/status` | Refresh state, last run, errors, interval |
| `GET` | `/api/history` | Snapshot history for change detection |
| `POST` | `/api/refresh` | Trigger a live re-query (returns immediately) |

## Development

```sh
uv sync
uv run python -m sesreport.collect     # run the collector and print JSON
uv run ruff check . && uv run pytest    # lint + smoke tests (no AWS calls)
uv run sesreport                       # start the server
```

## Support

Best-effort, community support. Open a GitHub issue for bugs or questions; see
[SUPPORT.md](SUPPORT.md). Report security issues privately per
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © 2026 Yoni Ryabinski.
