# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Synthetic model for demo mode — lets a new user run the report with no AWS."""

from __future__ import annotations

import datetime as dt
import random

from . import advisor as _advisor
from . import breakdowns as _breakdowns
from .providers import classify as _classify


def demo_model(seed: int = 7) -> dict:
    rng = random.Random(seed)
    today = dt.datetime.now(dt.timezone.utc)
    days = 455

    complaints: list[dict] = []
    day_offsets = sorted(rng.sample(range(5, 440), 22))
    counts = [2, 2, 2, 2] + [1] * 18  # sums to 26
    for off, c in zip(day_offsets, counts, strict=True):
        d = today - dt.timedelta(days=off)
        complaints.append({"date": d.date().isoformat(), "count": c})
    # collapse to one row per day
    merged: dict[str, int] = {}
    for c in complaints:
        merged[c["date"]] = merged.get(c["date"], 0) + c["count"]
    by_day = [{"date": k, "count": v} for k, v in sorted(merged.items())]
    total_complaints = sum(c["count"] for c in by_day)
    for row in by_day:
        row["expires"] = (dt.date.fromisoformat(row["date"]) + dt.timedelta(days=455)).isoformat()

    rate_series = []
    for i in range(days, 0, -1):
        d = today - dt.timedelta(days=i)
        rate_series.append({"date": d.date().isoformat(), "rate": round(0.0004 + rng.random() * 0.00055, 8)})

    providers = ["Yahoo!-Mail-Feedback/2.0", "JMRP/1.0", "ReturnPathFBL/2.0"]
    domains = ["example.com", "sample.net", "demo.org"]
    complainants = [
        {
            "email": f"user{rng.randint(1000,9999)}@{rng.choice(domains)}",
            "arf_provider": rng.choice(providers),
            "recorded_at": (today - dt.timedelta(days=rng.randint(5, 440))).isoformat(),
            "bounce_count": rng.randint(1, 2),
            "bouncing": True,
            "auth_provider": "magic_link",
        }
        for _ in range(8)
    ]
    complainants.sort(key=lambda x: x["recorded_at"])

    # Recipient-level complaint records, as an application store would retain
    # them. Includes a mailbox-simulator complaint: synthetic feedback from a
    # pipeline test that still counts in the CloudWatch metric.
    complaint_records = [
        {
            "email": c["email"],
            "store": "demo-accounts",
            "detail": c["arf_provider"],
            "source": "app account",
            "first_seen": c["recorded_at"],
            "last_seen": c["recorded_at"],
            "hits": None,
            "simulator": False,
        }
        for c in complainants
    ] + [
        {
            "email": "complaint@simulator.amazonses.com",
            "store": "demo-email-suppressions",
            "detail": "abuse",
            "source": "sns",
            "first_seen": (today - dt.timedelta(days=1)).isoformat(),
            "last_seen": (today - dt.timedelta(days=1)).isoformat(),
            "hits": 2,
            "simulator": True,
        }
    ]
    complaint_records.sort(key=lambda r: r["last_seen"] or "", reverse=True)
    _sim = sum(1 for r in complaint_records if r["simulator"])
    attribution = {
        "cloudwatch_recorded": total_complaints,
        "records": len(complaint_records),
        "real_records": len(complaint_records) - _sim,
        "simulator_records": _sim,
        "ledger_records": 0,
        "store_records": len(complaint_records),
        "latest_record": complaint_records[0]["last_seen"] if complaint_records else None,
    }

    config_set_complaints = [
        {
            "config_set": "transactional",
            "events": 8,
            "days": [{"date": b["date"], "count": 1} for b in by_day[-8:]],
        }
    ]

    config_sets = [
        {
            "name": name,
            "event_types": types,
            "destinations": [{"name": "feedback-to-sns", "enabled": True, "sns": f"arn:aws:sns:us-east-1:000000000000:{name}-feedback"}],
            "forwards_feedback": "COMPLAINT" in types,
        }
        for name, types in [
            ("transactional", ["BOUNCE", "COMPLAINT", "DELIVERY", "REJECT"]),
            ("marketing", ["BOUNCE", "COMPLAINT"]),
            ("engagement-open", ["OPEN"]),
            ("public-feedbackform", []),
        ]
    ]

    deliverability = _demo_deliverability(rng, today, config_sets)

    return {
        "meta": {
            "generated_at": today.isoformat(),
            "account": "000000000000",
            "region": "us-east-1",
            "profile": "demo",
            "collector_version": "1.0.0",
            "redacted": False,
            "demo": True,
        },
        "posture": {
            "enforcement_status": "HEALTHY",
            "production_access": True,
            "sending_enabled": True,
            "suppressed_reasons": ["BOUNCE", "COMPLAINT"],
            "vdm": "DISABLED",
            "send_quota": {"max_24h": 50000.0, "rate": 14.0, "sent_24h": 1102.0},
            "dedicated_ip_auto_warmup": True,
            "pricing_plan": "NONE",
        },
        "metrics": {
            "window": {
                "start": by_day[0]["date"],
                "end": by_day[-1]["date"],
                "retention_start": (today - dt.timedelta(days=days)).date().isoformat(),
                "retention_end": today.date().isoformat(),
                "days": days,
            },
            "total_complaints": total_complaints,
            "total_sends": 766163,
            "total_bounces": 1961,
            "overall_complaint_rate": total_complaints / 766163,
            "complaints_by_day": by_day,
            "sends_by_day": [],
            "bounces_by_day": [],
            "rate_series": rate_series,
            "peak_rate": max(rate_series, key=lambda r: r["rate"]),
            "latest_rate": rate_series[-1],
            "aws_review_line": 0.001,
            "alert_line": 0.0009,
        },
        "config_set_complaints": config_set_complaints,
        "complainants": complainants,
        "complaint_records": complaint_records,
        "attribution": attribution,
        "ledger": {
            "configured": False,
            "table": None,
            "records": [],
            "oldest": None,
            "newest": None,
        },
        "unattributed": [
            {"domain": "outlook.de", "feedback_type": "abuse", "events": 1},
            {"domain": "comcast.net", "feedback_type": "abuse", "events": 1},
        ],
        "log_groups": ["/aws/lambda/demo-api"],
        "stores": [
            {"store": "DynamoDB accounts (bounce_type=Complaint)", "complaints": 8, "detail": "exact address + ARF provider + timestamp"},
            {"store": "DynamoDB suppressions (reason=complaint)", "complaints": 0, "detail": "42 total rows — bounce: 42"},
            {"store": "SQS complaint-recovery (retained raw feedback)", "complaints": 0, "detail": "governance-controlled DLQ; bodies not read"},
            {"store": "CloudWatch Logs /aws/lambda/demo-api (7-day retention)", "complaints": 0, "detail": "unattributed feedback events, last 7 days"},
        ],
        "config_sets": config_sets,
        "suppression_coverage": [
            {"config_set": "transactional", "routes_feedback": True, "event_types": ["BOUNCE", "COMPLAINT"], "destinations": 1, "sends": 3553, "complaints": 8, "risk": "ok"},
            {"config_set": "marketing", "routes_feedback": True, "event_types": ["BOUNCE", "COMPLAINT"], "destinations": 1, "sends": None, "complaints": 0, "risk": "ok"},
            {"config_set": "engagement-open", "routes_feedback": False, "event_types": ["OPEN"], "destinations": 1, "sends": None, "complaints": 0, "risk": "medium"},
            {"config_set": "public-feedbackform", "routes_feedback": False, "event_types": [], "destinations": 0, "sends": None, "complaints": 0, "risk": "high"},
        ],
        "identities": {
            "verified": 38,
            "failed": 6,
            "pending": 0,
            "rows": [
                {"name": f"send{i}.example.com", "type": "DOMAIN",
                 "dkim_status": "FAILED" if i >= 34 else "NOT_STARTED" if i < 4 else "SUCCESS", "verified": i < 34, "sending": i < 34,
                 "mail_from_domain": "" if i in (3, 7) else f"bounce.send{i}.example.com",
                 "mail_from_status": "" if i in (3, 7) else "SUCCESS",
                 # send0-2 sign in-app (WebMail) and send3 is unsigned by SES with no app tag
                 "project": "WebMail" if i < 3 else ""}
                for i in range(40)
            ],
            "other_regions": [{"region": "us-west-2", "count": 1, "names": ["mail.demo.example"]}],
        },
        "identity_feedback": [
            {
                "identity": "send0.example.com",
                "bounce_topic": "arn:aws:sns:us-east-1:000000000000:feedback-topic",
                "complaint_topic": "arn:aws:sns:us-east-1:000000000000:feedback-topic",
                "forwarding_enabled": False,
            }
        ],
        "sns_topics": [
            {"name": "feedback-topic", "arn": "arn:aws:sns:us-east-1:000000000000:feedback-topic",
             "subscribers": [{"protocol": "lambda", "endpoint": "arn:aws:lambda:us-east-1:000000000000:function:parse-feedback"}]}
        ],
        "complaint_queue": {"name": "complaint-recovery", "visible": 0, "in_flight": 0, "retention_s": 604800, "kms": True},
        "suppression_list": {
            "total": 93,
            "by_reason": {"BOUNCE": 85, "COMPLAINT": 8},
            "complaints": 8,
            "oldest": (today - dt.timedelta(days=300)).isoformat(),
            "newest": (today - dt.timedelta(days=10)).isoformat(),
            "oldest_complaint": (today - dt.timedelta(days=300)).isoformat(),
            "newest_complaint": (today - dt.timedelta(days=25)).isoformat(),
            "expires": "never",
        },
        "retention": {
            "cloudwatch_days": 455,
            "oldest_expires": (dt.date.fromisoformat(by_day[0]["date"]) + dt.timedelta(days=455)).isoformat(),
            "newest_expires": (dt.date.fromisoformat(by_day[-1]["date"]) + dt.timedelta(days=455)).isoformat(),
        },
        "breakdowns": deliverability["breakdowns"],
        "advisor": None,  # filled below once identities/coverage exist
        "errors": [],
        "collect_ms": 12,
    }


