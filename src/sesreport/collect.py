# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Live AWS data collection for the SES complaint report.

Every section is isolated: a failing source records an error and the rest of
the report still renders. Returns a plain-dict "model" that the frontend draws.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import advisor as _advisor
from . import breakdowns as _breakdowns
from .config import load as _load_config
from .providers import Classifier, write_private

log = logging.getLogger("sesreport.collect")

CFG = _load_config()

REGION = CFG.region
PROFILE = CFG.profile
EXTRA_REGIONS = CFG.extra_regions

# SES event metrics: the account-wide Complaint count only began publishing on
# 2026-05-18 for the account this tool was built for. Older accounts may have
# more history; the level is only used as a floor for the label.
METRIC_EPOCH = dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc)
RETENTION_DAYS = 455
LOG_GROUP_PREFIX = CFG.log_group_prefix
LOG_GROUP_MATCH = CFG.log_group_match

# SES mailbox-simulator domain. Sending to `complaint@simulator.amazonses.com`
# (or `bounce@…`) makes SES fabricate feedback so a pipeline can be tested end
# to end. Those "complaints" still increment the account-wide Complaint count,
# but they are test artifacts, not a subscriber pressing "spam" — worth
# separating so recorded complaints are not mistaken for real ones.
SIMULATOR_DOMAIN = "simulator.amazonses.com"


def _client(name, region=REGION):
    return boto3.Session(profile_name=PROFILE or None).client(
        name,
        region_name=region,
        config=Config(retries={"max_attempts": 6, "mode": "standard"}),
    )


_ACCOUNT_ID: str | None = None


def _account_id() -> str:
    global _ACCOUNT_ID
    if _ACCOUNT_ID is None:
        try:
            _ACCOUNT_ID = _client("sts").get_caller_identity()["Account"]
        except (ClientError, BotoCoreError):
            _ACCOUNT_ID = "unknown"
    return _ACCOUNT_ID


def _iso(d) -> str:
    if isinstance(d, dt.datetime):
        return d.astimezone(dt.timezone.utc).isoformat()
    return str(d)


def _day(d) -> str:
    """UTC calendar day. CloudWatch datapoints, the ledger's ``day`` and the SES
    console all use UTC; a local-midnight day would label every winter day one
    day early on a BST machine."""
    if isinstance(d, dt.datetime):
        return d.astimezone(dt.timezone.utc).date().isoformat()
    return str(d)[:10]


class Errors:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, section: str, exc: Exception):
        log.warning("collector section %s failed: %s: %s", section, type(exc).__name__, exc)
        self.items.append({"section": section, "error": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------- #
# 1. Enforcement & suppression posture
# --------------------------------------------------------------------------- #
def collect_posture(errors: Errors) -> dict:
    ses = _client("sesv2")
    acc = ses.get_account()
    quota = acc.get("SendQuota", {})
    return {
        "enforcement_status": acc.get("EnforcementStatus", "UNKNOWN"),
        "production_access": bool(acc.get("ProductionAccessEnabled")),
        "sending_enabled": bool(acc.get("SendingEnabled")),
        "suppressed_reasons": acc.get("SuppressionAttributes", {}).get("SuppressedReasons", []),
        "vdm": acc.get("VdmAttributes", {}).get("VdmEnabled", "UNKNOWN"),
        "send_quota": {
            "max_24h": quota.get("Max24HourSend"),
            "rate": quota.get("MaxSendRate"),
            "sent_24h": quota.get("SentLast24Hours"),
        },
        "dedicated_ip_auto_warmup": bool(acc.get("DedicatedIpAutoWarmupEnabled")),
        "pricing_plan": acc.get("PricingAttributes", {}).get("CurrentPlan", "NONE"),
    }


# --------------------------------------------------------------------------- #
# 2. CloudWatch metrics (complaints, sends, bounces, reputation rate)
# --------------------------------------------------------------------------- #
def _aligned_window(days: int) -> tuple[dt.datetime, dt.datetime]:
    """A window whose daily periods align to UTC midnight, so a given complaint
    always lands on the same (UTC) calendar day regardless of when we query,
    and matches the ledger's ``day`` and the SES console."""
    now = dt.datetime.now(dt.timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
    start = end - dt.timedelta(days=days)
    return start, end


def _sum_series(cw, metric, days, dimension=None, extra_dims=None):
    """Return {date: summed value} for an account-wide (or dimension-filtered) metric."""
    start, end = _aligned_window(days)
    dims = []
    if dimension:
        dims.append(dimension)
    if extra_dims:
        dims.extend(extra_dims)
    out = cw.get_metric_statistics(
        Namespace="AWS/SES",
        MetricName=metric,
        StartTime=start,
        EndTime=end,
        Period=86400,
        Statistics=["Sum"],
        Dimensions=dims,
    )
    by_day: dict[str, float] = {}
    for dp in out.get("Datapoints", []):
        by_day[_day(dp["Timestamp"])] = by_day.get(_day(dp["Timestamp"]), 0.0) + dp["Sum"]
    return by_day


def _avg_series(cw, metric, days, dimension=None):
    start, end = _aligned_window(days)
    dims = [dimension] if dimension else []
    out = cw.get_metric_statistics(
        Namespace="AWS/SES",
        MetricName=metric,
        StartTime=start,
        EndTime=end,
        Period=86400,
        Statistics=["Average"],
        Dimensions=dims,
    )
    series = {}
    for dp in out.get("Datapoints", []):
        d = _day(dp["Timestamp"])
        # keep the latest average seen for a given day
        series[d] = dp["Average"]
    return series


def collect_metrics(errors: Errors) -> dict:
    cw = _client("cloudwatch")
    total_days = RETENTION_DAYS

    complaints = _sum_series(cw, "Complaint", total_days)
    sends = _sum_series(cw, "Send", total_days)
    bounces = _sum_series(cw, "Bounce", total_days)
    rate = _avg_series(cw, "Reputation.ComplaintRate", total_days)
    bounce_rate = _avg_series(cw, "Reputation.BounceRate", total_days)

    complaints_list = [{"date": k, "count": int(v)} for k, v in sorted(complaints.items())]
    total_complaints = int(sum(complaints.values()))
    total_sends = int(sum(sends.values()))
    total_bounces = int(sum(bounces.values()))

    rate_series = [{"date": k, "rate": v} for k, v in sorted(rate.items())]
    peak = max(rate_series, key=lambda r: r["rate"]) if rate_series else None
    latest = rate_series[-1] if rate_series else None
    bounce_series = [{"date": k, "rate": v} for k, v in sorted(bounce_rate.items())]

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    retention_start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=total_days)).date().isoformat()
    # The Complaint metric only exists from METRIC_EPOCH (or the first recorded
    # day); dividing it by 455 days of sends understated the rate 25x. Use the
    # sends from the same window instead. This is still "complaints per send,
    # all providers", not SES's enforcement rate (Reputation.ComplaintRate),
    # which only counts providers that send complaint feedback.
    window_start = complaints_list[0]["date"] if complaints_list else METRIC_EPOCH.date().isoformat()
    window_sends = int(sum(v for k, v in sends.items() if k >= window_start))
    return {
        "window": {
            # recorded-complaint window (the Complaint metric only exists here)
            "start": window_start,
            "end": today,
            # full retention window used for the send/bounce denominator
            "retention_start": retention_start,
            "retention_end": today,
            "days": total_days,
            "window_days": (dt.date.fromisoformat(today) - dt.date.fromisoformat(window_start)).days + 1,
        },
        "total_complaints": total_complaints,
        "total_sends": total_sends,
        "window_sends": window_sends,
        "total_bounces": total_bounces,
        "overall_complaint_rate": (total_complaints / window_sends) if window_sends else None,
        "complaints_by_day": complaints_list,
        "sends_by_day": [{"date": k, "count": int(v)} for k, v in sorted(sends.items())],
        "bounces_by_day": [{"date": k, "count": int(v)} for k, v in sorted(bounces.items())],
        "rate_series": rate_series,
        "peak_rate": peak,
        "latest_rate": latest,
        "bounce_rate_series": bounce_series,
        "latest_bounce_rate": bounce_series[-1] if bounce_series else None,
        "aws_review_line": _advisor.COMPLAINT_LINES[0][0],
        # one internal line everywhere: the Advisor's medium line, the hero's
        # "watch closely" band and the attention list all use this value
        "alert_line": _advisor.COMPLAINT_LINES[1][0],
    }


CS_DIM = "ses:configuration-set"
RECENT_DAYS = 14  # "used recently" window for config-set usage


def _cs_series(cw, metric: str, name: str, days: int = RETENTION_DAYS) -> dict[str, float]:
    return _sum_series(cw, metric, days, extra_dims=[{"Name": CS_DIM, "Value": name}])


def _recent(series: dict[str, float], days: int = RECENT_DAYS) -> int:
    cutoff = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days - 1)).isoformat()
    return int(sum(v for k, v in series.items() if k >= cutoff))


