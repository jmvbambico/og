"""Contract tests for installer/themes.json -- the `og agents` pane colour catalog.

Pure stdlib + pytest and no import from this repo: the catalog is DATA (the same
deal as installer/registry.json), so it is loaded by path and judged on its own
terms. The point of the file is contrast. A theme's palette is only worth
shipping if the pane is readable on a terminal that has never heard of it, so
every ratio below is recomputed here with the WCAG formula (sRGB -> linear,
0.2126/0.7152/0.0722, (L1 + 0.05) / (L2 + 0.05)) rather than trusted from
whoever wrote the row.

Two things this file pins that a shape test would not:

  * PUBLISHED / NUDGES. Each shipped colour is either the theme's own published
    hex or that same hex moved in LIGHTNESS ONLY (hue preserved). The nudge
    table is the record of which themes needed it and by how much; see
    _nudge_comment() for the human-readable summary. Asserting this is what
    keeps "we did not invent colours" checkable rather than aspirational.

  * The Dracula parchment exception. dracula.text is #f0e6d2, a warm parchment,
    NOT Dracula's own #f8f8f2. That is the operator's explicit request, not a
    typo -- see test_dracula_text_is_the_operators_parchment.

AND ONE ROW IS NOT A THEME AT ALL. `og` is what the pane painted before themes
existed, kept as a selectable row so an operator who wants none of this can have
none of it. It is EXEMPT from every contrast assertion below, and the exemption is
asserted explicitly rather than quietly skipped -- see OG_EXEMPT and
test_og_is_the_only_row_exempt_from_contrast_and_says_why.
"""
from __future__ import annotations

import colorsys
import json
import math
import re
from pathlib import Path

import pytest

CATALOG = Path(__file__).resolve().parents[1] / "installer" / "themes.json"

ROLES = ("bg", "text", "meta", "sel_bg", "idle", "working", "rail")
HEX_RE = re.compile(r"^#[0-9a-f]{6}$")

# The literal a role carries when it means curses' `-1`: allocate no colour and
# inherit the terminal's own. Legal in ANY of the seven roles.
DEFAULT = "default"

# `og` is the row that is exempt from the contrast assertions, and the ONLY one.
# WHY IT HAS TO BE, because the next reader's first instinct will be to close this
# gap by inventing hexes for it:
#
#   * Four of its seven roles are the string "default". The point of the row is
#     that the pane keeps the TERMINAL's own background, and a background this
#     process cannot see has NO luminance. `test_body_text_is_readable_on_the_pane`
#     over "default" does not fail gently -- `_rgb` raises ValueError on the word,
#     which is a confusing error about a catalog row that is perfectly correct.
#   * Its three real hexes are the exact sRGB of the indices the code already
#     shipped (109 / 46 / 226). Their contrast against anything is a fact about
#     the TERMINAL's own palette, not about this row, and asserting a number here
#     would be asserting a property the row does not have.
#   * Substituting readable hexes for the "default" roles would make the test pass
#     by turning the regression guard into a decoration. `og` exists so that
#     `render` is byte-identical to the pre-theme output; a theme with invented
#     backgrounds would make that check fail for the one row it exists to protect.
#
# So: `og` is exempt, the exemption is a named constant, and the test below pins
# both the exemption and this reasoning. A THIRD row appearing here is a bug.
OG_EXEMPT = frozenset({"og"})

# The 25 themes, in the order the operator's reference site (ansicolor.com)
# lists them in its picker. Labels are display names, verbatim. `og` is NOT in
# this list: it is not one of the reference site's themes, it is today's colours.
EXPECTED_LABELS = [
    "Andromeda", "Ayu Dark", "Ayu Light",
    "Bearded Dark", "Bearded Light",
    "Catppuccin Frappe", "Catppuccin Latte", "Catppuccin Macchiato", "Catppuccin Mocha",
    "Dracula",
    "GitHub Dark",
    "Gruvbox",
    "Monokai Dark", "Monokai Light",
    "Nord",
    "One Dark", "One Light",
    "Palenight",
    "Panda",
    "Solarized Dark", "Solarized Light",
    "Synthwave 84",
    "Tailwind",
    "Tokyo Night", "Tokyo Night Light",
]

