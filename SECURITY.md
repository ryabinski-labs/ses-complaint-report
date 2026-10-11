# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately, not in a public issue:

- Open a [GitHub Security Advisory](https://github.com/ryabinski-labs/ses-complaint-report/security/advisories/new)
  (preferred), or
- Email cigan1@gmail.com.

Include what you did, what you observed, and the impact you believe it has. We
aim to acknowledge reports within a few days; this is a personal project and we
cannot commit to a fixed remediation SLA.

## Scope and threat model

This is a **read-only, localhost-bound** reporting tool. It:

- Reads from AWS (SES, CloudWatch, SNS, SQS, DynamoDB, CloudWatch Logs) using the
  caller's own credentials. It never writes to AWS.
- Binds to `127.0.0.1` by default and has no authentication layer. Do not expose
  it to a network you do not control.
- Renders recipient addresses read from your application stores. Use
  `SESREPORT_REDACT_EMAILS=true` if you screenshot or share the output.

## Handling secrets

- Credentials come from your `~/.aws` profile; the tool stores none.
- `.env` is gitignored. Never commit it — see `.env.example` for the shape.
- The collector's cache (`src/sesreport/data/`) may contain recipient addresses
  and is gitignored. Keep it out of version control and shared artifacts.
