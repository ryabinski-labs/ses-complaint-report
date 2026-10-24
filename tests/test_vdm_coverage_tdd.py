# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Unit and integration scenarios from tdd/vdm-coverage.tdd.yaml.

VDM-equivalent coverage with VDM off: ledger delivery/open/click counters,
mailbox-provider classification, rate breakdowns, the built-in Advisor and the
Markdown fix briefs. Scenario IDs stay in the test names; QA maps results back
by ID. No AWS or DNS calls: every collaborator is an in-memory fake.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib.util
import json
import random
import re
import shlex
import string
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from sesreport import advisor, breakdowns, collect, fixbriefs, providers
from sesreport.demo import demo_model

HANDLER = Path(__file__).resolve().parents[1] / "infra" / "ledger_ingest" / "index.py"


def _load_ledger():
    spec = importlib.util.spec_from_file_location("ledger_ingest_vdm", HANDLER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ledger_ingest_vdm"] = module
    spec.loader.exec_module(module)
    return module


ledger = _load_ledger()
NOW = dt.datetime(2026, 10, 24, 0, 0, 0, tzinfo=dt.timezone.utc)
DAY = "2026-10-24"


def _event(kind="Delivery", recipients=("a@gmail.com", "b@gmail.com", "c@gmail.com"), config_set="transactional",
           message_id="0100M"):
    mail = {"timestamp": "2026-10-24T10:00:00.000Z", "messageId": message_id, "source": "no-reply@example.com",
            "destination": list(recipients)}
    if config_set:
        mail["tags"] = {"ses:configuration-set": [config_set]}
    ev = {"eventType": kind, "mail": mail}
    if kind == "Delivery":
        ev["delivery"] = {"timestamp": "2026-10-24T10:00:01.000Z", "recipients": list(recipients)}
    else:
        ev[kind.lower()] = {"timestamp": "2026-10-24T11:00:00.000Z"}
    return ev


class FakeTable:
    """In-memory DynamoDB table: conditional put and ADD updates."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803 - boto3 keywords
        key = (Item["recipient_hash"], Item["sk"])
        if ConditionExpression and key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):  # noqa: N803
        key = (Key["recipient_hash"], Key["sk"])
        it = self.items.setdefault(key, dict(Key))
        it["n"] = it.get("n", 0) + ExpressionAttributeValues[":n"]
        it["ttl"] = ExpressionAttributeValues[":ttl"]

    def counter(self, pk, sk):
        return self.items[(pk, sk)]

    def count(self, pk):
        return sum(1 for (p, _s) in self.items if p == pk)


def _sns(ev, message_id="sns-1"):
    return {"Records": [{"Sns": {"Type": "Notification", "MessageId": message_id,
                                 "TopicArn": "arn:aws:sns:us-east-1:000000000000:feedback", "Message": json.dumps(ev)}}]}


# --------------------------------------------------------------------- REQ-001
def test_sc_001_a_delivery_to_3_gmail_recipients_yields_one_counter_update_of_3() -> None:
    """SC-001-a: one update for AGG#<day>, gmail.com#example.com#transactional#DELIVERY, n=3."""
    updates = ledger.counters_for_event(_event(), NOW)
    assert [{k: u[k] for k in ("pk", "sk", "n")} for u in updates] == [
        {"pk": "AGG#2026-10-24", "sk": "gmail.com#example.com#transactional#DELIVERY", "n": 3}]


def test_sc_001_b_counter_updates_never_contain_a_recipient_address() -> None:
    """SC-001-b: no update field contains a local part or an @."""
    updates = ledger.counters_for_event(_event(recipients=("alice@gmail.com", "bob@yahoo.com")), NOW)
    assert updates
    assert not any("@" in str(v) or "alice" in str(v) or "bob" in str(v) for u in updates for v in u.values())


def test_sc_001_c_untagged_delivery_is_keyed_to_config_set_none() -> None:
    """SC-001-c: a Delivery without a config-set tag is keyed to (none)."""
    updates = ledger.counters_for_event(_event(config_set=None), NOW)
    assert updates[0]["sk"].split("#")[2] == "(none)"


def test_sc_001_d_a_redelivered_sns_message_is_counted_once(monkeypatch) -> None:
    """SC-001-d: SNS at-least-once delivery does not double-count."""
    fake = FakeTable()
    monkeypatch.setattr(ledger, "_TABLE_HANDLE", fake)
    ledger.handler(_sns(_event(message_id="M")), None)
    ledger.handler(_sns(_event(message_id="M"), message_id="sns-2"), None)
    assert fake.counter("AGG#2026-10-24", "gmail.com#example.com#transactional#DELIVERY")["n"] == 3
    assert fake.count("SEEN#M") == 1


def test_sc_001_e_counter_and_marker_ttls_follow_retention_rules(monkeypatch) -> None:
    """SC-001-e: counters live RETENTION_DAYS, dedupe markers 30 days."""
    monkeypatch.setattr(ledger, "RETENTION_DAYS", 396)
    u = ledger.counters_for_event(_event(), NOW)[0]
    marker = ledger.marker_for_event(_event(), NOW)
    epoch = int(NOW.timestamp())
    assert u["ttl"] == epoch + 396 * 86400
    assert marker["ttl"] == epoch + 30 * 86400


# --------------------------------------------------------------------- REQ-002
def _raiser(_d):
    raise AssertionError("MX lookup must not be called")


def test_sc_002_a_consumer_microsoft_domains_map_to_microsoft_without_dns() -> None:
    """SC-002-a: the static map answers without DNS."""
    assert all(providers.classify(d, mx=_raiser) == "Microsoft" for d in ("outlook.com", "hotmail.com", "live.com", "msn.com"))


def test_sc_002_b_business_domain_classified_by_mx_suffix() -> None:
    """SC-002-b: Microsoft 365 and Google Workspace MX hosts."""
    mx = {"acme.example": ["acme-example.mail.protection.outlook.com"], "beta.example": ["aspmx.l.google.com"]}
    assert providers.classify("acme.example", mx=mx.get) == "Microsoft"
    assert providers.classify("beta.example", mx=mx.get) == "Google"


def test_sc_002_c_unknown_or_failing_mx_maps_to_other() -> None:
    """SC-002-c: timeouts and unmatched MX hosts are Other."""
    def timeout(_d):
        raise advisor.DnsTimeout("t.example")
    assert providers.classify("t.example", mx=timeout) == "Other"
    assert providers.classify("u.example", mx=lambda d: ["mx.unknown-host.example"]) == "Other"


def test_sc_002_d_mx_results_are_cached_for_7_days_in_the_data_directory(tmp_path) -> None:
    """SC-002-d: one lookup per domain per 7 days, persisted across refreshes."""
    calls = []
    path = tmp_path / "providers-mx-cache.json"

    def mx(d):
        calls.append(d)
        return ["aspmx.l.google.com"]

    t0 = 1_800_000_000.0
    for day in (0, 6, 8):
        c = providers.Classifier(path, mx, clock=lambda day=day: t0 + day * 86400)
        assert c("studio.example") == "Google"
        c.save()
    assert len(calls) == 2
    assert path.exists()


# --------------------------------------------------------------------- REQ-003 / REQ-004
def _c(day, domain, n, event="DELIVERY", identity="example.com", cs="transactional"):
    return {"day": day, "domain": domain, "identity": identity, "config_set": cs, "event": event, "n": n}


def _rows(day, domain, event, k, identity="example.com", cs="transactional"):
    return [{"day": day, "recipient_domain": domain, "sending_identity": identity, "config_set": cs,
             "event_type": event, "simulator": False}] * k


def _by(rows):
    return {r["name"]: r for r in rows}


def test_sc_003_a_rates_are_complaints_and_bounces_over_deliveries() -> None:
    """SC-003-a: complaint 16/20000, bounce 300/20000."""
    b = breakdowns.build_breakdowns([_c(DAY, "yahoo.com", 20000)],
                                    _rows(DAY, "yahoo.com", "COMPLAINT", 16) + _rows(DAY, "yahoo.com", "BOUNCE", 300),
                                    classify=providers.classify)
    row = _by(b["providers"])["Yahoo"]
    assert row["complaint_rate"] == pytest.approx(0.0008)
    # a bounced message was never delivered: bounces divide by sends (deliveries + bounces), as SES does
    assert row["bounce_rate"] == pytest.approx(300 / 20300)
    assert row["sends"] == 20300


def test_sc_003_b_rate_floor_at_1_000_deliveries_boundary() -> None:
    """SC-003-b: 999 deliveries has no rate; 1,000 does."""
    b = breakdowns.build_breakdowns([_c(DAY, "a.example", 999), _c(DAY, "b.example", 1000)],
                                    _rows(DAY, "a.example", "COMPLAINT", 1) + _rows(DAY, "b.example", "COMPLAINT", 1))
    r999, r1000 = _by(b["providers"])["a.example"], _by(b["providers"])["b.example"]
    assert r999["complaint_rate"] is None and r999["rate_note"] == "too few sends"
    assert r1000["complaint_rate"] == pytest.approx(0.001)


def test_sc_003_c_rows_sorted_by_deliveries_descending() -> None:
    """SC-003-c: Google 30k, Yahoo 20k, Microsoft 12k."""
    b = breakdowns.build_breakdowns([_c(DAY, "gmail.com", 30000), _c(DAY, "outlook.com", 12000), _c(DAY, "yahoo.com", 20000)],
                                    [], classify=providers.classify)
    assert [r["name"] for r in b["providers"]] == ["Google", "Yahoo", "Microsoft"]


def test_sc_003_d_range_scopes_counts_and_rates() -> None:
    """SC-003-d: only days on or after `since` count."""
    b = breakdowns.build_breakdowns([_c("2026-10-20", "gmail.com", 1500), _c("2026-08-01", "gmail.com", 5000)], [],
                                    since="2026-09-24", classify=providers.classify)
    assert _by(b["providers"])["Google"]["deliveries"] == 1500


def test_sc_004_a_identity_and_config_set_groupings_share_columns_and_floor() -> None:
    """SC-004-a: identity and config-set rows use the same columns and floor."""
    counters = [_c(DAY, "gmail.com", 5000, identity="example.com", cs="transactional"),
                _c(DAY, "gmail.com", 400, identity="news.example.com", cs="(none)")]
    b = breakdowns.build_breakdowns(counters, [], classify=providers.classify)
    cols = {"deliveries", "complaints", "complaint_rate", "bounces", "bounce_rate"}
    assert cols <= set(b["identities"][0]) and cols <= set(b["config_sets"][0])
    assert _by(b["identities"])["news.example.com"]["complaint_rate"] is None
    assert _by(b["config_sets"])["(none)"]["complaint_rate"] is None
    assert _by(b["config_sets"])["transactional"]["complaint_rate"] == 0


# --------------------------------------------------------------------- Advisor
class StubResolver:
    def __init__(self, txt=None, mx=None, timeout=()):
        self._txt, self._mx, self._timeout = txt or {}, mx or {}, set(timeout)
        self.lookups = self.timeouts = 0

    def txt(self, name):
        self.lookups += 1
        if any(name.endswith(t) for t in self._timeout):
            self.timeouts += 1
            raise advisor.DnsTimeout(name)
        return self._txt.get(name, [])

    def mx(self, name):
        self.lookups += 1
        return self._mx.get(name, [])


GOOD_DMARC = ["v=DMARC1; p=reject; rua=mailto:r@x.example"]


def _ident(name, dkim="SUCCESS", mail_from="auto"):
    return {"name": name, "type": "DOMAIN", "dkim_status": dkim,
            "mail_from_domain": f"bounce.{name}" if mail_from == "auto" else mail_from}


def _passing(*names):
    txt = {}
    for n in names:
        txt[f"_dmarc.{n}"] = GOOD_DMARC
        txt[f"bounce.{n}"] = ["v=spf1 include:amazonses.com ~all"]
    return txt


def test_sc_005_a_failed_dkim_becomes_a_high_finding() -> None:
    """SC-005-a: DKIM FAILED is high, with a brief."""
    fs, _ = advisor.auth_findings([_ident("a.example"), _ident("b.example", dkim="FAILED")],
                                  StubResolver(txt=_passing("a.example", "b.example")))
    assert len(fs) == 1
    assert fs[0]["severity"] == "high" and fs[0]["resource"] == "b.example" and fs[0]["kind"] == "dkim" and fs[0]["brief"]


def _sev_of(fs, kind, resource):
    hits = [f["severity"] for f in fs if f["kind"] == kind and f["resource"] == resource]
    return hits[0] if hits else None


def test_sc_006_a_missing_custom_mail_from_is_a_note_when_dkim_aligns_and_high_when_nothing_does() -> None:
    """SC-006-a: no custom MAIL FROM: DMARC still passes on an aligned DKIM, so it is info;
    with no aligned DKIM either, nothing aligns and it is high."""
    fs, _ = advisor.auth_findings([_ident("c.example", mail_from="")], StubResolver(txt=_passing("c.example")))
    assert _sev_of(fs, "mail_from", "c.example") == "info"
    fs, _ = advisor.auth_findings([_ident("c.example", mail_from="", dkim="NOT_STARTED")],
                                  StubResolver(txt=_passing("c.example")))
    assert _sev_of(fs, "mail_from", "c.example") == "high"


def test_sc_006_b_mail_from_spf_without_amazonses_include_follows_dkim_alignment() -> None:
    """SC-006-b: MAIL FROM SPF without include:amazonses.com is info when DKIM aligns, high otherwise."""
    txt = _passing("d.example") | {"bounce.d.example": ["v=spf1 -all"]}
    fs, checks = advisor.auth_findings([_ident("d.example")], StubResolver(txt=txt))
    assert _sev_of(fs, "spf", "bounce.d.example") == "info"
    assert checks["d.example"]["spf"] == "fail: lacks include:amazonses.com"
    fs, _ = advisor.auth_findings([_ident("d.example", dkim="NOT_STARTED")], StubResolver(txt=txt))
    assert _sev_of(fs, "spf", "bounce.d.example") == "high"


def test_sc_007_a_dmarc_decision_table() -> None:
    """SC-007-a: missing high, p=none medium, no rua info, strong policy none."""
    doms = ["m1.example", "m2.example", "m3.example", "m4.example"]
    txt = _passing(*doms)
    txt["_dmarc.m1.example"] = []
    txt["_dmarc.m2.example"] = ["v=DMARC1; p=none; rua=mailto:r@x"]
    txt["_dmarc.m3.example"] = ["v=DMARC1; p=quarantine"]
    txt["_dmarc.m4.example"] = ["v=DMARC1; p=reject; rua=mailto:r@x"]
    fs, _ = advisor.auth_findings([_ident(d) for d in doms], StubResolver(txt=txt))
    assert [_sev_of(fs, "dmarc", d) for d in doms] == ["medium", "medium", "info", None]
    # Gmail/Yahoo make DMARC mandatory at 5,000 messages a day: missing becomes high there
    fs, _ = advisor.auth_findings([_ident(d) for d in doms], StubResolver(txt=txt), sent_24h=5000)
    assert [_sev_of(fs, "dmarc", d) for d in doms] == ["high", "medium", "info", None]


def test_sc_008_a_dns_timeout_is_not_a_finding() -> None:
    """SC-008-a: a DNS timeout is reported as not checked, not as a finding."""
    fs, checks = advisor.auth_findings([_ident("e.example")], StubResolver(timeout=["e.example"]))
    assert not [f for f in fs if f["resource"].endswith("e.example")]
    assert checks["e.example"]["dmarc"] == "not checked: DNS timeout"


def test_sc_008_b_resolver_is_configured_with_a_2_second_timeout() -> None:
    """SC-008-b: the real resolver's lifetime is 2 seconds."""
    r = advisor.make_resolver()
    assert r.lifetime == 2.0
    assert r._r.lifetime == 2.0


def _row(name, deliveries, complaints=0, bounces=0):
    b = breakdowns.build_breakdowns([_c(DAY, name, deliveries)],
                                    _rows(DAY, name, "COMPLAINT", complaints) + _rows(DAY, name, "BOUNCE", bounces))
    return b


def _sev(window, kind):
    fs = [f for f in advisor.threshold_findings(window) if f["kind"] == kind and f["group"] == "providers"]
    return fs[0]["severity"] if fs else None


def test_sc_009_a_complaint_threshold_boundaries() -> None:
    """SC-009-a: 7, 8, 10 complaints per 10,000 → none, medium, high."""
    assert [_sev(_row("p.example", 10000, complaints=k), "complaint_rate") for k in (7, 8, 10)] == [None, "medium", "high"]


def test_sc_009_b_bounce_threshold_boundaries() -> None:
    """SC-009-b: 499, 500, 1,000 bounces per 10,000 sends → none, medium, high (sends = deliveries + bounces)."""
    assert [_sev(_row("p.example", 10000 - k, bounces=k), "bounce_rate") for k in (499, 500, 1000)] == [None, "medium", "high"]


def test_sc_009_c_floor_suppresses_threshold_findings() -> None:
    """SC-009-c: below 1,000 deliveries no threshold finding fires."""
    assert advisor.threshold_findings(_row("p.example", 999, complaints=5)) == []
    # the Advisor enforces the floor itself, even if a row arrives with a rate
    crafted = {"providers": [{"name": "p.example", "deliveries": 999, "complaint_rate": 0.005, "bounce_rate": 0.2}]}
    assert advisor.threshold_findings(crafted) == []


# --------------------------------------------------------------------- REQ-010 briefs
def _headings(md):
    return [line[3:] for line in md.splitlines() if line.startswith("## ")]


def _first_command(md):
    block = md.split("```sh\n", 1)[1].split("\n```", 1)[0]
    cmd = []
    for line in block.splitlines():
        if line.startswith("#") and cmd:
            break
        if not line.startswith("#"):
            cmd.append(line)
    return shlex.split(" ".join(cmd).replace("\\ ", " ").replace(" \\", " "))


def test_sc_010_a_brief_has_the_seven_sections_in_order() -> None:
    """SC-010-a: Problem … Rollback, in order."""
    md = fixbriefs.dkim_brief("b.example", "FAILED")
    assert md.startswith("# ")
    assert _headings(md) == list(fixbriefs.SECTIONS)


def test_sc_010_b_commands_survive_the_shell_for_any_resource_name_property() -> None:
    """SC-010-b: shell-parsing the command returns the exact config-set name (property, 300 cases)."""
    rng = random.Random(1234)
    alphabet = string.ascii_letters + string.digits + " '\"$;&|`()<>*?!#-_"
    for _ in range(300):
        name = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24)))
        args = _first_command(fixbriefs.routing_brief(name))
        i = args.index("--configuration-set-name")
        assert args[i + 1] == name


