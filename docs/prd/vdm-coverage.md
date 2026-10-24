---
spec: spec-clarifier/v1
feature: vdm-coverage
title: Virtual Deliverability Manager coverage
status: complete
prompt: inline
prompt_sha256: 53391706d29223638a25fc7e0c95dad32e8d96474655bf6ef4662166e75eb708
created: 2026-10-24
---

# PRD: Virtual Deliverability Manager coverage

## 0. Source prompt

> I want to add some more requirements research https://docs.aws.amazon.com/ses/latest/dg/vdm.html understand it's functionality and identify gaps between what it offers and what we have, develop the functionality for identifyed gaps use stdio to find any skills that could help you with this /spec-clarifier


## 1. Problem

The operator watches complaint posture for one AWS account. Amazon SES Virtual Deliverability Manager (VDM) adds the breakdowns that matter most for that job: complaint and bounce rates per mailbox provider, per sending identity and per configuration set, engagement (open and click) rates, and an Advisor that flags DKIM, SPF, DMARC and threshold problems. VDM is billed per email sent. The operator has decided VDM stays off and nothing is paid for it (DL-001).

Today the tool reads only the `VdmAttributes.VdmEnabled` flag (`collect.py:collect_posture`) and shows "VDM is off". CloudWatch has no mailbox-provider dimension, so the operator cannot tell whether a complaint spike comes from Yahoo, Microsoft or Gmail, or which identity caused it. Evidence: the live account (us-east-1, 19 config sets, 40 identities, about 51,000 sends a month) has VDM `DISABLED`. Its self-owned ledger `ses_feedback_ledger` already holds 45 complaint and permanent-bounce rows with recipient domain, sending identity and config set. It records no deliveries, so no rate can be computed yet.

## 2. Target user

- **Primary:** the single operator of a self-hosted ses-complaint-report instance who owns SES deliverability for one AWS account (from repo: README "Who it's for").
- **Secondary:** a coding agent that receives a copied Markdown fix brief and applies the fix (DL-003).
- **Explicitly not for:** accounts that already pay for VDM. They get no VDM-sourced numbers from this release (DL-001).
- **Job-to-be-done:** When the complaint rate moves, I want to see which mailbox provider, identity and config set moved it, and what to fix, so I can act before AWS's 0.10% review line without paying for VDM.

## 3. Outcome and success metric

- **Outcome this moves:** the share of recorded complaints the operator can attribute to a mailbox provider and a sending identity.
- **Primary KPI:** provider-attributed complaint share. Definition: ledger complaint rows in the selected range whose recipient domain maps to a named provider (not "Other" or "Unknown"), divided by all ledger complaint rows in that range. Measured on the dashboard's Mailbox providers section.
- **Baseline:** 0% on 2026-10-24 (no provider grouping exists).
- **Target:** at least 90% within 7 days of the release reaching the operator's live instance.
- **Guardrail metrics:** a full refresh takes at most 10 seconds longer than before this release (`collect_ms`). The ledger stack's added AWS cost stays under 0.50 USD a month at 51,000 deliveries a month (A-008). No new AWS write permission on the dashboard profile.

## 4. Goals and non-goals

**Goals**
1. Show complaint and bounce counts and rates per mailbox provider, per sending identity and per config set, built from the ledger and with VDM off (DL-002).
2. Show open and click rates per mailbox provider (DL-002).
3. Run a built-in Advisor (DKIM, SPF with custom MAIL FROM, DMARC, complaint and bounce thresholds) and feed its findings into Needs attention (DL-006).
4. Give every finding a Fix button that copies a Markdown brief another agent can implement (DL-003).

**Non-goals**
- Using, enabling or reading VDM data, and any VDM charge (DL-001).
- Inbox placement and seed-list testing (DL-005).
- IP blocklist monitoring: sends go out on SES shared IPs (DL-005).
- Per-message insights and per-message delivery history (DL-005).
- SES tenant dimension breakdowns (DL-005).
- Any dashboard action that changes AWS state. The tool stays read-only (DL-003).

## 5. User stories and acceptance criteria

