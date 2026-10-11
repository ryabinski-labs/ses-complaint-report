# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Contract test: the frontend only reads fields the backend actually produces.

Catches the class of bug where a field is renamed in the collector but still
referenced in index.html (or vice versa).
"""

from __future__ import annotations

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
    "unattributed",
    "stores",
    "config_sets",
    "identities",
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

    for row in m["suppression_coverage"]:
        assert {"config_set", "routes_feedback", "risk"} <= set(row)

    for row in m["config_sets"]:
        assert {"name", "event_types", "destinations", "forwards_feedback"} <= set(row)


def test_complaint_counts_are_consistent():
    m = _model()
    met = m["metrics"]
    assert met["total_complaints"] == sum(d["count"] for d in met["complaints_by_day"])