def test_sc_010_c_routing_brief_embeds_the_existing_event_destination_command() -> None:
    """SC-010-c: the routing brief's command is create-configuration-set-event-destination for BOUNCE/COMPLAINT."""
    md = fixbriefs.routing_brief("legacy")
    args = _first_command(md)
    assert "create-configuration-set-event-destination" in args
    dest = json.loads(args[args.index("--event-destination") + 1])
    assert set(dest["MatchingEventTypes"]) >= {"BOUNCE", "COMPLAINT"}
    assert _headings(md) == list(fixbriefs.SECTIONS)


# --------------------------------------------------------------------- REQ-011 engagement
def test_sc_011_a_repeat_opens_of_one_message_count_once(monkeypatch) -> None:
    """SC-011-a: three Open events for one message → OPEN_UNIQUE n=1."""
    fake = FakeTable()
    monkeypatch.setattr(ledger, "_TABLE_HANDLE", fake)
    for i in range(3):
        ledger.handler(_sns(_event("Open", recipients=("x@yahoo.com",), message_id="M"), message_id=f"s{i}"), None)
    assert fake.counter("AGG#2026-10-24", "yahoo.com#example.com#transactional#OPEN_UNIQUE")["n"] == 1


def test_sc_011_b_open_and_click_rates_use_deliveries_as_denominator() -> None:
    """SC-011-b: 500/2000 opens, 40/2000 clicks."""
    b = breakdowns.build_breakdowns([_c(DAY, "yahoo.com", 2000), _c(DAY, "yahoo.com", 500, "OPEN_UNIQUE"),
                                     _c(DAY, "yahoo.com", 40, "CLICK_UNIQUE")], [], classify=providers.classify)
    row = _by(b["providers"])["Yahoo"]
    assert row["open_rate"] == pytest.approx(0.25) and row["click_rate"] == pytest.approx(0.02)