def collect_config_set_metrics(errors: Errors, config_sets: list[dict]) -> dict:
    """Per-configuration-set CloudWatch numbers: complaints and sends/usage.

    Every configuration set is queried by name. ``list_metrics`` only returns
    metrics with datapoints in the last 14 days, so enumerating from it hid
    every complaint older than two weeks.

    ``Send``/``Complaint`` with the config-set dimension exist only for sets
    with a CloudWatch event destination; SES's own ``Reputation.*`` metrics
    cover every set with reputation metrics enabled, so they are the fallback
    for usage. Sends are ``None`` only when neither source can know.
    """
    cw = _client("cloudwatch")
    complaint_days = (dt.datetime.now(dt.timezone.utc) - METRIC_EPOCH).days + 1

    def one(cs: dict) -> tuple[str, dict]:
        name = cs["name"]
        publishes_cw = any(d.get("type") == "cloudwatch" and d.get("enabled", True) for d in cs.get("destinations") or [])
        send = _cs_series(cw, "Send", name)
        source = "Send"
        if not send:
            send = _cs_series(cw, "Reputation.DeliveriesEligibleForBounceRate", name)
            source = "Reputation.DeliveriesEligibleForBounceRate" if send else None
        known = bool(send) or publishes_cw or cs.get("reputation_metrics", False)
        complaints = _cs_series(cw, "Complaint", name, complaint_days) if publishes_cw else {}
        return name, {
            "sends": int(sum(send.values())) if known else None,
            "sends_recent": _recent(send) if known else None,
            "last_send": max(send) if send else None,
            "sends_source": source,
            "complaints": int(sum(complaints.values())) if publishes_cw else None,
            "complaint_days": [{"date": k, "count": int(v)} for k, v in sorted(complaints.items()) if v],
        }

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(one, cs): cs["name"] for cs in config_sets}
        for f in as_completed(futs):
            try:
                name, row = f.result()
                out[name] = row
            except (ClientError, BotoCoreError) as e:
                errors.add(f"config_set_metrics:{futs[f]}", e)
    return out


def config_set_complaints(metrics: dict[str, dict]) -> list[dict]:
    """The ``config_set_complaints`` model section from ``collect_config_set_metrics``."""
    out = [{"config_set": n, "events": m["complaints"], "days": m["complaint_days"]}
           for n, m in metrics.items() if m.get("complaints")]
    out.sort(key=lambda x: x["events"], reverse=True)
    return out


# --------------------------------------------------------------------------- #
# 3. Individual complaint records (DynamoDB + app stores)
# --------------------------------------------------------------------------- #
def _scan_all(table, projection=None, filt=None):
    kw = {}
    if projection:
        kw["ProjectionExpression"] = projection
    if filt:
        kw["FilterExpression"] = filt
    items, lek = [], None
    while True:
        if lek:
            kw["ExclusiveStartKey"] = lek
        out = table.scan(**kw)
        items.extend(out.get("Items", []))
        lek = out.get("LastEvaluatedKey")
        if not lek:
            return items


def _maybe_redact(email: str) -> str:
    if not CFG.redact_emails or not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    keep = local[:1]
    return f"{keep}{'*' * max(3, len(local) - 1)}@{domain}"


def _is_simulator(email: str) -> bool:
    """True for AWS SES mailbox-simulator addresses — synthetic feedback that
    inflates the Complaint count but is not a real complainant."""
    return bool(email) and "@" in email and email.rsplit("@", 1)[1].lower() == SIMULATOR_DOMAIN


