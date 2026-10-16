# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Structural guards for the recipient-level complaint table (§4.2).

The table is built by index.html's JavaScript, so these are contract tests over
the static asset in the same style as `test_frontend_contract.py`: they exist so
a future refactor cannot silently drop the export fields, the sort fallback for
the stacked phone layout, the scroll affordances, or the region semantics that
were fixed after the table's columns were merged to stop it overflowing.

Behavioral coverage of the same contracts lives in `tests/ui/` (Playwright).
"""

from __future__ import annotations

import re
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "src" / "sesreport" / "static" / "index.html"

# Fields every complaint record carries; the merge into 6 visible columns must
# not shrink the CSV export.
RECORD_FIELDS = (
    "email",
    "simulator",
    "store",
    "source",
    "sending_identity",
    "event_type",
    "detail",
    "first_seen",
    "last_seen",
    "hits",
)


def _record_table_block() -> str:
    """The makeTable(...) call that mounts the §4.2 record table."""
    js = INDEX.read_text()
    start = js.index("document.getElementById('recordTable')")
    end = js.index("document.getElementById('complainantTable')")
    return js[start:end]


def test_record_table_exports_every_record_field():
    block = _record_table_block()
    assert "csvColumns:[" in block, "record table must pin its own CSV columns"
    csv_block = block[block.index("csvColumns:[") :]
    for key in RECORD_FIELDS:
        assert f"key:'{key}'" in csv_block, f"CSV export no longer includes {key!r}"


def test_record_table_keeps_sorting_available_when_headers_are_stacked():
    # Below 640px the table renders as cards and the sortable column headers
    # are hidden, so a toolbar control must take over.
    js = INDEX.read_text()
    assert "'Sort table'" in js, "stacked layout needs an accessible sort control"
    assert "class:'search selsort'" in js, "sort select is missing"
    assert "ghost mini selsort" in js, "sort direction toggle is missing"
    assert ".selsort { display:none; }" in js, "sort controls must not duplicate the headers"
    assert "@media(max-width:640px)" in js, "stacked (card) breakpoint is missing"
    assert ".controls .selsort { display:block; }" in js, "sort controls must appear only in the stacked layout"


def test_record_table_scroll_affordances_present():
    js = INDEX.read_text()
    for hook in (
        ".tablewrap th:first-child, .tablewrap td:first-child { position:sticky;",
        ".tablewrap.scrolled-x",
        "class:'scrollfade'",
        ".tablebox.more-right .scrollfade",
        "td.dataset.label = c.title",
        ".tablewrap td::before { content:attr(data-label);",
    ):
        assert hook in js, f"table scroll/stack affordance missing: {hook!r}"


def test_record_table_region_semantics_follow_scrollability():
    # A region that cannot scroll must neither take tab focus nor claim to be
    # scrollable; sorters must expose their state to assistive tech.
    js = INDEX.read_text()
    assert "wrap.tabIndex = scrollable ? 0 : -1" in js
    assert "aria-label', scrollable ? 'Scrollable table" in js
    assert "'aria-sort'" in js


# --- colour contrast: `.bad` text must clear WCAG AA on every surface -------


def _hex_var(js: str, name: str) -> str:
    match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", js)
    assert match, f"index.html no longer defines --{name}"
    return match.group(1)


def _relative_luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = linear(r), linear(g), linear(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: str, bg: str) -> float:
    hi, lo = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_alert_text_red_clears_wcag_aa_on_every_surface():
    # `.bad` text renders on the page background (tables), the card background
    # (stat cards, boxes, and the phone card layout) and the hover background;
    # the decorative `--red` token is reserved for borders, charts, and tags
    # where contrast rules do not apply the same way.
    js = INDEX.read_text()
    assert ".bad { color:var(--redtx); }" in js
    redtx = _hex_var(js, "redtx")
    for surface in ("bg", "card", "card2"):
        ratio = _contrast_ratio(redtx, _hex_var(js, surface))
        assert ratio >= 4.5, f"--redtx on --{surface} is {ratio:.2f}:1 (WCAG AA needs 4.5)"
