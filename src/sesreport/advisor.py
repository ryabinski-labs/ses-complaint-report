# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Built-in Advisor: the checks VDM's Advisor runs, computed locally with VDM off.

Authentication (DKIM from the SES API; custom MAIL FROM, SPF and DMARC from
public DNS), complaint/bounce thresholds (SES's own account rates plus the
ledger breakdowns), and gaps in feedback routing and Delivery publishing.
Every finding carries a Markdown fix brief.

Fidelity rules, each learnt from a false positive or negative in production:
- an unverified identity cannot send, so none of its auth checks apply;
- SES's DKIM status only says whether *SES* signs; an app may sign instead;
- a CloudWatch destination counts events, it does not deliver them anywhere;
- a disabled configuration set is not a gap; an idle one is a note;
- OPEN/CLICK publishing is off by policy, so its absence is never a finding;
- SES enforces on ``Reputation.ComplaintRate`` whatever the ledger says.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable

from . import fixbriefs
from .breakdowns import counts_toward_deliverability

COMPLAINT_LINES = ((0.0010, "high"), (0.0008, "medium"))
BOUNCE_LINES = ((0.10, "high"), (0.05, "medium"))
BULK_SENDER_PER_DAY = 5000  # Gmail/Yahoo bulk-sender line: DMARC becomes mandatory
DNS_TIMEOUT_S = 2.0
MAX_LOOKUPS = 160
DNS_DEADLINE_S = 10.0  # all DNS work in one refresh, so slow nameservers cannot stall it
DMARC_WALK_MAX = 5  # DMARCbis tree walk: how many parents to try
PUBLISHED = ("DELIVERY",)
ENGAGEMENT = ("OPEN", "CLICK")
NOT_CHECKED_TIMEOUT = "not checked: DNS timeout"
NOT_CHECKED_BUDGET = "not checked: lookup budget reached"
NOT_APPLICABLE = "n/a (identity not verified)"
TEST_LABELS = {"qa", "test", "testing", "staging", "stage", "dev", "sandbox"}
SPF_RE = re.compile(r"^v=spf1(\s|$)", re.IGNORECASE)
DKIM_RE = re.compile(r"(^|;)\s*p=([A-Za-z0-9+/=]+)")


class DnsTimeout(Exception):
    pass


class DnsBudget(Exception):
    pass


class DnsResolver:
    """Thin dnspython wrapper: ``txt``/``mx`` return strings, missing names
    return ``[]``, timeouts raise ``DnsTimeout``, and lookups are capped."""

    def __init__(self, lifetime: float = DNS_TIMEOUT_S, max_lookups: int = MAX_LOOKUPS,
                 deadline_s: float = DNS_DEADLINE_S, clock=time.monotonic):
        import dns.resolver

        self._dns = dns
        self._r = dns.resolver.Resolver()
        self._r.lifetime = self._r.timeout = lifetime
        self.lifetime = lifetime
        self.max_lookups = max_lookups
        self.lookups = 0
        self.timeouts = 0
        self.deadline_s = deadline_s
        self.clock = clock
        self._started: float | None = None

    def _query(self, name: str, rtype: str):
        now = self.clock()
        if self._started is None:
            self._started = now
        if self.lookups >= self.max_lookups or now - self._started >= self.deadline_s:
            raise DnsBudget(name)
        self.lookups += 1
        try:
            return self._r.resolve(name, rtype)
        except (self._dns.resolver.NXDOMAIN, self._dns.resolver.NoAnswer, self._dns.resolver.NoNameservers):
            return []
        except self._dns.exception.Timeout as e:
            self.timeouts += 1
            raise DnsTimeout(name) from e

    def txt(self, name: str) -> list[str]:
        return [b"".join(r.strings).decode("utf-8", "replace") for r in self._query(name, "TXT")]

    def mx(self, name: str) -> list[str]:
        return [str(r.exchange).rstrip(".") for r in sorted(self._query(name, "MX"), key=lambda r: r.preference)]


def make_resolver() -> DnsResolver:
    return DnsResolver()


def _finding(kind: str, severity: str, resource: str, title: str, detail: str, brief: str, **extra) -> dict:
    return {"id": f"{kind}:{resource}", "kind": kind, "severity": severity, "resource": resource,
            "title": title, "detail": detail, "brief": brief, **extra}


