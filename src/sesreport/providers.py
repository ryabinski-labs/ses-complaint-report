# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Map recipient domains to mailbox providers (what VDM calls an "ISP").

Known consumer domains map directly. Any other domain is classified by its MX
record, so business mail hosted on Google Workspace or Microsoft 365 lands under
Google or Microsoft. Unknown or failing lookups map to "Other". MX answers are
cached on disk for 7 days and each refresh makes at most ``max_lookups`` of them.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

OTHER = "Other"
_TABLE = json.loads((Path(__file__).resolve().parent / "providers.json").read_text())
DOMAINS: dict[str, str] = _TABLE["domains"]
MX_SUFFIXES: list[tuple[str, str]] = [tuple(x) for x in _TABLE["mx_suffixes"]]
KNOWN = set(DOMAINS.values()) | {p for _, p in MX_SUFFIXES} | {OTHER}
CACHE_TTL_S = 7 * 86400
MAX_LOOKUPS = 120


def provider_for_mx(hosts: list[str]) -> str:
    for host in hosts:
        h = host.lower().rstrip(".")
        for suffix, provider in MX_SUFFIXES:
            if h == suffix or h.endswith("." + suffix):
                return provider
    return OTHER


def classify(domain: str, mx: Callable[[str], list[str]] | None = None) -> str:
    """Provider for one domain; ``mx`` returns MX host names or raises."""
    d = (domain or "").strip().lower()
    if not d:
        return OTHER
    if d in DOMAINS:
        return DOMAINS[d]
    if mx is None:
        return OTHER
    try:
        return provider_for_mx(mx(d))
    except Exception:  # noqa: BLE001 - any DNS failure means "we don't know"
        return OTHER


class Classifier:
    """``classify`` with an on-disk MX cache and a per-refresh lookup budget."""

    def __init__(
        self,
        cache_path: Path | None,
        mx: Callable[[str], list[str]] | None,
        clock: Callable[[], float] = time.time,
        max_lookups: int = MAX_LOOKUPS,
    ):
        self.cache_path = cache_path
        self.mx = mx
        self.clock = clock
        self.max_lookups = max_lookups
        self.lookups = 0
        self.stats = {"from_map": 0, "from_mx": 0, "other": 0}
        self._cache: dict[str, dict] = {}
        if cache_path and cache_path.exists():
            try:
                raw = json.loads(cache_path.read_text())
            except (ValueError, OSError):
                raw = {}
            # the cache is data, not trusted input: keep only well-formed known providers
            self._cache = {d: v for d, v in (raw.items() if isinstance(raw, dict) else [])
                           if isinstance(v, dict) and v.get("provider") in KNOWN
                           and isinstance(v.get("at"), (int, float))}

    def __call__(self, domain: str) -> str:
        d = (domain or "").strip().lower()
        if d in DOMAINS:
            self.stats["from_map"] += 1
            return DOMAINS[d]
        hit = self._cache.get(d)
        if hit and self.clock() - hit.get("at", 0) < CACHE_TTL_S:
            return self._tally(hit["provider"])
        if not d or self.mx is None or self.lookups >= self.max_lookups:
            return self._tally(OTHER)
        self.lookups += 1
        try:
            hosts = self.mx(d)
        except Exception:  # noqa: BLE001 - a timeout or spent budget is "unknown", not "Other": retry next refresh
            return self._tally(OTHER)
        provider = provider_for_mx(hosts)
        self._cache[d] = {"provider": provider, "at": self.clock()}
        return self._tally(provider)

    def _tally(self, provider: str) -> str:
        self.stats["other" if provider == OTHER else "from_mx"] += 1
        return provider

    def save(self) -> None:
        if not self.cache_path:
            return
        try:
            write_private(self.cache_path, json.dumps(self._cache, sort_keys=True))
        except OSError:
            pass


def write_private(path: Path, text: str) -> None:
    """Write a data-dir file readable by its owner only (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)