# --------------------------------------------------------------------- REQ-012 publishing gaps
def test_sc_012_a_config_set_missing_delivery_is_flagged_open_click_only_by_policy() -> None:
    """SC-012-a: BOUNCE/COMPLAINT-only config set → finding for DELIVERY. OPEN/CLICK are off by policy
    (link rewriting), so they are listed only when engagement tracking is configured. The brief edits the
    existing destination only when its topic already feeds the ledger."""
    cs = [{"name": "transactional", "event_types": ["BOUNCE", "COMPLAINT"], "feedback_topics": ["arn:aws:sns:us-east-1:0:fb"],
           "destinations": [{"name": "to-sns", "sns": "arn:aws:sns:us-east-1:0:fb", "event_types": ["BOUNCE", "COMPLAINT"]}]}]
    topics = [{"name": "fb", "arn": "arn:aws:sns:us-east-1:0:fb", "ledger_subscribed": True, "consumers": [], "subscribers": []}]
    (f,) = advisor.publishing_findings(cs, [], sns_topics=topics)
    assert f["resource"] == "transactional" and f["missing"] == ["DELIVERY"]
    assert "update-configuration-set-event-destination" in f["brief"] and "tracking pixel" not in f["brief"]
    (f,) = advisor.publishing_findings(cs, [], sns_topics=topics, engagement_tracking=True)
    assert set(f["missing"]) == {"DELIVERY", "OPEN", "CLICK"} and "tracking pixel" in f["brief"]
    # an ops/alert topic must never be edited: every delivery would page someone
    ops = [{**topics[0], "ledger_subscribed": False, "subscribers": [{"protocol": "email", "endpoint": "ops@x"}]}]
    (f,) = advisor.publishing_findings(cs, [], sns_topics=ops)
    assert "create-configuration-set-event-destination" in f["brief"] and "update-configuration" not in f["brief"]