- **[P0]** As the operator, I want the ledger to count deliveries per recipient domain, sending identity and config set each day, so that rates have a denominator.
  - [ ] Given an SES Delivery notification for 3 recipients at gmail.com sent from example.com through config set `transactional` on 2026-10-24, when the ledger Lambda processes it, then the counter item for 2026-10-24, gmail.com, example.com, `transactional`, DELIVERY increases by 3 and no item stores a recipient address.
  - [ ] Given the same SNS message is delivered twice, when the Lambda processes the duplicate, then the counter does not increase a second time (dedupe on the SES message ID, A-001).
  - [ ] Given a Delivery event with no config-set tag, when it is counted, then the config-set key is `(none)` and the event still counts toward its provider and identity.
- **[P0]** As the operator, I want a Mailbox providers table, so that I see which provider drives complaints.
  - [ ] Given ledger data in the selected Range, when the dashboard renders, then each provider row shows deliveries, complaints, complaint rate, permanent bounces and bounce rate, sorted by deliveries descending.
  - [ ] Given a provider with fewer than 1,000 deliveries in the Range, when its row renders, then the rate cells read "too few deliveries" and the counts are still shown (DL-007).
  - [ ] Given recipient domain `outlook.com`, `hotmail.com` or a domain whose MX ends in `protection.outlook.com`, when it is classified, then it maps to provider "Microsoft" (DL-004).
  - [ ] Given the Range filter changes from All to 30d, when the section re-renders, then every count and rate in it uses only the last 30 days (DL-008).
- **[P0]** As the operator, I want the same breakdown per sending identity and per config set, so that I can see which sender caused a complaint.
  - [ ] Given ledger data, when the dashboard renders, then an identity table and a config-set table show the same five columns as the provider table and obey the same 1,000-delivery rule.
- **[P0]** As the operator, I want an Advisor for DKIM, SPF with custom MAIL FROM and DMARC, so that authentication gaps reach Needs attention.
  - [ ] Given an identity whose SES DKIM status is not SUCCESS, when the Advisor runs, then Needs attention lists it with severity high and a Fix brief.
  - [ ] Given a sending domain without a custom MAIL FROM, or whose MAIL FROM SPF TXT lacks `include:amazonses.com`, when the Advisor runs, then a medium finding names the domain.
  - [ ] Given a sending domain with no `_dmarc` TXT record, when the Advisor runs, then a high finding names it. Given `p=none`, then a medium finding names it. Given no `rua=` tag, then an info finding names it.
  - [ ] Given a DNS lookup times out after 2 seconds, when the Advisor runs, then that check reads "not checked: DNS timeout" and produces no finding.
- **[P0]** As the operator, I want threshold recommendations, so that a provider, identity or config set nearing AWS's lines is flagged.
  - [ ] Given a row with at least 1,000 deliveries and a complaint rate of 0.08% or more, when the Advisor runs, then a medium finding appears. At 0.10% or more it is high (A-005).
  - [ ] Given a row with at least 1,000 deliveries and a bounce rate of 5% or more, when the Advisor runs, then a medium finding appears. At 10% or more it is high (A-005).
- **[P0]** As the operator, I want every Fix button to copy a Markdown brief, so that another agent can implement the fix.
  - [ ] Given any finding with a Fix button, when it is clicked, then the clipboard holds Markdown with these sections: Problem, Affected resource, Why it matters, Steps, Commands (fenced, shell-quoted), Verify, Rollback. A toast names the resource (A-006).
  - [ ] Given the existing config-set routing gap findings, when their Fix button is clicked, then the brief embeds the existing `aws sesv2 create-configuration-set-event-destination` command (A-006).
- **[P0]** As the operator, I want open and click rates per provider, so that I see engagement like VDM shows it.
  - [ ] Given Open and Click events in the ledger, when the provider table renders, then it shows open rate (messages opened at least once, divided by deliveries) and click rate the same way, with the label "opens inflated by Apple Mail Privacy Protection" (A-003).
- **[P0]** As the operator, I want to see which config sets and identities do not publish Delivery, Open or Click events, so that I know why rates are missing.
  - [ ] Given a config set whose event destinations lack DELIVERY, OPEN or CLICK, when the dashboard renders, then the coverage grid marks the gap and Needs attention offers a Fix brief that adds the event types to the existing destination (DL-003).
  - [ ] Given an identity notification topic without Delivery notifications, when the dashboard renders, then an info finding offers a Fix brief that uses `aws ses set-identity-notification-topic --notification-type Delivery`.
