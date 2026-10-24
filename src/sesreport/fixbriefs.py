# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Agent-ready Markdown fix briefs, one per finding.

Every brief has the same seven sections (Problem, Affected resource, Why it
matters, Steps, Commands, Verify, Rollback) so a coding agent can pick it up and
apply the fix without opening the dashboard. Every command argument that comes
from AWS data is shell-quoted. The dashboard never runs these commands itself.
"""

from __future__ import annotations

import json
import shlex

SECTIONS = ("Problem", "Affected resource", "Why it matters", "Steps", "Commands", "Verify", "Rollback")
TRACKING_NOTE = (
    "Turning on OPEN and CLICK events makes SES add a 1x1 tracking pixel and rewrite links "
    "in mail sent through this configuration set. Confirm that is acceptable before running it."
)


def q(value: str) -> str:
    return shlex.quote(str(value))


def brief(title: str, *, problem: str, resource: str, why: str, steps: list[str],
          commands: list[str], verify: list[str], rollback: list[str], region: str = "") -> str:
    lines = [f"# Fix: {title}", "", "## Problem", problem, "", "## Affected resource",
             f"`{resource}`" + (f" in region `{region}`" if region else ""), "", "## Why it matters", why, "",
             "## Steps"]
    lines += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    lines += ["", "## Commands", "```sh", *commands, "```", "", "## Verify"]
    lines += [f"- {v}" for v in verify]
    lines += ["", "## Rollback"]
    lines += [f"- {r}" for r in rollback]
    return "\n".join(lines) + "\n"


def _region_flag(region: str) -> str:
    return f" --region {q(region)}" if region else ""


def routing_brief(config_set: str, region: str = "") -> str:
    dest = json.dumps({
        "Enabled": True,
        "MatchingEventTypes": ["BOUNCE", "COMPLAINT"],
        "CloudWatchDestination": {"DimensionConfigurations": [{
            "DimensionName": "ses:configuration-set", "DimensionValueSource": "messageTag",
            "DefaultDimensionValue": config_set}]},
    }, separators=(",", ":"))
    return brief(
        f"route BOUNCE/COMPLAINT events for {config_set}",
        problem=f"Configuration set `{config_set}` has no event destination for BOUNCE or COMPLAINT events.",
        resource=f"SES configuration set {config_set}", region=region,
        why="Complaints sent through this path never reach the feedback handler or the per-config-set "
            "CloudWatch metric, so ops cannot see them. SES still suppresses the address at account level.",
        steps=["Add a CloudWatch event destination that matches BOUNCE and COMPLAINT.",
               "If the ledger should also see these events, add an SNS destination to the ledger's feedback topic "
               "(see the SNS subscriptions command below)."],
        commands=[
            "aws sesv2 create-configuration-set-event-destination" + _region_flag(region) + " \\",
            f"  --configuration-set-name {q(config_set)} \\",
            "  --event-destination-name feedback-to-cloudwatch \\",
            f"  --event-destination {q(dest)}",
            "# find the ledger's feedback topic (the one the ledger Lambda subscribes to):",
            "aws sns list-subscriptions --query \"Subscriptions[?contains(Endpoint,'ledger')].TopicArn\"",
        ],
        verify=[f"`aws sesv2 get-configuration-set-event-destinations --configuration-set-name {q(config_set)}` "
                "lists BOUNCE and COMPLAINT.",
                "On the next dashboard refresh the coverage grid shows ticks for BOUNCE and COMPLAINT."],
        rollback=[f"`aws sesv2 delete-configuration-set-event-destination --configuration-set-name {q(config_set)} "
                  "--event-destination-name feedback-to-cloudwatch`"],
    )


def publishing_brief(config_set: str, missing: list[str], destination: dict | None, region: str = "") -> str:
    missing = sorted(missing)
    engagement = any(m in ("OPEN", "CLICK") for m in missing)
    if destination and destination.get("sns"):
        types = sorted(set((destination.get("event_types") or []) + missing))
        dest = json.dumps({"Enabled": True, "MatchingEventTypes": types,
                           "SnsDestination": {"TopicArn": destination["sns"]}}, separators=(",", ":"))
        cmds = ["aws sesv2 update-configuration-set-event-destination" + _region_flag(region) + " \\",
                f"  --configuration-set-name {q(config_set)} \\",
                f"  --event-destination-name {q(destination.get('name') or 'feedback-to-sns')} \\",
                f"  --event-destination {q(dest)}"]
        step = f"Add {', '.join(missing)} to the existing SNS destination `{destination.get('name')}`."
    else:
        dest = json.dumps({"Enabled": True, "MatchingEventTypes": ["BOUNCE", "COMPLAINT", *missing],
                           "SnsDestination": {"TopicArn": "LEDGER_TOPIC_ARN"}}, separators=(",", ":"))
        cmds = ["# replace LEDGER_TOPIC_ARN with the ledger feedback topic ARN:",
                "aws sns list-subscriptions --query \"Subscriptions[?contains(Endpoint,'ledger')].TopicArn\"",
                "aws sesv2 create-configuration-set-event-destination" + _region_flag(region) + " \\",
                f"  --configuration-set-name {q(config_set)} \\",
                "  --event-destination-name feedback-to-ledger \\",
                f"  --event-destination {q(dest)}"]
        step = "Create an SNS event destination on the ledger's feedback topic that matches the missing types."
    steps = [step, "Wait for the next sends through this config set, then refresh the dashboard."]
    if engagement:
        steps.insert(0, TRACKING_NOTE)
    return brief(
        f"publish {', '.join(missing)} events for {config_set}",
        problem=f"Configuration set `{config_set}` does not publish {', '.join(missing)} events to the ledger.",
        resource=f"SES configuration set {config_set}", region=region,
        why="The ledger counts deliveries, opens and clicks to compute per-provider rates. Without these events "
            "the Mailbox providers section has no denominator for sends through this path.",
        steps=steps, commands=cmds,
        verify=[f"`aws sesv2 get-configuration-set-event-destinations --configuration-set-name {q(config_set)}` "
                f"lists {', '.join(missing)}.",
                "Within a day the Mailbox providers section shows deliveries for this config set."],
        rollback=["Re-run the update with the previous MatchingEventTypes, or delete the destination created above."],
    )


def identity_delivery_brief(identity: str, topic: str, region: str = "") -> str:
    return brief(
        f"publish Delivery notifications for {identity}",
        problem=f"Identity `{identity}` sends Bounce and Complaint notifications to SNS but not Delivery notifications.",
        resource=f"SES identity {identity}", region=region,
        why="Sends made without a configuration set are only visible to the ledger through identity notifications. "
            "Without Delivery notifications their rates have no denominator.",
        steps=["Point the identity's Delivery notifications at the same topic as its Complaint notifications."],
        commands=["aws ses set-identity-notification-topic" + _region_flag(region) + " \\",
                  f"  --identity {q(identity)} \\", "  --notification-type Delivery \\", f"  --sns-topic {q(topic)}"],
        verify=[f"`aws ses get-identity-notification-attributes --identities {q(identity)}` shows a DeliveryTopic."],
        rollback=[f"`aws ses set-identity-notification-topic --identity {q(identity)} --notification-type Delivery` "
                  "(no --sns-topic) removes it."],
    )


def dkim_brief(identity: str, status: str, region: str = "") -> str:
    return brief(
        f"repair DKIM for {identity}",
        problem=f"Identity `{identity}` has DKIM status `{status or 'unknown'}`, not SUCCESS.",
        resource=f"SES identity {identity}", region=region,
        why="Mail without a valid DKIM signature fails DMARC alignment and is far more likely to be junked or "
            "rejected by Gmail, Yahoo and Microsoft.",
        steps=["Read the identity's DKIM tokens.",
               "Publish one CNAME per token: `<token>._domainkey.<domain>` → `<token>.dkim.amazonses.com`.",
               "Wait for SES to re-verify (up to 72 hours), or delete the identity if it is no longer used."],
        commands=[f"aws sesv2 get-email-identity --email-identity {q(identity)}{_region_flag(region)} "
                  "--query 'DkimAttributes'"],
        verify=["`DkimAttributes.Status` is SUCCESS and the dashboard's Authentication grid shows a tick."],
        rollback=["Remove the CNAME records added above."],
    )


def mail_from_brief(domain: str, region: str = "") -> str:
    mail_from = f"bounce.{domain}"
    return brief(
        f"set a custom MAIL FROM domain for {domain}",
        problem=f"Identity `{domain}` sends with the default MAIL FROM (amazonses.com).",
        resource=f"SES identity {domain}", region=region,
        why="SPF then aligns to amazonses.com instead of your domain, so DMARC passes only via DKIM. "
            "A custom MAIL FROM gives a second aligned pass.",
        steps=[f"Set MAIL FROM to `{mail_from}` with behaviour UseDefaultValue on MX failure.",
               f"Publish MX `{mail_from}` → `10 feedback-smtp.<region>.amazonses.com`.",
               f"Publish TXT `{mail_from}` → `\"v=spf1 include:amazonses.com ~all\"`."],
        commands=["aws sesv2 put-email-identity-mail-from-attributes" + _region_flag(region) + " \\",
                  f"  --email-identity {q(domain)} \\", f"  --mail-from-domain {q(mail_from)} \\",
                  "  --behavior-on-mx-failure USE_DEFAULT_VALUE"],
        verify=["`aws sesv2 get-email-identity` shows `MailFromAttributes.MailFromDomainStatus` SUCCESS."],
        rollback=[f"`aws sesv2 put-email-identity-mail-from-attributes --email-identity {q(domain)}` "
                  "(no --mail-from-domain) reverts to the default."],
    )


def spf_brief(mail_from: str) -> str:
    return brief(
        f"fix SPF for {mail_from}",
        problem=f"The MAIL FROM domain `{mail_from}` has no SPF record that includes amazonses.com.",
        resource=f"DNS TXT {mail_from}",
        why="SES's return-path servers are not authorised, so SPF fails and DMARC must rely on DKIM alone.",
        steps=[f"Publish (or edit) the TXT record on `{mail_from}` so it includes `include:amazonses.com`."],
        commands=[f"dig +short TXT {q(mail_from)}",
                  f"# expected after the fix: \"v=spf1 include:amazonses.com ~all\" on {mail_from}"],
        verify=[f"`dig +short TXT {q(mail_from)}` returns a v=spf1 record containing include:amazonses.com."],
        rollback=["Restore the previous TXT value."],
    )


def dmarc_brief(domain: str, finding: str) -> str:
    rec = f"_dmarc.{domain}"
    policy = {"missing": "has no DMARC record", "p=none": "has DMARC policy p=none (monitor only)",
              "no rua": "has DMARC without an rua= reporting address"}[finding]
    return brief(
        f"strengthen DMARC for {domain}",
        problem=f"`{domain}` {policy}.",
        resource=f"DNS TXT {rec}",
        why="Gmail and Yahoo bulk-sender rules require DMARC. A p=none policy lets spoofed mail through, and "
            "without rua= you receive no aggregate reports showing who sends as your domain.",
        steps=[f"Publish a TXT record on `{rec}`, starting with `v=DMARC1; p=none; rua=mailto:dmarc@{domain}` if none exists.",
               "After two weeks of clean aggregate reports, move to `p=quarantine`, then `p=reject`."],
        commands=[f"dig +short TXT {q(rec)}",
                  f"# target: \"v=DMARC1; p=quarantine; rua=mailto:dmarc@{domain}; adkim=s; aspf=r\""],
        verify=[f"`dig +short TXT {q(rec)}` returns the new record and the Authentication grid shows DMARC as pass."],
        rollback=["Lower the policy back to the previous value; never delete DMARC entirely."],
    )


def threshold_brief(group: str, name: str, metric: str, rate: float, deliveries: int, line: float) -> str:
    return brief(
        f"reduce the {metric} rate for {name}",
        problem=f"{group} `{name}` has a {metric} rate of {rate * 100:.3f}% over {deliveries:,} deliveries, "
                f"at or above the {line * 100:.2f}% line.",
        resource=f"{group} {name}",
        why="AWS reviews accounts at a 0.10% complaint or 5% bounce rate and can pause sending at 0.5% or 10%; "
            "mailbox providers throttle well before that.",
        steps=["Pull the recent complaint and bounce rows for this row from the dashboard's Records section (CSV).",
               "Find the list source or campaign behind them and stop sending to it.",
               "Add or tighten double opt-in and a visible unsubscribe link for that path.",
               "Suppress addresses that bounced or complained."],
        commands=["# export the ledger rows for review (read-only):",
                  "curl -s http://127.0.0.1:8787/api/report | jq '.complaint_records'"],
        verify=[f"Within 7 days the {metric} rate for `{name}` is under {line * 100:.2f}% on the dashboard."],
        rollback=["No AWS change is made by this brief."],
    )