def _identity_hash(value: str) -> str:
    """A source-independent identity for a recipient address.

    The ledger keeps only a *redacted* address and stores the SHA-256 of the
    real one as ``recipient_hash``; application stores keep the full address.
    Hashing either input with one function lets the merge recognise a single
    complainant across sources instead of counting the redacted ledger row and
    the full store row as two records. A redacted value that is not already a
    hash is returned unchanged so two equally-redacted rows still match.
    """
    v = str(value or "").strip().lower()
    if not v:
        return ""
    if len(v) == 64 and all(c in "0123456789abcdef" for c in v):
        return v  # already a SHA-256 (the ledger's recipient_hash)
    if "*" in v:
        return v  # redacted and unhashable; match on the redacted string
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def merge_complaint_records(
    ledger_records: list[dict], store_records: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Collapse ledger and application-store rows into one row per complainant
    per day, with the ledger authoritative.

    Matching on the address string alone would count a complaint twice once the
    ledger is deployed: the ledger stores a *redacted* address, while an
    application store keeps the full one. Identity is compared with
    ``_identity_hash`` instead. Ledger rows are considered first, so a store row
    never displaces a ledger row for the same complainant and day.

    Returns ``(merged, kept_ledger, kept_store)``; ``merged`` is newest-first.
    """
    seen: set[tuple] = set()

    def _merge(rows: list[dict]) -> list[dict]:
        kept = []
        for r in rows:
            key = (
                _identity_hash(r.get("recipient_hash") or r.get("email", "")),
                (r.get("last_seen") or "")[:10],
            )
            if key in seen:
                continue
            seen.add(key)
            kept.append(r)
        return kept

    kept_ledger = _merge(ledger_records)
    kept_store = _merge(store_records)
    merged = kept_ledger + kept_store
    merged.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    return merged, kept_ledger, kept_store


def collect_complainants(errors: Errors) -> list[dict]:
    if not CFG.complaint_table:
        return []
    res = boto3.Session(profile_name=PROFILE or None).resource("dynamodb", region_name=REGION)
    table = res.Table(CFG.complaint_table)
    rows = _scan_all(table, filt=Attr(CFG.complaint_attr).eq(CFG.complaint_value))
    out = []
    for r in rows:
        out.append(
            {
                "email": _maybe_redact(r.get("email")),
                "arf_provider": r.get("last_bounce_diagnostic", ""),
                "recorded_at": _iso(r.get("email_bounced_at") or r.get("created_at")),
                "bounce_count": int(r.get("bounce_count", 0) or 0),
                "bouncing": bool(r.get("email_bouncing")),
                "auth_provider": r.get("auth_provider", ""),
            }
        )
    out.sort(key=lambda x: x["recorded_at"])
    return out


def _suppression_record(item: dict, store: str) -> dict:
    """Normalise one application suppression row into a recipient-level
    complaint record shared across stores (see ``complaint_records``)."""
    email = item.get("email", "")
    return {
        "email": _maybe_redact(email),
        "store": store,
        "detail": str(item.get("detail", "") or ""),
        "source": str(item.get("source", "") or ""),
        "first_seen": _iso(item.get("first_suppressed_at")),
        "last_seen": _iso(item.get("last_suppressed_at") or item.get("first_suppressed_at")),
        "hits": int(item.get("hits", 0) or 0),
        "simulator": _is_simulator(email),
    }


def collect_app_stores(errors: Errors) -> dict:
    """Every configured application suppression table: a reason breakdown plus
    the recipient-level complaint rows retained there.

    These are app-owned tables (one per sending project), so a complaint here is
    attributed to the project that handled the feedback — the detail a
    config-set CloudWatch metric cannot give for sends that carried no config
    set.
    """
    counts: list[dict] = []
    records: list[dict] = []
    if not CFG.suppression_tables:
        return {"counts": counts, "records": records}
    res = boto3.Session(profile_name=PROFILE or None).resource("dynamodb", region_name=REGION)
    for table_name in CFG.suppression_tables:
        label = f"{table_name} (reason=complaint)"
        try:
            table = res.Table(table_name)
            items = _scan_all(table)
            breakdown: dict[str, int] = {}
            complaints = 0
            for i in items:
                reason = str(i.get("reason", ""))
                breakdown[reason] = breakdown.get(reason, 0) + 1
                if reason.lower() == "complaint":
                    complaints += 1
                    records.append(_suppression_record(i, table_name))
            counts.append(
                {
                    "table": table_name,
                    "label": label,
                    "total": len(items),
                    "complaints": complaints,
                    "breakdown": breakdown,
                }
            )
        except (ClientError, BotoCoreError) as e:
            errors.add(f"suppression:{table_name}", e)
            counts.append({"table": table_name, "label": label, "total": None, "complaints": None})
    return {"counts": counts, "records": records}


LEDGER_COUNTER_PREFIXES = ("AGG#", "SEEN#")


def _scan_ledger_rows(table) -> list[dict]:
    """Complaint and bounce rows only. Counter and dedupe items carry no
    ``day`` attribute, so the sparse ``by_day`` index excludes them; a table
    scan with a key filter is the fallback when the index is not readable."""
    try:
        items, lek = [], None
        while True:
            kw = {"IndexName": "by_day"}
            if lek:
                kw["ExclusiveStartKey"] = lek
            out = table.scan(**kw)
            items.extend(out.get("Items", []))
            lek = out.get("LastEvaluatedKey")
            if not lek:
                return items
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("ValidationException", "AccessDeniedException",
                                                             "ResourceNotFoundException"):
            raise
    return [it for it in _scan_all(table)
            if not str(it.get("recipient_hash", "")).startswith(LEDGER_COUNTER_PREFIXES)]


def _query_partition(table, pk: str) -> list[dict]:
    items, lek = [], None
    while True:
        kw = {"KeyConditionExpression": Key("recipient_hash").eq(pk)}
        if lek:
            kw["ExclusiveStartKey"] = lek
        out = table.query(**kw)
        items.extend(out.get("Items", []))
        lek = out.get("LastEvaluatedKey")
        if not lek:
            return items


def _valid_counter(r) -> bool:
    return (isinstance(r, dict) and all(isinstance(r.get(k), str) for k in ("domain", "identity", "config_set"))
            and r.get("event") in _breakdowns.EVENT_FIELD and isinstance(r.get("n"), int) and not isinstance(r.get("n"), bool))


def read_ledger_aggregates(table, cache_path: Path | None, today: dt.date, days: int = 396,
                           refresh_days: int = 2, workers: int = 8) -> list[dict]:
    """Daily Delivery/Open/Click counters as ``{day, domain, identity, config_set, event, n}``.

    Past days never change once written, so they are cached in ``cache_path``;
    a warm refresh re-reads only the last ``refresh_days`` partitions."""
    cache: dict[str, list] = {}
    if cache_path and cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text()).get("days", {})
        except (ValueError, OSError, AttributeError):
            raw = {}
        cache = {d: [r for r in rows if _valid_counter(r)] for d, rows in (raw.items() if isinstance(raw, dict) else [])
                 if isinstance(d, str) and isinstance(rows, list)}
    wanted = [(today - dt.timedelta(days=i)).isoformat() for i in range(days)]
    recent = set(wanted[:refresh_days])
    todo = [d for d in wanted if d in recent or d not in cache]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for day, items in zip(todo, ex.map(lambda d: _query_partition(table, f"AGG#{d}"), todo), strict=True):
            rows = []
            for it in items:
                parts = str(it.get("sk", "")).rsplit("#", 3)
                if len(parts) != 4:
                    continue
                domain, identity, config_set, event = (unquote(p) for p in parts)
                rows.append({"domain": domain, "identity": identity, "config_set": config_set,
                             "event": event, "n": int(it.get("n", 0))})
            cache[day] = rows
    cache = {d: v for d, v in cache.items() if d in set(wanted)}
    if cache_path:
        try:
            write_private(cache_path, json.dumps({"days": cache}, sort_keys=True))
        except OSError:
            pass  # the cache is an optimization; a read-only disk must not fail the run
    return [{"day": d, **r} for d, rows in sorted(cache.items()) for r in rows]


def collect_ledger_aggregates(errors: Errors, cache_dir: Path | None = None) -> list[dict]:
    if not CFG.ledger_table:
        return []
    res = boto3.Session(profile_name=PROFILE or None).resource("dynamodb", region_name=REGION)
    try:
        return read_ledger_aggregates(res.Table(CFG.ledger_table),
                                      (cache_dir / "ledger-aggregates.json") if cache_dir else None,
                                      dt.datetime.now(dt.timezone.utc).date())
    except (ClientError, BotoCoreError) as e:
        errors.add("ledger_aggregates", e)
        return []


def _identity_domain(value: str) -> str:
    """The domain of a sending identity, tolerating a stored display-name form
    (``"Name" <x@mail.example.net>`` was once kept as ``mail.example.net>``)."""
    v = (value or "").strip().strip("<>").strip()
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    return v.rstrip(">").lower()


def dedupe_ledger_rows(rows) -> list[dict]:
    """One row per SES event per recipient.

    The same bounce arrives on the config-set topic and on the identity topic
    with timestamps that can straddle a second boundary, so rows written by the
    older ingest sort key (``<epoch>#...``) can exist twice. Keep the copy that
    knows the configuration set (the identity feed carries none); without a
    message id there is nothing to match on, so the row is kept as is.
    """
    best: dict[tuple, dict] = {}
    passthrough: list[dict] = []
    for r in rows:
        mid = r.get("ses_message_id") or ""
        if not mid:
            passthrough.append(r)
            continue
        key = (r.get("recipient_hash") or r.get("email") or "", mid, (r.get("event_type") or "").upper())
        cur = best.get(key)
        if cur is None or (not cur.get("config_set") and r.get("config_set")):
            best[key] = r
    return list(best.values()) + passthrough


def collect_ledger(errors: Errors) -> dict:
    """Read the self-owned feedback ledger, if one is configured.

    The ledger (written by infra/ledger-stack.yaml's ingest Lambda) is the
    durable, attributed source of truth once deployed. Absent a table, or on a
    read error, this degrades to ``configured`` with no records so the rest of
    the report still renders from CloudWatch and the application stores.
    """
    if not CFG.ledger_table:
        return {"configured": False, "table": None, "records": [], "oldest": None, "newest": None}
    empty = {"configured": True, "table": CFG.ledger_table, "records": [], "oldest": None, "newest": None}
    res = boto3.Session(profile_name=PROFILE or None).resource("dynamodb", region_name=REGION)
    try:
        items = _scan_ledger_rows(res.Table(CFG.ledger_table))
    except (ClientError, BotoCoreError) as e:
        errors.add("ledger", e)
        return {**empty, "error": True}

    records = dedupe_ledger_rows(
        {
            "email": it.get("recipient", ""),
            "store": CFG.ledger_table,
            "detail": it.get("feedback_type", ""),
            "source": it.get("source", ""),
            "first_seen": _iso(it.get("ts")),
            "last_seen": _iso(it.get("ts")),
            "hits": 1,
            "simulator": bool(it.get("simulator")),
            "event_type": it.get("event_type", ""),
            "sending_identity": _identity_domain(it.get("sending_identity", "")),
            "config_set": it.get("config_set", ""),
            "recipient_domain": it.get("recipient_domain", ""),
            "day": it.get("day", ""),
            "ses_message_id": it.get("ses_message_id", ""),
            # The stored key is the SHA-256 of the real address; the report
            # keeps it so the ledger row can be matched against a full-address
            # application-store row (see ``_identity_hash``).
            "recipient_hash": it.get("recipient_hash", ""),
        }
        for it in items
    )
    records.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    stamps = [r["last_seen"] for r in records if r.get("last_seen")]
    return {
        "configured": True,
        "table": CFG.ledger_table,
        "records": records,
        "oldest": min(stamps) if stamps else None,
        "newest": max(stamps) if stamps else None,
    }


def collect_suppression_list(errors: Errors) -> dict | None:
    """The SES account-level suppression list — the fate of complainant addresses.

    SES does not expire these entries; they stay until removed, except for the
    paused-account-90-day rule (see the report's expiry note).
    """
    ses = _client("sesv2")
    try:
        total = 0
        by_reason: dict[str, int] = {}
        oldest: dt.datetime | None = None
        newest: dt.datetime | None = None
        oldest_complaint: dt.datetime | None = None
        newest_complaint: dt.datetime | None = None
        token = None
        while True:
            kw = {"PageSize": 1000}
            if token:
                kw["NextToken"] = token
            page = ses.list_suppressed_destinations(**kw)
            for s in page.get("SuppressedDestinationSummaries", []):
                total += 1
                reason = s.get("Reason", "UNKNOWN")
                by_reason[reason] = by_reason.get(reason, 0) + 1
                t = s.get("LastUpdateTime")
                if isinstance(t, dt.datetime):
                    oldest = t if oldest is None or t < oldest else oldest
                    newest = t if newest is None or t > newest else newest
                    if reason == "COMPLAINT":
                        oldest_complaint = t if oldest_complaint is None or t < oldest_complaint else oldest_complaint
                        newest_complaint = t if newest_complaint is None or t > newest_complaint else newest_complaint
            token = page.get("NextToken")
            if not token:
                break
        return {
            "total": total,
            "by_reason": by_reason,
            "complaints": by_reason.get("COMPLAINT", 0),
            "oldest": _iso(oldest) if oldest else None,
            "newest": _iso(newest) if newest else None,
            "oldest_complaint": _iso(oldest_complaint) if oldest_complaint else None,
            "newest_complaint": _iso(newest_complaint) if newest_complaint else None,
            "expires": "never",
        }
    except (ClientError, BotoCoreError) as e:
        errors.add("suppression_list", e)
        return None


# Names that look like public/unauthenticated send paths attract complaints.
_PUBLIC_HINTS = ("feedback", "invite", "public", "contact", "form", "signup", "waitlist", "refer")


def _usage(sends_recent) -> str:
    if sends_recent is None:
        return "unknown"
    return "active" if sends_recent > 0 else "idle"


def _coverage_risk(row: dict) -> str:
    """How much an unrouted config set matters.

    ``disabled`` sets cannot send, so they are not a gap. A set whose only
    plausible senders already route feedback through identity-level topics is a
    note, as is one nothing has used in RECENT_DAYS. Otherwise a public-looking
    name or recorded complaints make it high.
    """
    if not row["sending_enabled"]:
        return "disabled"
    if row["routes_feedback"]:
        return "ok"
    if row["covered_by"] or row["usage"] == "idle":
        return "info"
    if row["complaints"] or any(h in row["config_set"].lower() for h in _PUBLIC_HINTS):
        return "high"
    return "medium"


def _fmt_breakdown(breakdown) -> str:
    """Human-readable `reason: count` list instead of a Python dict repr."""
    if isinstance(breakdown, dict):
        return ", ".join(f"{k}: {v}" for k, v in breakdown.items())
    return str(breakdown)


def _covering_identities(config_set: str, identity_feedback: list[dict], topics: dict[str, dict],
                         observed: set[str]) -> list[str]:
    """Identities whose own Bounce/Complaint topic reaches the ledger or a
    handler and that plausibly send through ``config_set``: those the ledger
    has seen using it, plus a name match (``roadtrip-*`` ↔
    ``roadtrip.example``). Empty unless *every* candidate is covered, since
    one uncovered sender makes the set's gap real again."""
    token = config_set.split("-")[0].lower()
    candidates = set(observed)
    for row in identity_feedback:
        if token and token in row["identity"].lower().split("."):
            candidates.add(row["identity"])
    if not candidates:
        return []
    covered = {r["identity"] for r in identity_feedback
               if (r.get("complaint_topic") or r.get("bounce_topic"))
               and _topic_reaches(topics.get(r.get("complaint_topic") or r.get("bounce_topic")))}
    return sorted(candidates) if candidates <= covered else []


def _topic_reaches(topic: dict | None) -> bool:
    return bool(topic and (topic.get("ledger_subscribed") or topic.get("consumers")))


def build_suppression_coverage(config_sets: list[dict], cs_metrics: dict[str, dict], sns_topics: list[dict],
                               identity_feedback: list[dict], aggregates: list[dict] | None = None,
                               ledger_rows: list[dict] | None = None) -> list[dict]:
    topics = {t["arn"]: t for t in sns_topics}
    used_by: dict[str, set[str]] = {}
    for r in aggregates or []:
        if r.get("identity") and r["identity"] != "(unknown)":
            used_by.setdefault(r["config_set"], set()).add(r["identity"])
    for r in ledger_rows or []:
        if r.get("config_set") and r.get("sending_identity"):
            used_by.setdefault(r["config_set"], set()).add(r["sending_identity"])
    rows = []
    for c in config_sets:
        m = cs_metrics.get(c["name"]) or {}
        feedback_topics = [topics.get(a) for a in c.get("feedback_topics") or []]
        delivery_topics = [topics.get(a) for a in c.get("delivery_topics") or []]
        row = {
            "config_set": c["name"],
            "sending_enabled": c.get("sending_enabled", True),
            "routes_feedback": c["forwards_feedback"],
            "reaches_ledger": any(t and t.get("ledger_subscribed") for t in feedback_topics),
            "reaches_handler": any(t and t.get("consumers") for t in feedback_topics),
            "delivery_to_ledger": any(t and t.get("ledger_subscribed") for t in delivery_topics),
            "engagement_types": c.get("engagement_types") or [],
            "feedback_topics": c.get("feedback_topics") or [],
            "event_types": c["event_types"],
            "destinations": len(c["destinations"]),
            "sends": m.get("sends"),
            "sends_recent": m.get("sends_recent"),
            "last_send": m.get("last_send"),
            "usage": _usage(m.get("sends_recent")),
            "complaints": m.get("complaints"),
            "covered_by": _covering_identities(c["name"], identity_feedback, topics, used_by.get(c["name"], set())),
        }
        row["risk"] = _coverage_risk(row)
        rows.append(row)
    rows.sort(key=lambda x: (x["routes_feedback"], -(x["sends"] if x["sends"] is not None else -1), x["config_set"]))
    return rows


def collect_complaint_queue(errors: Errors) -> dict | None:
    if not CFG.complaint_queue:
        return None
    sqs = _client("sqs")
    try:
        url = sqs.get_queue_url(QueueName=CFG.complaint_queue)["QueueUrl"]
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
                "MessageRetentionPeriod",
                "KmsMasterKeyId",
            ],
        )["Attributes"]
        return {
            "name": CFG.complaint_queue,
            "visible": int(attrs.get("ApproximateNumberOfMessages", 0)),
            "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
            "delayed": int(attrs.get("ApproximateNumberOfMessagesDelayed", 0)),
            "retention_s": int(attrs.get("MessageRetentionPeriod", 0)),
            "kms": bool(attrs.get("KmsMasterKeyId")),
        }
    except (ClientError, BotoCoreError) as e:
        errors.add("complaint_queue", e)
        return None


def _resolve_log_groups() -> list[str]:
    if CFG.log_groups:
        return CFG.log_groups
    if not LOG_GROUP_MATCH:
        return []
    logs = _client("logs")
    groups, token = [], None
    while True:
        kw = {"logGroupNamePrefix": LOG_GROUP_PREFIX}
        if token:
            kw["nextToken"] = token
        page = logs.describe_log_groups(**kw)
        for g in page.get("logGroups", []):
            n = g["logGroupName"]
            if LOG_GROUP_MATCH in n and "cwsyn" not in n and not n.endswith("-lifecycle-cron"):
                groups.append(n)
        token = page.get("nextToken")
        if not token:
            break
    return groups[:20]


def collect_unattributed(errors: Errors) -> dict:
    """Recent unattributed feedback from CloudWatch Logs Insights (7-day retention)."""
    logs = _client("logs")
    groups = _resolve_log_groups()
    # The handler logs plain text, not JSON:
    #   event=feedback_unattributed kind=bounce domain=gmail.com type=Permanent subtype=General status=5.1.1
    # so the fields are parsed out of @message. A JSON-field filter here matched
    # nothing and the section always read 0.
    query = (
        "filter @message like /event=feedback_unattributed/ "
        "| parse @message /kind=(?<kind>\\S+) domain=(?<recipient_domain>\\S+) type=(?<feedback_type>\\S+)/ "
        "| stats count() as events by kind, recipient_domain, feedback_type"
    )
    end = int(time.time())
    start = end - 7 * 86400
    rows: list[dict] = []
    if groups:
        try:
            qid = logs.start_query(
                logGroupNames=groups, startTime=start, endTime=end, queryString=query
            )["queryId"]
        except (ClientError, BotoCoreError) as e:
            errors.add("unattributed", e)
            return {"log_groups": groups, "rows": []}
        res = {"status": "Running"}
        for _ in range(20):
            time.sleep(1)
            res = logs.get_query_results(queryId=qid)
            if res["status"] in ("Complete", "Failed", "Cancelled"):
                break
        if res["status"] != "Complete":
            errors.add("unattributed", RuntimeError(f"Logs Insights query {res['status']}"))
        else:
            for row in res["results"]:
                d = {f["field"]: f["value"] for f in row}
                try:
                    events = int(float(d.get("events", 0)))
                except ValueError:
                    events = 0
                if events:
                    rows.append(
                        {
                            "kind": (d.get("kind") or "").lower(),
                            "domain": d.get("recipient_domain", ""),
                            "feedback_type": d.get("feedback_type", ""),
                            "events": events,
                        }
                    )
    rows.sort(key=lambda x: x["events"], reverse=True)
    return {"log_groups": groups, "rows": rows}


# --------------------------------------------------------------------------- #
# 4. Infrastructure map
# --------------------------------------------------------------------------- #
def collect_config_sets(errors: Errors) -> list[dict]:
    """Config sets and their event destinations.

    Event destinations set through the SES v1 API are NOT returned by the v2
    ``get_configuration_set`` call, so the v1 ``describe_configuration_set`` is
    the source of truth here.
    """
    ses = _client("sesv2")
    ses1 = _client("ses")
    names, token = [], None
    while True:
        kw = {"NextToken": token} if token else {}
        page = ses.list_configuration_sets(**kw)
        names.extend(page.get("ConfigurationSets", []))
        token = page.get("NextToken")
        if not token:
            break
    def one(n: str) -> dict:
        r = ses1.describe_configuration_set(
            ConfigurationSetName=n,
            ConfigurationSetAttributeNames=["eventDestinations"],
        )
        # v2 knows the set's own state: whether it can send at all, whether it
        # overrides the account suppression list, and reputation metrics.
        try:
            v2 = ses.get_configuration_set(ConfigurationSetName=n)
        except (ClientError, BotoCoreError) as e:
            errors.add(f"config_set_state:{n}", e)
            v2 = {}
        sending_enabled = bool((v2.get("SendingOptions") or {}).get("SendingEnabled", True))
        suppression = v2.get("SuppressionOptions") or {}
        reputation = bool((v2.get("ReputationOptions") or {}).get("ReputationMetricsEnabled", False))
        dests = r.get("EventDestinations") or []
        types: list[str] = []
        destinations: list[dict] = []
        feedback_topics: list[str] = []
        delivery_topics: list[str] = []
        engagement: set[str] = set()
        for d in dests:
            enabled = bool(d.get("Enabled", True))
            d_types = sorted(t.upper() for t in d.get("MatchingEventTypes", []))
            entry = {"name": d.get("Name"), "enabled": enabled, "event_types": d_types}
            if "SNSDestination" in d:
                entry["type"] = "sns"
                entry["sns"] = d["SNSDestination"].get("TopicARN")
            elif "KinesisFirehoseDestination" in d:
                entry["type"] = "firehose"
                entry["firehose"] = d["KinesisFirehoseDestination"].get("DeliveryStreamARN")
            elif "CloudWatchDestination" in d:
                entry["type"] = "cloudwatch"
                entry["cloudwatch"] = [
                    f"{c.get('DimensionName')} ({c.get('DimensionValueSource')})"
                    for c in d["CloudWatchDestination"].get("DimensionConfigurations", [])
                ]
            else:
                entry["type"] = "other"
            destinations.append(entry)
            if not enabled:
                continue  # a disabled destination delivers nothing
            types.extend(d_types)
            engagement.update(t for t in d_types if t in ("OPEN", "CLICK"))
            if entry["type"] == "sns":
                if any(t in ("BOUNCE", "COMPLAINT") for t in d_types):
                    feedback_topics.append(entry["sns"])
                if "DELIVERY" in d_types:
                    delivery_topics.append(entry["sns"])
        # A CloudWatch metric is not a handler: nothing hears a complaint that
        # is only counted. Feedback "routes" when an enabled SNS or Firehose
        # destination carries BOUNCE or COMPLAINT.
        forwards = any(d["enabled"] and d["type"] in ("sns", "firehose")
                       and any(t in ("BOUNCE", "COMPLAINT") for t in d["event_types"]) for d in destinations)
        return {
            "name": n,
            "sending_enabled": sending_enabled,
            "suppressed_reasons": suppression.get("SuppressedReasons") if "SuppressedReasons" in suppression else None,
            "reputation_metrics": reputation,
            "event_types": sorted(set(types)),
            "engagement_types": sorted(engagement),
            "destinations": destinations,
            "feedback_topics": feedback_topics,
            "delivery_topics": delivery_topics,
            "forwards_feedback": forwards,
        }

    out = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(one, n): n for n in names}
        for f in as_completed(futs):
            try:
                out.append(f.result())
            except (ClientError, BotoCoreError) as e:
                errors.add(f"config_set:{futs[f]}", e)
    out.sort(key=lambda x: x["name"])
    return out


def collect_identities(errors: Errors) -> dict:
    ses = _client("sesv2")
    names, token = [], None
    while True:
        kw = {"PageSize": 100}
        if token:
            kw["NextToken"] = token
        page = ses.list_email_identities(**kw)
        for i in page.get("EmailIdentities", []):
            names.append((i["IdentityName"], i.get("IdentityType"), i.get("SendingEnabled")))
        token = page.get("NextToken")
        if not token:
            break

    # GetEmailIdentity has no SendingEnabled field (it read as False for every
    # identity); only the list call carries it.
    sending_by_name = {n: bool(se) for n, _t, se in names}

    def detail(name):
        d = ses.get_email_identity(EmailIdentity=name)
        dkim = d.get("DkimAttributes") or {}
        tags = {t.get("Key", ""): t.get("Value", "") for t in d.get("Tags") or []}
        return {
            "name": name,
            "type": d.get("IdentityType", ""),
            "dkim_status": dkim.get("Status", ""),
            "dkim_signing": bool(dkim.get("SigningEnabled")),
            "dkim_key_length": dkim.get("CurrentSigningKeyLength", ""),
            "verified": bool(d.get("VerifiedForSendingStatus")),
            "verification_status": d.get("VerificationStatus", ""),
            "sending": sending_by_name.get(name, False),
            "mail_from_domain": (d.get("MailFromAttributes") or {}).get("MailFromDomain", ""),
            "mail_from_status": (d.get("MailFromAttributes") or {}).get("MailFromDomainStatus", ""),
            "default_config_set": d.get("ConfigurationSetName", ""),
            "feedback_forwarding": bool(d.get("FeedbackForwardingStatus")),
            "project": tags.get("Project", ""),
            "tags": tags,
        }

    rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(detail, n[0]): n for n in names}
        for f in as_completed(futs):
            try:
                rows.append(f.result())
            except (ClientError, BotoCoreError) as e:
                errors.add(f"identity:{futs[f][0]}", e)
    rows.sort(key=lambda x: x["name"])

    extra = []
    for region in [r.strip() for r in EXTRA_REGIONS if r.strip() and r.strip() != REGION]:
        try:
            s = _client("sesv2", region=region)
            idents, token = [], None
            while True:
                kw = {"PageSize": 100}
                if token:
                    kw["NextToken"] = token
                page = s.list_email_identities(**kw)
                idents.extend(page.get("EmailIdentities", []))
                token = page.get("NextToken")
                if not token:
                    break
            acc = s.get_account()
            extra.append(
                {
                    "region": region,
                    "count": len(idents),
                    "names": [i["IdentityName"] for i in idents],
                    "production_access": bool(acc.get("ProductionAccessEnabled")),
                    "suppressed_reasons": (acc.get("SuppressionAttributes") or {}).get("SuppressedReasons", []),
                }
            )
        except (ClientError, BotoCoreError) as e:
            errors.add(f"identities_region:{region}", e)

    status = {r["name"]: (r["verification_status"] or ("SUCCESS" if r["verified"] else "FAILED")) for r in rows}
    return {
        "verified": sum(1 for s in status.values() if s == "SUCCESS"),
        "failed": sum(1 for s in status.values() if s in ("FAILED", "TEMPORARY_FAILURE", "NOT_STARTED")),
        "pending": sum(1 for s in status.values() if s == "PENDING"),
        "sending": sum(1 for r in rows if r["verified"] and r["sending"]),
        "verified_not_sending": sum(1 for r in rows if r["verified"] and not r["sending"]),
        "rows": rows,
        "other_regions": extra,
    }


