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
import re
import shlex

SECTIONS = ("Problem", "Affected resource", "Why it matters", "Steps", "Commands", "Verify", "Rollback")
TRACKING_NOTE = (
    "Turning on OPEN and CLICK events makes SES add a 1x1 tracking pixel and rewrite links "
    "in mail sent through this configuration set. Confirm that is acceptable before running it."
)


def q(value: str) -> str:
    return shlex.quote(str(value))


def _n(value) -> str:
    """A name as it may appear in Markdown: no backticks, no line breaks."""
    return " ".join(str(value).replace("`", "'").split())


def brief(title: str, *, problem: str, resource: str, why: str, steps: list[str],
          commands: list[str], verify: list[str], rollback: list[str], region: str = "") -> str:
    title, resource = _n(title), _n(resource)
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


LEDGER_HINT = ("# the ledger's feedback topic is any topic the ledger Lambda subscribes to:",
               "aws sns list-subscriptions --query \"Subscriptions[?contains(Endpoint,'ledger-ingest')].TopicArn\"")


def _ledger_topic_lines(ledger_topics: list[str] | None) -> tuple[str, list[str]]:
    """The topic ARN to use in a command, and the comment lines that explain it."""
    if ledger_topics:
        return ledger_topics[0], [f"# a topic the ledger already listens to: {_n(ledger_topics[0])}"]
    return "LEDGER_TOPIC_ARN", ["# replace LEDGER_TOPIC_ARN with the ledger feedback topic ARN:", *LEDGER_HINT]