# Which way each theme leans. Names that end in Light/Dark must agree with
# their own suffix (tested); the rest need saying out loud, because the likeliest
# error in a 25-entry hand-built table is one palette pasted into the wrong slot.
POLARITY = {
    "Andromeda": "dark",
    "Ayu Dark": "dark", "Ayu Light": "light",
    "Bearded Dark": "dark", "Bearded Light": "light",
    "Catppuccin Frappe": "dark", "Catppuccin Latte": "light",
    "Catppuccin Macchiato": "dark", "Catppuccin Mocha": "dark",
    "Dracula": "dark",
    "GitHub Dark": "dark",
    "Gruvbox": "dark",
    "Monokai Dark": "dark", "Monokai Light": "light",
    "Nord": "dark",
    "One Dark": "dark", "One Light": "light",
    "Palenight": "dark",
    "Panda": "dark",
    "Solarized Dark": "dark", "Solarized Light": "light",
    "Synthwave 84": "dark",
    # Tailwind ships no Dark/Light sibling; the pane follows Tailwind's own
    # default dark surface (slate-900), which is also the only Tailwind pairing
    # with an actual default.
    "Tailwind": "dark",
    "Tokyo Night": "dark", "Tokyo Night Light": "light",
}

LIGHT_MIN_LUMINANCE = 0.4  # the darkest bg in the light set is Tokyo Night Light, ~0.80
DARK_MAX_LUMINANCE = 0.15   # the lightest bg in the dark set is Catppuccin Frappe, ~0.035

# Minimum sRGB-space distance (0-255, Euclidean over r/g/b) between the two
# status glyph colours. The tightest real pair here is Solarized at ~50; a
# theme shipping #a1b2c3 against #a2b3c4 to mean "idle" vs "working" lands at
# ~3 and is a bug the eye cannot catch at a glance in a spinner.
MIN_GLYPH_DISTANCE = 24.0

# HSL lightness tolerance for a nudge. Exact for saturated colours; near-neutrals
# (Gruvbox's rail #3c3836, Ayu's rail) carry an unstable hue, so 12 degrees is
# the ceiling -- far below any colour a reader would call a different colour.
MAX_NUDGE_HUE_DRIFT = 12.0

# Targets are nudged to this multiple of the threshold, not to it exactly, so a
# row never lands on the knife edge and fails on a float rounding difference.
# A nudge therefore has to be justified by a real shortfall OR by sitting inside
# this band; see test_every_nudge_was_justified_by_a_real_shortfall.
HEADROOM = 1.02

# ---------------------------------------------------------------------------
# the catalog's provenance
# ---------------------------------------------------------------------------

