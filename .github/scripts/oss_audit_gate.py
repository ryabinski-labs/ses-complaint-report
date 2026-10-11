#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski
"""Fail CI when the OSS readiness audits regress.

Runs the vendored audit scripts and enforces a policy:

- ``opensource_audit``  may be ``go`` or ``conditional``; ``no-go`` fails.
  (``conditional`` is tolerated because its ``sensitive_terms`` heuristic
  flags boto3's ``.client(`` call, which is not a real finding.)
- ``readme_audit`` / ``license_metadata_audit`` / ``workflow_audit`` must be ``go``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

RANK = {"go": 2, "conditional": 1, "no-go": 0}

# module name -> minimum acceptable decision
POLICY = {
    "opensource_audit": "conditional",
    "readme_audit": "go",
    "license_metadata_audit": "go",
    "workflow_audit": "go",
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    failures: list[str] = []
    for name, required in POLICY.items():
        module = _load(name)
        report = module.audit(ROOT)
        decision = str(report.get("decision", "no-go"))
        counts = report.get("counts", {})
        mark = "ok" if RANK.get(decision, 0) >= RANK[required] else "FAIL"
        print(f"[{mark}] {name}: {decision} (requires >= {required}) counts={counts}")
        if mark == "FAIL":
            failures.append(f"{name} is '{decision}' (requires >= '{required}')")
            for finding in report.get("findings", []):
                sev = finding.get("severity") if isinstance(finding, dict) else None
                if sev in {"blocker", "warn"}:
                    path = finding.get("path") or finding.get("check")
                    msg = finding.get("message", "")
                    print(f"        - {sev}: {path} — {msg}")

    if failures:
        print("\nOSS audit gate FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nOSS audit gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