# --------------------------------------------------------------------------- #
# DNS record readers
# --------------------------------------------------------------------------- #
def _dmarc_at(resolver, name: str) -> list[str]:
    return [t for t in resolver.txt(f"_dmarc.{name}") if t.lower().startswith("v=dmarc1")]


def _dmarc_records(resolver, domain: str) -> tuple[list[str], str]:
    """DMARC records for ``domain`` and the name they were found at.

    Walks up the parent labels (the DMARCbis tree walk) instead of guessing the
    organisational domain from the last two labels, which would turn a
    subdomain of ``ollielabs.com.br`` into a lookup of ``_dmarc.com.br``.
    """
    labels = domain.split(".")
    recs = _dmarc_at(resolver, domain)
    if recs:
        return recs, domain
    for i in range(1, min(DMARC_WALK_MAX, len(labels) - 1)):
        parent = ".".join(labels[i:])
        if _is_public_suffix(parent):
            break
        recs = _dmarc_at(resolver, parent)
        if recs:
            return recs, parent
    return [], domain


# Second-level labels that are themselves registries under a ccTLD (com.br,
# co.uk, ac.jp ...): a record there would belong to nobody in particular.
_CC_SECOND_LEVEL = {"com", "net", "org", "co", "gov", "edu", "ac", "ne", "or", "go", "mil", "gob", "nom", "ltd", "plc"}


def _is_public_suffix(name: str) -> bool:
    labels = name.lower().split(".")
    return len(labels) == 2 and len(labels[-1]) == 2 and labels[0] in _CC_SECOND_LEVEL


def _tags(record: str) -> dict:
    out = {}
    for part in record.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _dns_note(exc: Exception) -> str:
    return NOT_CHECKED_BUDGET if isinstance(exc, DnsBudget) else NOT_CHECKED_TIMEOUT


def _spf_verdict(records: list[str]) -> tuple[bool, str]:
    """Strict SPF check for a MAIL FROM domain.

    Exactly one ``v=spf1`` record (two is a permerror), ``include:amazonses.com``
    as a whole token (``v=spf1include:amazonses.com-all``, a record published as
    three unjoined strings, must not pass) and a terminal ``-all``/``~all``.
    The MX is SES's own concern: MailFromDomainStatus is SUCCESS only when it
    resolves to feedback-smtp, so it is not re-checked here.
    """
    spf = [t for t in records if SPF_RE.match(t.strip())]
    if not spf:
        return False, "no v=spf1 record"
    if len(spf) > 1:
        return False, f"{len(spf)} SPF records (permerror)"
    tokens = spf[0].strip().split()
    if "include:amazonses.com" not in {t.lower() for t in tokens}:
        return False, "lacks include:amazonses.com"
    last = tokens[-1].lower()
    if last in ("+all", "all", "?all"):
        return False, f"ends in {last}"
    return True, "pass"