# Every value as the theme itself publishes it (upstream theme file or terminal
# port), before any contrast nudge. Bearded is the one row with no flat hex to
# copy: BeardedBear/bearded-theme publishes base + primary and derives its UI
# colours in helper.ts, so these are that derivation reproduced, not guesses.
PUBLISHED = {
    "andromeda": {"bg": "#1a1d23", "text": "#fefefe", "meta": "#746f77",
        "sel_bg": "#1e252c", "idle": "#96e072", "working": "#f3d56e", "rail": "#3a3f4c"},
    "ayu-dark": {"bg": "#0f1419", "text": "#e6e1cf", "meta": "#5c6773",
        "sel_bg": "#253340", "idle": "#aad94c", "working": "#ffb454", "rail": "#2d3640"},
    "ayu-light": {"bg": "#fafafa", "text": "#5c6773", "meta": "#abb0b6",
        "sel_bg": "#f0eee4", "idle": "#86b300", "working": "#eba400", "rail": "#d9d8d7"},
    "bearded-dark": {"bg": "#181a1f", "text": "#c8cbd4", "meta": "#515666",
        "sel_bg": "#34373d", "idle": "#37ae6f", "working": "#c9a022", "rail": "#0b0c0e"},
    "bearded-light": {"bg": "#f3f4f5", "text": "#0a1315", "meta": "#68909c",
        "sel_bg": "#c9e4ec", "idle": "#189433", "working": "#bb9600", "rail": "#c9ced2"},
    "catppuccin-frappe": {"bg": "#303446", "text": "#c6d0f5", "meta": "#737994",
        "sel_bg": "#414559", "idle": "#a6d189", "working": "#e5c890", "rail": "#51576d"},
    "catppuccin-latte": {"bg": "#eff1f5", "text": "#4c4f69", "meta": "#9ca0b0",
        "sel_bg": "#ccd0da", "idle": "#40a02b", "working": "#df8e1d", "rail": "#bcc0cc"},
    "catppuccin-macchiato": {"bg": "#24273a", "text": "#cad3f5", "meta": "#8087a2",
        "sel_bg": "#363a4f", "idle": "#a6da95", "working": "#eed49f", "rail": "#414559"},
    "catppuccin-mocha": {"bg": "#1e1e2e", "text": "#cdd6f4", "meta": "#7f849c",
        "sel_bg": "#313244", "idle": "#a6e3a1", "working": "#f9e2af", "rail": "#292c3c"},
    "dracula": {"bg": "#282a36", "text": "#f0e6d2", "meta": "#6272a4",
        "sel_bg": "#44475a", "idle": "#50fa7b", "working": "#f1fa8c", "rail": "#44475a"},
    "github-dark": {"bg": "#0d1117", "text": "#c9d1d9", "meta": "#8b949e",
        "sel_bg": "#163356", "idle": "#238636", "working": "#fcb32c", "rail": "#30363d"},
    "gruvbox": {"bg": "#282828", "text": "#ebdbb2", "meta": "#928374",
        "sel_bg": "#504945", "idle": "#b8bb26", "working": "#fabd2f", "rail": "#3c3836"},
    "monokai-dark": {"bg": "#272822", "text": "#fcfcfa", "meta": "#727072",
        "sel_bg": "#49483e", "idle": "#a8dc76", "working": "#ffd966", "rail": "#3e3d32"},
    "monokai-light": {"bg": "#faf4f2", "text": "#29242a", "meta": "#a59fa0",
        "sel_bg": "#eee5de", "idle": "#269d69", "working": "#cc7a0a", "rail": "#e6ded9"},
    "nord": {"bg": "#2e3440", "text": "#d8dee9", "meta": "#616e88",
        "sel_bg": "#3b4252", "idle": "#a3be8c", "working": "#ebcb8b", "rail": "#4c566a"},
    "one-dark": {"bg": "#282c34", "text": "#abb2bf", "meta": "#5c6370",
        "sel_bg": "#3e4451", "idle": "#98c379", "working": "#d19a66", "rail": "#495162"},
    "one-light": {"bg": "#fafafa", "text": "#2a2b33", "meta": "#a0a1a7",
        "sel_bg": "#d0d0d0", "idle": "#3e953a", "working": "#d2b67b", "rail": "#d4d4d4"},
    "palenight": {"bg": "#292d3e", "text": "#bfc7d5", "meta": "#697098",
        "sel_bg": "#32374c", "idle": "#a9c77d", "working": "#ffcb6b", "rail": "#676e95"},
    "panda": {"bg": "#292a2b", "text": "#e6e6e6", "meta": "#757575",
        "sel_bg": "#31353a", "idle": "#19f9d8", "working": "#ffb86c", "rail": "#3e4250"},
    "solarized-dark": {"bg": "#002b36", "text": "#93a1a1", "meta": "#586e75",
        "sel_bg": "#073642", "idle": "#859900", "working": "#b58900", "rail": "#073642"},
    "solarized-light": {"bg": "#fdf6e3", "text": "#657b83", "meta": "#93a1a1",
        "sel_bg": "#eee8d5", "idle": "#859900", "working": "#b58900", "rail": "#eee8d5"},
    "synthwave-84": {"bg": "#262335", "text": "#f2f2f2", "meta": "#848bbd",
        "sel_bg": "#34294f", "idle": "#72f1b8", "working": "#f3e70f", "rail": "#1a1a2e"},
    "tailwind": {"bg": "#0f172a", "text": "#e2e8f0", "meta": "#64748b",
        "sel_bg": "#1e293b", "idle": "#22c55e", "working": "#eab308", "rail": "#334155"},
    "tokyo-night": {"bg": "#1a1b26", "text": "#a9b1d6", "meta": "#565f89",
        "sel_bg": "#33467c", "idle": "#9ece6a", "working": "#e0af68", "rail": "#1e202e"},
    "tokyo-night-light": {"bg": "#e6e7ed", "text": "#343b59", "meta": "#848cb5",
        "sel_bg": "#dcdee3", "idle": "#385f0d", "working": "#8f5e15", "rail": "#c1c2c7"},
}

