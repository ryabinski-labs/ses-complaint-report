# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Regression tests for the 2026-10-24 fidelity audit.

Four read-only audits compared every finding and number on the live dashboard
with AWS, DNS and the sending apps' code. Each test here pins one false
positive or false negative they found, so it cannot come back.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from sesreport import advisor, breakdowns, collect
from tests.test_vdm_coverage_tdd import GOOD_DMARC, StubResolver

HANDLER = Path(__file__).resolve().parents[1] / "infra" / "ledger_ingest" / "index.py"
_spec = importlib.util.spec_from_file_location("ledger_ingest_fidelity", HANDLER)
ledger = importlib.util.module_from_spec(_spec)
sys.modules["ledger_ingest_fidelity"] = ledger
_spec.loader.exec_module(ledger)

NOW = dt.datetime(2026, 10, 24, 12, tzinfo=dt.timezone.utc)
TOPIC = "arn:aws:sns:us-east-1:0:fb"
LEDGER_FN = "arn:aws:lambda:us-east-1:0:function:ses-complaint-report-ledger-ingest"


def _ident(name, dkim="SUCCESS", mail_from="auto", **kw):
    row = {"name": name, "type": "DOMAIN", "dkim_status": dkim, "verified": True, "verification_status": "SUCCESS",
           "sending": True, "mail_from_domain": f"bounce.{name}" if mail_from == "auto" else mail_from,
           "mail_from_status": "SUCCESS" if mail_from == "auto" else "", "project": "", "tags": {}}
    row.update(kw)
    return row


def _dns(*names, **extra):
    txt = {}
    for n in names:
        txt[f"_dmarc.{n}"] = GOOD_DMARC
        txt[f"bounce.{n}"] = ["v=spf1 include:amazonses.com ~all"]
    txt.update(extra)
    return txt


def _kinds(fs, resource=None):
    return {(f["kind"], f["severity"]) for f in fs if resource is None or f["resource"] == resource}


def _topic(name=TOPIC, ledger=False, consumers=(), subscribers=(), pending=()):
    return {"name": name.split(":")[-1], "arn": name, "ledger_subscribed": ledger, "consumers": list(consumers),
            "subscribers": list(subscribers), "pending": list(pending)}


# --------------------------------------------------------------------- authentication
def test_unverified_identity_gets_one_note_and_no_auth_checks():
    """Two tenants' domains: FAILED verification, tagged WebMail. They showed as
    'pass · signed by WebMail' plus high DMARC and medium SPF findings; SES cannot send from them."""
    ids = [_ident("dead.example", dkim="NOT_STARTED", project="WebMail", verified=False, verification_status="FAILED",
                  sending=False)]
    fs, checks = advisor.auth_findings(ids, StubResolver(txt={}))
    assert _kinds(fs) == {("identity_unverified", "info")}
    assert checks["dead.example"]["dkim"] == advisor.NOT_APPLICABLE
    assert checks["dead.example"]["dmarc"] == advisor.NOT_APPLICABLE


def test_untagged_domain_whose_mx_is_the_app_inbound_counts_as_app_signed():
    """Domains that predate the webmail app's Project tag."""
    r = StubResolver(txt=_dns("old.example"), mx={"old.example": ["inbound.mail.example.net."]})
    fs, checks = advisor.auth_findings([_ident("old.example", dkim="NOT_STARTED")], r,
                                       app_dkim_mx=["inbound.mail.example.net"])
    assert checks["old.example"]["dkim"] == "pass · signed by app (by MX)"
    assert not [f for f in fs if f["kind"].startswith("dkim")]
    # a MAIL FROM note only, since DKIM aligns
    assert _kinds(fs) <= {("mail_from", "info")}


def test_app_selector_tag_is_checked_for_a_published_key():
    ids = [_ident("sel.example", dkim="NOT_STARTED", project="WebMail", tags={"Project": "WebMail", "DkimSelector": "k1"})]
    good = StubResolver(txt=_dns("sel.example", **{"k1._domainkey.sel.example": ["v=DKIM1; k=rsa; p=MIIB"]}))
    assert advisor.auth_findings(ids, good)[1]["sel.example"]["dkim"] == "pass · signed by WebMail"
    bad = StubResolver(txt=_dns("sel.example", **{"k1._domainkey.sel.example": []}))
    fs, checks = advisor.auth_findings(ids, bad)
    assert checks["sel.example"]["dkim"] == "fail" and ("dkim", "high") in _kinds(fs)