def _looks_like_test(domain: str, test_identities: Iterable[str]) -> bool:
    d = domain.lower()
    if d in {t.lower() for t in test_identities}:
        return True
    # the TLD never counts: steada.dev is a product, not a dev box
    return any(label in TEST_LABELS or label.startswith("qa-") or label.endswith("-qa") for label in d.split(".")[:-1])


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def auth_findings(identities: list[dict], resolver, region: str = "",
                  app_signed: Iterable[str] = ("WebMail",), app_dkim_mx: Iterable[str] = (),
                  test_identities: Iterable[str] = (), sent_24h: int | None = None) -> tuple[list[dict], dict]:
    """DKIM, MAIL FROM/SPF and DMARC findings for DOMAIN identities, plus a
    per-domain check grid for the Authentication panel.

    SES's DKIM status only says whether *SES* signs. NOT_STARTED means SES's
    Easy DKIM was never set up, which is correct when the sending app signs the
    message itself before SendRawEmail (Folio/WebMail does). Identities whose
    Project tag is in ``app_signed``, or whose MX is one of ``app_dkim_mx``
    (identities that predate tagging), count as signed by the app; other
    NOT_STARTED identities get an info note, not a high "fails DMARC" finding.

    Severity follows whether DMARC can pass: a missing MAIL FROM is a note when
    DKIM already aligns and high when nothing aligns. Unverified identities get
    one housekeeping note and no auth checks, because SES will not send from
    them. ``sent_24h`` at or above the bulk-sender line makes a missing DMARC
    record high; a QA/test identity makes it a note.
    """
    findings: list[dict] = []
    checks: dict[str, dict] = {}
    app_signed = {p.lower() for p in app_signed if p}
    app_dkim_mx = {m.lower().rstrip(".") for m in app_dkim_mx if m}
    verified_domains = {i["name"].lower() for i in identities
                        if (i.get("type") or "").upper() in ("DOMAIN", "") and i.get("verified", True)}
    bulk = sent_24h is not None and sent_24h >= BULK_SENDER_PER_DAY

    for ident in identities:
        name = ident["name"]
        itype = (ident.get("type") or "").upper()
        if itype == "EMAIL_ADDRESS":
            domain = name.rsplit("@", 1)[-1].lower()
            if ident.get("verified", True) and domain not in verified_domains:
                findings.append(_finding("identity_email_only", "info", name,
                                         f"{name} is a verified address on a domain SES cannot sign for",
                                         f"Mail sent as {name} through SES fails DMARC at the receiver: SES cannot "
                                         f"DKIM-sign {domain} and the return path is amazonses.com.",
                                         fixbriefs.email_identity_brief(name, region)))
            continue
        if itype not in ("DOMAIN", ""):
            continue
        domain = name
        row = checks.setdefault(domain, {})
        if not ident.get("verified", True):
            vstatus = ident.get("verification_status") or "FAILED"
            row.update(dkim=NOT_APPLICABLE, mail_from=NOT_APPLICABLE, spf=NOT_APPLICABLE, dmarc=NOT_APPLICABLE,
                       note=f"identity {vstatus}; cannot send")
            findings.append(_finding("identity_unverified", "info", domain,
                                     f"{domain} is not verified and cannot send",
                                     f"Verification is {vstatus}, so SES refuses mail from it and its "
                                     "authentication records are moot. Re-verify it or delete it.",
                                     fixbriefs.identity_unverified_brief(domain, vstatus, region)))
            continue
        test = _looks_like_test(domain, test_identities)
        if test:
            row["note"] = "test identity"

        # --- DMARC first: its alignment tags decide how SPF/MAIL FROM are read
        dmarc_tags: dict | None = None
        dmarc_note: str | None = None
        dmarc_source = domain
        try:
            recs, dmarc_source = _dmarc_records(resolver, domain)
        except (DnsTimeout, DnsBudget) as e:
            dmarc_note = _dns_note(e)
            recs = []
        if dmarc_note is None and len(recs) > 1:
            dmarc_note = "invalid (multiple records)"
            findings.append(_finding("dmarc", "medium", domain, f"{domain} publishes {len(recs)} DMARC records",
                                     "Receivers treat more than one DMARC record as none at all.",
                                     fixbriefs.dmarc_brief(domain, "multiple")))
        elif dmarc_note is None and recs:
            dmarc_tags = _tags(recs[0])

        # --- DKIM
        status = ident.get("dkim_status") or ""
        project = ident.get("project") or ""
        dkim_aligned = False
        if status == "SUCCESS":
            row["dkim"] = "pass"
            dkim_aligned = True
            if (ident.get("dkim_key_length") or "") == "RSA_1024_BIT":
                findings.append(_finding("dkim_key_length", "info", domain, f"{domain} signs with a 1024-bit DKIM key",
                                         "Still accepted everywhere, but below current guidance (2048-bit).",
                                         fixbriefs.dkim_key_brief(domain, region)))
        elif status == "NOT_STARTED":
            signer = project if project.lower() in app_signed else ""
            if not signer and app_dkim_mx:
                try:
                    hosts = {h.lower().rstrip(".") for h in resolver.mx(domain)}
                    if hosts & app_dkim_mx:
                        signer = "app (by MX)"
                except (DnsTimeout, DnsBudget):
                    signer = ""  # unknown MX: fall through to the "SES not signing" note
            selector = (ident.get("tags") or {}).get("DkimSelector") or ""
            if signer and selector:
                # the app told us its selector: the key must actually be published
                try:
                    txt = resolver.txt(f"{selector}._domainkey.{domain}")
                    has_key = any("v=dkim1" in t.lower() and DKIM_RE.search(t) for t in txt)
                except (DnsTimeout, DnsBudget):
                    has_key = True  # unknown, not failed
                if not has_key:
                    signer = ""
                    row["dkim"] = "fail"
                    findings.append(_finding("dkim", "high", domain, f"No DKIM key at {selector}._domainkey.{domain}",
                                             f"The identity claims {project} signs it, but the selector has no public key.",
                                             fixbriefs.dkim_brief(domain, "app key missing", region)))
            if signer:
                row["dkim"] = f"pass · signed by {signer}"
                dkim_aligned = True
            elif "dkim" not in row:
                row["dkim"] = "SES not signing"
                findings.append(_finding("dkim_unsigned", "info", domain, f"SES is not DKIM-signing {domain}",
                                         "SES's built-in DKIM is off for this identity. That is expected if the app that "
                                         "sends this mail signs it itself; otherwise its mail fails DMARC alignment.",
                                         fixbriefs.dkim_unsigned_brief(domain, region)))
        else:
            row["dkim"] = "fail"
            findings.append(_finding("dkim", "high", domain, f"DKIM is {status or 'not set up'} for {domain}",
                                     "Mail from this identity is not DKIM-signed, so it fails DMARC alignment.",
                                     fixbriefs.dkim_brief(domain, status, region)))

        # --- MAIL FROM and SPF
        mail_from = ident.get("mail_from_domain") or ""
        mf_status = ident.get("mail_from_status") or ""
        mf_sev = "info" if dkim_aligned else "high"
        aspf_strict = bool(dmarc_tags) and (dmarc_tags.get("aspf") or "r").lower() == "s"
        if not mail_from:
            row["mail_from"] = "default (amazonses.com)"
            row["spf"] = "aligned to amazonses.com"
            findings.append(_finding("mail_from", mf_sev, domain, f"No custom MAIL FROM for {domain}",
                                     "SPF aligns to amazonses.com, not your domain"
                                     + ("; DKIM aligns, so DMARC still passes." if dkim_aligned
                                        else ", and nothing else aligns: DMARC fails."),
                                     fixbriefs.mail_from_brief(domain, region, dkim_aligned=dkim_aligned)))
        elif mf_status and mf_status != "SUCCESS":
            row["mail_from"] = f"{mail_from} ({mf_status} → amazonses.com)"
            row["spf"] = f"n/a (MAIL FROM {mf_status})"
            findings.append(_finding("mail_from", mf_sev, domain, f"MAIL FROM {mail_from} is {mf_status}",
                                     "SES falls back to amazonses.com, so SPF does not align"
                                     + ("; DKIM aligns, so DMARC still passes." if dkim_aligned
                                        else " and DMARC fails."),
                                     fixbriefs.mail_from_brief(domain, region, status=mf_status, configured=mail_from,
                                                               dkim_aligned=dkim_aligned)))
        else:
            row["mail_from"] = mail_from
            try:
                ok, why = _spf_verdict(resolver.txt(mail_from))
                if ok:
                    row["spf"] = "pass (not aligned, aspf=s)" if aspf_strict and mail_from.lower() != domain.lower() else "pass"
                else:
                    row["spf"] = f"fail: {why}"
                    findings.append(_finding("spf", "info" if dkim_aligned else "high", mail_from,
                                             f"SPF for {mail_from} {why}",
                                             "SES's return-path servers are not authorised by a valid SPF record"
                                             + ("; DKIM aligns, so DMARC still passes." if dkim_aligned
                                                else " and nothing else aligns."),
                                             fixbriefs.spf_brief(mail_from)))
            except (DnsTimeout, DnsBudget) as e:
                row["spf"] = _dns_note(e)

        # --- DMARC verdict
        if dmarc_note is not None:
            row["dmarc"] = dmarc_note
            continue
        if not dmarc_tags:
            row["dmarc"] = "missing"
            sev = "info" if test else "high" if bulk else "medium"
            findings.append(_finding("dmarc", sev, domain, f"No DMARC record for {domain}",
                                     ("Gmail and Yahoo require DMARC for bulk senders." if bulk else
                                      "Receivers cannot tell your mail from spoofed mail; Gmail and Yahoo require "
                                      "DMARC from bulk senders."),
                                     fixbriefs.dmarc_brief(domain, "missing")))
            continue
        inherited = dmarc_source.lower() != domain.lower()
        policy = ((dmarc_tags.get("sp") if inherited and dmarc_tags.get("sp") else dmarc_tags.get("p")) or "").lower()
        via = f" (via {dmarc_source}, sp={policy})" if inherited else ""
        if policy not in ("none", "quarantine", "reject"):
            row["dmarc"] = "invalid (no p=)"
            findings.append(_finding("dmarc", "medium", domain, f"DMARC for {domain} has no valid p= policy",
                                     f"The record is `{recs[0][:60]}`; without p= no policy applies (RFC 7489 §6.6.3). "
                                     "Some DNS hosts cut TXT values at the first ';'.",
                                     fixbriefs.dmarc_brief(domain, "invalid")))
        elif policy == "none":
            row["dmarc"] = f"p=none{via}"
            findings.append(_finding("dmarc", "info" if test else "medium", domain, f"DMARC for {domain} is p=none",
                                     "A monitor-only policy lets spoofed mail through.",
                                     fixbriefs.dmarc_brief(domain, "p=none")))
        else:
            pct = dmarc_tags.get("pct", "100")
            if pct.isdigit() and int(pct) < 100:
                row["dmarc"] = f"p={policy}, pct={pct}{via}"
                findings.append(_finding("dmarc", "info", domain, f"DMARC for {domain} applies to {pct}% of mail",
                                         "pct<100 leaves the rest unenforced.", fixbriefs.dmarc_brief(domain, "pct")))
            elif "rua" not in dmarc_tags:
                row["dmarc"] = f"p={policy}, no rua{via}"
                findings.append(_finding("dmarc", "info", domain, f"DMARC for {domain} has no rua reporting address",
                                         "No aggregate reports show who sends as this domain.",
                                         fixbriefs.dmarc_brief(domain, "no rua")))
            else:
                row["dmarc"] = f"pass{via}"
    return findings, checks