class _DemoResolver:
    """Deterministic DNS for demo mode: no network calls."""

    lookups = 0
    timeouts = 0
    MX = {"acme-corp.example": ["acme-corp-example.mail.protection.outlook.com"],
          "studio.example": ["aspmx.l.google.com"]}

    def txt(self, name: str) -> list[str]:
        self.lookups += 1
        if name.startswith("_dmarc.send1.") or name.startswith("_dmarc.send2."):
            return ["v=DMARC1; p=none; rua=mailto:dmarc@example.com"]
        if name.startswith("_dmarc.send5."):
            return []
        if name.startswith("_dmarc."):
            return ["v=DMARC1; p=quarantine; rua=mailto:dmarc@example.com"]
        if name == "bounce.send4.example.com":
            return ["v=spf1 -all"]
        return ["v=spf1 include:amazonses.com ~all"]

    def mx(self, name: str) -> list[str]:
        self.lookups += 1
        return self.MX.get(name, [])


# (domain, deliveries/day, complaint rate, permanent-bounce rate, open rate, click rate)
_DEMO_TRAFFIC = [
    ("gmail.com", 300, 0.0003, 0.008, 0.31, 0.024),
    ("yahoo.com", 180, 0.0007, 0.012, 0.22, 0.015),
    ("outlook.com", 90, 0.0004, 0.020, 0.18, 0.012),
    ("hotmail.com", 40, 0.0006, 0.031, 0.16, 0.010),
    ("icloud.com", 45, 0.0002, 0.006, 0.52, 0.020),
    ("acme-corp.example", 18, 0.0, 0.004, 0.41, 0.060),
    ("studio.example", 9, 0.0, 0.002, 0.38, 0.050),
    ("tinyisp.example", 6, 0.0, 0.01, 0.30, 0.0),
]