def test_truncated_dmarc_record_is_an_invalid_policy_not_a_missing_rua():
    """A DNS host that cuts TXT values at the first ';' publishes just 'v=DMARC1'.
    The grid said 'p=, no rua' at info; with no p= no policy applies at all."""
    txt = _dns("t.example", **{"_dmarc.t.example": ["v=DMARC1"]})
    fs, checks = advisor.auth_findings([_ident("t.example")], StubResolver(txt=txt))
    assert checks["t.example"]["dmarc"] == "invalid (no p=)"
    assert ("dmarc", "medium") in _kinds(fs, "t.example")


def test_multiple_dmarc_records_are_invalid():
    txt = _dns("m.example", **{"_dmarc.m.example": ["v=DMARC1; p=reject", "v=DMARC1; p=none"]})
    fs, checks = advisor.auth_findings([_ident("m.example")], StubResolver(txt=txt))
    assert checks["m.example"]["dmarc"] == "invalid (multiple records)"
    assert ("dmarc", "medium") in _kinds(fs, "m.example")


def test_dmarc_tree_walk_reads_the_parent_sp_and_never_a_public_suffix():
    """mail.brand.example inherits brand.example's sp=reject; a subdomain of shop.com.br must not
    look up _dmarc.com.br."""
    r = StubResolver(txt=_dns("mail.brand.example", "x.shop.com.br", **{
        "_dmarc.mail.brand.example": [], "_dmarc.brand.example": ["v=DMARC1; p=quarantine; sp=reject; rua=mailto:r@x"],
        "_dmarc.x.shop.com.br": [], "_dmarc.shop.com.br": [], "_dmarc.com.br": ["v=DMARC1; p=reject; rua=mailto:x@y"]}))
    fs, checks = advisor.auth_findings([_ident("mail.brand.example"), _ident("x.shop.com.br")], r)
    assert checks["mail.brand.example"]["dmarc"] == "pass (via brand.example, sp=reject)"
    assert checks["x.shop.com.br"]["dmarc"] == "missing"
    assert ("dmarc", "medium") in _kinds(fs, "x.shop.com.br")


def test_dmarc_pct_below_100_and_aspf_strict_are_noted():
    txt = _dns("p.example", **{"_dmarc.p.example": ["v=DMARC1; p=reject; pct=50; aspf=s; rua=mailto:r@x"]})
    fs, checks = advisor.auth_findings([_ident("p.example")], StubResolver(txt=txt))
    assert checks["p.example"]["dmarc"] == "p=reject, pct=50"
    assert checks["p.example"]["spf"] == "pass (not aligned, aspf=s)"
    assert ("dmarc", "info") in _kinds(fs, "p.example")


@pytest.mark.parametrize("records,why", [
    (["v=spf1include:amazonses.com-all"], "no v=spf1 record"),  # three unjoined strings
    (["v=spf1 include:amazonses.com ~all", "v=spf1 -all"], "2 SPF records (permerror)"),
    (["v=spf1 include:amazonses.com +all"], "ends in +all"),
    (["v=spf1 include:amazonses.com.evil ~all"], "lacks include:amazonses.com"),
])
def test_spf_check_is_strict(records, why):
    assert advisor._spf_verdict(records) == (False, why)


def test_spf_check_accepts_a_proper_record():
    assert advisor._spf_verdict(["v=spf1 include:amazonses.com -all"]) == (True, "pass")


def test_failed_mail_from_is_reported_as_fallback_not_checked_for_spf():
    ids = [_ident("f.example", mail_from="mail.f.example", mail_from_status="FAILED")]
    fs, checks = advisor.auth_findings(ids, StubResolver(txt=_dns("f.example")))
    assert checks["f.example"]["mail_from"] == "mail.f.example (FAILED → amazonses.com)"
    assert checks["f.example"]["spf"] == "n/a (MAIL FROM FAILED)"
    assert _kinds(fs, "f.example") == {("mail_from", "info")} and not [f for f in fs if f["kind"] == "spf"]