# --------------------------------------------------------------------------- #
# Rates
# --------------------------------------------------------------------------- #
def _severity(rate: float | None, lines) -> str | None:
    if rate is None:
        return None
    for line, sev in lines:
        if rate >= line - 1e-12:
            return sev
    return None


def _line(lines, sev: str) -> float:
    return next(v for v, s in lines if s == sev)


def account_findings(complaint: float | None, bounce: float | None) -> list[dict]:
    """SES's own account-wide reputation rates against the same lines as the
    ledger rows. These are the numbers AWS enforces on, and they need no
    delivery floor: SES has already applied its own."""
    out = []
    for metric, rate, lines in (("complaint", complaint, COMPLAINT_LINES), ("bounce", bounce, BOUNCE_LINES)):
        sev = _severity(rate, lines)
        if not sev:
            continue
        line = _line(lines, sev)
        out.append(_finding(f"{metric}_rate", sev, "account",
                            f"Account {metric} rate {rate * 100:.3f}% is at or above the {line * 100:.2f}% line",
                            f"SES's Reputation.{metric.capitalize()}Rate, the figure AWS reviews on"
                            + (" (Gmail sends no complaint feedback, so it is not in this number)." if metric == "complaint" else "."),
                            fixbriefs.account_rate_brief(metric, rate, line), group="account", name="account"))
    return out


