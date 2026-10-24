# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Built-in Advisor: the checks VDM's Advisor runs, computed locally with VDM off.

Authentication (DKIM from the SES API; custom MAIL FROM, SPF and DMARC from
public DNS), complaint/bounce thresholds from the ledger breakdowns, and gaps in
Delivery/Open/Click publishing. Every finding carries a Markdown fix brief.
"""

from __future__ import annotations

from . import fixbriefs

COMPLAINT_LINES = ((0.0010, "high"), (0.0008, "medium"))
BOUNCE_LINES = ((0.10, "high"), (0.05, "medium"))
DNS_TIMEOUT_S = 2.0
MAX_LOOKUPS = 120
PUBLISHED = ("DELIVERY", "OPEN", "CLICK")
NOT_CHECKED_TIMEOUT = "not checked: DNS timeout"
NOT_CHECKED_BUDGET = "not checked: lookup budget reached"


class DnsTimeout(Exception):
    pass


class DnsBudget(Exception):
    pass


class DnsResolver:
    """Thin dnspython wrapper: ``txt``/``mx`` return strings, missing names
    return ``[]``, timeouts raise ``DnsTimeout``, and lookups are capped."""

    def __init__(self, lifetime: float = DNS_TIMEOUT_S, max_lookups: int = MAX_LOOKUPS):
        import dns.resolver

        self._dns = dns
        self._r = dns.resolver.Resolver()
        self._r.lifetime = self._r.timeout = lifetime
        self.lifetime = lifetime
        self.max_lookups = max_lookups
        self.lookups = 0
        self.timeouts = 0

    def _query(self, name: str, rtype: str):
        if self.lookups >= self.max_lookups:
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


def _dmarc_records(resolver, domain: str) -> list[str]:
    recs = [t for t in resolver.txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]
    labels = domain.split(".")
    if not recs and len(labels) > 2:  # fall back to the organisational domain
        org = ".".join(labels[-2:])
        recs = [t for t in resolver.txt(f"_dmarc.{org}") if t.lower().startswith("v=dmarc1")]
    return recs


def _tags(record: str) -> dict:
    out = {}
    for part in record.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _dns_note(exc: Exception) -> str:
    return NOT_CHECKED_BUDGET if isinstance(exc, DnsBudget) else NOT_CHECKED_TIMEOUT


def auth_findings(identities: list[dict], resolver, region: str = "") -> tuple[list[dict], dict]:
    """DKIM, MAIL FROM/SPF and DMARC findings for DOMAIN identities, plus a
    per-domain check grid for the Authentication panel."""
    findings: list[dict] = []
    checks: dict[str, dict] = {}
    for ident in identities:
        if (ident.get("type") or "").upper() not in ("DOMAIN", ""):
            continue
        domain = ident["name"]
        row = checks.setdefault(domain, {})
        status = ident.get("dkim_status") or ""
        if status == "SUCCESS":
            row["dkim"] = "pass"
        else:
            row["dkim"] = "fail"
            findings.append(_finding("dkim", "high", domain, f"DKIM is {status or 'not set up'} for {domain}",
                                     "Mail from this identity is not DKIM-signed, so it fails DMARC alignment.",
                                     fixbriefs.dkim_brief(domain, status, region)))
        mail_from = ident.get("mail_from_domain") or ""
        if not mail_from:
            row["mail_from"] = "default (amazonses.com)"
            row["spf"] = "aligned to amazonses.com"
            findings.append(_finding("mail_from", "medium", domain, f"No custom MAIL FROM for {domain}",
                                     "SPF aligns to amazonses.com, not your domain.",
                                     fixbriefs.mail_from_brief(domain, region)))
        else:
            row["mail_from"] = mail_from
            try:
                spf = [t for t in resolver.txt(mail_from) if t.lower().startswith("v=spf1")]
                if any("include:amazonses.com" in t.lower() for t in spf):
                    row["spf"] = "pass"
                else:
                    row["spf"] = "fail"
                    findings.append(_finding("spf", "medium", mail_from, f"SPF for {mail_from} lacks amazonses.com",
                                             "SES's return-path servers are not authorised by SPF.",
                                             fixbriefs.spf_brief(mail_from)))
            except (DnsTimeout, DnsBudget) as e:
                row["spf"] = _dns_note(e)
        try:
            recs = _dmarc_records(resolver, domain)
        except (DnsTimeout, DnsBudget) as e:
            row["dmarc"] = _dns_note(e)
            continue
        if not recs:
            row["dmarc"] = "missing"
            findings.append(_finding("dmarc", "high", domain, f"No DMARC record for {domain}",
                                     "Gmail and Yahoo require DMARC for bulk senders.",
                                     fixbriefs.dmarc_brief(domain, "missing")))
            continue
        tags = _tags(recs[0])
        policy = (tags.get("p") or "").lower()
        if policy == "none":
            row["dmarc"] = "p=none"
            findings.append(_finding("dmarc", "medium", domain, f"DMARC for {domain} is p=none",
                                     "A monitor-only policy lets spoofed mail through.",
                                     fixbriefs.dmarc_brief(domain, "p=none")))
        elif "rua" not in tags:
            row["dmarc"] = f"p={policy}, no rua"
            findings.append(_finding("dmarc", "info", domain, f"DMARC for {domain} has no rua reporting address",
                                     "No aggregate reports show who sends as this domain.",
                                     fixbriefs.dmarc_brief(domain, "no rua")))
        else:
            row["dmarc"] = "pass"
    return findings, checks


def _severity(rate: float | None, lines) -> str | None:
    if rate is None:
        return None
    for line, sev in lines:
        if rate >= line - 1e-12:
            return sev
    return None


def threshold_findings(window: dict, floor: int = 1000) -> list[dict]:
    """Complaint/bounce threshold findings for every row past the floor."""
    out = []
    groups = (("providers", "Mailbox provider"), ("identities", "Sending identity"), ("config_sets", "Config set"))
    for key, label in groups:
        for r in window.get(key) or []:
            if r.get("deliveries", 0) < floor:
                continue
            for metric, rate_key, lines in (("complaint", "complaint_rate", COMPLAINT_LINES),
                                            ("bounce", "bounce_rate", BOUNCE_LINES)):
                sev = _severity(r.get(rate_key), lines)
                if not sev:
                    continue
                line = next(v for v, s in lines if s == sev)
                out.append(_finding(f"{metric}_rate", sev, f"{key}/{r['name']}",
                                    f"{label} {r['name']}: {metric} rate {r[rate_key] * 100:.3f}%",
                                    f"Over {r['deliveries']:,} deliveries, at or above the {line * 100:.2f}% line.",
                                    fixbriefs.threshold_brief(label, r["name"], metric, r[rate_key], r["deliveries"], line),
                                    group=key, name=r["name"]))
    return out


def publishing_findings(config_sets: list[dict], identity_feedback: list[dict], region: str = "") -> list[dict]:
    """Config sets and identity topics that do not feed Delivery/Open/Click to SNS."""
    out = []
    for cs in config_sets:
        if not cs.get("destinations"):
            continue  # no destinations at all is the routing finding's job
        missing = [t for t in PUBLISHED if t not in (cs.get("event_types") or [])]
        if not missing:
            continue
        sns_dest = next((d for d in cs["destinations"] if d.get("sns")), None)
        out.append(_finding("publishing", "medium", cs["name"],
                            f"{cs['name']} does not publish {', '.join(missing)} events",
                            "Without these events the ledger cannot compute rates or engagement for this path.",
                            fixbriefs.publishing_brief(cs["name"], missing, sns_dest, region), missing=missing))
    for row in identity_feedback:
        topic = row.get("complaint_topic") or row.get("bounce_topic")
        if topic and not row.get("delivery_topic"):
            out.append(_finding("identity_delivery", "info", row["identity"],
                                f"{row['identity']} does not send Delivery notifications",
                                "Sends without a config set from this identity have no delivery count.",
                                fixbriefs.identity_delivery_brief(row["identity"], topic, region)))
    return out


def routing_findings(coverage: list[dict], region: str = "") -> list[dict]:
    """BOUNCE/COMPLAINT routing gaps (the coverage grid's Fix buttons)."""
    return [
        _finding("routing", "high" if c.get("risk") == "high" else "medium", c["config_set"],
                 f"{c['config_set']} does not route BOUNCE/COMPLAINT events",
                 "Complaints sent through this config set never reach your handler"
                 + (f" ({c['complaints']} recorded)" if c.get("complaints") else "")
                 + ". SES still suppresses the address at account level.",
                 fixbriefs.routing_brief(c["config_set"], region))
        for c in coverage if not c.get("routes_feedback")
    ]


RANK = {"high": 0, "medium": 1, "info": 2}


def run(*, identities: list[dict], coverage: list[dict], config_sets: list[dict], identity_feedback: list[dict],
        window: dict | None, resolver, region: str = "", floor: int = 1000, auth=None) -> dict:
    if auth is None:
        auth = auth_findings(identities, resolver, region) if resolver else ([], {})
    findings, checks = list(auth[0]), auth[1]
    findings += routing_findings(coverage, region)
    findings += publishing_findings(config_sets, identity_feedback, region)
    if window:
        findings += threshold_findings(window, floor)
    findings.sort(key=lambda f: (RANK.get(f["severity"], 3), f["kind"], f["resource"]))
    return {"findings": findings, "checks": checks,
            "dns": {"lookups": getattr(resolver, "lookups", 0), "timeouts": getattr(resolver, "timeouts", 0)}}