def test_sc_012_b_identity_topic_without_delivery_notifications_is_an_info_finding() -> None:
    """SC-012-b: identity with complaint topic but no delivery topic → info with set-identity-notification-topic."""
    (f,) = advisor.publishing_findings([], [{"identity": "send0.example.com", "bounce_topic": "arn:t",
                                             "complaint_topic": "arn:t", "delivery_topic": None}])
    assert f["severity"] == "info"
    assert "set-identity-notification-topic" in f["brief"] and "--notification-type Delivery" in f["brief"]


# --------------------------------------------------------------------- REQ-013 states
def test_sc_013_a_ledger_not_configured_state() -> None:
    """SC-013-a: no ledger → ledger_not_configured with empty tables."""
    b = breakdowns.build_breakdowns([], [], configured=False)
    assert b["state"] == "ledger_not_configured" and b["providers"] == []


def test_sc_013_b_complaints_but_no_deliveries_state() -> None:
    """SC-013-b: complaints with zero deliveries → no_delivery_data, counts kept, no rates."""
    b = breakdowns.build_breakdowns([], _rows(DAY, "gmail.com", "COMPLAINT", 5), classify=providers.classify)
    assert b["state"] == "no_delivery_data"
    assert sum(r["complaints"] for r in b["providers"]) == 5
    assert all(r["complaint_rate"] is None for r in b["providers"])