def test_qa_identity_dmarc_gap_is_a_note_but_a_dev_tld_is_not_a_test():
    """mail-qa.example.org only sends QA mail; product.dev is a product."""
    r = StubResolver(txt=_dns("mail-qa.example.org", "product.dev", **{
        "_dmarc.mail-qa.example.org": [], "_dmarc.example.org": [], "_dmarc.product.dev": []}))
    fs, checks = advisor.auth_findings([_ident("mail-qa.example.org"), _ident("product.dev")], r)
    assert _kinds(fs, "mail-qa.example.org") == {("dmarc", "info")}
    assert checks["mail-qa.example.org"]["note"] == "test identity"
    assert ("dmarc", "medium") in _kinds(fs, "product.dev") and "note" not in checks["product.dev"]


def test_email_address_identity_on_a_foreign_domain_is_noted_and_1024_bit_keys_too():
    ids = [_ident("k.example", dkim_key_length="RSA_1024_BIT"),
           {"name": "me@gmail.com", "type": "EMAIL_ADDRESS", "verified": True}]
    fs, _ = advisor.auth_findings(ids, StubResolver(txt=_dns("k.example")))
    assert ("dkim_key_length", "info") in _kinds(fs, "k.example")
    assert _kinds(fs, "me@gmail.com") == {("identity_email_only", "info")}


# --------------------------------------------------------------------- rates
def test_account_reputation_rates_are_checked_without_a_floor():
    """An account rate above the 0.08% line produced no finding: only ledger rows were checked."""
    assert [f["severity"] for f in advisor.account_findings(0.000922, 0.0027)] == ["medium"]
    assert [f["severity"] for f in advisor.account_findings(0.0011, 0.06)] == ["high", "medium"]
    assert advisor.account_findings(0.0005, 0.01) == []
    assert advisor.account_findings(None, None) == []


def test_breakdown_numerators_start_when_delivery_counting_started():
    """Bounce rows began 2026-10-14, delivery counters 2026-10-24: ten days of bounces sat over one day
    of deliveries in every range."""
    counters = [{"day": "2026-10-24", "domain": "x.example", "identity": "a", "config_set": "c", "event": "DELIVERY", "n": 2000}]
    rows = [{"day": d, "recipient_domain": "x.example", "sending_identity": "a", "config_set": "c", "event_type": "BOUNCE"}
            for d in ("2026-10-14", "2026-10-20", "2026-10-24")]
    b = breakdowns.build_breakdowns(counters, rows, since="2026-09-25")
    assert b["since_effective"] == "2026-10-24"
    assert b["providers"][0]["bounces"] == 1 and b["providers"][0]["bounce_rate"] == pytest.approx(1 / 2001)


def test_thirty_day_range_is_thirty_days():
    today = dt.date(2026, 10, 24)
    r = breakdowns.build_all_ranges([], [], today)
    assert r["ranges"]["30"]["since"] == "2026-09-25"


# --------------------------------------------------------------------- routing and publishing
def _cov(name, **kw):
    row = {"config_set": name, "sending_enabled": True, "routes_feedback": False, "reaches_ledger": False,
           "reaches_handler": False, "delivery_to_ledger": False, "engagement_types": [], "feedback_topics": [],
           "event_types": [], "destinations": 0, "sends": 10, "sends_recent": 5, "last_send": "2026-10-24",
           "usage": "active", "complaints": None, "covered_by": []}
    row.update(kw)
    row["risk"] = collect._coverage_risk(row)
    return row


def test_coverage_risk_matrix():
    assert _cov("legacy-prod", sending_enabled=False)["risk"] == "disabled"
    assert _cov("roadtrip-engagement", covered_by=["roadtrip.example"])["risk"] == "info"
    assert _cov("site-feedback", sends_recent=0, usage="idle")["risk"] == "info"
    assert _cov("forms-feedback")["risk"] == "high"
    assert _cov("plain", complaints=2)["risk"] == "high"
    assert _cov("plain")["risk"] == "medium"
    assert _cov("ok", routes_feedback=True)["risk"] == "ok"