def routing_brief(config_set: str, region: str = "", ledger_topics: list[str] | None = None,
                  covered_by: list[str] | None = None, usage: str = "") -> str:
    config_set_md = _n(config_set)
    if covered_by:
        topic = "LEDGER_TOPIC_ARN"
        topic_lines = [
            "# Optional hardening only. Create a dedicated topic for ledger feedback;",
            "# do not reuse an ops or alert topic: SES bounce and complaint payloads",
            "# include recipient addresses, and an email subscriber would receive them raw.",
            "LEDGER_TOPIC_ARN=$(aws sns create-topic --name ses-complaint-feedback-ledger"
            + _region_flag(region) + " --query TopicArn --output text)",
            "LEDGER_ARN=$(aws lambda get-function --function-name ses-complaint-report-ledger-ingest"
            + _region_flag(region) + " --query Configuration.FunctionArn --output text)",
            "aws sns subscribe --topic-arn \"$LEDGER_TOPIC_ARN\" --protocol lambda "
            "--notification-endpoint \"$LEDGER_ARN\"" + _region_flag(region),
            "# replace LEDGER_TOPIC_ARN below with the ARN created above",
        ]
        title = f"optionally route BOUNCE/COMPLAINT events for {config_set_md}"
        problem = (f"Configuration set `{config_set_md}` has no enabled SNS or Firehose event destination for BOUNCE "
                   "or COMPLAINT events. The identities that currently send through it already route both through "
                   "identity-level notifications, so no complaint is currently going unseen.")
        why = (f"Mail from {', '.join(_n(c) for c in covered_by)} is already covered by identity-level notifications, "
               "which fire whatever configuration set the message used. This change is optional defense-in-depth: it "
               "only matters if another identity starts sending through this set. Do not add delivery events here; "
               "delivery has its own publishing brief.")
        steps = [
            "Optional: create a dedicated SNS topic for this path and subscribe the ledger ingest Lambda. Do not "
            "reuse an ops or alert topic, because an email subscriber would receive raw bounce and complaint "
            "payloads including recipient addresses.",
            "Add a BOUNCE/COMPLAINT-only event destination on the set, pointing at the dedicated topic. Do not "
            "include delivery events; delivery has its own publishing brief.",
            "If the sending app needs the events too, subscribe its handler to the same dedicated topic rather than "
            "creating a second path.",
            f"No action is needed while only {', '.join(_n(c) for c in covered_by)} sends through this set.",
        ]
        verify = [
            f"`aws sesv2 get-configuration-set-event-destinations --configuration-set-name {q(config_set)}` lists "
            "BOUNCE and COMPLAINT on the dedicated topic.",
            "The destination does not publish delivery events.",
            "The ledger dedupes the identity-level copy by SES message ID, so the extra feed does not double-count.",
        ]
        rollback = [
            f"`aws sesv2 delete-configuration-set-event-destination --configuration-set-name {q(config_set)} "
            "--event-destination-name feedback-to-ledger`",
            "Unsubscribe the ledger Lambda and delete the dedicated topic if nothing else uses it.",
        ]
    else:
        topic, topic_lines = _ledger_topic_lines(ledger_topics)
        title = f"route BOUNCE/COMPLAINT events for {config_set_md}"
        problem = (f"Configuration set `{config_set_md}` has no enabled SNS or Firehose event destination for BOUNCE "
                   "or COMPLAINT events. A CloudWatch destination only counts them; nothing hears them.")
        why = ("Complaints sent through this path never reach a feedback handler or the ledger, so ops cannot see "
               "them and the per-provider rates have no numerator. SES still suppresses the address at account level.")
        if usage == "idle":
            why = ("Nothing has used this set in the last 14 days, so this is a latent gap: it becomes real on the "
                   "first send.")
        steps = [
            "Add a BOUNCE/COMPLAINT-only SNS event destination on a ledger-subscribed topic. Do not add delivery " +
            "events here; delivery has its own publishing brief.",
            "If the sending app needs the events too, subscribe its handler to the same topic rather than creating "
            "a second path.",
        ]
        verify = [
            f"`aws sesv2 get-configuration-set-event-destinations --configuration-set-name {q(config_set)}` lists "
            "BOUNCE and COMPLAINT on a ledger-subscribed topic.",
            "The destination does not publish delivery events.",
            "On the next dashboard refresh the coverage grid shows this set as routed.",
        ]
        rollback = [f"`aws sesv2 delete-configuration-set-event-destination --configuration-set-name {q(config_set)} "
                    "--event-destination-name feedback-to-ledger`"]

    dest = json.dumps({"Enabled": True, "MatchingEventTypes": ["BOUNCE", "COMPLAINT"],
                       "SnsDestination": {"TopicArn": topic}}, separators=(",", ":"))
    return brief(
        title,
        problem=problem,
        resource=f"SES configuration set {config_set_md}", region=region,
        why=why,
        steps=steps,
        commands=[*topic_lines,
                  "aws sesv2 create-configuration-set-event-destination" + _region_flag(region) + " \\",
                  f"  --configuration-set-name {q(config_set)} \\",
                  "  --event-destination-name feedback-to-ledger \\",
                  f"  --event-destination {q(dest)}"],
        verify=verify,
        rollback=rollback,
    )


def ledger_gap_brief(config_set: str, feedback_topics: list[str], region: str = "") -> str:
    config_set_md = _n(config_set)
    topic = feedback_topics[0] if feedback_topics else "TOPIC_ARN"
    return brief(
        f"let the ledger see feedback for {config_set_md}",
        problem=f"Configuration set `{config_set_md}` routes BOUNCE/COMPLAINT to "
                f"{', '.join(_n(t) for t in feedback_topics) or 'a destination'}, but the ledger Lambda is not "
                "subscribed there.",
        resource=f"SES configuration set {config_set_md}", region=region,
        why="The app that owns the topic hears the complaint, but the ledger does not, so this path has no "
            "per-provider rates and its complaints are missing from the recipient-level records.",
        steps=[("Subscribe the ledger ingest Lambda to the set's feedback topic (the ledger stack already grants "
                "SNS invoke for the whole account)."),
               "Check the topic's other subscribers tolerate nothing new: the subscription adds a reader, not events."],
        commands=["LEDGER_ARN=$(aws lambda get-function --function-name ses-complaint-report-ledger-ingest"
                  + _region_flag(region) + " --query Configuration.FunctionArn --output text)",
                  f"aws sns subscribe --topic-arn {q(topic)} --protocol lambda --notification-endpoint \"$LEDGER_ARN\""
                  + _region_flag(region)],
        verify=[f"`aws sns list-subscriptions-by-topic --topic-arn {q(topic)}` lists the ledger Lambda.",
                "The coverage grid shows the set as reaching the ledger on the next refresh."],
        rollback=["`aws sns unsubscribe --subscription-arn <arn>` for the subscription created above."],
    )