- **[P2]** As a new user in demo mode, I want synthetic provider, engagement and Advisor data, so that I can see the feature without AWS.
  - [ ] Given `SESREPORT_DEMO=1`, when the dashboard renders, then the Mailbox providers, identity and config-set tables and at least 3 Advisor findings show synthetic data.
- **[P0]** As the operator, I want the section to state why data is missing, so that empty tables are never ambiguous.
  - [ ] Given the ledger is not configured, when the dashboard renders, then the section says "Ledger not configured" and links to the ledger setup in README.
  - [ ] Given the ledger has complaints but zero DELIVERY counters, when the dashboard renders, then rates read "no delivery data yet" and a Fix brief explains how to enable Delivery publishing.
  - [ ] Given the ledger read fails, when the dashboard renders, then a collector warning names section `ledger_aggregates` and the rest of the report renders.

## 6. Experience notes

- **Surface:** the existing single-page dashboard (`src/sesreport/static/index.html`), Ocean Depths theme, light and dark (from repo).
- **Placement:** a new section "Mailbox providers" between Attribution and Records, with an entry in the section nav. It holds the provider table, then the identity and config-set tables inside a closed details element (A-007).
- **Advisor:** findings join the existing Needs attention list, using its severity ranking (high, medium, info). The Posture section gains an Authentication grid: one row per sending domain, with DKIM, SPF/MAIL FROM and DMARC cells using the coverage-grid tick and cross marks (A-007).
- **Range:** the existing Range control scopes the new section (DL-008).
- **States:** ledger not configured, no delivery data yet, below the 1,000-delivery floor, DNS timeout, and read error, as written in §5.
- **Accessibility:** the same bar as the current page. axe-core reports 0 violations in light and dark at 1440 px and 390 px, and nothing scrolls horizontally at 320 px through 1440 px (from repo: tests/ui/dashboard-hardening.spec.mjs).

## 7. Scope

**In scope:** the ledger Lambda change (counting Delivery, Open and Click), the collector reading counters and running DNS checks, the provider mapping, the Advisor, the Markdown fix briefs (including the existing fix buttons), the new UI section, demo data, README and IAM updates.

**Out of scope:** everything listed under Non-goals in §4.

**Dependencies:** the ledger stack (`infra/ledger-stack.yaml`, `infra/deploy_ledger.sh`) is redeployed once with the new Lambda code. The operator runs the copied Fix briefs that turn on Delivery, Open and Click publishing.

**Constraints:**
- The dashboard's AWS profile stays read-only. The only new read permissions are `dynamodb:Query` on the ledger table, which it already holds, and `ses:GetEmailIdentity`, which it already holds (from repo: collect.py).
- VDM stays off. No call to `BatchGetMetricData`, `ListRecommendations`, `PutAccountVdmAttributes` or `CreateExportJob` (DL-001).
- Open and Click events exist only for sends made through a config set with an open or click event destination. Sends without a config set get delivery counts but no engagement (SES behavior).
- Ledger privacy rules hold: counters store recipient domains, never addresses (from repo: infra/ledger_ingest/index.py docstring).

## 8. Risks and open questions

| ID | Risk or question | Type | Owner | Resolve by |
|---|---|---|---|---|
| R-001 | Enabling open and click tracking adds a tracking pixel and rewrites links in the operator's mail | Risk | operator | before running the engagement Fix brief |
| R-002 | Apple Mail Privacy Protection inflates open rates | Risk | product | labelled in UI per A-003 |
| R-003 | MX-based classification misses providers that front mail with a gateway | Risk | product | unknown MX maps to Other per A-002 |

## 9. Instrumentation

| Event | Trigger | Properties | Purpose |
|---|---|---|---|
| ledger_aggregates collected | each refresh | days read, counter items, ms | refresh-time guardrail |
| provider_classification | each refresh | domains classified, from map, from MX, unknown | primary KPI denominator check |
| advisor_run | each refresh | checks run, findings by severity, DNS timeouts | Advisor health |

These are log lines and fields in the report model, not third-party analytics (from repo: the tool has no analytics).

## 10. Rollout and rollback