def threshold_findings(window: dict, floor: int = 1000) -> list[dict]:
    """Complaint/bounce threshold findings for every row past the floor."""
    out = []
    groups = (("providers", "Mailbox provider"), ("identities", "Sending identity"), ("config_sets", "Config set"))
    for key, label in groups:
        for r in window.get(key) or []:
            sends = r.get("deliveries", 0) + r.get("bounces", 0)
            if sends < floor:
                continue
            for metric, rate_key, lines in (("complaint", "complaint_rate", COMPLAINT_LINES),
                                            ("bounce", "bounce_rate", BOUNCE_LINES)):
                sev = _severity(r.get(rate_key), lines)
                if not sev:
                    continue
                line = _line(lines, sev)
                out.append(_finding(f"{metric}_rate", sev, f"{key}/{r['name']}",
                                    f"{label} {r['name']}: {metric} rate {r[rate_key] * 100:.3f}%",
                                    f"Over {sends:,} sends, at or above the {line * 100:.2f}% line.",
                                    fixbriefs.threshold_brief(label, r["name"], metric, r[rate_key], sends, line),
                                    group=key, name=r["name"]))
    return out


# --------------------------------------------------------------------------- #
# Routing and publishing
# --------------------------------------------------------------------------- #
def _ledger_topics(sns_topics: list[dict]) -> list[str]:
    return [t["arn"] for t in sns_topics if t.get("ledger_subscribed")]


