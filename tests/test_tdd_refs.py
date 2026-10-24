# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Every pytest ``test_ref`` in a TDD artifact must name a test that exists.

QA runs scenarios by their ``test_ref``; a ref that does not resolve makes the
scenario ``not_run`` and silently drops it from the release gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def test_every_pytest_test_ref_resolves_to_a_test_function():
    bad = []
    for artifact in sorted((ROOT / "tdd").glob("*.tdd.yaml")):
        doc = yaml.safe_load(artifact.read_text())
        for req in doc.get("requirements", []):
            for sc in req.get("scenarios", []):
                ref = sc.get("test_ref") or ""
                if not ref.endswith(".py") and ".py::" not in ref:
                    continue
                file, _, func = ref.partition("::")
                path = ROOT / file
                if not path.exists() or func not in _functions(path):
                    bad.append(f"{artifact.name}:{sc['id']} -> {ref}")
    assert not bad, "unresolvable test_refs: " + ", ".join(bad)


def test_every_playwright_test_ref_names_a_test_title():
    bad = []
    for artifact in sorted((ROOT / "tdd").glob("*.tdd.yaml")):
        doc = yaml.safe_load(artifact.read_text())
        for req in doc.get("requirements", []):
            for sc in req.get("scenarios", []):
                ref = sc.get("test_ref") or ""
                if ".spec." not in ref:
                    continue
                file = ref.split()[0]
                path = ROOT / file
                if not path.exists() or f"'{sc['id']} " not in path.read_text():
                    bad.append(f"{artifact.name}:{sc['id']} -> {ref}")
    assert not bad, "unresolvable test_refs: " + ", ".join(bad)
