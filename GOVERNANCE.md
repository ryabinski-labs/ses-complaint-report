# Governance

This is a small, single-maintainer project. This document describes how decisions
are made today and how that can change. It is deliberately lightweight so it stays
honest about the project's actual scale.

## Roles

- **Maintainer** — has write access to the repository, reviews and merges pull
  requests, triages issues, and cuts releases. The current maintainer is listed in
  [`MAINTAINERS.md`](MAINTAINERS.md).
- **Contributor** — anyone who opens an issue or a pull request. No write access is
  granted implicitly.

## Decision making

- Day-to-day changes are made through pull requests and merged by a maintainer once
  required CI checks pass (see [CONTRIBUTING.md](CONTRIBUTING.md)).
- For anything with lasting impact — a new dependency, a change to the published
  data model, a new required configuration variable, or a change to this document —
  the maintainer opens an issue first so the reasoning is recorded and others can
  comment before the work lands.
- Where consensus is unclear, the maintainer decides and records the rationale in
  the issue or pull request. Security-sensitive decisions follow
  [`SECURITY.md`](SECURITY.md).

## Sensitive areas

Some paths require the maintainer's review as code owners. These are listed in
[`.github/CODEOWNERS`](.github/CODEOWNERS) and cover CI configuration, the AWS-facing
collector and config, the service launcher, and the ownership/security/license files.

## Becoming a maintainer

There is no fixed bar, but a maintainer will be added when someone has a sustained
record of high-quality contributions and review, understands the project's ground
rules (read-only against AWS, never commit credentials or recipient data), and wants
the responsibility. The existing maintainer records the change in `MAINTAINERS.md`,
and write access is granted through the organization.

## Changing this document

Changes to governance are ordinary pull requests, but they should start with an
issue and be left open for comment for at least a few days before merge.

## Licensing of contributions

Contributions are accepted under the project's [MIT license](LICENSE)
(inbound = outbound). By opening a pull request you agree that your contribution may
be distributed under that license.