def _idle_sev(usage: str, default: str) -> str:
    return "info" if usage == "idle" else default


def routing_findings(coverage: list[dict], region: str = "", sns_topics: list[dict] | None = None) -> list[dict]:
    """BOUNCE/COMPLAINT routing gaps (the coverage grid's Fix buttons), and
    sets that route to a handler the ledger does not read."""
    sns_topics = sns_topics or []
    ledger_topics = _ledger_topics(sns_topics)
    by_arn = {t["arn"]: t for t in sns_topics}
    out = []
    for c in coverage:
        if not c.get("sending_enabled", True):
            continue  # cannot send: not a gap
        name = c["config_set"]
        usage = c.get("usage") or "unknown"
        if not c.get("routes_feedback"):
            covered = c.get("covered_by") or []
            sev = c.get("risk") if c.get("risk") in ("high", "medium", "info") else "medium"
            if covered:
                detail = (f"No destination hears them, but {', '.join(covered)} already route"
                          f"{'s' if len(covered) == 1 else ''} bounces and complaints through identity-level "
                          "notifications, which fire whatever config set a message used. Only another identity "
                          "sending through this set would go unseen.")
            elif usage == "idle":
                detail = ("Nothing used this set in the last 14 days. The gap is latent: the first complaint through "
                          "it would reach no handler. SES still suppresses the address at account level.")
            else:
                detail = ("Complaints sent through this config set never reach your handler or the ledger"
                          + (f" ({c['complaints']} recorded)" if c.get("complaints") else "")
                          + ". SES still suppresses the address at account level.")
            out.append(_finding("routing", sev, name, f"{name} does not route BOUNCE/COMPLAINT events", detail,
                                fixbriefs.routing_brief(name, region, ledger_topics, covered, usage)))
            continue
        if not c.get("reaches_ledger"):
            topics = [a for a in (c.get("feedback_topics") or [])]
            names = [by_arn.get(a, {}).get("name") or a.split(":")[-1] for a in topics]
            out.append(_finding("ledger_gap", _idle_sev(usage, "medium"), name,
                                f"{name} routes feedback the ledger never sees",
                                f"Its BOUNCE/COMPLAINT destination ({', '.join(names) or 'SNS/Firehose'}) reaches a handler "
                                "but not the ledger, so this path has no per-provider rates and its complaints are "
                                "missing from the recipient-level records.",
                                fixbriefs.ledger_gap_brief(name, topics, region)))
    return out


