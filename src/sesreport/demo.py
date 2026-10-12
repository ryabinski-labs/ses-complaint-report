# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Synthetic model for demo mode — lets a new user run the report with no AWS."""

from __future__ import annotations

import datetime as dt
import random


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
            ("legacy", []),
        ]
    ]

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
            {"store": "DynamoDB suppressions (reason=complaint)", "complaints": 0, "detail": "42 total rows — {'bounce': 42}"},
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
                {"name": f"send{i}.example.com", "type": "DOMAIN", "dkim_status": "SUCCESS", "verified": True, "sending": True}
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
        "errors": [],
        "collect_ms": 12,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(demo_model(), indent=2, default=str))
