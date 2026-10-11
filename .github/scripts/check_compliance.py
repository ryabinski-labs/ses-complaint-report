#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski
"""Fail CI when required OSS files are missing or sensitive material is tracked."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REQUIRED = [
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "MAINTAINERS.md",
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
]

# Files that must never be tracked if present on disk.
FORBIDDEN_TRACKED = [".env"]
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12"}

failures: list[str] = []

for rel in REQUIRED:
    if not (ROOT / rel).exists():
        failures.append(f"missing required file: {rel}")


def tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        )
        return out.stdout.splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


for rel in tracked():
    if rel in FORBIDDEN_TRACKED:
        failures.append(f"forbidden file is tracked: {rel}")
    if Path(rel).suffix in FORBIDDEN_SUFFIXES:
        failures.append(f"sensitive file is tracked: {rel}")

# The maintainer roster must name the project owner.
maint = ROOT / "MAINTAINERS.md"
if maint.exists() and "cigan1@gmail.com" not in maint.read_text():
    failures.append("MAINTAINERS.md must list cigan1@gmail.com")

if failures:
    print("Compliance check FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("Compliance check passed.")
