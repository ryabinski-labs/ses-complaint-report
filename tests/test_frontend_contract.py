# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Contract test: the frontend only reads fields the backend actually produces.

Catches the class of bug where a field is renamed in the collector but still
referenced in index.html (or vice versa).
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from sesreport.demo import demo_model

INDEX = Path(__file__).resolve().parents[1] / "src" / "sesreport" / "static" / "index.html"

REQUIRED_TOP = {
    "meta",
    "posture",
    "metrics",
    "config_set_complaints",
    "complainants",
    "complaint_records",
    "attribution",
    "ledger",
    "unattributed",
    "stores",
    "config_sets",
    "identities",
    "identity_feedback",
    "sns_topics",
    "complaint_queue",
    "suppression_list",
    "retention",
    "suppression_coverage",
    "errors",
    "collect_ms",
}


def _model() -> dict:
    return demo_model()


def test_known_top_level_keys_present():
    model = _model()
    missing = REQUIRED_TOP - set(model)
    assert not missing, f"model missing top-level keys the UI expects: {sorted(missing)}"


def test_frontend_state_model_refs_are_produced():
    """Every `state.model.<key>` reference must exist in the model."""
    js = INDEX.read_text()
    refs = set(re.findall(r"state\.model\.(\w+)", js))
    # `state.model` is also used bare (e.g. `state.model` in JSON export)
    model = _model()
    missing = {r for r in refs if r not in model}
    assert not missing, f"index.html reads state.model.* keys the model lacks: {sorted(missing)}"


def test_nested_contracts():
    m = _model()
    met = m["metrics"]
    for k in (
        "window",
        "total_complaints",
        "total_sends",
        "overall_complaint_rate",
        "complaints_by_day",
        "rate_series",
        "peak_rate",
        "latest_rate",
    ):
        assert k in met, f"metrics missing {k}"

    for k in ("start", "end", "days", "retention_start", "retention_end"):
        assert k in met["window"], f"metrics.window missing {k}"

    # every complaint day the UI renders must carry an expiry
    for row in met["complaints_by_day"]:
        assert "date" in row and "expires" in row

    assert {"total", "complaints", "oldest_complaint", "expires"} <= set(m["suppression_list"])
    assert {"cloudwatch_days", "oldest_expires", "newest_expires"} <= set(m["retention"])

    for row in m["complainants"]:
        assert {"email", "arf_provider", "recorded_at", "bounce_count", "bouncing"} <= set(row)

    for row in m["complaint_records"]:
        assert {"email", "store", "detail", "source", "first_seen", "last_seen", "hits", "simulator"} <= set(row)

    assert {"cloudwatch_recorded", "records", "real_records", "simulator_records"} <= set(m["attribution"])

    assert {"configured", "table", "records"} <= set(m["ledger"])

    for row in m["identity_feedback"]:
        assert {"identity", "bounce_topic", "complaint_topic", "forwarding_enabled"} <= set(row)

    for row in m["suppression_coverage"]:
        assert {"config_set", "routes_feedback", "risk"} <= set(row)

    for row in m["config_sets"]:
        assert {"name", "event_types", "destinations", "forwards_feedback"} <= set(row)


def test_complaint_counts_are_consistent():
    m = _model()
    met = m["metrics"]
    assert met["total_complaints"] == sum(d["count"] for d in met["complaints_by_day"])


def test_attribution_counts_are_self_consistent():
    # The UI prints "records" alongside the ledger and store subtotals, so the
    # three must agree.
    a = _model()["attribution"]
    assert a["records"] == a["ledger_records"] + a["store_records"]
    assert a["real_records"] + a["simulator_records"] == a["records"]


def test_advisor_findings_carry_checked_at():
    """The UI prints "checked <time>" beside each finding, so each must be dated."""
    findings = _model()["advisor"]["findings"]
    assert findings
    missing = [f["id"] for f in findings if not f.get("checked_at")]
    assert not missing, f"Advisor findings without checked_at: {missing}"
    for f in findings:
        dt.datetime.fromisoformat(f["checked_at"])  # must parse as ISO-8601


def test_dashboard_has_accessibility_hooks():
    # Guards the a11y fixes: a main landmark, accessible names on the table
    # controls, and keyboard-focusable scroll regions.
    js = INDEX.read_text()
    assert '<main id="app"' in js, "dashboard needs a <main> landmark"
    assert "'aria-label'" in js, "table controls need accessible names"
    assert "tabindex:'0'" in js, "scrollable table regions must be focusable"