def test_routing_findings_skip_disabled_sets_and_downgrade_covered_or_idle_ones():
    cov = [_cov("legacy-prod", sending_enabled=False), _cov("roadtrip-engagement", covered_by=["roadtrip.example"]),
           _cov("site-feedback", sends_recent=0, usage="idle"), _cov("forms-feedback")]
    fs = advisor.routing_findings(cov)
    assert {(f["resource"], f["severity"]) for f in fs} == {("roadtrip-engagement", "info"), ("site-feedback", "info"),
                                                            ("forms-feedback", "high")}
    assert "roadtrip.example already routes" in next(f for f in fs if f["resource"] == "roadtrip-engagement")["detail"]
    assert "create-configuration-set-event-destination" in fs[0]["brief"] and "CloudWatchDestination" not in fs[0]["brief"]


def test_a_handler_the_ledger_cannot_see_is_a_ledger_gap():
    cov = [_cov("marketing", routes_feedback=True, reaches_handler=True, feedback_topics=[TOPIC])]
    (f,) = advisor.routing_findings(cov, sns_topics=[_topic(consumers=["https://api.x/ses"])])
    assert (f["kind"], f["severity"]) == ("ledger_gap", "medium") and "sns subscribe" in f["brief"]


def test_delivery_to_an_ops_topic_does_not_count_and_the_brief_never_edits_it():
    """monitor-prod publishes DELIVERY to monitor-ops-alerts; the old brief would have added
    every delivery to an ops email."""
    ops = "arn:aws:sns:us-east-1:0:ops-alerts"
    cs = [{"name": "monitor-prod", "sending_enabled": True, "event_types": ["BOUNCE", "COMPLAINT", "DELIVERY"],
           "feedback_topics": [ops], "delivery_topics": [ops], "engagement_types": [],
           "destinations": [{"name": "ops", "enabled": True, "type": "sns", "sns": ops, "event_types": ["BOUNCE", "COMPLAINT", "DELIVERY"]}]}]
    topics = [_topic(ops, subscribers=[{"protocol": "email", "endpoint": "ops@x"}])]
    (f,) = advisor.publishing_findings(cs, [], coverage=[_cov("monitor-prod", routes_feedback=True)], sns_topics=topics)
    assert f["missing"] == ["DELIVERY"] and f["severity"] == "medium"
    assert "update-configuration-set-event-destination" not in f["brief"]
    assert "feedback-to-ledger" in f["brief"] and "ops/alert topic" in f["brief"]


def test_delivery_to_a_ledger_topic_is_enough_and_disabled_sets_are_skipped():
    cs = [{"name": "webmail-compose", "sending_enabled": True, "event_types": ["BOUNCE", "COMPLAINT", "DELIVERY"],
           "feedback_topics": [TOPIC], "delivery_topics": [TOPIC], "engagement_types": [],
           "destinations": [{"name": "d", "enabled": True, "type": "sns", "sns": TOPIC, "event_types": ["BOUNCE", "COMPLAINT", "DELIVERY"]}]},
          {"name": "webmail", "sending_enabled": False, "event_types": ["BOUNCE", "OPEN"], "engagement_types": ["OPEN"],
           "destinations": [{"name": "d", "enabled": True, "type": "sns", "sns": TOPIC, "event_types": ["BOUNCE", "OPEN"]}]}]
    assert advisor.publishing_findings(cs, [], sns_topics=[_topic(ledger=True)]) == []


def test_tracking_that_is_on_is_a_note_unless_it_is_policy():
    cs = [{"name": "roadtrip-engagement", "sending_enabled": True, "event_types": ["CLICK", "OPEN", "DELIVERY"],
           "feedback_topics": [], "delivery_topics": [TOPIC], "engagement_types": ["CLICK", "OPEN"],
           "destinations": [{"name": "d", "enabled": True, "type": "sns", "sns": TOPIC, "event_types": ["CLICK", "DELIVERY", "OPEN"]}]}]
    fs = advisor.publishing_findings(cs, [], sns_topics=[_topic(ledger=True)])
    assert _kinds(fs) == {("tracking", "info")} and "rewrites every link" in fs[0]["detail"]
    assert advisor.publishing_findings(cs, [], sns_topics=[_topic(ledger=True)], engagement_tracking=True) == []