def publishing_brief(config_set: str, missing: list[str], destination: dict | None, region: str = "",
                     ledger_topics: list[str] | None = None, usage: str = "") -> str:
    """Publish the missing event types to a ledger-subscribed topic.

    ``destination`` is only passed when its topic already reaches the ledger;
    an ops/alert topic must never be edited (every delivery would page someone),
    so otherwise a new destination on a ledger topic is created.
    """
    config_set_md = _n(config_set)
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
        step = f"Add {', '.join(missing)} to the existing SNS destination `{_n(destination.get('name'))}`, which already feeds the ledger."
    else:
        topic, topic_lines = _ledger_topic_lines(ledger_topics)
        types = sorted({"BOUNCE", "COMPLAINT", "DELIVERY", *missing})
        dest = json.dumps({"Enabled": True, "MatchingEventTypes": types,
                           "SnsDestination": {"TopicArn": topic}}, separators=(",", ":"))
        cmds = [*topic_lines,
                "aws sesv2 create-configuration-set-event-destination" + _region_flag(region) + " \\",
                f"  --configuration-set-name {q(config_set)} \\",
                "  --event-destination-name feedback-to-ledger \\",
                f"  --event-destination {q(dest)}"]
        step = ("Create a new SNS event destination on a ledger-subscribed topic. Do not add DELIVERY to an existing "
                "ops/alert topic: its subscribers would be notified on every delivery.")
    steps = [step, "Wait for the next sends through this config set, then refresh the dashboard."]
    if engagement:
        steps.insert(0, TRACKING_NOTE)
    why = ("The ledger counts deliveries to compute per-provider complaint and bounce rates. Without DELIVERY "
           "events the Mailbox providers section has no denominator for sends through this path.")
    if usage == "idle":
        why += " Nothing used this set in the last 14 days, so this is a latent gap."
    return brief(
        f"publish {', '.join(missing)} events for {config_set_md}",
        problem=f"Configuration set `{config_set_md}` does not publish {', '.join(missing)} events to a topic the ledger reads.",
        resource=f"SES configuration set {config_set_md}", region=region,
        why=why,
        steps=steps, commands=cmds,
        verify=[f"`aws sesv2 get-configuration-set-event-destinations --configuration-set-name {q(config_set)}` "
                f"lists {', '.join(missing)} on a ledger-subscribed topic.",
                "Within a day the Mailbox providers section shows deliveries for this config set."],
        rollback=["Re-run the update with the previous MatchingEventTypes, or delete the destination created above."],
    )


def tracking_brief(config_set: str, destination: dict, types: list[str], region: str = "") -> str:
    config_set_md = _n(config_set)
    keep = sorted(t for t in (destination.get("event_types") or []) if t not in ("OPEN", "CLICK"))
    dest = json.dumps({"Enabled": True, "MatchingEventTypes": keep,
                       "SnsDestination": {"TopicArn": destination.get("sns")}}, separators=(",", ":"))
    return brief(
        f"confirm engagement tracking on {config_set_md}",
        problem=f"Configuration set `{config_set_md}` publishes {', '.join(types)} events, so SES adds a tracking pixel"
                + (" and rewrites every link through its redirect domain" if "CLICK" in types else "") + ".",
        resource=f"SES configuration set {config_set_md}", region=region,
        why="Link rewriting breaks magic links and signed URLs when the redirect host is blocked, and tracking "
            "pixels are a privacy disclosure. This is a note, not a fault: keep it if engagement data is wanted here.",
        steps=["Decide whether engagement tracking is intended for this path.",
               f"If not, remove {', '.join(types)} from destination `{_n(destination.get('name'))}` (command below)."],
        commands=["aws sesv2 update-configuration-set-event-destination" + _region_flag(region) + " \\",
                  f"  --configuration-set-name {q(config_set)} \\",
                  f"  --event-destination-name {q(destination.get('name') or '')} \\",
                  f"  --event-destination {q(dest)}"],
        verify=["Links in a test message are no longer rewritten through awstrack.me."],
        rollback=["Re-run the update with the previous MatchingEventTypes."],
    )


