# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

import dataclasses
import datetime as dt

from sesreport import collect
from sesreport.collect import _day, _is_simulator, _iso


def test_day_uses_wall_clock_not_utc():
    # CloudWatch publishes these buckets in local time; a 20:00-04:00 bucket is
    # still that same day, not the next day in UTC.
    tz = dt.timezone(dt.timedelta(hours=-4))
    d = dt.datetime(2026, 9, 15, 20, 0, tzinfo=tz)
    assert _day(d) == "2026-09-15"


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
