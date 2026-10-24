# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""SES's DKIM status only says whether SES signs. Apps that sign before
SendRawEmail (a webmail app) leave Easy DKIM at NOT_STARTED, and that must not
be reported as "fails DMARC" (regression: a domain showed 100/100 in
the app while this dashboard flagged it high)."""

from __future__ import annotations

from sesreport import advisor, config, fixbriefs


class NoDns:
    def txt(self, _name):
        return []

    def mx(self, _name):
        return []


def _ident(name, status, project=""):
    return {"name": name, "type": "DOMAIN", "dkim_status": status, "project": project,
            "mail_from_domain": f"bounce.{name}", "mail_from_status": "SUCCESS"}


def _dkim(findings):
    return {f["resource"]: (f["kind"], f["severity"]) for f in findings if f["kind"].startswith("dkim")}


def test_app_signed_identity_passes_without_a_finding():
    fs, checks = advisor.auth_findings([_ident("bank.example", "NOT_STARTED", "WebMail")], NoDns())
    assert _dkim(fs) == {}
    assert checks["bank.example"]["dkim"] == "pass · signed by WebMail"


def test_app_signed_project_match_is_case_insensitive_and_configurable():
    ids = [_ident("a.example", "NOT_STARTED", "webmail"), _ident("b.example", "NOT_STARTED", "Billing")]
    assert _dkim(advisor.auth_findings(ids, NoDns())[0]) == {"b.example": ("dkim_unsigned", "info")}
    assert _dkim(advisor.auth_findings(ids, NoDns(), app_signed=["billing", "WebMail"])[0]) == {}


def test_untagged_not_started_is_an_info_note_not_high():
    fs, checks = advisor.auth_findings([_ident("studio.example", "NOT_STARTED")], NoDns())
    assert _dkim(fs) == {"studio.example": ("dkim_unsigned", "info")}
    assert checks["studio.example"]["dkim"] == "SES not signing"
    (f,) = [f for f in fs if f["kind"] == "dkim_unsigned"]
    assert "fails DMARC alignment." not in f["detail"].split(";")[0]


def test_broken_ses_dkim_stays_high_even_for_app_projects():
    ids = [_ident("x.example", s, "WebMail") for s in ("FAILED", "PENDING", "TEMPORARY_FAILURE", "")]
    ids = [{**i, "name": f"{n}.example"} for n, i in enumerate(ids)]
    assert set(_dkim(advisor.auth_findings(ids, NoDns())[0]).values()) == {("dkim", "high")}


def test_unsigned_brief_verifies_before_enabling_easy_dkim():
    md = fixbriefs.dkim_unsigned_brief("studio.example", "us-east-1")
    assert [line for line in md.splitlines() if line.startswith("## ")] == [f"## {s}" for s in fixbriefs.SECTIONS]
    steps = md.split("## Steps")[1].split("## Commands")[0]
    assert steps.index("DKIM-Signature") < steps.index("enable Easy DKIM")
    assert "--signing-enabled" in md and "only if no app signs" in md


def test_default_app_dkim_projects(monkeypatch):
    monkeypatch.delenv("SESREPORT_APP_DKIM_PROJECTS", raising=False)
    assert config.load().app_dkim_projects == ["WebMail"]
    monkeypatch.setenv("SESREPORT_APP_DKIM_PROJECTS", "WebMail, Billing")
    assert config.load().app_dkim_projects == ["WebMail", "Billing"]


class _Ses:
    """GetEmailIdentity already returns Tags (and no SendingEnabled: that field
    only exists on the list call), so no ListTagsForResource round trip."""

    def __init__(self, tags=True, sending=True):
        self.tags, self.sending = tags, sending

    def list_email_identities(self, **_kw):
        return {"EmailIdentities": [{"IdentityName": "bank.example", "IdentityType": "DOMAIN",
                                     "SendingEnabled": self.sending, "VerificationStatus": "SUCCESS"}]}

    def get_email_identity(self, EmailIdentity):  # noqa: N803
        return {"IdentityType": "DOMAIN", "DkimAttributes": {"Status": "NOT_STARTED"},
                "VerifiedForSendingStatus": True, "VerificationStatus": "SUCCESS",
                "Tags": [{"Key": "Project", "Value": "WebMail"}] if self.tags else []}


def _collect(monkeypatch, ses):
    from sesreport import collect
    monkeypatch.setattr(collect, "_client", lambda *a, **k: ses)
    monkeypatch.setattr(collect, "_ACCOUNT_ID", "000000000000")
    monkeypatch.setattr(collect, "REGION", "us-east-1")
    monkeypatch.setattr(collect, "EXTRA_REGIONS", [])
    return collect.collect_identities(collect.Errors())["rows"]


def test_collector_reads_the_project_tag_from_get_email_identity(monkeypatch):
    (row,) = _collect(monkeypatch, _Ses())
    assert row["project"] == "WebMail" and row["tags"] == {"Project": "WebMail"}


def test_untagged_identity_is_kept(monkeypatch):
    (row,) = _collect(monkeypatch, _Ses(tags=False))
    assert row["name"] == "bank.example" and row["project"] == ""


def test_sending_comes_from_the_list_call(monkeypatch):
    # GetEmailIdentity has no SendingEnabled; reading it there showed "no" for every identity
    assert _collect(monkeypatch, _Ses(sending=True))[0]["sending"] is True
    assert _collect(monkeypatch, _Ses(sending=False))[0]["sending"] is False
