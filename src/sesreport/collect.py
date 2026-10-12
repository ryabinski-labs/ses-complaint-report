# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Live AWS data collection for the SES complaint report.

Every section is isolated: a failing source records an error and the rest of
the report still renders. Returns a plain-dict "model" that the frontend draws.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import load as _load_config

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
    """Wall-clock day exactly as CloudWatch published it (local tz from botocore)."""
    if isinstance(d, dt.datetime):
        return d.date().isoformat()
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
    """A window whose daily periods align to local midnight, so a given complaint
    always lands on the same calendar day regardless of when we query."""
    now = dt.datetime.now().astimezone()
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

    complaints_list = [{"date": k, "count": int(v)} for k, v in sorted(complaints.items())]
    total_complaints = int(sum(complaints.values()))
    total_sends = int(sum(sends.values()))
    total_bounces = int(sum(bounces.values()))

    rate_series = [{"date": k, "rate": v} for k, v in sorted(rate.items())]
    peak = max(rate_series, key=lambda r: r["rate"]) if rate_series else None
    latest = rate_series[-1] if rate_series else None

    retention_start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=total_days)).date().isoformat()
    return {
        "window": {
            # recorded-complaint window (the Complaint metric only exists here)
            "start": complaints_list[0]["date"] if complaints_list else METRIC_EPOCH.date().isoformat(),
            "end": complaints_list[-1]["date"] if complaints_list else dt.datetime.now(dt.timezone.utc).date().isoformat(),
            # full retention window used for the send/bounce denominator
            "retention_start": retention_start,
            "retention_end": dt.datetime.now(dt.timezone.utc).date().isoformat(),
            "days": total_days,
        },
        "total_complaints": total_complaints,
        "total_sends": total_sends,
        "total_bounces": total_bounces,
        "overall_complaint_rate": (total_complaints / total_sends) if total_sends else None,
        "complaints_by_day": complaints_list,
        "sends_by_day": [{"date": k, "count": int(v)} for k, v in sorted(sends.items())],
        "bounces_by_day": [{"date": k, "count": int(v)} for k, v in sorted(bounces.items())],
        "rate_series": rate_series,
        "peak_rate": peak,
        "latest_rate": latest,
        "aws_review_line": 0.001,
        "alert_line": 0.0009,
    }


def collect_config_set_complaints(errors: Errors) -> list[dict]:
    """Complaint counts per SES configuration set (CloudWatch dimension)."""
    cw = _client("cloudwatch")
    metrics = cw.list_metrics(Namespace="AWS/SES", MetricName="Complaint")["Metrics"]
    out = []
    for m in metrics:
        dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
        if "ses:configuration-set" not in dims:
            continue
        cs = dims["ses:configuration-set"]
        series = _sum_series(
            cw,
            "Complaint",
            (dt.datetime.now(dt.timezone.utc) - METRIC_EPOCH).days + 1,
            extra_dims=[{"Name": "ses:configuration-set", "Value": cs}],
        )
        days = [{"date": k, "count": int(v)} for k, v in sorted(series.items()) if v]
        if days:
            out.append({"config_set": cs, "events": int(sum(series.values())), "days": days})
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
        items = _scan_all(res.Table(CFG.ledger_table))
    except (ClientError, BotoCoreError) as e:
        errors.add("ledger", e)
        return {**empty, "error": True}

    records = []
    for it in items:
        records.append(
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
                "sending_identity": it.get("sending_identity", ""),
                "config_set": it.get("config_set", ""),
                # The stored key is the SHA-256 of the real address; the report
                # keeps it so the ledger row can be matched against a full-address
                # application-store row (see ``_identity_hash``).
                "recipient_hash": it.get("recipient_hash", ""),
            }
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


def collect_config_set_sends(errors: Errors) -> dict[str, int]:
    """Sends per configuration set — only sets with a CloudWatch event destination
    publish this metric, so the map is partial by nature."""
    cw = _client("cloudwatch")
    metrics = cw.list_metrics(Namespace="AWS/SES", MetricName="Send")["Metrics"]
    names = sorted(
        {d["Value"] for m in metrics for d in m["Dimensions"] if d["Name"] == "ses:configuration-set"}
    )
    out: dict[str, int] = {}
    for n in names:
        series = _sum_series(
            cw, "Send", RETENTION_DAYS, extra_dims=[{"Name": "ses:configuration-set", "Value": n}]
        )
        out[n] = int(sum(series.values()))
    return out


