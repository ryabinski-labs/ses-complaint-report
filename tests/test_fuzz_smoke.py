# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Deterministic regression corpus for the fuzz targets.

These run in the normal ``validate`` job (no Atheris required) so the parsers
keep their "never crash" property between fuzzing runs.
"""

from __future__ import annotations

import json

import pytest

from sesreport import config
from sesreport.app import parse_history

CORPUS = [
    "",
    "\n\n",
    "{}\n",
    '{"a": 1}\n',
    '{"a": 1}\n\nnot json\n',
    "[]\nnull\n1\n",
    "{" + '"a": ' * 1000 + "1" + "}" * 1000,
    "[" * 4000 + "]" * 4000,
    "\x00\xff\xfe not utf-8 \n",
]


@pytest.mark.parametrize("text", CORPUS)
def test_parse_history_never_raises(text):
    assert isinstance(parse_history(text), list)


def test_parse_history_reads_valid_lines():
    text = json.dumps({"generated_at": "x", "total_complaints": 1}) + "\n"
    assert parse_history(text) == [{"generated_at": "x", "total_complaints": 1}]


def test_parse_history_skips_blank_and_bad_lines():
    assert parse_history("\n  \n{bad}\n") == []


def test_config_rejects_non_numeric_port(monkeypatch):
    monkeypatch.setenv("SESREPORT_PORT", "not-a-number")
    with pytest.raises(ValueError):
        config.load()


def test_config_tolerates_arbitrary_text(monkeypatch):
    monkeypatch.setenv("SESREPORT_HOST", "x" * 5000)
    monkeypatch.setenv("SESREPORT_SUPPRESSION_TABLES", ", ,a, b ,")
    cfg = config.load()
    assert cfg.suppression_tables == ["a", "b"]