def _demo_deliverability(rng: random.Random, today: dt.datetime, config_sets: list[dict]) -> dict:
    counters, rows = [], []
    for back in range(120):
        day = (today - dt.timedelta(days=back)).date().isoformat()
        for domain, per_day, cr, br, orate, crate in _DEMO_TRAFFIC:
            n = max(0, int(rng.gauss(per_day, per_day * 0.15)))
            for identity, cs, share in (("send0.example.com", "transactional", 0.7), ("news.example.com", "marketing", 0.3)):
                k = int(n * share)
                if not k:
                    continue
                base = {"day": day, "domain": domain, "identity": identity, "config_set": cs}
                counters += [{**base, "event": "DELIVERY", "n": k},
                             {**base, "event": "OPEN_UNIQUE", "n": int(k * orate)},
                             {**base, "event": "CLICK_UNIQUE", "n": int(k * crate)}]
                for event, rate in (("COMPLAINT", cr), ("BOUNCE", br)):
                    for _ in range(sum(1 for _ in range(k) if rng.random() < rate)):
                        rows.append({"day": day, "recipient_domain": domain, "sending_identity": identity,
                                     "config_set": cs, "event_type": event, "simulator": False})
    mx = _DemoResolver.MX
    classify = lambda d: _classify(d, mx=lambda x: mx.get(x, []))  # noqa: E731
    return {"breakdowns": _breakdowns.build_all_ranges(counters, rows, today.date(), classify=classify)}


def _with_advisor(model: dict) -> dict:
    window = model["breakdowns"]["ranges"]["30"]
    model["advisor"] = _advisor.run(identities=model["identities"]["rows"], coverage=model["suppression_coverage"],
                                    config_sets=model["config_sets"], identity_feedback=model["identity_feedback"],
                                    window=window, resolver=_DemoResolver(), region=model["meta"]["region"])
    return model


_demo_model = demo_model


def demo_model(seed: int = 7) -> dict:  # noqa: F811 - wraps the base builder with the Advisor
    return _with_advisor(_demo_model(seed))


if __name__ == "__main__":
    import json

    print(json.dumps(demo_model(), indent=2, default=str))