# Names that look like public/unauthenticated send paths attract complaints.
_PUBLIC_HINTS = ("feedback", "invite", "public", "contact", "form", "signup", "waitlist", "refer")


def _coverage_risk(name: str, routes: bool) -> str:
    if routes:
        return "ok"
    if any(h in name.lower() for h in _PUBLIC_HINTS):
        return "high"
    return "medium"


def build_suppression_coverage(
    config_sets: list[dict], sends: dict[str, int], complaint_events: dict[str, int]
) -> list[dict]:
    rows = []
    for c in config_sets:
        routes = c["forwards_feedback"]
        rows.append(
            {
                "config_set": c["name"],
                "routes_feedback": routes,
                "event_types": c["event_types"],
                "destinations": len(c["destinations"]),
                "sends": sends.get(c["name"]),
                "complaints": complaint_events.get(c["name"], 0),
                "risk": _coverage_risk(c["name"], routes),
            }
        )
    rows.sort(key=lambda x: (x["routes_feedback"], -(x["sends"] or -1), x["config_set"]))
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
                "MessageRetentionPeriod",
                "KmsMasterKeyId",
            ],
        )["Attributes"]
        return {
            "name": CFG.complaint_queue,
            "visible": int(attrs.get("ApproximateNumberOfMessages", 0)),
            "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
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
    query = (
        'fields recipient_domain, feedback_type '
        '| filter event = "feedback_unattributed" '
        "| stats count() as events by recipient_domain, feedback_type"
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
        if res["status"] == "Complete":
            for row in res["results"]:
                d = {f["field"]: f["value"] for f in row}
                try:
                    events = int(float(d.get("events", 0)))
                except ValueError:
                    events = 0
                if events:
                    rows.append(
                        {
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
    out = []
    for n in names:
        try:
            r = ses1.describe_configuration_set(
                ConfigurationSetName=n,
                ConfigurationSetAttributeNames=["eventDestinations"],
            )
        except (ClientError, BotoCoreError) as e:
            errors.add(f"config_set:{n}", e)
            continue
        dests = r.get("EventDestinations") or []
        types: list[str] = []
        destinations: list[dict] = []
        for d in dests:
            types.extend(t.upper() for t in d.get("MatchingEventTypes", []))
            entry = {"name": d.get("Name"), "enabled": d.get("Enabled", True)}
            if "SNSDestination" in d:
                entry["sns"] = d["SNSDestination"].get("TopicARN")
            if "CloudWatchDestination" in d:
                entry["cloudwatch"] = [
                    f"{c.get('DimensionName')} ({c.get('DimensionValueSource')})"
                    for c in d["CloudWatchDestination"].get("DimensionConfigurations", [])
                ]
            if "KinesisFirehoseDestination" in d:
                entry["firehose"] = d["KinesisFirehoseDestination"].get("DeliveryStreamARN")
            destinations.append(entry)
        out.append(
            {
                "name": n,
                "event_types": sorted(set(types)),
                "destinations": destinations,
                "forwards_feedback": any(t in ("BOUNCE", "COMPLAINT") for t in types),
            }
        )
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

    def detail(name):
        d = ses.get_email_identity(EmailIdentity=name)
        return {
            "name": name,
            "type": d.get("IdentityType", ""),
            "dkim_status": d.get("DkimAttributes", {}).get("Status", ""),
            "verified": bool(d.get("VerifiedForSendingStatus")),
            "sending": bool(d.get("SendingEnabled")),
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
            page = s.list_email_identities()
            extra.append(
                {
                    "region": region,
                    "count": len(page.get("EmailIdentities", [])),
                    "names": [i["IdentityName"] for i in page.get("EmailIdentities", [])],
                }
            )
        except (ClientError, BotoCoreError) as e:
            errors.add(f"identities_region:{region}", e)

    return {
        "verified": sum(1 for r in rows if r["verified"]),
        "failed": sum(1 for r in rows if not r["verified"] and r["dkim_status"] != "PENDING"),
        "pending": sum(1 for r in rows if r["dkim_status"] == "PENDING"),
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
            if bounce or complaint:
                out.append(
                    {
                        "identity": ident,
                        "bounce_topic": bounce,
                        "complaint_topic": complaint,
                        "forwarding_enabled": bool(attrs.get("ForwardingEnabled")),
                    }
                )
    out.sort(key=lambda x: x["identity"])
    return out


def collect_sns(errors: Errors) -> list[dict]:
    sns = _client("sns")
    topics = sns.list_topics().get("Topics", [])
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
                subs.append({"protocol": s.get("Protocol"), "endpoint": s.get("Endpoint")})
            token = page.get("NextToken")
            if not token:
                break
        out.append({"name": name, "arn": arn, "subscribers": subs})
    out.sort(key=lambda x: x["name"])
    return out


def collect_sqs(errors: Errors) -> dict | None:
    q = collect_complaint_queue(errors)
    return q


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def collect_all() -> dict:
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
        f_cscomp = ex.submit(guard, "config_set_complaints", collect_config_set_complaints, [])
        f_complainants = ex.submit(guard, "complainants", collect_complainants, [])
        f_appstores = ex.submit(guard, "app_stores", collect_app_stores, {"counts": [], "records": []})
        f_unattr = ex.submit(guard, "unattributed", collect_unattributed, {"log_groups": [], "rows": []})
        f_configsets = ex.submit(guard, "config_sets", collect_config_sets, [])
        f_identities = ex.submit(guard, "identities", collect_identities, {})
        f_idfb = ex.submit(guard, "identity_feedback", collect_identity_feedback, [])
        f_sns = ex.submit(guard, "sns", collect_sns, [])
        f_queue = ex.submit(guard, "complaint_queue", collect_complaint_queue, None)
        f_supplist = ex.submit(guard, "suppression_list", collect_suppression_list, None)
        f_cssends = ex.submit(guard, "config_set_sends", collect_config_set_sends, {})
        f_ledger = ex.submit(
            guard,
            "ledger",
            collect_ledger,
            {"configured": False, "table": None, "records": [], "oldest": None, "newest": None},
        )

        posture = f_posture.result()
        metrics = f_metrics.result()
        cs_complaints = f_cscomp.result()
        complainants = f_complainants.result()
        app_stores = f_appstores.result()
        unattributed_raw = f_unattr.result()
        config_sets = f_configsets.result()
        identities = f_identities.result()
        identity_feedback = f_idfb.result()
        sns_topics = f_sns.result()
        queue = f_queue.result()
        suppression_list = f_supplist.result()
        cs_sends = f_cssends.result()
        ledger = f_ledger.result()

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

    complaint_events = {c["config_set"]: c["events"] for c in cs_complaints}
    suppression_coverage = build_suppression_coverage(config_sets, cs_sends, complaint_events)

    # A single recipient-level view of every complaint an application store has
    # retained, so the account-wide CloudWatch count can be attributed to a
    # project (and to test artefacts vs. real complainants). When the self-owned
    # ledger is deployed it is the primary, deduped source; store rows are kept
    # for anything it does not cover yet.
    ledger_records = ledger.get("records", [])
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
    attribution = {
        "cloudwatch_recorded": metrics.get("total_complaints"),
        "records": len(complaint_records),
        "real_records": len(complaint_records) - simulator_records,
        "simulator_records": simulator_records,
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
                + (f" — {s.get('breakdown')}" if s.get("breakdown") else ""),
            }
        )
    if CFG.complaint_queue:
        finish_reason_rows.append(
            {
                "store": f"SQS {CFG.complaint_queue} (retained raw feedback)",
                "complaints": (queue or {}).get("visible"),
                "detail": "governance-controlled DLQ; bodies not read",
            }
        )
    finish_reason_rows.append(
        {
            "store": "CloudWatch Logs " + (", ".join(log_groups) if log_groups else "(no matching log group)")
            + " (7-day retention)",
            "complaints": sum(u["events"] for u in unattributed) if unattributed else 0,
            "detail": "unattributed feedback_unattributed events, last 7 days",
        }
    )

    model = {
        "meta": {
            "generated_at": started.isoformat(),
            "account": _account_id(),
            "region": REGION,
            "profile": PROFILE,
            "collector_version": "1.0.0",
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
        "errors": errors.items,
        "collect_ms": int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000),
    }
    return model


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)
    print(json.dumps(collect_all(), indent=2, default=str))
