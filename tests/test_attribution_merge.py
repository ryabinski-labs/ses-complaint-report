# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Regression tests for recipient-level complaint attribution.

The self-owned ledger stores a *redacted* address plus the SHA-256 of the real
one; an application store keeps the full address. Merging on the address string
alone counted a single complaint twice once the ledger was deployed — these
tests pin the identity-aware, ledger-authoritative merge.
"""

from __future__ import annotations

import hashlib

from sesreport.collect import _identity_hash, merge_complaint_records

DAY = "2026-10-12T11:30:34+00:00"
NEXT = "2026-10-13T09:00:00+00:00"
ALICE = hashlib.sha256(b"alice@example.com").hexdigest()


def _ledger(recipient_hash, last_seen=DAY, **kw):
    row = {
        "email": "a****@example.com",
        "store": "ses_feedback_ledger",
        "detail": "abuse",
        "source": "example-ses-feedback",
        "first_seen": last_seen,
        "last_seen": last_seen,
        "hits": 1,
        "simulator": False,
        "event_type": "COMPLAINT",
        "sending_identity": "example.com",
        "config_set": "",
        "recipient_hash": recipient_hash,
    }
    row.update(kw)
    return row


def _store(email, last_seen=DAY, **kw):
    row = {
        "email": email,
        "store": "app-suppressions",
        "detail": "abuse",
        "source": "sns",
        "first_seen": last_seen,
        "last_seen": last_seen,
        "hits": 1,
        "simulator": False,
    }
    row.update(kw)
    return row


def test_identity_hash_matches_the_ledger_hash():
    # Case-insensitive, and an already-hashed value passes through unchanged.
    assert _identity_hash("Alice@Example.com") == ALICE
    assert _identity_hash(ALICE) == ALICE


def test_identity_hash_keeps_redacted_strings():
    assert _identity_hash("a****@example.com") == "a****@example.com"
    assert _identity_hash("") == ""
    assert _identity_hash(None) == ""


def test_same_complaint_in_ledger_and_store_counts_once():
    merged, kept_ledger, kept_store = merge_complaint_records(
        [_ledger(ALICE)], [_store("alice@example.com")]
    )
    assert len(merged) == 1
    assert len(kept_ledger) == 1 and len(kept_store) == 0
    # the ledger row is authoritative
    assert merged[0]["store"] == "ses_feedback_ledger"


def test_records_equals_ledger_plus_store():
    merged, kept_ledger, kept_store = merge_complaint_records(
        [
            _ledger(ALICE),
            _ledger(
                hashlib.sha256(b"carol@example.com").hexdigest(),
                email="c***@example.com",
            ),
        ],
        [_store("alice@example.com"), _store("bob@example.com")],
    )
    assert len(merged) == len(kept_ledger) + len(kept_store)
    assert len(merged) == 3


def test_distinct_days_are_separate_records():
    merged, _, _ = merge_complaint_records(
        [_ledger(ALICE)], [_store("alice@example.com", last_seen=NEXT)]
    )
    assert len(merged) == 2


def test_store_row_fills_a_gap_the_ledger_did_not_capture():
    merged, kept_ledger, kept_store = merge_complaint_records(
        [_ledger(ALICE)], [_store("bob@example.com")]
    )
    assert len(merged) == 2
    assert len(kept_ledger) == 1 and len(kept_store) == 1


def test_merge_is_newest_first():
    merged, _, _ = merge_complaint_records(
        [_ledger(ALICE, last_seen=NEXT)], [_store("bob@example.com", last_seen=DAY)]
    )
    assert merged[0]["last_seen"] == NEXT