def identity_feedback_gap_brief(identity: str, topic: str, subscribers: list[str], region: str = "") -> str:
    identity_md = _n(identity)
    return brief(
        f"give {identity_md}'s feedback a handler",
        problem=f"Identity `{identity_md}` sends Bounce/Complaint notifications to `{_n(topic)}`, whose only "
                f"subscribers are {', '.join(_n(s) for s in subscribers) or 'none'}. No handler or ledger reads it.",
        resource=f"SES identity {identity_md}", region=region,
        why="A complaint lands in an inbox and is never recorded, so the address keeps receiving mail from the app "
            "and the complaint is missing from every rate on this dashboard.",
        steps=["Subscribe the ledger ingest Lambda to the topic so every event is recorded.",
               "If the sending app has a feedback endpoint, subscribe it too so it suppresses the address."],
        commands=["LEDGER_ARN=$(aws lambda get-function --function-name ses-complaint-report-ledger-ingest"
                  + _region_flag(region) + " --query Configuration.FunctionArn --output text)",
                  f"aws sns subscribe --topic-arn {q(topic)} --protocol lambda --notification-endpoint \"$LEDGER_ARN\""
                  + _region_flag(region)],
        verify=[f"`aws sns list-subscriptions-by-topic --topic-arn {q(topic)}` lists the ledger Lambda."],
        rollback=["`aws sns unsubscribe --subscription-arn <arn>` for the subscription created above."],
    )


def identity_unverified_brief(identity: str, status: str, region: str = "") -> str:
    identity_md = _n(identity)
    return brief(
        f"re-verify or delete {identity_md}",
        problem=f"Identity `{identity_md}` has verification status `{_n(status) or 'unknown'}`; SES will not send from it.",
        resource=f"SES identity {identity_md}", region=region,
        why="This is housekeeping, not a sending risk: SES already refuses mail from an unverified identity. "
            "Authentication checks are skipped for it because none of them can apply.",
        steps=[("If the domain is still yours and should send, publish the DKIM CNAMEs SES asks for and wait for "
                "re-verification."), "Otherwise delete the identity so it stops appearing here."],
        commands=[f"aws sesv2 get-email-identity --email-identity {q(identity)}{_region_flag(region)} "
                  "--query 'DkimAttributes.Tokens'",
                  "# only if the domain is no longer used:",
                  f"aws sesv2 delete-email-identity --email-identity {q(identity)}{_region_flag(region)}"],
        verify=["The identity is SUCCESS, or gone from `aws sesv2 list-email-identities`."],
        rollback=["Re-create the identity with `aws sesv2 create-email-identity` and verify it again."],
    )


def unattributed_sends_brief(identities: list[str], share: dict, region: str = "") -> str:
    names = [_n(i) for i in identities]
    first = identities[0] if identities else "IDENTITY"
    return brief(
        "give every sending identity a default configuration set",
        problem=f"In the last {share.get('days', 14)} days {share.get('unattributed') or 0:,} of "
                f"{share.get('account_sends') or 0:,} sends carried no configuration set, so no event destination "
                f"saw their bounces or complaints. {len(names)} identities can send without one: {', '.join(names) or 'none'}.",
        resource="SES account", region=region,
        why="Feedback for those messages arrives only as forwarded email (if forwarding is on). It never reaches "
            "a handler or the ledger, so the address is not suppressed by the app and the complaint is missing "
            "from every rate here except SES's own.",
        steps=[("For each identity, set a default configuration set that routes BOUNCE, COMPLAINT and DELIVERY to a "
                "ledger-subscribed topic. Sends that name a set explicitly are unaffected."),
               "Alternatively, point the identity's own Bounce and Complaint notifications at such a topic."],
        commands=["aws sesv2 put-email-identity-configuration-set-attributes" + _region_flag(region) + " \\",
                  f"  --email-identity {q(first)} \\", "  --configuration-set-name CONFIG_SET_WITH_FEEDBACK",
                  "# repeat for each identity listed above"],
        verify=["On the next refresh the unattributed share in Needs attention falls, and `aws sesv2 get-email-identity` "
                "shows a ConfigurationSetName for each identity."],
        rollback=["`put-email-identity-configuration-set-attributes --email-identity <id>` with no set name removes the default."],
    )


