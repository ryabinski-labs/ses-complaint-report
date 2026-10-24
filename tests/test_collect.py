# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

import dataclasses
import datetime as dt

from sesreport import collect
from sesreport.collect import _day, _is_simulator, _iso


def test_day_uses_utc_like_cloudwatch_and_the_ledger():
    # CloudWatch daily buckets, the ledger's ``day`` and the SES console are all
    # UTC. Labelling by local time put every winter day one day early on a
    # BST machine (a 2025-11-24 UTC bounce showed as 2025-11-23).
    tz = dt.timezone(dt.timedelta(hours=-4))
    d = dt.datetime(2026, 9, 15, 20, 0, tzinfo=tz)  # 00:00 UTC on the 16th
    assert _day(d) == "2026-09-16"
    assert _day(dt.datetime(2026, 9, 15, 23, 59, tzinfo=dt.timezone.utc)) == "2026-09-15"


def test_aligned_window_ends_at_utc_midnight():
    start, end = collect._aligned_window(3)
    assert end.tzinfo == dt.timezone.utc and (end.hour, end.minute) == (0, 0)
    assert (end - start).days == 3


def test_day_of_plain_date():
    assert _day("2026-05-18") == "2026-05-18"


def test_iso_normalises_datetime_to_utc():
    tz = dt.timezone(dt.timedelta(hours=-4))
    d = dt.datetime(2026, 5, 19, 3, 36, 50, tzinfo=tz)
    assert _iso(d).startswith("2026-05-19T07:36:50")


def test_iso_passes_through_strings():
    assert _iso("2026-05-19T07:36:50Z") == "2026-05-19T07:36:50Z"


def test_simulator_detects_mailbox_simulator():
    assert _is_simulator("complaint@simulator.amazonses.com")
    assert _is_simulator("bounce@simulator.amazonses.com")


def test_simulator_rejects_real_and_malformed_addresses():
    assert not _is_simulator("user@example.com")
    assert not _is_simulator("simulator.amazonses.com")  # no @
    assert not _is_simulator("")


def _with_redact(monkeypatch, on: bool):
    monkeypatch.setattr(collect, "CFG", dataclasses.replace(collect.CFG, redact_emails=on))


def test_redaction_is_off_by_default(monkeypatch):
    _with_redact(monkeypatch, False)
    assert collect._maybe_redact("alice@example.com") == "alice@example.com"


def test_redaction_masks_local_part_and_keeps_domain(monkeypatch):
    _with_redact(monkeypatch, True)
    assert collect._maybe_redact("alice@example.com") == "a****@example.com"
    assert collect._maybe_redact("a@x.com") == "a***@x.com"
    assert collect._maybe_redact("") == ""
    assert collect._maybe_redact("not-an-email") == "not-an-email"


def test_redaction_matches_the_ledger_formula(monkeypatch):
    # The report must redact to exactly what the ingest Lambda stores, or a
    # redacted store row would not match its redacted ledger row.
    import importlib.util
    import sys
    from pathlib import Path

    _with_redact(monkeypatch, True)
    handler = Path(__file__).resolve().parents[1] / "infra" / "ledger_ingest" / "index.py"
    spec = importlib.util.spec_from_file_location("ledger_ingest_parity", handler)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ledger_ingest_parity"] = mod
    spec.loader.exec_module(mod)
    for addr in ("alice@example.com", "a@x.com", "complaint@simulator.amazonses.com"):
        assert collect._maybe_redact(addr) == mod.redact(addr)


def test_store_breakdown_is_human_readable():
    # The stores table showed a raw Python dict repr ({'bounce': 42}).
    assert collect._fmt_breakdown({"bounce": 42, "complaint": 3}) == "bounce: 42, complaint: 3"
    assert collect._fmt_breakdown("custom") == "custom"
