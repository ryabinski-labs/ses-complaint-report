# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Unit tests for the self-owned ledger ingest Lambda.

The handler is loaded straight from ``infra/ledger_ingest/index.py`` (the file
that is zipped and deployed) so these tests exercise the shipped code, not a
copy. The DynamoDB handle is injected, so no AWS calls are made.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

HANDLER = Path(__file__).resolve().parents[1] / "infra" / "ledger_ingest" / "index.py"


def _load():
    spec = importlib.util.spec_from_file_location("ledger_ingest", HANDLER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ledger_ingest"] = module
    spec.loader.exec_module(module)
    return module


ledger = _load()
NOW = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=dt.timezone.utc)


def _complaint_event(recipients=("complaint@simulator.amazonses.com",), config_set=None):
    mail = {
        "timestamp": "2026-10-12T11:30:34.000Z",
        "messageId": "0100abc",
        "source": "no-reply@example.com",
    }
    if config_set:
        mail["tags"] = {"ses:configuration-set": [config_set]}
    return {
        "notificationType": "Complaint",
        "complaint": {
            "complaintFeedbackType": "abuse",
            "timestamp": "2026-10-12T11:30:34.000Z",
            "complainedRecipients": [{"emailAddress": r} for r in recipients],
        },
        "mail": mail,
    }


def test_hash_is_stable_and_hex():
    a = ledger.recipient_hash("User@Example.com")
    b = ledger.recipient_hash("user@example.com")
    assert a == b and len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_redact_masks_local_part_keeps_domain():
    assert ledger.redact("complaint@simulator.amazonses.com") == "c********@simulator.amazonses.com"


def test_is_simulator():
    assert ledger.is_simulator("complaint@simulator.amazonses.com")
    assert not ledger.is_simulator("real@example.com")