def collect_identity_feedback(errors: Errors) -> list[dict]:
    """SES identities with identity-level Bounce/Complaint notification topics.

    Feedback routing is not only a configuration-set concern: an identity can
    have its own Bounce/Complaint topic (``SetIdentityNotificationTopic``),
    which delivers events for sends that carried *no* configuration set. That is
    why an account-level complaint can appear with no per-config-set metric.
    """
    ses = _client("ses")
    sesv2 = _client("sesv2")
    names, token = [], None
    while True:
        kw = {"PageSize": 100}
        if token:
            kw["NextToken"] = token
        page = sesv2.list_email_identities(**kw)
        names.extend(i["IdentityName"] for i in page.get("EmailIdentities", []))
        token = page.get("NextToken")
        if not token:
            break
    out = []
    for start in range(0, len(names), 100):
        chunk = names[start : start + 100]
        try:
            r = ses.get_identity_notification_attributes(Identities=chunk)
        except (ClientError, BotoCoreError) as e:
            errors.add("identity_feedback", e)
            continue
        for ident, attrs in r.get("NotificationAttributes", {}).items():
            bounce = attrs.get("BounceTopic")
            complaint = attrs.get("ComplaintTopic")
            delivery = attrs.get("DeliveryTopic")
            if bounce or complaint or delivery:
                out.append(
                    {
                        "identity": ident,
                        "bounce_topic": bounce,
                        "complaint_topic": complaint,
                        "delivery_topic": delivery,
                        "forwarding_enabled": bool(attrs.get("ForwardingEnabled")),
                    }
                )
    out.sort(key=lambda x: x["identity"])
    return out