def test_identity_topic_with_only_an_email_subscriber_is_a_feedback_gap():
    """photos.example's bounce/complaint topic notify-me only emails a person."""
    idfb = [{"identity": "photos.example", "bounce_topic": TOPIC, "complaint_topic": TOPIC, "delivery_topic": None}]
    ids = [_ident("photos.example")]
    topics = [_topic(subscribers=[{"protocol": "email", "endpoint": "me@x"}])]
    (f,) = advisor.publishing_findings([], idfb, sns_topics=topics, identities=ids)
    assert (f["kind"], f["severity"]) == ("identity_feedback_gap", "medium") and "email me@x" in f["detail"]
    # an ARN we never listed is not evidence of a gap
    (g,) = advisor.publishing_findings([], idfb, sns_topics=[], identities=ids)
    assert g["kind"] == "identity_delivery" and g["severity"] == "info"


def test_identity_delivery_is_skipped_when_the_default_set_publishes_delivery_and_raised_when_bounces_have_no_denominator():
    idfb = [{"identity": "webmail.example", "bounce_topic": TOPIC, "complaint_topic": TOPIC, "delivery_topic": None},
            {"identity": "roadtrip.example", "bounce_topic": TOPIC, "complaint_topic": TOPIC, "delivery_topic": None}]
    ids = [_ident("webmail.example", default_config_set="webmail-unattributed"), _ident("roadtrip.example")]
    cov = [_cov("webmail-unattributed", routes_feedback=True, delivery_to_ledger=True)]
    fs = advisor.publishing_findings([], idfb, coverage=cov, sns_topics=[_topic(ledger=True, consumers=["https://api.roadtrip/ses"])],
                                     identities=ids, identity_activity={"roadtrip.example": {"feedback": 27, "deliveries": 0}})
    assert {(f["resource"], f["severity"]) for f in fs} == {("roadtrip.example", "medium")}
    assert "other subscribers (https://api.roadtrip/ses)" in fs[0]["brief"]


def test_unattributed_sends_share_is_computed_and_graded():
    cov = [_cov("a", sends_recent=7000, routes_feedback=True), _cov("b", sends_recent=694, routes_feedback=True)]
    metrics = {"sends_by_day": [{"date": (NOW.date() - dt.timedelta(days=i)).isoformat(), "count": 1000} for i in range(20)]}
    u = collect.unattributed_sends(metrics, cov, today=NOW.date())
    assert (u["account_sends"], u["config_set_sends"], u["unattributed"]) == (14000, 7694, 6306)
    ids = [_ident("seo.example"), _ident("x.example", default_config_set="a"), _ident("dead.example", verified=False)]
    (f,) = advisor.unattributed_findings(u, ids, [], cov)
    words = {w.strip(".,:") for w in f["detail"].split()}  # identities the detail lists, as whole words
    assert f["severity"] == "medium" and [i["name"] for i in ids if i["name"] in words] == ["seo.example"]
    assert advisor.unattributed_findings({**u, "unattributed": 0, "share": 0.0}, ids, [], cov) == []
    unknown = collect.unattributed_sends(metrics, cov + [_cov("c", sends=None, sends_recent=None, usage="unknown")],
                                         today=NOW.date())
    assert unknown["unattributed"] is None and unknown["unknown_sets"] == ["c"]
    (g,) = advisor.unattributed_findings(unknown, ids, [], cov)
    assert g["severity"] == "info" and "usage is unknown for c" in g["detail"]


def test_pending_subscriptions_on_feedback_topics_are_flagged():
    cs = [{"name": "s", "feedback_topics": [TOPIC], "delivery_topics": []}]
    topics = [_topic(pending=["https://api.x/ses"]), _topic("arn:aws:sns:us-east-1:0:other", pending=["x"])]
    (f,) = advisor.sns_findings(topics, cs, [])
    assert f["resource"] == "fb" and f["severity"] == "medium"


# --------------------------------------------------------------------- collector
def test_covering_identities_match_by_name_or_ledger_and_need_every_candidate_covered():
    idfb = [{"identity": "roadtrip.example", "complaint_topic": TOPIC, "bounce_topic": TOPIC},
            {"identity": "other.example", "complaint_topic": "arn:none", "bounce_topic": None}]
    topics = {TOPIC: _topic(ledger=True)}
    assert collect._covering_identities("roadtrip-engagement", idfb, topics, set()) == ["roadtrip.example"]
    assert collect._covering_identities("roadtrip-engagement", idfb, topics, {"other.example"}) == []
    assert collect._covering_identities("beta-invites", idfb, topics, set()) == []