- **Strategy:**
  1. Ship through a PR (from repo: CLAUDE.md workflow).
  2. Redeploy the ledger stack with `infra/deploy_ledger.sh`.
  3. Run the copied Fix briefs to publish Delivery, then Open and Click.
  4. Rates appear once a provider passes 1,000 deliveries.
- **Rollback trigger:** a refresh that takes more than 10 seconds longer than the 8.1 second baseline, or ledger Lambda errors above 1% of invocations over 1 hour.
- **Rollback:** redeploy the previous Lambda version (CloudFormation stack rollback). The dashboard degrades to the "no delivery data yet" state. Counter items expire on their TTL.
- **Migration:** none. Counter items are new item types in the existing table, with the existing TTL attribute.

## 11. Compliance gates

- [x] Privacy: counters keep recipient domains only. Dedupe markers keep the SES message ID only, which identifies no person, and expire after 30 days (A-001).
- [x] Tracking disclosure: R-001 is shown in the engagement Fix brief before the commands.
- No other gates apply, because no payment, no new personal data store and no new external recipient is added.

## 12. Data and integrations

- **Ledger counter items** (existing table `ses_feedback_ledger`, A-001): partition key `AGG#<yyyy-mm-dd>`, sort key `<recipient-domain>#<sending-identity>#<config-set>#<EVENT>` where EVENT is DELIVERY, OPEN_UNIQUE or CLICK_UNIQUE, a numeric `n` incremented with `ADD`, and the existing TTL of RETENTION_DAYS (396 days).
- **Dedupe markers:** partition key `SEEN#<ses-message-id>`, sort key `<EVENT>`, TTL 30 days. A conditional put that fails means the event was already counted.
- **Complaints and bounces:** unchanged ledger rows (from repo: records_for_event). Rates use them as numerators.
- **Provider map:** a static JSON map in `src/sesreport/providers.json` (consumer domains to provider) plus an MX cache file `providers-mx-cache.json` in the data directory, refreshed every 7 days (A-002).
- **Integrations:** DynamoDB Query through the existing boto3 client. Public DNS (MX, TXT) through `dnspython` with a 2 second timeout (A-002). The SES v2 `GetEmailIdentity` call already made per identity. Tests replace DNS with a stub resolver and DynamoDB with in-memory fakes (from repo: tests/test_ledger_ingest.py pattern).

## 13. Non-functional requirements

- A full refresh adds at most 10 seconds at 51,000 deliveries a month and 40 identities.
- DNS: each lookup times out at 2 seconds, at most 120 lookups per refresh, and results are cached 7 days.
- Counter reads: past days are cached in the data directory and only the last 2 days are re-read on each refresh, so a steady-state refresh reads at most 2 DynamoDB partitions.
- The ledger Lambda keeps p95 duration under 300 ms per SNS message.
- Added AWS cost stays under 0.50 USD a month at 51,000 deliveries a month (A-008).
- The rate floor is 1,000 deliveries per row in the selected Range (DL-007).

## 14. Assumptions

| ID | Dimension | Assumption | Why this default | Overturn if |
|---|---|---|---|---|
| A-001 | data | Delivery, Open and Click are stored as daily counter items keyed by recipient domain, sending identity and config set, plus 30-day dedupe markers keyed by SES message ID; no per-message delivery rows and no addresses | Matches the ledger privacy rule and keeps storage to a few hundred items a day | Per-message insights are wanted later |
| A-002 | integrations | MX classification runs in the dashboard collector with dnspython, a 2 s timeout and a 7-day cache; unknown or failed lookups map to Other | Keeps the Lambda dependency-free; dnspython is a small ISC-licensed pure-Python library | The project forbids new dependencies |
| A-003 | outputs | Open rate counts messages opened at least once divided by deliveries, click rate likewise, labelled as inflated by Apple Mail Privacy Protection | Mirrors VDM's DELIVERY_OPEN and DELIVERY_CLICK definitions | Raw event counts are preferred |
| A-004 | outputs | The hero verdict keeps CloudWatch Reputation.ComplaintRate as the primary rate; ledger rates appear only in the new section | AWS enforces on Reputation.ComplaintRate | The operator wants the ledger rate as primary |
| A-005 | errors | Thresholds are complaint 0.08% medium and 0.10% high, bounce 5% medium and 10% high | The internal alert line and AWS's published review and probation lines | Different alert lines are configured |
| A-006 | outputs | Every Fix button, including the existing routing fixes, copies one Markdown brief (Problem, Affected resource, Why it matters, Steps, Commands, Verify, Rollback) with commands embedded | DL-003 asked for agent-ready Markdown from the fix button | A separate copy-command button is wanted |
| A-007 | interface | A new Mailbox providers section sits between Attribution and Records, and an Authentication grid is added to Posture | Keeps the page's story order: what happened, who, where to fix | A separate page is preferred |
| A-008 | non_functional | Added AWS cost is SNS, Lambda and DynamoDB on-demand writes for about 51,000 deliveries plus opens and clicks a month, under 0.50 USD | SNS and Lambda first 1M free; DynamoDB about 1.25 USD per million writes | Volume grows past 300,000 deliveries a month |
| A-009 | rollout | The success target is measured 7 days after the operator redeploys the ledger and enables Delivery publishing | Rates need a week of deliveries to pass the floor | Enablement is delayed |
| A-010 | permissions | No new roles or auth: the dashboard stays a single-user loopback tool | From repo: app.py binds 127.0.0.1 with a host allowlist | Multi-user access is added |

