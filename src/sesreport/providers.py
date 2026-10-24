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
import time
from collections.abc import Callable
from pathlib import Path

OTHER = "Other"
_TABLE = json.loads((Path(__file__).resolve().parent / "providers.json").read_text())
DOMAINS: dict[str, str] = _TABLE["domains"]
MX_SUFFIXES: list[tuple[str, str]] = [tuple(x) for x in _TABLE["mx_suffixes"]]
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
                self._cache = json.loads(cache_path.read_text())
            except (ValueError, OSError):
                self._cache = {}

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
        provider = classify(d, self.mx)
        self._cache[d] = {"provider": provider, "at": self.clock()}
        return self._tally(provider)

    def _tally(self, provider: str) -> str:
        self.stats["other" if provider == OTHER else "from_mx"] += 1
        return provider

    def save(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, sort_keys=True))
        except OSError:
            pass
