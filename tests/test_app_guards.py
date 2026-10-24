# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Regression tests for the local-server guards found in QA.

- Demo mode must not share the live data directory (it overwrote latest.json and
  evicted real snapshots from the capped history).
- POST /api/refresh must refuse cross-site callers (any page the user visits could
  otherwise start billed AWS collection runs).
- Only loopback/bound Host headers are answered (DNS-rebinding guard).
"""

from __future__ import annotations

from pathlib import Path

from sesreport import app as appmod


def test_demo_mode_uses_its_own_data_dir():
    live = appmod.data_dir({}, demo=False)
    demo = appmod.data_dir({}, demo=True)
    assert demo != live
    assert demo.name == "demo"


def test_explicit_data_dir_wins_in_both_modes(tmp_path: Path):
    env = {"SESREPORT_DATA_DIR": str(tmp_path)}
    assert appmod.data_dir(env, demo=True) == tmp_path
    assert appmod.data_dir(env, demo=False) == tmp_path


def _headers(**kw) -> dict:
    h = {"host": "127.0.0.1:8787", appmod.REFRESH_HEADER: appmod.REFRESH_HEADER_VALUE}
    h.update({k.replace("_", "-"): v for k, v in kw.items()})
    return {k: v for k, v in h.items() if v is not None}


def test_refresh_allowed_from_the_page_itself():
    assert appmod.refresh_allowed(_headers(origin="http://127.0.0.1:8787", sec_fetch_site="same-origin"))
    assert appmod.refresh_allowed(_headers())  # curl / no browser metadata


def test_refresh_refused_without_custom_header():
    # a cross-site form POST or text/plain fetch cannot set this header
    h = _headers()
    del h[appmod.REFRESH_HEADER]
    assert not appmod.refresh_allowed(h)


def test_refresh_refused_cross_site():
    assert not appmod.refresh_allowed(_headers(origin="https://evil.example"))
    assert not appmod.refresh_allowed(_headers(sec_fetch_site="cross-site"))
    assert not appmod.refresh_allowed(_headers(origin="http://127.0.0.1:9999"))


def test_allowed_hosts_are_loopback_plus_bind_host():
    assert "attacker.example" not in appmod.allowed_hosts("127.0.0.1")
    assert set(appmod.allowed_hosts("0.0.0.0")) == {"127.0.0.1", "localhost", "::1", "[::1]"}
    assert "dash.internal" in appmod.allowed_hosts("127.0.0.1", "dash.internal")


def test_app_installs_trusted_host_middleware():
    names = [m.cls.__name__ for m in appmod.app.user_middleware]
    assert "TrustedHostMiddleware" in names