HANDLER_PROTOCOLS = ("lambda", "https", "sqs")


def _is_ledger(sub: dict) -> bool:
    return sub.get("protocol") == "lambda" and str(sub.get("endpoint") or "").split(":")[-1].endswith(CFG.ledger_function)


def collect_sns(errors: Errors) -> list[dict]:
    """Every SNS topic with its subscriptions, and what they mean for feedback:
    ``ledger_subscribed`` when the ledger ingest Lambda listens, ``consumers``
    for other lambda/https/sqs handlers (an email-only topic is not a handler),
    and ``pending`` subscriptions that deliver nothing until confirmed."""
    sns = _client("sns")
    topics, token = [], None
    while True:
        kw = {"NextToken": token} if token else {}
        page = sns.list_topics(**kw)
        topics.extend(page.get("Topics", []))
        token = page.get("NextToken")
        if not token:
            break
    out = []
    for t in topics:
        arn = t["TopicArn"]
        name = arn.split(":")[-1]
        subs = []
        token = None
        while True:
            kw = {"TopicArn": arn}
            if token:
                kw["NextToken"] = token
            page = sns.list_subscriptions_by_topic(**kw)
            for s in page.get("Subscriptions", []):
                subs.append({"protocol": s.get("Protocol"), "endpoint": s.get("Endpoint"),
                             "pending": s.get("SubscriptionArn") == "PendingConfirmation"})
            token = page.get("NextToken")
            if not token:
                break
        live = [s for s in subs if not s["pending"]]
        out.append({
            "name": name, "arn": arn, "subscribers": subs,
            "ledger_subscribed": any(_is_ledger(s) for s in live),
            "consumers": [s["endpoint"] for s in live if s["protocol"] in HANDLER_PROTOCOLS and not _is_ledger(s)],
            "pending": [s["endpoint"] for s in subs if s["pending"]],
        })
    out.sort(key=lambda x: x["name"])
    return out