# Themes whose published colour could not clear its contrast target and were
# nudged in HSL lightness only, by the least amount that clears it. The comment
# on each line is that amount (HSL lightness delta).
#
# Nobody had to move a `text` except three: One Dark, Tokyo Night and
# Solarized Light, all by <= 0.06 -- their foregrounds were close. The rest of
# the movement is in `meta` (comment colours are deliberately dim by design and
# most sat under 3.0) and `rail` (structural greys that were effectively
# invisible against their own background).
NUDGES = {
    "ayu-dark": {
        "rail": ("#2d3640", "#2e3741"),  # L +0.004
    },
    "ayu-light": {
        "meta": ("#abb0b6", "#899099"),  # L -0.124
        "idle": ("#86b300", "#749c00"),  # L -0.045
        "working": ("#eba400", "#bf8500"),  # L -0.086
        "rail": ("#d9d8d7", "#cecccb"),  # L -0.045
    },
    "bearded-dark": {
        "meta": ("#515666", "#61677a"),  # L +0.071
        "rail": ("#0b0c0e", "#363b44"),  # L +0.190
    },
    "bearded-light": {
        "working": ("#bb9600", "#aa8800"),  # L -0.033
        "rail": ("#c9ced2", "#c3c8cd"),  # L -0.022
    },
    "catppuccin-frappe": {
        "meta": ("#737994", "#787e98"),  # L +0.018
    },
    "catppuccin-latte": {
        "meta": ("#9ca0b0", "#84899d"),  # L -0.084
        "idle": ("#40a02b", "#3f9d2a"),  # L -0.008
        "working": ("#df8e1d", "#c07a19"),  # L -0.069
    },
    "catppuccin-mocha": {
        "rail": ("#292c3c", "#393d53"),  # L +0.076
    },
    "gruvbox": {
        "rail": ("#3c3836", "#494442"),  # L +0.049
    },
    "monokai-dark": {
        "meta": ("#727072", "#737173"),  # L +0.004
        "rail": ("#3e3d32", "#464538"),  # L +0.027
    },
    "monokai-light": {
        "meta": ("#a59fa0", "#928b8c"),  # L -0.076
        "working": ("#cc7a0a", "#cb790a"),  # L -0.002
        "rail": ("#e6ded9", "#d3c5bd"),  # L -0.092
    },
    "nord": {
        "meta": ("#616e88", "#717f9a"),  # L +0.067
    },
    "one-dark": {
        "text": ("#abb2bf", "#acb3bf"),  # L +0.002
        "meta": ("#5c6370", "#6e7686"),  # L +0.078
    },
    "one-light": {
        "meta": ("#a0a1a7", "#8e8f96"),  # L -0.069
        "working": ("#d2b67b", "#af8a3c"),  # L -0.192
        "rail": ("#d4d4d4", "#cccccc"),  # L -0.031
    },
    "palenight": {
        "meta": ("#697098", "#6f769c"),  # L +0.020
    },
    "panda": {
        "rail": ("#3e4250", "#424655"),  # L +0.018
    },
    "solarized-dark": {
        "meta": ("#586e75", "#5d757c"),  # L +0.024
        "rail": ("#073642", "#0a4a5b"),  # L +0.055
    },
    "solarized-light": {
        "text": ("#657b83", "#586a72"),  # L -0.059
        "meta": ("#93a1a1", "#809090"),  # L -0.071
        "idle": ("#859900", "#829600"),  # L -0.006
        "working": ("#b58900", "#b28700"),  # L -0.006
        "rail": ("#eee8d5", "#d7c99c"),  # L -0.157
    },
    "synthwave-84": {
        "rail": ("#1a1a2e", "#3e3e6d"),  # L +0.194
    },
    "tokyo-night": {
        "text": ("#a9b1d6", "#afb7d9"),  # L +0.018
        "meta": ("#565f89", "#5c6692"),  # L +0.029
        "rail": ("#1e202e", "#363a53"),  # L +0.120
    },
    "tokyo-night-light": {
        "meta": ("#848cb5", "#7881ae"),  # L -0.037
        "rail": ("#c1c2c7", "#bbbcc2"),  # L -0.022
    },
}