def publishing_findings(config_sets: list[dict], identity_feedback: list[dict], region: str = "",
                        coverage: list[dict] | None = None, sns_topics: list[dict] | None = None,
                        engagement_tracking: bool = False, identities: list[dict] | None = None,
                        identity_activity: dict | None = None) -> list[dict]:
    """Config sets that do not feed Delivery (and, only when tracking is policy,
    Open/Click) to a ledger-subscribed topic; identity topics without Delivery;
    tracking that is switched on; feedback topics with no reader."""
    sns_topics = sns_topics or []
    cov = {c["config_set"]: c for c in coverage or []}
    by_arn = {t["arn"]: t for t in sns_topics}
    ledger_topics = _ledger_topics(sns_topics)
    wanted = PUBLISHED + (ENGAGEMENT if engagement_tracking else ())
    out = []
    for cs in config_sets:
        if not cs.get("sending_enabled", True):
            continue
        c = cov.get(cs["name"], {})
        usage = c.get("usage") or "unknown"
        if not cs.get("destinations"):
            continue  # no destinations at all is the routing finding's job
        # a DELIVERY event only counts when it lands on a topic the ledger reads
        delivery_ok = any(by_arn.get(a, {}).get("ledger_subscribed") for a in cs.get("delivery_topics") or [])
        missing = [t for t in wanted if (t == "DELIVERY" and not delivery_ok)
                   or (t != "DELIVERY" and t not in (cs.get("event_types") or []))]
        if missing:
            # only an existing destination that already feeds the ledger may be edited
            editable = next((d for d in cs["destinations"] if d.get("enabled", True) and d.get("sns")
                             and by_arn.get(d["sns"], {}).get("ledger_subscribed")), None)
            out.append(_finding("publishing", _idle_sev(usage, "medium"), cs["name"],
                                f"{cs['name']} does not publish {', '.join(missing)} events to the ledger",
                                ("Without DELIVERY events the ledger cannot compute rates for this path"
                                 + (" (the set's only SNS topic is an alert topic, so a dedicated topic is needed)."
                                    if not editable and cs.get("feedback_topics") else ".")
                                 + (" Nothing used it in the last 14 days." if usage == "idle" else "")),
                                fixbriefs.publishing_brief(cs["name"], missing, editable, region, ledger_topics, usage),
                                missing=missing))
        if not engagement_tracking and cs.get("engagement_types"):
            dest = next((d for d in cs["destinations"] if d.get("enabled", True)
                         and any(t in ENGAGEMENT for t in d.get("event_types") or [])), cs["destinations"][0])
            types = list(cs["engagement_types"])
            out.append(_finding("tracking", "info", cs["name"],
                                f"{cs['name']} has {' and '.join(types).lower()} tracking on",
                                ("SES rewrites every link in this set's mail through its redirect domain" if "CLICK" in types
                                 else "SES adds a tracking pixel to this set's mail")
                                + ". Fine if intended; confirm it is.",
                                fixbriefs.tracking_brief(cs["name"], dest, types, region)))

    ids = {i["name"]: i for i in identities or []}
    for row in identity_feedback:
        ident = row["identity"]
        info = ids.get(ident, {})
        if not info.get("verified", True) or not info.get("sending", True):
            continue
        topic = row.get("complaint_topic") or row.get("bounce_topic")
        if not topic:
            continue
        t = by_arn.get(topic, {})
        # only a topic we actually listed can be called reader-less; an unknown
        # ARN (other account, SNS section failed) is not evidence of a gap
        if t and not t.get("ledger_subscribed") and not t.get("consumers"):
            subs = [f"{s['protocol']} {s['endpoint']}" for s in t.get("subscribers") or []]
            out.append(_finding("identity_feedback_gap", "medium", ident,
                                f"{ident}'s feedback goes only to {t.get('name') or topic.split(':')[-1]}",
                                "That topic has no handler or ledger subscription"
                                + (f" (subscribers: {', '.join(subs)})" if subs else " (no subscribers)")
                                + ", so its complaints are read by nobody and the address keeps getting mail.",
                                fixbriefs.identity_feedback_gap_brief(ident, topic, subs, region)))
            continue
        if row.get("delivery_topic"):
            continue
        default_cs = info.get("default_config_set") or ""
        default = cov.get(default_cs)
        if default and default.get("delivery_to_ledger"):
            continue  # every send from this identity carries a set that publishes DELIVERY
        activity = (identity_activity or {}).get(ident, {})
        sev = "medium" if activity.get("feedback") and not activity.get("deliveries") else "info"
        detail = "Sends without a config set from this identity have no delivery count"
        if sev == "medium":
            detail = (f"The ledger holds {activity['feedback']} bounce/complaint rows for it and no deliveries, so its "
                      "rate can never be computed")
        out.append(_finding("identity_delivery", sev, ident, f"{ident} does not send Delivery notifications",
                            detail + ".", fixbriefs.identity_delivery_brief(ident, topic, region, t.get("consumers"))))
    return out


def unattributed_findings(unattributed: dict | None, identities: list[dict], identity_feedback: list[dict],
                          coverage: list[dict], region: str = "") -> list[dict]:
    """Sends that no configuration set accounts for: their feedback reaches nobody."""
    if not unattributed:
        return []
    routed = {r["identity"] for r in identity_feedback if r.get("complaint_topic") or r.get("bounce_topic")}
    cov = {c["config_set"]: c for c in coverage}
    exposed = sorted(i["name"] for i in identities
                     if (i.get("type") or "DOMAIN").upper() == "DOMAIN" and i.get("verified") and i.get("sending")
                     and i["name"] not in routed
                     and not cov.get(i.get("default_config_set") or "", {}).get("routes_feedback"))
    n = unattributed.get("unattributed")
    if n is None:
        if not exposed:
            return []
        return [_finding("unattributed_sends", "info", "account",
                         f"{len(exposed)} identities can send with no feedback path",
                         f"{', '.join(exposed[:4])}{' and more' if len(exposed) > 4 else ''} have no default config set "
                         "and no identity-level notifications. The unattributed share cannot be computed because usage "
                         f"is unknown for {', '.join(unattributed.get('unknown_sets') or [])}.",
                         fixbriefs.unattributed_sends_brief(exposed, unattributed, region))]
    if n <= 0:
        return []
    share = unattributed.get("share") or 0.0
    sev = "medium" if (share >= 0.05 or n >= 100) else "info"
    return [_finding("unattributed_sends", sev, "account",
                     f"{n:,} of {unattributed['account_sends']:,} sends ({share * 100:.0f}%) carried no configuration set",
                     f"Over the last {unattributed['days']} days their bounces and complaints reached no handler or "
                     f"the ledger. {len(exposed)} identities can send this way"
                     + (f": {', '.join(exposed[:4])}{' and more' if len(exposed) > 4 else ''}." if exposed else "."),
                     fixbriefs.unattributed_sends_brief(exposed, unattributed, region))]


