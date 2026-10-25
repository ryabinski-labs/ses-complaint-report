# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Coverage display and routing-brief regressions (2026-10-25).

The dashboard used to paint a missing BOUNCE/COMPLAINT cell red even when
identity-level notifications already covered every sender, and the generated
brief used to point at the first ledger topic (which could be an email-only ops
topic) while adding delivery events. These tests pin the display contract and
the safer brief.
"""

from __future__ import annotations

from pathlib import Path

from sesreport import advisor, fixbriefs

INDEX = Path(__file__).resolve().parents[1] / "src" / "sesreport" / "static" / "index.html"


def _topic(name: str, *, ledger: bool = True, consumers=(), subscribers=()) -> dict:
    return {
        "name": name,
        "arn": f"arn:aws:sns:us-east-1:0:{name}",
        "ledger_subscribed": ledger,
        "consumers": list(consumers),
        "subscribers": list(subscribers),
        "pending": [],
    }


def _email(endpoint: str = "ops@example.com") -> dict:
    return {"protocol": "email", "endpoint": endpoint, "pending": False}


def test_ledger_topics_rank_a_feedback_topic_above_an_email_ops_topic() -> None:
    topics = [
        _topic("NotifyMe", subscribers=[_email()]),
        _topic("app-ses-feedback", consumers=["https://api.example/ses"],
               subscribers=[{"protocol": "https", "endpoint": "https://api.example/ses", "pending": False}]),
        _topic("not-a-ledger-topic", ledger=False, consumers=["https://api.example/ses"]),
    ]
    assert advisor._ledger_topics(topics) == [
        "arn:aws:sns:us-east-1:0:app-ses-feedback",
        "arn:aws:sns:us-east-1:0:NotifyMe",
    ]


def test_ledger_topics_keep_an_email_only_topic_but_rank_it_last() -> None:
    topics = [
        _topic("a-email", subscribers=[_email()]),
        _topic("z-plain"),
    ]
    assert advisor._ledger_topics(topics) == [
        "arn:aws:sns:us-east-1:0:z-plain",
        "arn:aws:sns:us-east-1:0:a-email",
    ]


def test_covered_routing_brief_is_optional_and_does_not_touch_a_shared_topic() -> None:
    text = fixbriefs.routing_brief(
        "alaskaroadtrip-engagement", "us-east-1",
        ledger_topics=["arn:aws:sns:us-east-1:0:NotifyMe",
                       "arn:aws:sns:us-east-1:0:alaskatrip-ses-feedback"],
        covered_by=["alaskaroadtrip.com"], usage="active")
    lowered = text.lower()
    assert "optional" in lowered
    assert "defense-in-depth" in lowered
    assert "no action is needed" in lowered
    assert "dedicated" in lowered
    assert "NotifyMe" not in text
    assert "DELIVERY" not in text
    assert '"MatchingEventTypes":["BOUNCE","COMPLAINT"]' in text
    assert "LEDGER_TOPIC_ARN" in text
    assert "create-topic" in text


def test_uncovered_routing_brief_uses_the_best_ledger_topic_without_delivery() -> None:
    topics = [
        _topic("NotifyMe", subscribers=[_email()]),
        _topic("app-ses-feedback", consumers=["https://api.example/ses"]),
    ]
    text = fixbriefs.routing_brief(
        "open-set", "us-east-1", ledger_topics=advisor._ledger_topics(topics))
    assert "app-ses-feedback" in text
    assert "NotifyMe" not in text
    assert "DELIVERY" not in text
    assert '"MatchingEventTypes":["BOUNCE","COMPLAINT"]' in text


def test_routing_finding_for_a_covered_set_uses_the_safe_brief() -> None:
    topics = [
        _topic("NotifyMe", subscribers=[_email()]),
        _topic("app-ses-feedback", consumers=["https://api.example/ses"]),
    ]
    coverage = [{
        "config_set": "alaskaroadtrip-engagement",
        "sending_enabled": True,
        "routes_feedback": False,
        "risk": "info",
        "covered_by": ["alaskaroadtrip.com"],
        "usage": "active",
        "complaints": None,
    }]
    finding = advisor.routing_findings(coverage, "us-east-1", topics)[0]
    assert finding["severity"] == "info"
    assert "NotifyMe" not in finding["brief"]
    assert "DELIVERY" not in finding["brief"]


def test_coverage_matrix_marks_covered_cells_distinctly() -> None:
    js = INDEX.read_text()
    assert ".mx.c {" in js
    assert "covered by identity notifications" in js
    assert "cell(r.types.includes(c), crit.includes(c), r)" in js
    assert "r.covered.length" in js


def test_gap_counts_exclude_covered_sets() -> None:
    js = INDEX.read_text()
    assert "openGaps.filter(c=>!c.covered.length)" in js
    assert "const covered = openGaps.length - gaps;" in js
    assert "covered by identity" in js


def test_markdown_gap_list_excludes_covered_sets() -> None:
    js = INDEX.read_text()
    assert "openGaps.filter(c=>!(c.covered_by||[]).length)" in js
    assert "covered by identity notifications" in js