def sns_pending_brief(topic: str, endpoints: list[str], region: str = "") -> str:
    return brief(
        f"confirm pending subscriptions on {_n(topic)}",
        problem=f"Topic `{_n(topic)}` has unconfirmed subscriptions: {', '.join(_n(e) for e in endpoints)}. "
                "They receive nothing until confirmed.",
        resource=f"SNS topic {_n(topic)}", region=region,
        why="A feedback handler that never confirmed its subscription silently misses every complaint.",
        steps=[("Have the endpoint confirm (an https endpoint receives a SubscriptionConfirmation POST; a Lambda "
                "confirms automatically once it has invoke permission)."), "Remove subscriptions nobody owns."],
        commands=[f"aws sns list-subscriptions-by-topic --topic-arn {q(topic)}{_region_flag(region)}"],
        verify=["No subscription on the topic shows `PendingConfirmation`."],
        rollback=["None: this brief only confirms or removes subscriptions."],
    )


def dkim_key_brief(identity: str, region: str = "") -> str:
    identity_md = _n(identity)
    return brief(
        f"rotate {identity_md} to a 2048-bit DKIM key",
        problem=f"Identity `{identity_md}` signs with a 1024-bit RSA key.",
        resource=f"SES identity {identity_md}", region=region,
        why="1024-bit keys are below current guidance (RFC 8301 recommends 2048). Mail still passes today, so this "
            "is a note.",
        steps=[("Ask SES for a 2048-bit key; SES publishes new tokens and keeps signing with the old key until "
                "the new CNAMEs resolve."), "Publish the three new CNAMEs SES returns."],
        commands=[f"aws sesv2 put-email-identity-dkim-signing-attributes --email-identity {q(identity)}"
                  f"{_region_flag(region)} --signing-attributes-origin AWS_SES --signing-attributes NextSigningKeyLength=RSA_2048_BIT"],
        verify=["`DkimAttributes.CurrentSigningKeyLength` is RSA_2048_BIT after the CNAMEs propagate."],
        rollback=["Repeat with `NextSigningKeyLength=RSA_1024_BIT`."],
    )


def email_identity_brief(identity: str, region: str = "") -> str:
    identity_md = _n(identity)
    domain = identity.rsplit("@", 1)[-1]
    return brief(
        f"stop sending as {identity_md} through SES",
        problem=f"`{identity_md}` is a verified email-address identity whose domain `{_n(domain)}` is not a verified "
                "domain identity in this account.",
        resource=f"SES identity {identity_md}", region=region,
        why=f"SES cannot DKIM-sign for `{_n(domain)}` and the MAIL FROM is amazonses.com, so nothing aligns with the "
            "From header: every message fails DMARC at the receiver. Fine for a test inbox, not for anything a user sees.",
        steps=["Send only from domains this account owns and has verified.",
               "Delete the address identity if it was only used for a sandbox test."],
        commands=[f"aws sesv2 delete-email-identity --email-identity {q(identity)}{_region_flag(region)}"],
        verify=["The identity no longer appears in `aws sesv2 list-email-identities`."],
        rollback=[f"`aws sesv2 create-email-identity --email-identity {q(identity)}` and click the verification link."],
    )