# --- recording fakes for a whole collection run (no AWS) ----------------------
class R(dict):
    def __missing__(self, key):
        return R()


class RecordingClient:
    def __init__(self, ops):
        self.ops = ops

    def __getattr__(self, op):
        def call(*_a, **_k):
            self.ops.append(op)
            return R()
        return call


class FakeLedgerTable:
    def __init__(self, ops, fail_aggregates=False):
        self.ops, self.fail = ops, fail_aggregates

    def scan(self, **_kw):
        self.ops.append("scan")
        return {"Items": []}

    def query(self, **kw):
        self.ops.append("query")
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Query")
        return {"Items": []}


def _patch_aws(monkeypatch, ops, fail_aggregates=False):
    monkeypatch.setattr(collect, "CFG", dataclasses.replace(collect.CFG, ledger_table="ledger", demo=False))
    monkeypatch.setattr(collect, "_client", lambda *a, **k: RecordingClient(ops))
    monkeypatch.setattr(collect, "_ACCOUNT_ID", "000000000000")
    table = FakeLedgerTable(ops, fail_aggregates)

    class Session:
        def __init__(self, *a, **k):
            pass

        def resource(self, *a, **k):
            return type("Res", (), {"Table": lambda _self, _n: table})()

    monkeypatch.setattr(collect.boto3, "Session", Session)