def test_ledger_rows_are_deduped_across_feeds_and_identity_parsed():
    """One wsg.com bounce was stored twice (config-set feed at :12.334, identity feed at :13) and the
    From display name left the identity as 'mail.example.net>'."""
    a = {"recipient_hash": "h", "ses_message_id": "m1", "event_type": "BOUNCE", "config_set": "", "sending_identity": "x"}
    b = {**a, "config_set": "webmail-forwarding"}
    kept = collect.dedupe_ledger_rows([a, b])
    assert kept == [b]
    assert collect.dedupe_ledger_rows([b, a]) == [b]
    assert collect._identity_domain("mail.example.net>") == "mail.example.net"
    assert collect._identity_domain('"Name" <x@mail.example.net>') == "mail.example.net"


def test_config_set_complaints_come_from_per_set_metrics_not_list_metrics():
    m = {"a": {"complaints": 8, "complaint_days": [{"date": "2026-08-30", "count": 1}]}, "b": {"complaints": 0, "complaint_days": []},
         "c": {"complaints": None, "complaint_days": []}}
    assert collect.config_set_complaints(m) == [{"config_set": "a", "events": 8, "days": [{"date": "2026-08-30", "count": 1}]}]


def test_only_complaint_ledger_rows_are_complainants(monkeypatch, tmp_path):
    from tests.test_vdm_coverage_tdd import _patch_aws

    ops: list[str] = []
    _patch_aws(monkeypatch, ops)
    monkeypatch.setattr(collect, "collect_ledger", lambda e: {
        "configured": True, "table": "ledger", "oldest": None, "newest": None,
        "records": [{"email": "a***@x", "recipient_hash": "h1", "last_seen": "2026-10-20T00:00:00+00:00", "event_type": "BOUNCE",
                     "simulator": False, "hits": 1},
                    {"email": "b***@x", "recipient_hash": "h2", "last_seen": "2026-10-21T00:00:00+00:00", "event_type": "COMPLAINT",
                     "simulator": False, "hits": 1}]})
    model = collect.collect_all(cache_dir=tmp_path, resolver=StubResolver())
    assert model["attribution"]["ledger_records"] == 1
    assert [r["event_type"] for r in model["complaint_records"] if "event_type" in r] == ["COMPLAINT"]


# --------------------------------------------------------------------- ingest Lambda
class _Table:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803
        from botocore.exceptions import ClientError
        key = (Item["recipient_hash"], Item["sk"])
        cur = self.items.get(key)
        if cur is not None and ConditionExpression:
            if not ("attribute_not_exists(config_set)" in ConditionExpression and "config_set" not in cur):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = Item


def _bounce(ts, source, tags=True):
    ev = {"notificationType": "Bounce",
          "mail": {"messageId": "010001a15b38", "source": source, "timestamp": "2026-10-20T21:30:12.000Z"},
          "bounce": {"bounceType": "Permanent", "bounceSubType": "General", "timestamp": ts,
                     "bouncedRecipients": [{"emailAddress": "u@wsg.com"}]}}
    if tags:
        ev["mail"]["tags"] = {"ses:configuration-set": ["webmail-forwarding"]}
    return ev


def _env(ev, topic):
    return {"Records": [{"Sns": {"Type": "Notification", "MessageId": "s", "TopicArn": f"arn:aws:sns:us-east-1:0:{topic}",
                                 "Message": json.dumps(ev)}}]}


@pytest.mark.parametrize("first_is_identity", [False, True])
def test_the_same_bounce_from_both_feeds_writes_one_row_that_knows_the_config_set(monkeypatch, first_is_identity):
    table = _Table()
    monkeypatch.setattr(ledger, "_TABLE_HANDLE", table)
    cs = _env(_bounce("2026-10-20T21:30:12.334Z", '"Example" <noreply@mail.example.net>'), "webmail-ses-events")
    ident = _env(_bounce("2026-10-20T21:30:13.000Z", "noreply@mail.example.net", tags=False), "webmail-ses-bounces")
    for env in ([ident, cs] if first_is_identity else [cs, ident]):
        ledger.handler(env, None)
    (row,) = table.items.values()
    assert row["sk"] == "BOUNCE#010001a15b38"
    assert row["config_set"] == "webmail-forwarding"
    assert row["sending_identity"] == "mail.example.net"
