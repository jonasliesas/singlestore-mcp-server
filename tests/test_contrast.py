"""Contrast of the colours changed in the 2026-10 design round (WCAG 2.x, small text needs 4.5:1), measured from
shared.css itself: the engine chips (text on their 12 % tint), the primary button and count pills (white on the
primary fill), the alert badge, the SpeedyStore / standard-connector caslib badges and the "Ask Claude" sparkle, in
light and dark (the fallback page colours of shared.css). The browser test in tests/ui/test_studio_ui.py measures the
rendered page too."""

from __future__ import annotations

import re

import pytest
from support import APPS_DIR

CSS = (APPS_DIR / "shared.css").read_text(encoding="utf-8")
BG = {"light": ["#ffffff", "#f6f7f9"], "dark": ["#1e1e1e", "#262626"]}  # --s2-bg / --s2-bg-2 fallbacks


def _token(name: str) -> dict[str, str]:
    m = re.search(rf"--{name}:\s*([^;]+);", CSS)
    assert m, f"--{name} missing from shared.css"
    v = m.group(1).strip()
    ld = re.fullmatch(r"light-dark\(\s*(#[0-9a-fA-F]{6})\s*,\s*(#[0-9a-fA-F]{6})\s*\)", v)
    if ld:
        return {"light": ld.group(1), "dark": ld.group(2)}
    ld = re.fullmatch(r"light-dark\(\s*var\([^)]*\)\s*,\s*(#[0-9a-fA-F]{6})\s*\)", v)
    if ld:  # light: the host's colour (checked elsewhere); dark: ours
        return {"dark": ld.group(1)}
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", v), f"--{name}: {v}"
    return {"light": v, "dark": v}


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lum(h: str) -> float:
    def f(c: int) -> float:
        x = c / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a: str, b: str) -> float:
    x, y = sorted((_lum(a), _lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def mix(fg: str, bg: str, p: float) -> str:
    """color-mix(in srgb, fg p, bg)."""
    return "#" + "".join(f"{round(a * p + b * (1 - p)):02x}" for a, b in zip(_rgb(fg), _rgb(bg), strict=True))


def test_ratio_reference() -> None:
    assert round(ratio("#ffffff", "#000000"), 1) == 21.0
    assert round(ratio("#ffffff", "#4493f8"), 1) == 3.1  # the old dark primary button (the review's measurement)


@pytest.mark.parametrize("engine", ["s2", "compute", "cas", "python", "sas"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_engine_chip_text_on_its_tint(engine: str, theme: str) -> None:
    fg = _token(f"s2-eng-{engine}")[theme]
    for bg in BG[theme]:
        chip = mix(fg, bg, 0.12)  # .s2-engine / .sq-lang: color-mix(in srgb, var(--ec) 12%, var(--s2-bg))
        assert ratio(fg, chip) >= 4.5, f"{engine} {theme}: {fg} on {chip} = {ratio(fg, chip):.2f}"
        assert ratio(fg, bg) >= 4.5, f"{engine} {theme}: {fg} on {bg} = {ratio(fg, bg):.2f}"


def test_white_on_fills() -> None:
    on = _token("s2-on-primary")["dark"]
    assert ratio(on, _token("s2-primary")["dark"]) >= 4.5  # dark primary button / count pills (was 3.1)
    for theme in ("light", "dark"):
        assert ratio("#ffffff", _token("s2-err-fill")[theme]) >= 4.5  # alert count badge (dark was 3.35)
        assert ratio("#ffffff", _token("s2-src-ss")[theme]) >= 4.5    # SpeedyStore caslib badge


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_outline_badge_and_sparkle(theme: str) -> None:
    for name in ("s2-src-s2", "s2-ai"):
        fg = _token(name)[theme]
        for bg in BG[theme]:
            assert ratio(fg, bg) >= 4.5, f"--{name} {theme}: {fg} on {bg} = {ratio(fg, bg):.2f}"


def test_views_use_the_tokens() -> None:
    """The old hard-coded colours that failed in dark mode are gone from the views."""
    for page in ("sql_editor.html", "schema_explorer.html", "notebook.html", "workload.html"):
        html = (APPS_DIR / page).read_text(encoding="utf-8")
        for old in ("#c2410c", "#7c3aed", "#0f766e", "#0378cd", "#5b7290", "--s2-accent"):
            assert old not in html, f"{page} still uses {old}"