def account_rate_brief(metric: str, rate: float, line: float) -> str:
    metric_md = _n(metric)
    name = "Reputation.ComplaintRate" if metric == "complaint" else "Reputation.BounceRate"
    return brief(
        f"bring the account {metric_md} rate down",
        problem=f"SES's account-wide `{name}` is {rate * 100:.3f}%, at or above the {line * 100:.2f}% line.",
        resource="SES account",
        why=("This is the number AWS enforces on: it reviews accounts at 0.10% complaints or 5% bounces and can pause "
             "sending at 0.5% or 10%. It only counts providers that send complaint feedback, so Gmail's complaints "
             "are invisible here and the true rate may be higher."),
        steps=[("Open the Mailbox providers and Records sections to find which path and list source produced the "
                "recent complaints or bounces."),
               "Stop sending to that source; add double opt-in and a visible unsubscribe link.",
               "Watch the rolling rate: it is a trailing average and takes days to fall."],
        commands=["aws cloudwatch get-metric-statistics --namespace AWS/SES --metric-name " + name
                  + " --statistics Average --period 86400 \\",
                  "  --start-time $(date -u -v-14d +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -d '14 days ago' +%Y-%m-%dT00:00:00Z) "
                  "--end-time $(date -u +%Y-%m-%dT00:00:00Z)"],
        verify=[f"`{name}` is below {line * 100:.2f}% on the hero and the finding disappears."],
        rollback=["No AWS change is made by this brief."],
    )


def _default_set_name(identity: str) -> str:
    """A valid SES configuration-set name derived from an identity."""
    name = re.sub(r"[^A-Za-z0-9_-]", "-", identity).strip("-")[:48]
    return f"{name or 'identity'}-default"


def identity_delivery_brief(identity: str, topic: str, region: str = "", others: list[str] | None = None) -> str:
    identity_md = _n(identity)
    default_set = _default_set_name(identity)
    region_flag = _region_flag(region)
    steps = [
        ("Prefer adding `DELIVERY` to the event destination of the configuration set this identity's mail "
         "already uses. That keeps the `ses:configuration-set` tag, so the ledger can attribute the delivery."),
        (f"If this identity's mail does not use a set, create a default feedback set (`{default_set}`) matching "
         "`BOUNCE`, `COMPLAINT` and `DELIVERY` — never `OPEN` or `CLICK` — attach it to the ledger's feedback "
         "topic, and assign it as the identity's default. That covers sends which omit a set without enabling "
         "link rewriting."),
        ("Use the identity notification topic only as a fallback. Identity feedback does not publish the "
         "`ses:configuration-set` tag, so those deliveries are counted under `(none)` and the per-config-set "
         "rate stays empty."),
    ]
    if others:
        steps.append(f"The identity topic's other subscribers ({', '.join(_n(o) for o in others)}) would receive "
                     "a Delivery notification per message too. If they cannot take that volume, create a dedicated "
                     "topic subscribed only by the ledger and use it for the default feedback set instead.")
    dest = json.dumps({"Enabled": True, "MatchingEventTypes": ["BOUNCE", "COMPLAINT", "DELIVERY"],
                       "SnsDestination": {"TopicArn": topic}}, separators=(",", ":"))
    return brief(
        f"publish Delivery notifications for {identity_md}",
        problem=f"Identity `{identity_md}` sends Bounce and Complaint notifications to SNS but not Delivery notifications.",
        resource=f"SES identity {identity_md}", region=region,
        why="Sends made without a configuration set are only visible to the ledger through identity notifications. "
            "Without a Delivery denominator their bounces and complaints cannot be turned into rates. Prefer a "
            "configuration set: identity feedback does not publish the `ses:configuration-set` tag, so identity-level "
            "deliveries are attributed to `(none)`.",
        steps=steps,
        commands=[
            "# Preferred: add DELIVERY to the set this identity's mail already uses.",
            "# Inspect the existing destination, then add DELIVERY without removing its other types:",
            "aws sesv2 get-configuration-set-event-destinations" + region_flag + " \\",
            "  --configuration-set-name SET_NAME",
            "",
            "# If the identity has no set, create a default feedback set (no OPEN/CLICK):",
            f"aws sesv2 create-configuration-set{region_flag} \\",
            f"  --configuration-set-name {q(default_set)}",
            f"aws sesv2 create-configuration-set-event-destination{region_flag} \\",
            f"  --configuration-set-name {q(default_set)} \\",
            "  --event-destination-name feedback-to-ledger \\",
            f"  --event-destination {q(dest)}",
            f"aws sesv2 put-email-identity-configuration-set-attributes{region_flag} \\",
            f"  --email-identity {q(identity)} \\",
            f"  --configuration-set-name {q(default_set)}",
            "",
            "# Fallback only: identity Delivery has no config-set attribution.",
            f"aws ses set-identity-notification-topic{region_flag} \\",
            f"  --identity {q(identity)} \\",
            "  --notification-type Delivery \\",
            f"  --sns-topic {q(topic)}",
        ],
        verify=[
            f"`aws sesv2 get-email-identity --email-identity {q(identity)}{region_flag} --query 'ConfigurationSetName'` "
            f"shows `{default_set}` after the default-set path.",
            f"`aws sesv2 get-configuration-set-event-destinations --configuration-set-name {q(default_set)}{region_flag}` "
            "shows a `DELIVERY` destination on the ledger topic.",
            f"`aws ses get-identity-notification-attributes --identities {q(identity)}` shows a `DeliveryTopic` only "
            "if you used the fallback.",
        ],
        rollback=[
            f"`aws sesv2 put-email-identity-configuration-set-attributes --email-identity {q(identity)}{region_flag}` "
            "removes the default-set association.",
            f"`aws sesv2 delete-configuration-set-event-destination --configuration-set-name {q(default_set)} "
            f"--event-destination-name feedback-to-ledger{region_flag}` removes the destination; then delete the set "
            "if you created it.",
            f"`aws ses set-identity-notification-topic --identity {q(identity)} --notification-type Delivery{region_flag}` "
            "removes the identity fallback.",
        ],
    )


