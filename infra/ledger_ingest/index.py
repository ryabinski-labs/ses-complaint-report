# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""SES feedback -> self-owned complaint ledger ingest Lambda.

Subscribed to the account's SES feedback SNS topics. Each ``Notification``
carries a bounce or a complaint — published either by a configuration-set event
destination or by an identity-level notification topic. Every event is
normalised into one recipient-level row so the report can attribute the
account-wide CloudWatch ``Complaint`` count (which has no dimension for sends
made without a configuration set) and retain its own history past CloudWatch's
455-day window.

Privacy (per project decision): the partition key is a SHA-256 of the address
and only a *redacted* form of the address is stored. A mailbox-simulator flag is
computed at ingest, before redaction could hide the domain, and every item
carries a TTL so rows expire on the configured retention window (default 13
months).

Idempotency: the sort key is derived from the SES event itself
(``<epoch>#<eventType>#<mail.messageId>``), not from the SNS delivery, so the
same complaint delivered to two topics (config-set *and* identity-level) writes
one row. SNS is at-least-once; the put is keyed and safe to repeat.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
from urllib.request import HTTPRedirectHandler, build_opener

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "ses_feedback_ledger")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "396"))
REDACT_PLAINTEXT = os.environ.get("REDACT_PLAINTEXT", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
AUTO_CONFIRM = os.environ.get("AUTO_CONFIRM", "true").strip().lower() in ("1", "true", "yes", "on")

SIMULATOR_DOMAIN = "simulator.amazonses.com"

# Only ever GET an SNS SubscribeURL that points at an AWS SNS host (SSRF guard).
_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - refuse redirects
        return None


_no_redirect_opener = build_opener(_NoRedirects)

_TABLE_HANDLE = None


def _table():
    """Lazily build the DynamoDB handle so importing this module (tests, tools)
    never needs AWS credentials or a region."""
    global _TABLE_HANDLE
    if _TABLE_HANDLE is None:
        _TABLE_HANDLE = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _TABLE_HANDLE


def recipient_hash(email: str) -> str:
    """Stable partition key: SHA-256 of the lower-cased address."""
    return hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()


def redact(email: str) -> str:
    """Keep the first local-part character and the domain: ``a***@host``."""
    if not email or "@" not in email:
        return email or ""
    local, _, domain = email.partition("@")
    if not REDACT_PLAINTEXT:
        return email
    keep = local[:1]
    return f"{keep}{'*' * max(3, len(local) - 1)}@{domain}"


def is_simulator(email: str) -> bool:
    """AWS SES mailbox-simulator addresses emit synthetic feedback."""
    return bool(email) and "@" in email and email.rsplit("@", 1)[1].lower() == SIMULATOR_DOMAIN


def _parse_ts(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def _event_kind(event: dict) -> str:
    return event.get("notificationType") or event.get("eventType") or ""


def _sending_identity(event: dict) -> str:
    source = ((event.get("mail") or {}).get("source")) or ""
    return source.rsplit("@", 1)[-1].lower() if "@" in source else ""


def _config_set(event: dict) -> str:
    tags = (event.get("mail") or {}).get("tags") or {}
    cs = tags.get("ses:configuration-set") or []
    if isinstance(cs, list):
        return cs[0] if cs else ""
    return str(cs)


def records_for_event(event: dict, source: str, sns_message_id: str, now: dt.datetime) -> list[dict]:
    """Normalise one SES event into one ledger row per recipient.

    Pure: takes the parsed SES event and returns plain dicts, so it is unit
    testable without DynamoDB. Only complaints and *permanent* bounces are
    recorded — transient bounces are noise for a reputation ledger.
    """
    kind = _event_kind(event)
    mail = event.get("mail") or {}
    ses_message_id = mail.get("messageId") or ""
    config_set = _config_set(event)
    sending_identity = _sending_identity(event)
    # Which dimension best attributes this event to a sender?
    source_key = config_set or sending_identity or "account"
    ttl = int(now.timestamp()) + RETENTION_DAYS * 86400

    if kind == "Complaint":
        complaint = event.get("complaint") or {}
        ts = _parse_ts(complaint.get("timestamp") or mail.get("timestamp")) or now
        recipients = [
            (r.get("emailAddress") or "").strip()
            for r in (complaint.get("complainedRecipients") or [])
        ]
        feedback_type = complaint.get("complaintFeedbackType") or ""
        event_type = "COMPLAINT"
    elif kind == "Bounce":
        bounce = event.get("bounce") or {}
        if bounce.get("bounceType") != "Permanent":
            return []
        ts = _parse_ts(bounce.get("timestamp") or mail.get("timestamp")) or now
        recipients = [
            (r.get("emailAddress") or "").strip()
            for r in (bounce.get("bouncedRecipients") or [])
        ]
        feedback_type = "/".join(
            p for p in (bounce.get("bounceType"), bounce.get("bounceSubType")) if p
        )
        event_type = "BOUNCE"
    else:
        return []

    epoch = int(ts.timestamp())
    out = []
    for email in recipients:
        if not email:
            continue
        item = {
            "recipient_hash": recipient_hash(email),
            "sk": f"{epoch}#{event_type}#{ses_message_id}",
            "event_type": event_type,
            "ts": ts.isoformat(),
            "ts_epoch": epoch,
            "day": ts.date().isoformat(),
            "recipient": redact(email),
            "recipient_domain": email.rsplit("@", 1)[-1].lower() if "@" in email else "",
            "sending_identity": sending_identity,
            "config_set": config_set,
            "source_key": source_key,
            "feedback_type": feedback_type,
            "source": source,
            "ses_message_id": ses_message_id,
            "sns_message_id": sns_message_id,
            "simulator": is_simulator(email),
            "ttl": ttl,
        }
        # DynamoDB rejects empty-string attribute values; drop them so a send
        # with no configuration set / no message id still writes. Booleans and
        # numbers (including False and 0) are kept.
        out.append({k: v for k, v in item.items() if v != "" and v is not None})
    return out


# Delivery, Open and Click events are counted, never stored per message: one
# counter item per day x recipient domain x sending identity x config set, so
# per-provider rates get a denominator without keeping any recipient address.
COUNTED_EVENTS = {"Delivery": "DELIVERY", "Open": "OPEN_UNIQUE", "Click": "CLICK_UNIQUE"}
MARKER_DAYS = 30
NO_CONFIG_SET = "(none)"


def _counted_recipients(kind: str, event: dict) -> list[str]:
    if kind == "Delivery":
        return list((event.get("delivery") or {}).get("recipients") or [])
    # Open/Click apply to the message, so attribute them to its recipients.
    return list((event.get("mail") or {}).get("destination") or [])


def _counted_ts(kind: str, event: dict) -> str:
    body = event.get(kind.lower()) or {}
    return body.get("timestamp") or (event.get("mail") or {}).get("timestamp") or ""


def counters_for_event(event: dict, now: dt.datetime) -> list[dict]:
    """Counter increments for a Delivery, Open or Click event (pure).

    Returns ``[{"pk", "sk", "n", "ttl"}]`` with one entry per recipient domain;
    any other event type returns ``[]``. Keys carry domains only.
    """
    kind = _event_kind(event)
    if kind not in COUNTED_EVENTS:
        return []
    ts = _parse_ts(_counted_ts(kind, event)) or now
    identity = _sending_identity(event) or "(unknown)"
    config_set = _config_set(event) or NO_CONFIG_SET
    per_domain: dict[str, int] = {}
    for r in _counted_recipients(kind, event):
        r = (r or "").strip()
        if "@" not in r:
            continue
        domain = r.rsplit("@", 1)[1].lower()
        per_domain[domain] = per_domain.get(domain, 0) + 1
    ttl = int(now.timestamp()) + RETENTION_DAYS * 86400
    return [
        {
            "pk": f"AGG#{ts.date().isoformat()}",
            "sk": f"{domain}#{identity}#{config_set}#{COUNTED_EVENTS[kind]}",
            "n": n,
            "ttl": ttl,
        }
        for domain, n in sorted(per_domain.items())
    ]


def marker_for_event(event: dict, now: dt.datetime) -> dict | None:
    """Dedupe marker: the first write per (message, counted event) wins, so an
    SNS redelivery or a repeat open of the same message is counted once."""
    kind = _event_kind(event)
    message_id = (event.get("mail") or {}).get("messageId") or ""
    if kind not in COUNTED_EVENTS or not message_id:
        return None
    return {
        "pk": f"SEEN#{message_id}",
        "sk": COUNTED_EVENTS[kind],
        "ttl": int(now.timestamp()) + MARKER_DAYS * 86400,
    }


def _is_conditional_failure(exc: Exception) -> bool:
    return getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _count(event: dict, now: dt.datetime) -> int:
    updates = counters_for_event(event, now)
    if not updates:
        return 0
    table = _table()
    marker = marker_for_event(event, now)
    if marker:
        try:
            table.put_item(
                Item={"recipient_hash": marker["pk"], "sk": marker["sk"], "ttl": marker["ttl"]},
                ConditionExpression="attribute_not_exists(recipient_hash)",
            )
        except Exception as exc:  # noqa: BLE001
            if _is_conditional_failure(exc):
                return 0  # already counted
            log.warning("[ledger] marker put failed: %s", exc)
            return 0
    done = 0
    for u in updates:
        try:
            table.update_item(
                Key={"recipient_hash": u["pk"], "sk": u["sk"]},
                UpdateExpression="ADD n :n SET #ttl = :ttl",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":n": u["n"], ":ttl": u["ttl"]},
            )
            done += 1
        except Exception as exc:  # noqa: BLE001 - one bad counter must not drop the batch
            log.warning("[ledger] counter update failed for sk=%s: %s", u["sk"], exc)
    return done


def _confirm_subscription(url: str) -> None:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return
    if parsed.scheme != "https" or not _SNS_HOST.match(parsed.netloc):
        log.warning("[ledger] refusing to confirm non-AWS SubscribeURL")
        return
    try:
        _no_redirect_opener.open(url, timeout=5)  # noqa: S310 - host validated above
        log.info("[ledger] subscription confirmed")
    except Exception as exc:  # noqa: BLE001
        log.warning("[ledger] subscription confirm failed: %s", exc)


def _write(items: list[dict]) -> int:
    table = _table()
    written = 0
    for item in items:
        try:
            table.put_item(Item=item)
            written += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not drop the batch
            log.warning("[ledger] put failed for sk=%s: %s", item.get("sk"), exc)
    return written


def handler(event, context):  # noqa: ARG001 - Lambda signature
    records = event.get("Records") or []
    written = 0
    for record in records:
        sns = record.get("Sns") or {}
        msg_type = sns.get("Type")
        if msg_type == "SubscriptionConfirmation":
            if AUTO_CONFIRM:
                _confirm_subscription(sns.get("SubscribeURL") or "")
            continue
        if msg_type == "UnsubscribeConfirmation":
            continue
        if msg_type and msg_type != "Notification":
            continue

        raw = sns.get("Message") or "{}"
        try:
            ses_event = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("[ledger] Message was not valid JSON")
            continue
        if not isinstance(ses_event, dict):
            # A shared ops topic also carries non-SES payloads (a bare array or
            # string); ignore them rather than failing the whole invocation.
            log.warning("[ledger] Message JSON was not an object; ignoring")
            continue

        topic_name = (sns.get("TopicArn") or "").split(":")[-1]
        if _event_kind(ses_event) in COUNTED_EVENTS:
            try:
                written += _count(ses_event, dt.datetime.now(dt.timezone.utc))
            except Exception as exc:  # noqa: BLE001
                log.warning("[ledger] could not count event: %s", exc)
            continue
        try:
            items = records_for_event(
                ses_event,
                source=topic_name,
                sns_message_id=sns.get("MessageId") or "",
                now=dt.datetime.now(dt.timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001 - one bad message must not fail the batch
            log.warning("[ledger] could not normalise event: %s", exc)
            continue
        written += _write(items)

    log.info("[ledger] wrote %d record(s) from %d SNS message(s)", written, len(records))
    return {"statusCode": 200, "written": written}