def test_complaint_record_fields_and_ttl():
    rows = ledger.records_for_event(_complaint_event(), "example-ses-feedback", "sns-1", NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "COMPLAINT"
    assert r["feedback_type"] == "abuse"
    assert r["simulator"] is True
    assert r["sending_identity"] == "example.com"
    assert r["source"] == "example-ses-feedback"
    assert r["recipient_hash"] == ledger.recipient_hash("complaint@simulator.amazonses.com")
    # keyed by event, not by timestamp: the config-set and identity copies of
    # one event can carry timestamps a second apart
    assert r["sk"] == "COMPLAINT#0100abc"
    assert r["ts_epoch"] == int(dt.datetime(2026, 10, 12, 11, 30, 34, tzinfo=dt.timezone.utc).timestamp())
    assert r["ttl"] == int(NOW.timestamp()) + ledger.RETENTION_DAYS * 86400
    # source_key falls back to the sending identity when there is no config set
    assert r["source_key"] == "example.com"


def test_config_set_preferred_as_source_key():
    rows = ledger.records_for_event(_complaint_event(config_set="transactional"), "t", "m", NOW)
    assert rows[0]["config_set"] == "transactional"
    assert rows[0]["source_key"] == "transactional"


def test_empty_attributes_are_dropped_for_dynamodb():
    # DynamoDB rejects empty-string values; a send with no config set must not
    # carry an empty config_set attribute.
    rows = ledger.records_for_event(_complaint_event(), "t", "", NOW)
    assert "config_set" not in rows[0]
    assert "" not in rows[0].values()
    assert all(v != "" for v in rows[0].values())


def test_multiple_recipients_yield_multiple_rows():
    rows = ledger.records_for_event(_complaint_event(("a@x.com", "b@x.com")), "t", "m", NOW)
    assert len(rows) == 2
    assert {r["recipient_hash"] for r in rows} == {ledger.recipient_hash("a@x.com"), ledger.recipient_hash("b@x.com")}


def test_transient_bounce_skipped_permanent_kept():
    transient = {
        "notificationType": "Bounce",
        "bounce": {"bounceType": "Transient", "bounceSubType": "MailboxFull", "bouncedRecipients": [{"emailAddress": "a@x.com"}]},
        "mail": {"messageId": "m1"},
    }
    permanent = {
        "notificationType": "Bounce",
        "bounce": {"bounceType": "Permanent", "bounceSubType": "General", "bouncedRecipients": [{"emailAddress": "a@x.com"}]},
        "mail": {"messageId": "m2"},
    }
    assert ledger.records_for_event(transient, "t", "s", NOW) == []
    rows = ledger.records_for_event(permanent, "t", "s", NOW)
    assert len(rows) == 1 and rows[0]["feedback_type"] == "Permanent/General"


def test_non_feedback_event_ignored():
    assert ledger.records_for_event({"notificationType": "Delivery"}, "t", "s", NOW) == []


class _FakeTable:
    """Enough of a DynamoDB table to honour the ingest's conditional put."""

    def __init__(self):
        self.items = []

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803 - boto3 keyword
        from botocore.exceptions import ClientError
        key = (Item["recipient_hash"], Item["sk"])
        existing = next((i for i in self.items if (i["recipient_hash"], i["sk"]) == key), None)
        if existing is not None and ConditionExpression:
            may_replace = "attribute_not_exists(config_set)" in ConditionExpression and "config_set" not in existing
            if not may_replace:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
            self.items.remove(existing)
        self.items.append(Item)


def _sns_envelope(inner):
    return {
        "Records": [
            {
                "Sns": {
                    "Type": "Notification",
                    "MessageId": "sns-abc",
                    "TopicArn": "arn:aws:sns:us-east-1:123:example-ses-feedback",
                    "Message": json.dumps(inner),
                }
            }
        ]
    }


def test_handler_writes_normalised_rows(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(ledger, "_TABLE_HANDLE", fake)
    result = ledger.handler(_sns_envelope(_complaint_event()), None)
    assert result["written"] == 1
    assert len(fake.items) == 1
    assert fake.items[0]["source"] == "example-ses-feedback"
    assert fake.items[0]["simulator"] is True


def test_config_set_accepts_scalar_list_and_missing():
    assert ledger._config_set({"mail": {"tags": {"ses:configuration-set": "x"}}}) == "x"
    assert ledger._config_set({"mail": {"tags": {"ses:configuration-set": ["y", "z"]}}}) == "y"
    assert ledger._config_set({"mail": {}}) == ""
    assert ledger._config_set({}) == ""


def test_event_type_fallback_when_notification_type_absent():
    ev = {
        "eventType": "Complaint",
        "mail": {"messageId": "m"},
        "complaint": {"complainedRecipients": [{"emailAddress": "a@x.com"}]},
    }
    rows = ledger.records_for_event(ev, "t", "s", NOW)
    assert rows and rows[0]["event_type"] == "COMPLAINT"


def _sns_env_with_messages(*messages):
    return {
        "Records": [
            {
                "Sns": {
                    "Type": "Notification",
                    "MessageId": f"sns-{i}",
                    "TopicArn": "arn:aws:sns:us-east-1:123:shared-ops",
                    "Message": m,
                }
            }
            for i, m in enumerate(messages)
        ]
    }


def test_handler_ignores_non_object_json_without_crashing(monkeypatch):
    # A shared ops topic can carry a bare array/string payload. It must be
    # skipped, not raise (which would fail the whole invocation and retry).
    fake = _FakeTable()
    monkeypatch.setattr(ledger, "_TABLE_HANDLE", fake)
    for message in ("[1, 2, 3]", '"hello"', "null"):
        result = ledger.handler(_sns_env_with_messages(message), None)
        assert result == {"statusCode": 200, "written": 0}
    assert fake.items == []


def test_handler_batch_survives_a_malformed_record(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(ledger, "_TABLE_HANDLE", fake)
    good = json.dumps(_complaint_event())
    result = ledger.handler(_sns_env_with_messages("[1,2]", "not json", good), None)
    assert result["written"] == 1
    assert len(fake.items) == 1


def test_handler_isolates_a_raising_normaliser(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(ledger, "_TABLE_HANDLE", fake)

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ledger, "records_for_event", boom)
    # Must not propagate: otherwise one poisoned message fails the whole batch.
    result = ledger.handler(_sns_env_with_messages(json.dumps(_complaint_event())), None)
    assert result == {"statusCode": 200, "written": 0}