# What each role has to beat, and against what.
TEXT_TARGET = 4.5
SUBTLE_TARGET = 3.0   # meta, and both status glyphs
RAIL_TARGET = 1.5     # structural: a rail should be seen, not read


# ---------------------------------------------------------------------------
# colour maths (the whole file is these three functions)
# ---------------------------------------------------------------------------

def _rgb(value: str) -> tuple[float, float, float]:
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb)


def _luminance(value: str) -> float:
    """WCAG relative luminance: sRGB -> linear, then 0.2126/0.7152/0.0722."""
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(value)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _hsl(value: str) -> tuple[float, float, float]:
    return colorsys.rgb_to_hls(*_rgb(value))


def _distance(a: str, b: str) -> float:
    return math.dist(
        tuple(round(c * 255) for c in _rgb(a)),
        tuple(round(c * 255) for c in _rgb(b)),
    )


def _hue_drift(a: str, b: str) -> float:
    ha = _hsl(a)[0] * 360
    hb = _hsl(b)[0] * 360
    return min(abs(ha - hb), 360 - abs(ha - hb))


@pytest.fixture(scope="module")
def catalog() -> dict:
    assert CATALOG.is_file(), f"missing {CATALOG}"
    return json.loads(CATALOG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def themes(catalog) -> dict:
    return catalog["themes"]


def _slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def _rows(themes: dict):
    """(slug, label, colors) for every MEASURABLE row, in the catalog's own order.

    `og` is dropped here and only here. Every contrast, polarity and distance
    assertion in this file iterates `_rows`, so the exemption is stated in ONE
    place and cannot drift into a per-test `if slug == "og"` that the next edit
    forgets to extend.
    """
    return [(slug, t["label"], t["colors"]) for slug, t in themes.items()
            if slug not in OG_EXEMPT]


def _nudge_comment() -> str:
    lines = []
    for slug, moved in NUDGES.items():
        deltas = ", ".join(
            f"{role} {_hsl(b)[1] - _hsl(a)[1]:+.3f}" for role, (a, b) in moved.items()
        )
        lines.append(f"    {slug:20} {deltas}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------

def test_version_is_one(catalog):
    assert catalog["version"] == 1


def test_exactly_26_themes(themes):
    assert len(themes) == 26, (
        f"expected 26 themes (25 from the reference site plus `og`), "
        f"found {len(themes)}: {sorted(themes)}")


def test_labels_are_the_reference_set_plus_og(themes):
    labels = [t["label"] for t in themes.values()]
    assert sorted(labels) == sorted(EXPECTED_LABELS + ["OG"])
    assert len(labels) == len(set(labels)), "duplicate label"
    # The 25 keep the reference site's picker order; `og` sits at the front
    # because order 0 puts it first in the picker and dracula is still the
    # default. `og` is not one of the reference site's themes, so it is checked
    # separately rather than folded into EXPECTED_LABELS.
    ordered = [themes[slug]["label"] for slug, _ in
               sorted(themes.items(), key=lambda kv: kv[1]["order"])]
    assert ordered == ["OG"] + EXPECTED_LABELS


def test_order_is_a_permutation_of_0_to_25(themes):
    orders = sorted(t["order"] for t in themes.values())
    assert orders == list(range(0, 26))


def test_og_is_the_only_row_exempt_from_contrast_and_says_why(themes):
    """THE EXEMPTION, ASSERTED RATHER THAN ASSUMED.

    A contrast test over `"default"` does not fail politely -- `_rgb` raises
    ValueError on the word -- and an exemption written as `if slug == "og"` in
    twelve places is an exemption the twelfth place will forget. So the exemption
    lives in `OG_EXEMPT`, every measurable row goes through `_rows`, and this test
    pins BOTH: that `og` really is the only exempt row, and that the reason is
    written down where the next reader will hit it.
    """
    assert OG_EXEMPT == {"og"}, "a second exempt row needs its own justification"
    assert "og" in themes, "the regression guard for the whole change is missing"
    # Every OTHER row is measured, i.e. carries real hexes throughout.
    for slug, _label, colors in _rows(themes):
        assert DEFAULT not in colors.values(), (
            f"{slug} carries a 'default' role but is not exempt. Either add it to "
            "OG_EXEMPT with a reason, or give it a real colour.")

    og = themes["og"]
    assert og["label"] == "OG" and og["order"] == 0
    assert og["colors"] == {
        "bg": "default", "text": "default", "meta": "#87afaf",
        "sel_bg": "default", "idle": "#00ff00", "working": "#ffff00",
        "rail": "default",
    }
    # The four transparent roles are what make the row worth having, so pin them
    # by name rather than by the dict above alone: a future edit that fills in
    # `bg` has stopped `og` being the way back to a transparent pane.
    assert og["colors"]["bg"] == DEFAULT
    note = og.get("_note", "")
    for phrase in ("today", "default", "exempt"):
        assert phrase in note.lower(), f"the _note does not explain {phrase!r}: {note!r}"


def test_slug_is_the_kebab_case_of_the_label(themes):
    for slug, label, _ in _rows(themes):
        assert slug == _slugify(label), f"{slug!r} is not the slug of {label!r}"
    assert "tokyo-night-light" in themes and "catppuccin-frappe" in themes
    assert "synthwave-84" in themes and "github-dark" in themes
    # `og` is the exception to the kebab-case rule and says so: "OG" slugifies to
    # "og" by accident, not by derivation, and it is spelled lower-case because it
    # is a slug and not a label.
    assert themes["og"]["label"] == "OG" and "og" in themes


def test_every_row_defines_all_seven_roles(themes):
    """Seven roles on every row -- and a role is EITHER `#rrggbb` or `"default"`.

    The lowercase-hex half of this used to be the whole rule. `"default"` is now
    legal in ANY of the seven roles, which is the single structural decision that
    lets one theme reproduce exactly what the pane painted before themes existed;
    so the shape test accepts both and the CONTRAST tests are the ones that
    decide which rows may use `"default"` at all (they may not, except `og` --
    see OG_EXEMPT).
    """
    for slug, theme in themes.items():
        colors = theme["colors"]
        assert set(colors) == set(ROLES), f"{slug}: roles {sorted(colors)}"
        for role, value in colors.items():
            assert isinstance(value, str), f"{slug}.{role} is not a string"
            if value == DEFAULT:
                continue
            assert HEX_RE.match(value), (
                f"{slug}.{role} = {value!r} is neither lowercase #rrggbb "
                "(no shorthand, no alpha, no uppercase) nor \"default\""
            )


# ---------------------------------------------------------------------------
# contrast
# ---------------------------------------------------------------------------

def test_body_text_is_readable_on_the_pane(themes):
    """The whole point of the change: the pane must work on any terminal."""
    for slug, label, colors in _rows(themes):
        ratio = _contrast(colors["text"], colors["bg"])
        assert ratio >= TEXT_TARGET, (
            f"{label}: text {colors['text']} on bg {colors['bg']} is {ratio:.2f}:1, "
            f"needs {TEXT_TARGET}:1"
        )


def test_body_text_is_readable_on_the_selected_row(themes):
    """Easy to miss: the selected row is where the eye actually is."""
    for slug, label, colors in _rows(themes):
        ratio = _contrast(colors["text"], colors["sel_bg"])
        assert ratio >= TEXT_TARGET, (
            f"{label}: text {colors['text']} on selection {colors['sel_bg']} is "
            f"{ratio:.2f}:1, needs {TEXT_TARGET}:1"
        )


@pytest.mark.parametrize("role", ["meta", "idle", "working"])
def test_secondary_text_and_status_glyphs_are_readable(themes, role):
    for slug, label, colors in _rows(themes):
        ratio = _contrast(colors[role], colors["bg"])
        assert ratio >= SUBTLE_TARGET, (
            f"{label}: {role} {colors[role]} on bg {colors['bg']} is {ratio:.2f}:1, "
            f"needs {SUBTLE_TARGET}:1"
        )


def test_rails_are_visible_without_being_read(themes):
    for slug, label, colors in _rows(themes):
        ratio = _contrast(colors["rail"], colors["bg"])
        assert ratio >= RAIL_TARGET, (
            f"{label}: rail {colors['rail']} on bg {colors['bg']} is {ratio:.2f}:1, "
            f"needs {RAIL_TARGET}:1 (a rail you cannot see is not a rail)"
        )


def test_status_glyphs_are_tellable_apart(themes):
    for slug, label, colors in _rows(themes):
        distance = _distance(colors["idle"], colors["working"])
        assert distance >= MIN_GLYPH_DISTANCE, (
            f"{label}: idle {colors['idle']} and working {colors['working']} are "
            f"{distance:.1f}/255 apart, need {MIN_GLYPH_DISTANCE:.0f} -- a check mark "
            "and a spinner in the same colour is not a status indicator"
        )


# ---------------------------------------------------------------------------
# the likeliest mistake in a hand-built table: a palette in the wrong slot
# ---------------------------------------------------------------------------

def test_light_themes_are_light_and_dark_themes_are_dark(themes):
    for slug, label, colors in _rows(themes):
        assert label in POLARITY, f"{label} has no declared polarity"
        luminance = _luminance(colors["bg"])
        if POLARITY[label] == "light":
            assert luminance >= LIGHT_MIN_LUMINANCE, (
                f"{label} is a light theme but its bg {colors['bg']} has luminance "
                f"{luminance:.3f} -- looks like a dark palette pasted in"
            )
        else:
            assert luminance <= DARK_MAX_LUMINANCE, (
                f"{label} is a dark theme but its bg {colors['bg']} has luminance "
                f"{luminance:.3f} -- looks like a light palette pasted in"
            )


def test_polarity_agrees_with_the_light_dark_suffix(themes):
    for slug, label, _ in _rows(themes):
        if label.endswith(" Light"):
            assert POLARITY[label] == "light", f"{label} ends in Light but is declared dark"
        elif label.endswith(" Dark"):
            assert POLARITY[label] == "dark", f"{label} ends in Dark but is declared light"
        elif "Light" in label or "Dark" in label:
            pytest.fail(f"{label} mixes a Light/Dark word into its name -- rename it")


# ---------------------------------------------------------------------------
# provenance: published palette, or the same palette in a different lightness
# ---------------------------------------------------------------------------

def test_every_shipped_colour_is_published_or_a_hue_preserving_lightness_nudge(themes):
    assert set(themes) - OG_EXEMPT == set(PUBLISHED), (
        "a theme has no PUBLISHED provenance")
    for slug, label, colors in _rows(themes):
        moved = NUDGES.get(slug, {})
        assert set(moved) <= set(ROLES)
        for role in ROLES:
            published = PUBLISHED[slug][role]
            if role not in moved:
                assert colors[role] == published, (
                    f"{label}.{role} is {colors[role]}, not the published {published} "
                    "and not a recorded nudge"
                )
                continue
            was, now = moved[role]
            assert was == published, (
                f"{label}.{role} nudge records {was} as the published value, "
                f"but PUBLISHED says {published}"
            )
            assert colors[role] == now, f"{label}.{role} is {colors[role]}, nudge says {now}"
            assert _hue_drift(was, now) <= MAX_NUDGE_HUE_DRIFT, (
                f"{label}.{role} moved {_hue_drift(was, now):.0f} degrees of hue "
                f"({was} -> {now}); a nudge may change lightness only"
            )


def test_every_nudge_was_justified_by_a_real_shortfall(themes):
    """A nudge is a fix for a failing number, never a quiet restyling."""
    targets = {
        "text": (TEXT_TARGET, "bg"),
        "meta": (SUBTLE_TARGET, "bg"),
        "idle": (SUBTLE_TARGET, "bg"),
        "working": (SUBTLE_TARGET, "bg"),
        "rail": (RAIL_TARGET, "bg"),
    }
    for slug, moved in NUDGES.items():
        colors = themes[slug]["colors"]
        for role, (was, now) in moved.items():
            target, against = targets[role]
            if role == "text":
                # text has to hold on the pane AND on the selected row; a nudge
                # is justified by whichever of the two it failed.
                shorts = [b for b in ("bg", "sel_bg") if _contrast(was, colors[b]) < target * HEADROOM]
                assert shorts, (
                    f"{themes[slug]['label']}.text was nudged {was} -> {now} but the "
                    f"published colour already cleared {target}:1 against both the "
                    "pane and the selected row"
                )
                for b in shorts:
                    assert _contrast(now, colors[b]) >= target * HEADROOM
            else:
                before = _contrast(was, colors[against])
                assert before < target * HEADROOM, (
                    f"{themes[slug]['label']}.{role} was nudged {was} -> {now} but the "
                    f"published colour already cleared {target}:1 against {against} "
                    f"({before:.2f}:1) with headroom to spare"
                )
                assert _contrast(now, colors[against]) >= target * HEADROOM
            moved_away_from_bg = _luminance(colors["bg"]) < 0.5
            assert (_hsl(now)[1] > _hsl(was)[1]) == moved_away_from_bg, (
                f"{themes[slug]['label']}.{role} moved toward its background"
            )


def test_nudge_table_covers_every_shipped_palette():
    """Guard the guard: a new theme added to the catalog must arrive with its
    provenance here, or test_every_shipped_colour_is_published_or_a_hue_preserving
    _lightness_nudge starts failing on the new row for the right reason."""
    summary = _nudge_comment()
    assert len(NUDGES) == 20, f"nudged-theme count changed; re-read the table:\n{summary}"


# ---------------------------------------------------------------------------
# the one deliberate departure from a theme's own palette
# ---------------------------------------------------------------------------

def test_dracula_text_is_the_operators_parchment(themes):
    """dracula.text is #f0e6d2, NOT Dracula's own #f8f8f2. Do not "fix" it.

    The operator asked for a warm parchment as the dominant text colour and
    dracula is the default theme, so the default pane reads parchment. Every
    other theme uses its own published foreground; this one value is the
    operator's call and is recorded in the catalog's `_note`.
    """
    dracula = themes["dracula"]
    assert dracula["colors"]["text"] == "#f0e6d2"
    assert dracula["colors"]["text"] != "#f8f8f2", "Dracula's Parchment leaked back in"
    note = dracula.get("_note", "")
    assert "#f0e6d2" in note and "parchment" in note.lower(), (
        f"the _note explaining the parchment exception is missing or vague: {note!r}"
    )
    # And it is only ever a *text* exception -- Dracula's other six roles are
    # upstream Dracula, which is what makes the pane still read as Dracula.
    assert dracula["colors"]["bg"] == "#282a36"
    assert dracula["colors"]["sel_bg"] == "#44475a"
    assert dracula["colors"]["idle"] == "#50fa7b"
    assert dracula["colors"]["working"] == "#f1fa8c"