def collect_sqs(errors: Errors) -> dict | None:
    q = collect_complaint_queue(errors)
    return q


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def collect_all(cache_dir: Path | None = None, resolver=None) -> dict:
    started = dt.datetime.now(dt.timezone.utc)
    errors = Errors()

    def guard(name, fn, default):
        try:
            return fn(errors)
        except (ClientError, BotoCoreError) as e:
            errors.add(name, e)
            return default

    with ThreadPoolExecutor(max_workers=6) as ex:
        f_posture = ex.submit(guard, "posture", collect_posture, {})
        f_metrics = ex.submit(guard, "metrics", collect_metrics, {})
        f_complainants = ex.submit(guard, "complainants", collect_complainants, [])
        f_appstores = ex.submit(guard, "app_stores", collect_app_stores, {"counts": [], "records": []})
        f_unattr = ex.submit(guard, "unattributed", collect_unattributed, {"log_groups": [], "rows": []})
        f_configsets = ex.submit(guard, "config_sets", collect_config_sets, [])
        f_identities = ex.submit(guard, "identities", collect_identities, {})
        f_idfb = ex.submit(guard, "identity_feedback", collect_identity_feedback, [])
        f_sns = ex.submit(guard, "sns", collect_sns, [])
        f_queue = ex.submit(guard, "complaint_queue", collect_complaint_queue, None)
        f_supplist = ex.submit(guard, "suppression_list", collect_suppression_list, None)
        f_ledger = ex.submit(
            guard,
            "ledger",
            collect_ledger,
            {"configured": False, "table": None, "records": [], "oldest": None, "newest": None},
        )
        f_aggs = ex.submit(guard, "ledger_aggregates", lambda e: collect_ledger_aggregates(e, cache_dir), [])

        config_sets = f_configsets.result()
        # per-set metrics need the set names and their destinations first
        f_csmetrics = ex.submit(guard, "config_set_metrics", lambda e: collect_config_set_metrics(e, config_sets), {})

        posture = f_posture.result()
        metrics = f_metrics.result()
        complainants = f_complainants.result()
        app_stores = f_appstores.result()
        unattributed_raw = f_unattr.result()
        identities = f_identities.result()
        identity_feedback = f_idfb.result()
        sns_topics = f_sns.result()
        queue = f_queue.result()
        suppression_list = f_supplist.result()
        ledger = f_ledger.result()
        aggregates = f_aggs.result()
        cs_metrics = f_csmetrics.result()
    cs_complaints = config_set_complaints(cs_metrics)

    suppression = app_stores.get("counts", [])
    suppression_records = app_stores.get("records", [])

    unattributed = unattributed_raw.get("rows", [])
    log_groups = unattributed_raw.get("log_groups", [])

    # When does each recorded complaint age out of CloudWatch? Its daily data
    # point is kept at hourly resolution for RETENTION_DAYS.
    def _expires(day: str) -> str:
        d = dt.date.fromisoformat(day) + dt.timedelta(days=RETENTION_DAYS)
        return d.isoformat()

    complaints_by_day = metrics.get("complaints_by_day", [])
    for row in complaints_by_day:
        row["expires"] = _expires(row["date"])

    if complaints_by_day:
        retention = {
            "cloudwatch_days": RETENTION_DAYS,
            "oldest_expires": _expires(complaints_by_day[0]["date"]),
            "newest_expires": _expires(complaints_by_day[-1]["date"]),
        }
    else:
        retention = {"cloudwatch_days": RETENTION_DAYS, "oldest_expires": None, "newest_expires": None}

    suppression_coverage = build_suppression_coverage(config_sets, cs_metrics, sns_topics, identity_feedback,
                                                      aggregates, ledger.get("records", []))

    # A single recipient-level view of every complaint an application store has
    # retained, so the account-wide CloudWatch count can be attributed to a
    # project (and to test artefacts vs. real complainants). When the self-owned
    # ledger is deployed it is the primary, deduped source; store rows are kept
    # for anything it does not cover yet. The ledger also holds permanent
    # bounces; only its COMPLAINT rows belong in a list of complainants.
    ledger_records = [r for r in ledger.get("records", []) if (r.get("event_type") or "").upper() == "COMPLAINT"]
    store_records = list(suppression_records)
    for c in complainants:
        store_records.append(
            {
                "email": c["email"],
                "store": CFG.complaint_table,
                "detail": c.get("arf_provider", ""),
                "source": "app account",
                "first_seen": c.get("recorded_at"),
                "last_seen": c.get("recorded_at"),
                "hits": None,
                "simulator": _is_simulator(c.get("email", "")),
            }
        )

    # One row per complainant per day. Ledger rows are authoritative; store rows
    # fill only the complainants the ledger did not capture (see
    # ``merge_complaint_records`` for why the address string alone cannot be the
    # join key once the ledger redacts addresses).
    complaint_records, kept_ledger, kept_store = merge_complaint_records(
        ledger_records, store_records
    )
    simulator_records = sum(1 for r in complaint_records if r["simulator"])
    # A store row can stand for several events (``hits``); the CloudWatch count
    # is per event, so the simulator share of the account total uses hits.
    simulator_hits = sum(int(r.get("hits") or 1) for r in complaint_records if r["simulator"])
    cloudwatch_recorded = metrics.get("total_complaints")
    attribution = {
        "cloudwatch_recorded": cloudwatch_recorded,
        "real_cloudwatch": (cloudwatch_recorded - simulator_hits) if cloudwatch_recorded is not None else None,
        "records": len(complaint_records),
        "real_records": len(complaint_records) - simulator_records,
        "simulator_records": simulator_records,
        "simulator_hits": simulator_hits,
        "ledger_records": len(kept_ledger),
        "store_records": len(kept_store),
        "latest_record": complaint_records[0]["last_seen"] if complaint_records else None,
    }

    finish_reason_rows = []
    if CFG.complaint_table:
        finish_reason_rows.append(
            {
                "store": f"DynamoDB {CFG.complaint_table} ({CFG.complaint_attr}={CFG.complaint_value})",
                "complaints": len(complainants),
                "detail": "exact address + ARF provider + timestamp",
            }
        )
    for s in suppression:
        finish_reason_rows.append(
            {
                "store": f"DynamoDB {s['table']} (reason=complaint)",
                "complaints": s.get("complaints"),
                "detail": f"{s.get('total')} total rows"
                + (f" — {_fmt_breakdown(s.get('breakdown'))}" if s.get("breakdown") else ""),
            }
        )
    if CFG.complaint_queue:
        # The queue is the SNS subscriptions' dead-letter queue: it holds
        # notifications of every event type whose https delivery failed, not
        # complaints, so it has no complaint count.
        q = queue or {}
        finish_reason_rows.append(
            {
                "store": f"SQS {CFG.complaint_queue} (SNS delivery DLQ)",
                "complaints": None,
                "detail": (f"{q.get('visible', 0)} undelivered notification(s) waiting, {q.get('in_flight', 0)} in flight"
                           if queue else "not readable") + "; bodies not read",
            }
        )
    unattr_complaints = sum(u["events"] for u in unattributed if u.get("kind") == "complaint")
    unattr_total = sum(u["events"] for u in unattributed)
    finish_reason_rows.append(
        {
            "store": "CloudWatch Logs " + (", ".join(log_groups) if log_groups else "(no matching log group)")
            + " (7-day retention)",
            "complaints": unattr_complaints,
            "detail": f"{unattr_total} unattributed feedback event(s) in the last 7 days"
                      + (f" ({unattr_total - unattr_complaints} bounces)" if unattr_total else ""),
        }
    )

    try:
        deliverability = build_deliverability(
            ledger=ledger, aggregates=aggregates, identities=identities, coverage=suppression_coverage,
            config_sets=config_sets, identity_feedback=identity_feedback, errors=errors,
            cache_dir=cache_dir, resolver=resolver, metrics=metrics, posture=posture, sns_topics=sns_topics,
        )
    except Exception as e:  # noqa: BLE001 - never let the new section take the whole report down
        errors.add("deliverability", e)
        deliverability = {"breakdowns": {"state": "unavailable", "floor": _breakdowns.FLOOR, "ranges": {}},
                          "advisor": {"findings": [], "checks": {}, "dns": {}}}

    model = {
        "meta": {
            "generated_at": started.isoformat(),
            "account": _account_id(),
            "region": REGION,
            "profile": PROFILE,
            "collector_version": "1.1.1",
            "redacted": CFG.redact_emails,
        },
        "posture": posture,
        "metrics": metrics,
        "config_set_complaints": cs_complaints,
        "complainants": complainants,
        "complaint_records": complaint_records,
        "attribution": attribution,
        "ledger": ledger,
        "unattributed": unattributed,
        "log_groups": log_groups,
        "stores": finish_reason_rows,
        "config_sets": config_sets,
        "identities": identities,
        "identity_feedback": identity_feedback,
        "sns_topics": sns_topics,
        "complaint_queue": queue,
        "suppression_list": suppression_list,
        "suppression_coverage": suppression_coverage,
        "retention": retention,
        "breakdowns": deliverability["breakdowns"],
        "advisor": deliverability["advisor"],
        "errors": errors.items,
        "collect_ms": int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000),
    }
    return model


