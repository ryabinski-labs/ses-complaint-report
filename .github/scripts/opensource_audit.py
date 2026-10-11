#!/usr/bin/env python3
"""Heuristic open-source readiness audit for a repository."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "coverage",
    ".next",
    ".nuxt",
    "target",
}

TEXT_EXTS = {
    ".c",
    ".cc",
    ".cfg",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".env",
    ".go",
    ".h",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".lock",
    ".md",
    ".mjs",
    ".php",
    ".properties",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("generic_assignment", re.compile(r"(?i)\b(secret|token|api[_-]?key|password)\b\s*[:=]\s*['\"][^'\"\s]{16,}['\"]")),
]

RISKY_FILENAMES = [
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_dsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "kubeconfig",
]


@dataclass
class Finding:
    severity: str
    check: str
    path: str
    message: str


def git(args: list[str], root: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return 127, ""
    return proc.returncode, proc.stdout.strip()


def exists_any(root: Path, names: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    lower_names = {name.lower() for name in names}
    for child in root.iterdir():
        if child.name.lower() in lower_names:
            found.append(child)
    github = root / ".github"
    if github.exists():
        for name in names:
            candidate = github / name
            if candidate.exists():
                found.append(candidate)
    return found


def git_visible_files(root: Path) -> list[Path]:
    code, output = git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    if code != 0:
        return []
    return [root / item for item in output.split("\0") if item]


def walk_files(root: Path) -> Iterable[Path]:
    public_files = git_visible_files(root)
    if public_files:
        for path in public_files:
            if path.is_file():
                yield path
        return

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".terraform")]
        for filename in filenames:
            path = Path(dirpath) / filename
            yield path


def is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTS:
        return True
    if path.name in RISKY_FILENAMES:
        return True
    return False


def read_text(path: Path, limit: int = 512_000) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(errors="ignore")
    except OSError:
        return None


def package_metadata(root: Path) -> dict[str, object]:
    metadata: dict[str, object] = {}
    package_json = root / "package.json"
    if package_json.exists():
        try:
            metadata["package.json"] = json.loads(package_json.read_text())
        except Exception:
            metadata["package.json"] = {"_parse_error": True}
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = read_text(pyproject) or ""
        metadata["pyproject.toml"] = {
            "has_license": bool(re.search(r"(?m)^\s*license\s*=", text)),
            "has_project": "[project]" in text or "[tool.poetry]" in text,
        }
    cargo = root / "Cargo.toml"
    if cargo.exists():
        text = read_text(cargo) or ""
        metadata["Cargo.toml"] = {"has_license": bool(re.search(r"(?m)^\s*license\s*=", text))}
    go_mod = root / "go.mod"
    if go_mod.exists():
        metadata["go.mod"] = {"present": True}
    return metadata


def add_presence(findings: list[Finding], root: Path, names: list[str], check: str, required: bool, detail: str) -> None:
    found = exists_any(root, names)
    if found:
        paths = ", ".join(str(path.relative_to(root)) for path in found)
        findings.append(Finding("ok", check, paths, "present"))
    else:
        findings.append(Finding("blocker" if required else "warn", check, ".", detail))


def audit(root: Path) -> dict[str, object]:
    findings: list[Finding] = []
    root = root.resolve()

    if not root.exists() or not root.is_dir():
        raise SystemExit(f"not a directory: {root}")

    code, inside = git(["rev-parse", "--is-inside-work-tree"], root)
    if code == 0 and inside == "true":
        findings.append(Finding("ok", "git", ".", "git repository detected"))
        _, status = git(["status", "--short"], root)
        if status:
            findings.append(Finding("warn", "git_dirty", ".", "working tree has uncommitted changes"))
        _, remotes = git(["remote", "-v"], root)
        if remotes:
            findings.append(Finding("info", "git_remotes", ".", "review remotes before publishing"))
    else:
        findings.append(Finding("warn", "git", ".", "not detected as a git repository"))

    add_presence(findings, root, ["README.md", "README.rst", "README"], "readme", True, "missing README")
    add_presence(findings, root, ["LICENSE", "LICENSE.md", "COPYING"], "license_file", True, "missing license file")
    add_presence(findings, root, ["CONTRIBUTING.md"], "contributing", False, "missing contributor guide")
    add_presence(findings, root, ["CODE_OF_CONDUCT.md"], "code_of_conduct", False, "missing code of conduct")
    add_presence(findings, root, ["SECURITY.md"], "security_policy", False, "missing vulnerability reporting policy")
    add_presence(findings, root, ["CHANGELOG.md", "CHANGES.md", "NEWS.md"], "changelog", False, "missing changelog or release notes")
    add_presence(findings, root, ["MAINTAINERS.md"], "maintainers_roster", True, "missing MAINTAINERS.md with cigan1@gmail.com")
    maintainers = next((path for path in (root / "MAINTAINERS.md", root / ".github" / "MAINTAINERS.md") if path.exists()), None)
    if maintainers:
        text = read_text(maintainers) or ""
        rel = str(maintainers.relative_to(root))
        if "cigan1@gmail.com" in text:
            findings.append(Finding("ok", "required_maintainer", rel, "cigan1@gmail.com listed as maintainer"))
        else:
            findings.append(Finding("blocker", "required_maintainer", rel, "cigan1@gmail.com must be listed as a maintainer"))

    github = root / ".github"
    if (github / "workflows").exists():
        findings.append(Finding("ok", "ci", ".github/workflows", "CI workflows present"))
    else:
        findings.append(Finding("warn", "ci", ".", "no .github/workflows directory detected"))
    if (github / "dependabot.yml").exists():
        findings.append(Finding("ok", "dependabot", ".github/dependabot.yml", "dependency update config present"))
    else:
        findings.append(Finding("info", "dependabot", ".", "consider dependency update automation"))

    metadata = package_metadata(root)
    if metadata:
        for name, data in metadata.items():
            if name == "package.json" and isinstance(data, dict):
                if data.get("license"):
                    findings.append(Finding("ok", "package_license", name, "license metadata present"))
                else:
                    findings.append(Finding("warn", "package_license", name, "missing license metadata"))
                if data.get("scripts", {}).get("test"):
                    findings.append(Finding("ok", "tests", name, "test script present"))
                else:
                    findings.append(Finding("warn", "tests", name, "no package test script detected"))
            elif isinstance(data, dict) and data.get("has_license"):
                findings.append(Finding("ok", "package_license", name, "license metadata present"))
            elif name in {"pyproject.toml", "Cargo.toml"}:
                findings.append(Finding("warn", "package_license", name, "missing license metadata"))
    else:
        findings.append(Finding("info", "package_metadata", ".", "no recognized package metadata found"))

    test_paths = [p for p in ["tests", "test", "__tests__", "spec"] if (root / p).exists()]
    if test_paths:
        findings.append(Finding("ok", "test_layout", ", ".join(test_paths), "test directory detected"))
    else:
        findings.append(Finding("warn", "test_layout", ".", "no common test directory detected"))

    for path in walk_files(root):
        rel = str(path.relative_to(root))
        name = path.name
        lower = name.lower()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 50 * 1024 * 1024:
            findings.append(Finding("warn", "large_file", rel, f"large file ({size // (1024 * 1024)} MiB)"))
        if lower in RISKY_FILENAMES or lower.endswith((".pem", ".key", ".p12", ".pfx")):
            findings.append(Finding("blocker", "sensitive_filename", rel, "sensitive filename requires review"))
        if not is_probably_text(path):
            continue
        text = read_text(path)
        if text is None:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if "example" in rel.lower() or "sample" in rel.lower():
                continue
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(Finding("blocker", f"secret:{label}", f"{rel}:{line_no}", "possible secret or credential"))
                    break
        if re.search(r"(?i)\b(customer|client|confidential|internal only|do not distribute)\b", text):
            findings.append(Finding("warn", "sensitive_terms", rel, "contains terms that may indicate private/internal material"))

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    if counts.get("blocker"):
        decision = "no-go"
    elif counts.get("warn"):
        decision = "conditional"
    else:
        decision = "go"

    return {
        "root": str(root),
        "decision": decision,
        "counts": counts,
        "findings": [asdict(f) for f in findings],
    }


def print_human(report: dict[str, object]) -> None:
    print(f"Open source readiness: {report['decision']}")
    print(f"Repository: {report['root']}")
    print(f"Counts: {report['counts']}")
    print()
    for finding in report["findings"]:  # type: ignore[index]
        print(f"[{finding['severity']}] {finding['check']} {finding['path']} - {finding['message']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", default=".", help="repository path")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    report = audit(Path(args.repo))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
