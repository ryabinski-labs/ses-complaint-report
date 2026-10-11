#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski
"""Fail CI when the OSS readiness audits regress.

Runs the vendored audit scripts and enforces a policy:

- ``opensource_audit``  may be ``go`` or ``conditional``; ``no-go`` fails.
  (``conditional`` is tolerated because its ``sensitive_terms`` heuristic
  flags boto3's ``.client(`` call, which is not a real finding.)
- ``readme_audit`` / ``license_metadata_audit`` must be ``go``.
- ``workflow_audit`` must be ``go``, with one narrow exception: the CodeQL
  workflow's ``security-events: write`` on ``pull_request`` is allow-listed
  (see ``_workflow_warnings_allowed``). Any other workflow warning still fails.
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

CODEQL_WORKFLOW = ".github/workflows/codeql.yml"


def _workflow_warnings_allowed(report: dict) -> bool:
    """Allow only the CodeQL PR-triggered write-permission warning.

    The CodeQL workflow must request ``security-events: write`` on
    ``pull_request`` so code-scanning results attach to PR head SHAs (which is
    what the OpenSSF Scorecard SAST check measures). That permission is safe
    for ``github/codeql-action``. Every other warning or any blocker fails.
    """
    findings = [f for f in report.get("findings", []) if isinstance(f, dict)]
    if any(f.get("severity") == "blocker" for f in findings):
        return False
    warns = [f for f in findings if f.get("severity") == "warn"]
    if not warns:
        return False
    codeql = ROOT / CODEQL_WORKFLOW
    uses_codeql = codeql.exists() and "github/codeql-action/analyze" in codeql.read_text()
    return uses_codeql and all(
        f.get("check") == "pr_write_permissions" and str(f.get("path", "")).endswith("codeql.yml")
        for f in warns
    )


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
        note = ""
        if mark == "FAIL" and name == "workflow_audit" and _workflow_warnings_allowed(report):
            mark = "ok"
            note = " (allow-listed: CodeQL security-events on pull_request)"
        print(f"[{mark}] {name}: {decision} (requires >= {required}){note} counts={counts}")
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
