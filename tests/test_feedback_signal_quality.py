# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Regression tests for the 2026-10-24 Delivery investigation.

Three behaviours are pinned here:

- ``OnAccountSuppressionList`` bounces are SES refusing to send to an address
  already on the account suppression list. AWS does not count them toward the
  bounce rate, so they must not inflate the provider rates or the Advisor's
  identity severity.
- An identity whose only "feedback" is that suppression-list traffic is a
  configuration-hygiene note, not a medium Delivery gap.
- The identity Delivery fix brief leads with the configuration-set attribution
  path; identity-level Delivery is a fallback because identity feedback does
  not publish the ``ses:configuration-set`` tag.
"""

from __future__ import annotations

import pytest

from sesreport import advisor, breakdowns, fixbriefs

DAY = "2026-10-24"
TOPIC = "arn:aws:sns:us-east-1:0:fb"


def _topic() -> dict:
    return {"name": "fb", "arn": TOPIC, "ledger_subscribed": True, "consumers": [],
            "subscribers": [], "pending": []}


def _identity(name: str) -> dict:
    return {"name": name, "type": "DOMAIN", "verified": True, "default_config_set": ""}


def test_identity_activity_ignores_suppression_list_and_simulator_rows() -> None:
    rows = [
        {"sending_identity": "a.example", "event_type": "BOUNCE", "feedback_type": "Permanent/General"},
        {"sending_identity": "a.example", "event_type": "BOUNCE",
         "feedback_type": "Permanent/OnAccountSuppressionList"},
        {"sending_identity": "a.example", "event_type": "COMPLAINT", "feedback_type": "abuse", "simulator": True},
    ]
    aggregates = [{"event": "DELIVERY", "identity": "a.example", "n": 500}]
    assert advisor.identity_activity(rows, aggregates) == {
        "a.example": {"feedback": 1, "deliveries": 500}
    }


def test_breakdowns_ignore_suppression_list_bounces() -> None:
    counters = [{"day": DAY, "domain": "gmail.com", "identity": "a.example",
                 "config_set": "c", "event": "DELIVERY", "n": 2000}]
    rows = [
        {"day": DAY, "recipient_domain": "gmail.com", "sending_identity": "a.example",
         "config_set": "c", "event_type": "BOUNCE", "feedback_type": "Permanent/General"},
        {"day": DAY, "recipient_domain": "gmail.com", "sending_identity": "a.example",
         "config_set": "c", "event_type": "BOUNCE",
         "feedback_type": "Permanent/OnAccountSuppressionList"},
    ]
    b = breakdowns.build_breakdowns(counters, rows)
    row = b["providers"][0]
    assert row["bounces"] == 1
    assert row["bounce_rate"] == pytest.approx(1 / 2001)


def test_only_suppression_bounces_make_identity_delivery_info_not_medium() -> None:
    idfb = [{"identity": "roadtrip.example", "bounce_topic": TOPIC,
             "complaint_topic": TOPIC, "delivery_topic": None}]
    rows = [{"sending_identity": "roadtrip.example", "event_type": "BOUNCE",
             "feedback_type": "Permanent/OnAccountSuppressionList"} for _ in range(27)]
    activity = advisor.identity_activity(rows, [])
    findings = advisor.publishing_findings(
        [], idfb, sns_topics=[_topic()], identities=[_identity("roadtrip.example")],
        identity_activity=activity)
    assert [(f["kind"], f["severity"]) for f in findings] == [("identity_delivery", "info")]


def test_a_real_bounce_still_raises_the_identity_delivery_severity() -> None:
    idfb = [{"identity": "roadtrip.example", "bounce_topic": TOPIC,
             "complaint_topic": TOPIC, "delivery_topic": None}]
    rows = [{"sending_identity": "roadtrip.example", "event_type": "BOUNCE",
             "feedback_type": "Permanent/General"}]
    activity = advisor.identity_activity(rows, [])
    findings = advisor.publishing_findings(
        [], idfb, sns_topics=[_topic()], identities=[_identity("roadtrip.example")],
        identity_activity=activity)
    assert [(f["kind"], f["severity"]) for f in findings] == [("identity_delivery", "medium")]


def test_identity_delivery_brief_prefers_config_set_attribution() -> None:
    text = fixbriefs.identity_delivery_brief(
        "roadtrip.example", TOPIC, "us-east-1", ["https://api.roadtrip/ses"])
    assert "create-configuration-set-event-destination" in text
    assert "put-email-identity-configuration-set-attributes" in text
    assert "does not publish the `ses:configuration-set` tag" in text
    assert "other subscribers (https://api.roadtrip/ses)" in text
    # Identity-level Delivery is offered only after the config-set path.
    assert text.index("create-configuration-set-event-destination") < text.index(
        "set-identity-notification-topic")
    assert "feedback-to-ledger" in text
    assert '"MatchingEventTypes":["BOUNCE","COMPLAINT","DELIVERY"]' in text
