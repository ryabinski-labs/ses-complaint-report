# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Atheris fuzz targets for the parsers that read untrusted-ish input.

Two surfaces are fuzzed:

- ``history`` — :func:`sesreport.app.parse_history`, the JSON-lines reader for
  ``data/history.jsonl``. It must never raise: a corrupt or hostile line is
  skipped, not fatal.
- ``config`` — :func:`sesreport.config.load`, which reads values from the
  environment. A malformed numeric (port/interval) is the only documented
  failure and must surface as ``ValueError``.

Run locally (Linux only; Atheris has no macOS wheel)::

    uv run --group fuzz python fuzz/fuzz_targets.py -max_total_time=60

Pick a target with ``FUZZ_TARGET=config``. On a platform without Atheris the
script falls back to a small deterministic corpus so it is still runnable.
"""

from __future__ import annotations

import os
import sys

from sesreport import config
from sesreport.app import parse_history

_ENV_KEYS = (
    "SESREPORT_PROFILE",
    "SESREPORT_REGION",
    "SESREPORT_EXTRA_REGIONS",
    "SESREPORT_LOG_GROUPS",
    "SESREPORT_LOG_GROUP_PREFIX",
    "SESREPORT_LOG_GROUP_MATCH",
    "SESREPORT_SUPPRESSION_TABLES",
    "SESREPORT_REDACT_EMAILS",
    "SESREPORT_DEMO",
    "SESREPORT_HOST",
    "SESREPORT_PORT",
    "SESREPORT_REFRESH_SECONDS",
    "SESREPORT_HISTORY_MAX",
)


def fuzz_history(data: bytes) -> None:
    parse_history(data.decode("utf-8", "replace"))


def fuzz_config(data: bytes) -> None:
    # An environment value cannot contain a NUL byte, so strip NULs to keep the
    # harness within states a real environment can hold.
    value = data.decode("utf-8", "replace").replace("\x00", "")
    for key in _ENV_KEYS:
        os.environ[key] = value
    try:
        config.load()
    except ValueError:
        # A non-numeric port/interval is the only expected, documented failure.
        pass
    finally:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)


TARGETS = {"history": fuzz_history, "config": fuzz_config}


def _smoke() -> None:
    for item in (b"", b"{}\n", b'{"a": 1}\n\nnot json\n', b"[" * 4000 + b"]" * 4000):
        fuzz_history(item)
    fuzz_config(b"1")
    fuzz_config(b"not-a-number")
    print("atheris not installed; ran the smoke corpus only.")


def main() -> None:
    try:
        import atheris
    except ImportError:
        _smoke()
        return

    name = os.environ.get("FUZZ_TARGET", "history")
    if name not in TARGETS:
        raise SystemExit(f"unknown FUZZ_TARGET={name!r}; choose from {sorted(TARGETS)}")
    atheris.Setup(sys.argv, TARGETS[name])
    atheris.Fuzz()


if __name__ == "__main__":
    main()
