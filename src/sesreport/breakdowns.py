# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Per-provider, per-identity and per-config-set rates from the ledger (VDM off).

Numerators are the ledger's complaint and permanent-bounce rows; denominators
are the ledger's daily Delivery counters; engagement uses its unique Open and
Click counters. Both come from the same SNS feed, so a rate never mixes sources.
A rate is shown only once a row has ``floor`` sends in the window.

The bounce and complaint feeds predate Delivery counting (identity-level topics
were subscribed first), so a window's numerator starts no earlier than its first
Delivery counter: ``since_effective``. Otherwise ten days of bounces would sit
over one day of deliveries.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable

FLOOR = 1000
RANGES = {"30": 30, "90": 90, "180": 180, "365": 365, "all": None}
SIMULATOR_DOMAIN = "simulator.amazonses.com"
TOO_FEW = "too few sends"
NO_DELIVERIES = "no delivery data yet"
EVENT_FIELD = {"DELIVERY": "deliveries", "OPEN_UNIQUE": "opens", "CLICK_UNIQUE": "clicks"}


def _row(name: str) -> dict:
    return {"name": name, "deliveries": 0, "complaints": 0, "bounces": 0, "opens": 0, "clicks": 0}


def _finish(row: dict, floor: int, have_deliveries: bool) -> dict:
    d = row["deliveries"]
    # A bounced message was never delivered, so SES's bounce rate divides by
    # sends, not deliveries; dividing by deliveries can exceed 100%.
    sends = d + row["bounces"]
    row["sends"] = sends
    if sends >= floor and d > 0:
        row.update(
            complaint_rate=row["complaints"] / d,
            bounce_rate=row["bounces"] / sends,
            open_rate=row["opens"] / d,
            click_rate=row["clicks"] / d,
            rate_note=None,
        )
    else:
        row.update(complaint_rate=None, bounce_rate=None, open_rate=None, click_rate=None,
                   rate_note=TOO_FEW if have_deliveries else NO_DELIVERIES)
    return row


def first_delivery_day(counters: Iterable[dict]) -> str | None:
    days = [c["day"] for c in counters if c.get("event") == "DELIVERY" and int(c.get("n") or 0) > 0]
    return min(days) if days else None


def build_breakdowns(
    counters: Iterable[dict],
    ledger_rows: Iterable[dict],
    since: str | None = None,
    floor: int = FLOOR,
    classify: Callable[[str], str] = lambda d: d,
    configured: bool = True,
) -> dict:
    """One window's breakdowns. ``since`` is an inclusive ISO day, or None for all."""
    if not configured:
        return {"state": "ledger_not_configured", "since": since, "since_effective": since,
                "providers": [], "identities": [], "config_sets": []}
    counters = list(counters)
    start = first_delivery_day(counters)
    # numerators and denominators cover the same days
    eff = max(filter(None, (since, start)), default=None)
    groups = {"providers": {}, "identities": {}, "config_sets": {}}

    def bump(domain: str, identity: str, config_set: str, field: str, n: int) -> None:
        if domain == SIMULATOR_DOMAIN:
            return
        for key, name in (("providers", classify(domain)), ("identities", identity or "(unknown)"),
                          ("config_sets", config_set or "(none)")):
            groups[key].setdefault(name, _row(name))[field] += n

    have_deliveries = False
    for c in counters:
        if eff and c["day"] < eff:
            continue
        field = EVENT_FIELD.get(c["event"])
        if not field:
            continue
        have_deliveries = have_deliveries or (field == "deliveries" and c["n"] > 0)
        bump(c["domain"], c["identity"], c["config_set"], field, int(c["n"]))
    for r in ledger_rows:
        day = (r.get("day") or r.get("last_seen") or "")[:10]
        if eff and day < eff:
            continue
        if r.get("simulator"):
            continue
        field = {"COMPLAINT": "complaints", "BOUNCE": "bounces"}.get((r.get("event_type") or "").upper())
        if field:
            bump(r.get("recipient_domain") or "", r.get("sending_identity") or "", r.get("config_set") or "", field, 1)

    out = {"state": "ok" if have_deliveries else "no_delivery_data", "since": since, "since_effective": eff}
    for key, rows in groups.items():
        out[key] = sorted((_finish(r, floor, have_deliveries) for r in rows.values()),
                          key=lambda r: (-r["deliveries"], -r["complaints"], r["name"]))
    return out


def build_all_ranges(counters: list[dict], ledger_rows: list[dict], today: dt.date,
                     classify: Callable[[str], str] = lambda d: d, configured: bool = True,
                     floor: int = FLOOR) -> dict:
    ranges = {}
    for key, days in RANGES.items():
        # "30" means today and the 29 days before it, not 31 days
        since = None if days is None else (today - dt.timedelta(days=days - 1)).isoformat()
        ranges[key] = build_breakdowns(counters, ledger_rows, since, floor, classify, configured)
    state = ranges["all"]["state"]
    return {"state": state, "floor": floor, "source": "ledger", "ranges": ranges,
            "first_delivery_day": first_delivery_day(counters)}