def sns_findings(sns_topics: list[dict], config_sets: list[dict], identity_feedback: list[dict],
                 region: str = "") -> list[dict]:
    """Feedback topics with subscriptions that never confirmed."""
    feedback_arns = {a for cs in config_sets for a in (cs.get("feedback_topics") or []) + (cs.get("delivery_topics") or [])}
    feedback_arns |= {r[k] for r in identity_feedback for k in ("complaint_topic", "bounce_topic", "delivery_topic") if r.get(k)}
    return [_finding("sns_pending", "medium", t["name"], f"{t['name']} has {len(t['pending'])} unconfirmed subscription(s)",
                     f"{', '.join(t['pending'])} receive nothing until confirmed.",
                     fixbriefs.sns_pending_brief(t["arn"], t["pending"], region))
            for t in sns_topics if t.get("pending") and t["arn"] in feedback_arns]


def identity_activity(ledger_rows: list[dict], aggregates: list[dict]) -> dict[str, dict]:
    """Per sending identity: how many real bounce/complaint rows and delivery
    counts the ledger holds.

    ``OnAccountSuppressionList`` and simulator rows are deliberately excluded:
    they are not deliverability failures and AWS does not count them toward the
    bounce rate. Counting them made a suppressed Google Play review address
    look like a 27-bounce identity with no denominator.
    """
    out: dict[str, dict] = {}
    for r in ledger_rows:
        ident = r.get("sending_identity") or ""
        if ident and counts_toward_deliverability(r):
            out.setdefault(ident, {"feedback": 0, "deliveries": 0})["feedback"] += 1
    for a in aggregates:
        if a.get("event") == "DELIVERY" and a.get("identity"):
            out.setdefault(a["identity"], {"feedback": 0, "deliveries": 0})["deliveries"] += int(a.get("n") or 0)
    return out


RANK = {"high": 0, "medium": 1, "info": 2}


def run(*, identities: list[dict], coverage: list[dict], config_sets: list[dict], identity_feedback: list[dict],
        window: dict | None, resolver, region: str = "", floor: int = 1000, auth=None,
        app_signed: Iterable[str] = ("WebMail",), sns_topics: list[dict] | None = None,
        engagement_tracking: bool = False, account_rates: dict | None = None, unattributed: dict | None = None,
        ledger_rows: list[dict] | None = None, aggregates: list[dict] | None = None) -> dict:
    sns_topics = sns_topics or []
    if auth is None:
        auth = auth_findings(identities, resolver, region, app_signed) if resolver else ([], {})
    findings, checks = list(auth[0]), auth[1]
    findings += routing_findings(coverage, region, sns_topics)
    findings += publishing_findings(config_sets, identity_feedback, region, coverage, sns_topics, engagement_tracking,
                                    identities, identity_activity(ledger_rows or [], aggregates or []))
    findings += unattributed_findings(unattributed, identities, identity_feedback, coverage, region)
    findings += sns_findings(sns_topics, config_sets, identity_feedback, region)
    if account_rates:
        findings += account_findings(account_rates.get("complaint"), account_rates.get("bounce"))
    if window:
        findings += threshold_findings(window, floor)
    findings.sort(key=lambda f: (RANK.get(f["severity"], 3), f["kind"], f["resource"]))
    return {"findings": findings, "checks": checks,
            "dns": {"lookups": getattr(resolver, "lookups", 0), "timeouts": getattr(resolver, "timeouts", 0)}}