def dkim_brief(identity: str, status: str, region: str = "") -> str:
    identity_md = _n(identity)
    status_md = _n(status)
    return brief(
        f"repair DKIM for {identity_md}",
        problem=f"Identity `{identity_md}` has DKIM status `{status_md or 'unknown'}`, not SUCCESS.",
        resource=f"SES identity {identity_md}", region=region,
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


def dkim_unsigned_brief(identity: str, region: str = "") -> str:
    identity_md = _n(identity)
    return brief(
        f"confirm who DKIM-signs {identity_md}",
        problem=f"SES's built-in DKIM is NOT_STARTED for `{identity_md}`, so SES adds no `d={identity_md}` signature.",
        resource=f"SES identity {identity_md}", region=region,
        why="This is only a problem if nothing else signs the mail. Apps that sign before SendRawEmail (for example "
            "a webmail app with its own selector) are correct to leave Easy DKIM off; enabling it would only add a "
            "second set of DNS records.",
        steps=[f"Send a test message from `{identity_md}` and open its raw headers at the recipient.",
               f"If a `DKIM-Signature` with `d={identity_md}` is present and `Authentication-Results` shows "
               "`dkim=pass`, the app signs: tag the identity with its `Project` so the dashboard counts it as signed.",
               "If there is no aligned signature, enable Easy DKIM and publish the three CNAMEs SES returns."],
        commands=[f"aws sesv2 get-email-identity --email-identity {q(identity)}{_region_flag(region)} "
                  "--query 'DkimAttributes'",
                  "# only if no app signs this domain:",
                  f"aws sesv2 put-email-identity-dkim-attributes --email-identity {q(identity)}"
                  f"{_region_flag(region)} --signing-enabled"],
        verify=["A received message shows `dkim=pass` with `header.d` equal to the domain.",
                "The dashboard's Authentication grid shows a tick for DKIM."],
        rollback=["If Easy DKIM was enabled: run `put-email-identity-dkim-attributes --no-signing-enabled` and "
                  "remove its CNAME records."],
    )


def mail_from_brief(domain: str, region: str = "", status: str = "", configured: str = "",
                    dkim_aligned: bool = True) -> str:
    domain_md = _n(domain)
    mail_from = configured or f"bounce.{domain_md}"
    if configured and status and status != "SUCCESS":
        problem = (f"Identity `{domain_md}` has MAIL FROM `{_n(configured)}` in status `{_n(status)}`, so SES falls back "
                   "to amazonses.com.")
    else:
        problem = f"Identity `{domain_md}` sends with the default MAIL FROM (amazonses.com)."
    why = ("SPF aligns to amazonses.com instead of your domain, so DMARC passes only via DKIM. "
           + ("DKIM does align today, so this adds redundancy rather than fixing a failure."
              if dkim_aligned else "Nothing DKIM-signs this domain either, so DMARC fails outright."))
    return brief(
        f"set a custom MAIL FROM domain for {domain_md}",
        problem=problem,
        resource=f"SES identity {domain_md}", region=region,
        why=why,
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
    mail_from_md = _n(mail_from)
    return brief(
        f"fix SPF for {mail_from_md}",
        problem=f"The MAIL FROM domain `{mail_from_md}` has no SPF record that includes amazonses.com.",
        resource=f"DNS TXT {mail_from_md}",
        why="SES's return-path servers are not authorised, so SPF fails and DMARC must rely on DKIM alone.",
        steps=[f"Publish (or edit) the TXT record on `{mail_from_md}` so it includes `include:amazonses.com`."],
        commands=[f"dig +short TXT {q(mail_from)}",
                  f"# expected after the fix: \"v=spf1 include:amazonses.com ~all\" on {mail_from_md}"],
        verify=[f"`dig +short TXT {q(mail_from)}` returns a v=spf1 record containing include:amazonses.com."],
        rollback=["Restore the previous TXT value."],
    )


def dmarc_brief(domain: str, finding: str) -> str:
    domain_md = _n(domain)
    rec = f"_dmarc.{domain_md}"
    policy = {"missing": "has no DMARC record", "p=none": "has DMARC policy p=none (monitor only)",
              "no rua": "has DMARC without an rua= reporting address",
              "invalid": "has a DMARC record with no valid p= policy (RFC 7489 §6.6.3: no policy applies)",
              "multiple": "publishes more than one DMARC record, which receivers treat as none",
              "pct": "applies its DMARC policy to only part of its mail (pct<100)"}[finding]
    return brief(
        f"strengthen DMARC for {domain_md}",
        problem=f"`{domain_md}` {policy}.",
        resource=f"DNS TXT {rec}",
        why="Gmail and Yahoo bulk-sender rules require DMARC. A p=none policy lets spoofed mail through, and "
            "without rua= you receive no aggregate reports showing who sends as your domain.",
        steps=[f"Publish exactly one TXT record on `{rec}`, starting with `v=DMARC1; p=none; rua=mailto:dmarc@{domain_md}` "
               "if none exists. Check the published value with dig: some DNS hosts cut TXT values at the first `;`.",
               "After two weeks of clean aggregate reports, move to `p=quarantine`, then `p=reject`."],
        commands=[f"dig +short TXT {q(rec)}",
                  f"# target: \"v=DMARC1; p=quarantine; rua=mailto:dmarc@{domain_md}; adkim=s; aspf=r\""],
        verify=[f"`dig +short TXT {q(rec)}` returns the new record and the Authentication grid shows DMARC as pass."],
        rollback=["Lower the policy back to the previous value; never delete DMARC entirely."],
    )


def threshold_brief(group: str, name: str, metric: str, rate: float, deliveries: int, line: float) -> str:
    group_md = _n(group)
    name_md = _n(name)
    metric_md = _n(metric)
    return brief(
        f"reduce the {metric_md} rate for {name_md}",
        problem=f"{group_md} `{name_md}` has a {metric_md} rate of {rate * 100:.3f}% over {deliveries:,} deliveries, "
                f"at or above the {line * 100:.2f}% line.",
        resource=f"{group_md} {name_md}",
        why="AWS reviews accounts at a 0.10% complaint or 5% bounce rate and can pause sending at 0.5% or 10%; "
            "mailbox providers throttle well before that.",
        steps=["Pull the recent complaint and bounce rows for this row from the dashboard's Records section (CSV).",
               "Find the list source or campaign behind them and stop sending to it.",
               "Add or tighten double opt-in and a visible unsubscribe link for that path.",
               "Suppress addresses that bounced or complained."],
        commands=["# export the ledger rows for review (read-only):",
                  "curl -s http://127.0.0.1:8787/api/report | jq '.complaint_records'"],
        verify=[f"Within 7 days the {metric_md} rate for `{name_md}` is under {line * 100:.2f}% on the dashboard."],
        rollback=["No AWS change is made by this brief."],
    )