def test_sc_013_c_ledger_aggregate_read_failure_becomes_a_collector_warning(monkeypatch, tmp_path) -> None:
    """SC-013-c: a failing aggregate read is a warning; the report still assembles."""
    ops: list[str] = []
    _patch_aws(monkeypatch, ops, fail_aggregates=True)
    model = collect.collect_all(cache_dir=tmp_path, resolver=StubResolver())
    assert "ledger_aggregates" in [e["section"] for e in model["errors"]]
    assert "metrics" in model and "posture" in model and "breakdowns" in model


# --------------------------------------------------------------------- REQ-014 demo
def test_sc_014_a_demo_model_carries_breakdowns_and_at_least_3_advisor_findings() -> None:
    """SC-014-a: demo mode shows synthetic providers and Advisor findings."""
    m = demo_model()
    assert len(m["breakdowns"]["ranges"]["all"]["providers"]) >= 3
    assert len(m["advisor"]["findings"]) >= 3


# --------------------------------------------------------------------- REQ-015 read-only
FORBIDDEN = {"batch_get_metric_data", "list_recommendations", "put_account_vdm_attributes", "create_export_job"}


def test_sc_015_a_a_full_collection_makes_no_vdm_or_write_calls(monkeypatch, tmp_path) -> None:
    """SC-015-a: no VDM API and no Put/Create/Update/Delete/Set operation is called."""
    ops: list[str] = []
    _patch_aws(monkeypatch, ops)
    collect.collect_all(cache_dir=tmp_path, resolver=StubResolver())
    assert ops, "the recording fakes were not exercised"
    assert not [op for op in ops if op in FORBIDDEN or re.match(r"(put|create|update|delete|set)_", op)]


# --------------------------------------------------------------------- REQ-016 bounded refresh
def test_sc_016_a_dns_lookups_capped_at_120_per_refresh(tmp_path) -> None:
    """SC-016-a: 200 unknown domains → at most 120 MX lookups."""
    calls = []
    c = providers.Classifier(tmp_path / "mx.json", lambda d: calls.append(d) or [])
    for i in range(200):
        c(f"d{i}.example")
    assert len(calls) <= 120


def test_sc_016_b_warm_refresh_re_reads_only_the_last_2_days(tmp_path) -> None:
    """SC-016-b: with 365 cached days only today and yesterday are queried."""
    today = dt.date(2026, 10, 24)
    days = {(today - dt.timedelta(days=i)).isoformat(): [] for i in range(396)}
    path = tmp_path / "ledger-aggregates.json"
    path.write_text(json.dumps({"days": days}))
    queried = []

    class T:
        def query(self, KeyConditionExpression):  # noqa: N803
            queried.append(KeyConditionExpression.get_expression()["values"][1])
            return {"Items": []}

    collect.read_ledger_aggregates(T(), path, today)
    assert len(set(queried)) <= 2
