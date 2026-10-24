# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""The dashboard reads complaint/bounce rows only, never counter or dedupe items.

Delivery/Open/Click counters (``AGG#<day>``) and dedupe markers (``SEEN#<id>``)
share the ledger table with complaint rows. They carry no ``day`` attribute, so
the sparse ``by_day`` index excludes them; when that index cannot be read the
table-scan fallback must drop them, or they would show up as fake complaints.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

from sesreport import collect

ROW = {"recipient_hash": "ab" * 32, "sk": "1#COMPLAINT#m", "day": "2026-10-24", "ts_epoch": 1, "event_type": "COMPLAINT"}
COUNTER = {"recipient_hash": "AGG#2026-10-24", "sk": "gmail.com#example.com#transactional#DELIVERY", "n": 3}
MARKER = {"recipient_hash": "SEEN#0100M", "sk": "DELIVERY", "ttl": 1}


class Table:
    def __init__(self, index_error=None):
        self.index_error, self.calls = index_error, []

    def scan(self, **kw):
        self.calls.append(kw.get("IndexName"))
        if kw.get("IndexName"):
            if self.index_error:
                raise ClientError({"Error": {"Code": self.index_error, "Message": "x"}}, "Scan")
            return {"Items": [ROW]}  # the sparse index only ever holds rows with `day`
        return {"Items": [ROW, COUNTER, MARKER]}


def test_rows_come_from_the_by_day_index():
    t = Table()
    assert collect._scan_ledger_rows(t) == [ROW]
    assert t.calls == ["by_day"]


def test_table_scan_fallback_drops_counter_and_marker_items():
    for code in ("AccessDeniedException", "ValidationException", "ResourceNotFoundException"):
        t = Table(index_error=code)
        assert collect._scan_ledger_rows(t) == [ROW], code
        assert t.calls == ["by_day", None]


def test_other_index_errors_are_not_swallowed():
    t = Table(index_error="ProvisionedThroughputExceededException")
    try:
        collect._scan_ledger_rows(t)
    except ClientError:
        return
    raise AssertionError("throttling must surface as a collector warning, not a silent full scan")