def unattributed_sends(metrics: dict, coverage: list[dict], days: int = RECENT_DAYS,
                       today: dt.date | None = None) -> dict:
    """Account sends in the last ``days`` that no configuration set accounts for.

    Those messages carry no event destination, so their bounces and complaints
    arrive only as forwarded email (if at all). ``None`` values mean the share
    cannot be computed because some active set's usage is unknown.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    cutoff = (today - dt.timedelta(days=days - 1)).isoformat()
    account = sum(int(r.get("count") or 0) for r in metrics.get("sends_by_day") or [] if r.get("date", "") >= cutoff)
    known = [c for c in coverage if c.get("sends_recent") is not None]
    unknown = [c["config_set"] for c in coverage if c.get("sends_recent") is None and c.get("sending_enabled", True)]
    tagged = sum(c["sends_recent"] for c in known)
    out = {"days": days, "account_sends": account, "config_set_sends": tagged, "unknown_sets": unknown}
    if unknown:
        out.update(unattributed=None, share=None)
    else:
        unattributed = max(0, account - tagged)
        out.update(unattributed=unattributed, share=(unattributed / account) if account else 0.0)
    return out


def build_deliverability(*, ledger, aggregates, identities, coverage, config_sets, identity_feedback,
                         errors: Errors, cache_dir: Path | None = None, resolver=None, today=None,
                         metrics: dict | None = None, posture: dict | None = None,
                         sns_topics: list[dict] | None = None) -> dict:
    """VDM-equivalent breakdowns and Advisor findings (VDM itself stays off)."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    metrics = metrics or {}
    if resolver is None:
        try:
            resolver = _advisor.make_resolver()
        except Exception as e:  # noqa: BLE001 - DNS library or config missing
            errors.add("advisor_dns", e)
            resolver = None
    sent_24h = ((posture or {}).get("send_quota") or {}).get("sent_24h")
    # authentication checks get first call on the shared DNS budget
    auth = _advisor.auth_findings((identities or {}).get("rows", []), resolver, REGION,
                                   CFG.app_dkim_projects, app_dkim_mx=CFG.app_dkim_mx,
                                   test_identities=CFG.test_identities, sent_24h=sent_24h) if resolver else ([], {})
    classifier = Classifier((cache_dir / "providers-mx-cache.json") if cache_dir else None,
                            getattr(resolver, "mx", None))
    breakdowns = _breakdowns.build_all_ranges(aggregates, ledger.get("records", []), today,
                                              classify=classifier, configured=bool(ledger.get("configured")))
    classifier.save()
    breakdowns["classification"] = classifier.stats
    unattributed = unattributed_sends(metrics, coverage)
    advisor = _advisor.run(identities=(identities or {}).get("rows", []), coverage=coverage,
                           config_sets=config_sets, identity_feedback=identity_feedback,
                           window=breakdowns["ranges"]["30"], resolver=resolver, region=REGION, auth=auth,
                           sns_topics=sns_topics or [], engagement_tracking=CFG.engagement_tracking,
                           account_rates={"complaint": (metrics.get("latest_rate") or {}).get("rate"),
                                          "bounce": (metrics.get("latest_bounce_rate") or {}).get("rate")},
                           unattributed=unattributed, ledger_rows=ledger.get("records", []), aggregates=aggregates)
    advisor["unattributed_sends"] = unattributed
    log.info("deliverability: %d counters, classification %s, %d findings, dns %s",
             len(aggregates), classifier.stats, len(advisor["findings"]), advisor["dns"])
    return {"breakdowns": breakdowns, "advisor": advisor}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(collect_all(), indent=2, default=str))