## 15. Decision log

| ID | Dimension | Question | Options offered | Answer | Applied in |
|---|---|---|---|---|---|
| DL-001 | purpose | Which build is meant by developing the functionality for the identified VDM gaps? | A. Read-only VDM integration B. VDM-off guidance only C. Integration plus enable from dashboard D. Integration plus per-message insights | Custom: do not use VDM; build VDM's functionality into this software; pay nothing for VDM; VDM stays off | §1, §4, §7 |
| DL-002 | purpose | Which VDM-equivalent build without VDM? | A. Rates plus Advisor B. Counts plus Advisor at zero new cost C. Rates plus Advisor plus engagement D. Advisor only | C | §4, §5, §7 |
| DL-003 | constraints | Who turns on Delivery, Open and Click publishing given the read-only dashboard? | A. Copy-fix CLI commands B. Ledger deploy script C. Dashboard button | Custom: the Fix button copies Markdown instructions describing exactly how to implement the fix, for another agent to pick up | §2, §4, §5 |
| DL-004 | inputs | How a recipient address maps to a mailbox provider? | A. Domain map plus MX lookup B. Static domain map only C. Raw recipient domain | A | §5, §12 |
| DL-005 | non_goals | Which VDM features stay out of this release? | A. Inbox placement and seed tests B. IP blocklist monitoring C. Per-message insights D. Tenant dimension | All four excluded | §4, §7 |
| DL-006 | outputs | Which Advisor checks run? | A. DKIM per identity B. SPF plus custom MAIL FROM C. DMARC presence and policy D. Complaint and bounce thresholds | A, B, C and D | §4, §5 |
| DL-007 | outputs | When a per-row rate is shown? | A. At least 1,000 deliveries B. Always with sample size C. At least 100 deliveries | A | §5, §13 |
| DL-008 | triggers | Which time window the breakdowns use? | A. Reuse the page Range filter B. Fixed last 30 days C. Own 7, 30 and 60 day selector | A | §5, §6 |
| DL-009 | acceptance | Which stories must ship in the first release? | A. Rates core B. Advisor auth checks C. Markdown fix briefs D. Engagement plus identity and config-set breakdowns | A, B, C and D; demo data stays P2 | §5 |

## Coverage

| Dimension | Status | Ref |
|---|---|---|
| purpose | specified | §1, §3 |
| actors | specified | §2 |
| permissions | assumed | §7, A-010 |
| triggers | specified | §5, §6 |
| inputs | specified | §5, §12 |
| outputs | specified | §5, A-003, A-004, A-006 |
| states | specified | §5, §6 |
| errors | specified | §5, A-005 |
| interface | assumed | §6, A-007 |
| data | assumed | §12, A-001 |
| integrations | assumed | §12, A-002 |
| non_functional | specified | §13, A-008 |
| constraints | specified | §7, §11 |
| non_goals | out-of-scope | DL-005 |
| acceptance | specified | §5, DL-009 |
| rollout | assumed | §9, §10, A-009 |
