"""Contract tests for installer/themes.json -- the `og agents` pane colour catalog.

Pure stdlib + pytest and no import from this repo: the catalog is DATA (the same
deal as installer/registry.json), so it is loaded by path and judged on its own
terms. The point of the file is contrast. A theme's palette is only worth
shipping if the pane is readable on a terminal that has never heard of it, so
every ratio below is recomputed here with the WCAG formula (sRGB -> linear,
0.2126/0.7152/0.0722, (L1 + 0.05) / (L2 + 0.05)) rather than trusted from
whoever wrote the row.

  THE ONE EXCEPTION TO "NO IMPORT FROM THIS REPO", AND WHY IT IS NOT ONE. The
  floors below are re-run against the RESOLVED 256-colour CELLS, which means
  asking what the theme hexes actually PAINT, and the honest way to ask is to run
  the shipped converter (installer/og_themes.py) rather than a second
  implementation of it copied in here. A copy would agree with itself and catch
  nothing: the defect this section exists for was invisible to a test that only
  ever looked at the hex, and a reimplementation of the converter would be the
  same test wearing a costume. `import og_themes` is therefore added BELOW the
  tables, after everything that must stay self-contained has been read.

Three things this file pins that a shape test would not:

  * PUBLISHED / NUDGES. Each shipped colour is either the theme's own published
    hex or that same hex moved in LIGHTNESS ONLY (hue preserved). The nudge
    table is the record of which themes needed it and by how much; see
    _nudge_comment() for the human-readable summary. Asserting this is what
    keeps "we did not invent colours" checkable rather than aspirational.
    A nudge marked `post-conversion` was needed by the SECOND run of the floors
    -- the one that measures the resolved cells -- and POST_CONVERSION names them.

  * NO ROLE COLLAPSES TO NEUTRAL. What a theme hex converts to is half the spec;
    what it converts to is the rest of it. See
    test_no_role_collapses_from_chromatic_to_a_neutral_cell.

  * The Dracula parchment exception. dracula.text is #ffd7af, ansi 223 exactly,
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

# The shipped converter, for the resolved-cell assertions only. See the module
# docstring: the hex-side floors above stay self-contained, and the cell-side
# floors cannot be, because re-implementing the converter would test nothing.
import og_themes as th

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
    "dracula": {"bg": "#282a36", "text": "#ffd7af", "meta": "#6272a4",
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
# THE TWO ROUNDS. A line marked `post-conversion` was moved by the SECOND run of
# the floors -- the one that measures the RESOLVED 256-colour CELLS rather than
# the declared hexes. Every one of them passed on paper and failed on the
# terminal, which is the whole point: the hex is half the spec and the cell it
# converts to is the other half, and the hex alone is not what anyone looks at.
# A colour can clear 3.07:1 on paper and paint at 1.99:1, because the cell it
# lands on is a different colour.
#
# THE MOVEMENT IS MOSTLY `meta` and `rail`, for two reasons that happen to agree:
# comment colours are dim by design and most sat under 3.0, and structural greys
# were effectively invisible against their own background. `bg` moves once
# (Catppuccin Frappe, +0.096) and `sel_bg` twice (Nord, Tailwind), because the
# converter put both of them on the same cell as the pane in three themes and a
# selected row you cannot see is not a selected row.
#
# `text` moved in six themes and the cell run is why three of them exist at all.
# Solarized Dark is the worst case at +0.184: its #93a1a1 has so little chroma
# (0.016, below the threshold) that it is a neutral, it resolves to ramp cell
# 247, and on a 256 terminal that is 2.80:1 against its own selection cell --
# so the only direction available to a dark theme is up, and the text ends up on
# cell 252. It is a visible departure from Solarized's published foreground and it
# is recorded here rather than hidden: the alternative was shipping a body text
# that does not clear its floor.
NUDGES = {
    "ayu-dark": {
        "rail": ("#2d3640", "#2e3741"),  # L +0.004
    },
    "ayu-light": {
        "idle": ("#86b300", "#729900"),  # L -0.051, post-conversion
        "meta": ("#abb0b6", "#899099"),  # L -0.124
        "rail": ("#d9d8d7", "#cecccb"),  # L -0.045
        "working": ("#eba400", "#bf8500"),  # L -0.086
    },
    "bearded-dark": {
        "meta": ("#515666", "#6c7388"),  # L +0.120, post-conversion
        "rail": ("#0b0c0e", "#3b404a"),  # L +0.212, post-conversion
    },
    "bearded-light": {
        "rail": ("#c9ced2", "#bcc2c7"),  # L -0.047, post-conversion
        "working": ("#bb9600", "#9b7c00"),  # L -0.063, post-conversion
    },
    "catppuccin-frappe": {
        "bg": ("#303446", "#444963"),  # L +0.096, post-conversion
        "meta": ("#737994", "#a5a9ba"),  # L +0.173, post-conversion
        "rail": ("#51576d", "#6c7492"),  # L +0.125, post-conversion
    },
    "catppuccin-latte": {
        "idle": ("#40a02b", "#3e9b29"),  # L -0.014, post-conversion
        "meta": ("#9ca0b0", "#777d93"),  # L -0.129, post-conversion
        "text": ("#4c4f69", "#484b63"),  # L -0.020, post-conversion
        "working": ("#df8e1d", "#b57318"),  # L -0.092, post-conversion
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
        "idle": ("#269d69", "#259966"),  # L -0.010, post-conversion
        "meta": ("#a59fa0", "#8d8687"),  # L -0.096, post-conversion
        "rail": ("#e6ded9", "#d2c4bb"),  # L -0.098, post-conversion
        "working": ("#cc7a0a", "#c4750a"),  # L -0.016, post-conversion
    },
    "nord": {
        "meta": ("#616e88", "#909bb0"),  # L +0.171, post-conversion
        "rail": ("#4c566a", "#67748f"),  # L +0.125, post-conversion
        "sel_bg": ("#3b4252", "#4a5367"),  # L +0.071, post-conversion
        "text": ("#d8dee9", "#dbe1eb"),  # L +0.010, post-conversion
    },
    "one-dark": {
        "meta": ("#5c6370", "#6e7686"),  # L +0.078
        "text": ("#abb2bf", "#c7ccd4"),  # L +0.096, post-conversion
    },
    "one-light": {
        "meta": ("#a0a1a7", "#8e8f96"),  # L -0.069
        "rail": ("#d4d4d4", "#cccccc"),  # L -0.031
        "working": ("#d2b67b", "#af8a3c"),  # L -0.192
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
        "text": ("#93a1a1", "#c5cdcd"),  # L +0.184, post-conversion
    },
    "solarized-light": {
        "idle": ("#859900", "#829600"),  # L -0.006
        "meta": ("#93a1a1", "#809090"),  # L -0.071
        "rail": ("#eee8d5", "#d3c493"),  # L -0.182, post-conversion
        "text": ("#657b83", "#586a72"),  # L -0.059
        "working": ("#b58900", "#b28700"),  # L -0.006
    },
    "synthwave-84": {
        "rail": ("#1a1a2e", "#3e3e6d"),  # L +0.194
    },
    "tokyo-night": {
        "meta": ("#565f89", "#6872a0"),  # L +0.080, post-conversion
        "rail": ("#1e202e", "#363a53"),  # L +0.120
        "text": ("#a9b1d6", "#bfc5e0"),  # L +0.063, post-conversion
    },
    "tokyo-night-light": {
        "meta": ("#848cb5", "#6671a3"),  # L -0.094, post-conversion
        "rail": ("#c1c2c7", "#b4b5bc"),  # L -0.047, post-conversion
    },
    "tailwind": {
        "sel_bg": ("#1e293b", "#253249"),  # L +0.041, post-conversion
    },
}

POST_CONVERSION = {
    "ayu-light.idle",
    "bearded-dark.meta",
    "bearded-dark.rail",
    "bearded-light.rail",
    "bearded-light.working",
    "catppuccin-frappe.bg",
    "catppuccin-frappe.meta",
    "catppuccin-frappe.rail",
    "catppuccin-latte.idle",
    "catppuccin-latte.meta",
    "catppuccin-latte.text",
    "catppuccin-latte.working",
    "monokai-light.idle",
    "monokai-light.meta",
    "monokai-light.rail",
    "monokai-light.working",
    "nord.meta",
    "nord.rail",
    "nord.sel_bg",
    "nord.text",
    "one-dark.text",
    "solarized-dark.text",
    "solarized-light.rail",
    "tailwind.sel_bg",
    "tokyo-night-light.meta",
    "tokyo-night-light.rail",
    "tokyo-night.meta",
    "tokyo-night.text",
}


# What each role has to beat, and against what.
TEXT_TARGET = 4.5
SUBTLE_TARGET = 3.0   # meta, and both status glyphs
RAIL_TARGET = 1.5     # structural: a rail should be seen, not read

# THE SECOND SET OF FLOORS, AND WHAT THEY ARE FOR. Every constant below is the
# one above, re-used verbatim: the cell assertions do not get their own, easier
# numbers. A floor that only exists for the hexes is a floor a theme can pass on
# paper and fail on the terminal, and that is precisely how 24 role-colours got
# through the first version of this file.
CELL_FLOORS = {
    ("text", "bg"): TEXT_TARGET,
    ("text", "sel_bg"): TEXT_TARGET,
    ("meta", "bg"): SUBTLE_TARGET,
    ("idle", "bg"): SUBTLE_TARGET,
    ("working", "bg"): SUBTLE_TARGET,
    ("rail", "bg"): RAIL_TARGET,
}
CELL_FLOOR_CASES = [
    (role, against, target)
    for (role, against), target in CELL_FLOORS.items()
]


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


def _cell(value: str) -> str:
    """What `value` PAINTS on a 256-colour terminal: the converter's cell, as hex.

    The whole point of the section below. A theme hex is a request; this is the
    answer the terminal gives, and the two are different colours often enough
    that judging the catalog on the first one alone judges it on something no
    operator will ever look at.
    """
    return th.rgb_to_hex(th.ansi256_rgb(th.hex_to_ansi256(value)))


def _index(value: str) -> int:
    return th.hex_to_ansi256(value)


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
# THE SAME FLOORS, ON WHAT ACTUALLY PAINTS
# ---------------------------------------------------------------------------
#
# Everything above measures the DECLARED hex. On the terminal this shipped
# against -- `TERM=tmux-256color`, so `curses.COLORS` is 256 even though
# `COLORTERM=truecolor` is exported, and `infocmp` reports no `ccc`, so
# `init_color` is not available either -- what a role paints is one of the 240
# existing palette cells and NOT its hex. That makes the two sets of numbers
# different questions, and only the second one is about the pane.
#
# The gap is not subtle. Before the converter refused to desaturate, dracula's
# parchment #f0e6d2 painted as 254 (#e4e4e4, a pure grey) -- 96 degrees of hue
# away from the colour that was asked for -- and 23 of the 25 catalog themes lost
# at least one role the same way. Twenty-four role-colours also cleared every
# floor above and still painted below it: Catppuccin Frappe's meta was 3.07:1 on
# paper and 1.99:1 on the cell, because #005f5f (its converted background) is a
# different colour from #303446.
#
# Every constant here is the one used above. The floors did not move; the
# colours did, in LIGHTNESS ONLY, and each such move is recorded in NUDGES and
# named in POST_CONVERSION.

@pytest.mark.parametrize("role,against,target", CELL_FLOOR_CASES)
def test_every_floor_holds_on_the_resolved_cell_not_only_on_the_hex(themes,
                                                                   role, against,
                                                                   target):
    for slug, label, colors in _rows(themes):
        fg, bg = _cell(colors[role]), _cell(colors[against])
        ratio = _contrast(fg, bg)
        assert ratio >= target, (
            f"{label}: {role} {colors[role]} paints as {fg} on {against} "
            f"{colors[against]} painting as {bg}: {ratio:.2f}:1, needs {target}:1. "
            "The hex passes; the cell does not. Nudge that hex in LIGHTNESS ONLY "
            "and record it in NUDGES as post-conversion -- do NOT relax the floor."
        )


def test_the_selected_row_is_still_a_different_colour_from_the_pane(themes):
    """The one invariant the hex-level tests could see but not judge.

    `og_agents` already asserts that `sel_bg != bg` as hexes, and until the
    converter started refusing neutral cells that assertion was ENOUGH: the two
    roles were far enough apart to land on far enough apart. They are not any
    more. Catppuccin Frappe's #303446 and #414559, Nord's #2e3440 and #3b4252,
    and Tailwind's #0f172a and #1e293b each resolved to the SAME cell -- so the
    selected row would have been invisible, in three themes, on every terminal
    that does not claim truecolor. A hex test cannot see that and this one can.
    """
    for slug, label, colors in _rows(themes):
        pane, selected = _index(colors["bg"]), _index(colors["sel_bg"])
        assert pane != selected, (
            f"{label}: bg {colors['bg']} and sel_bg {colors['sel_bg']} both paint "
            f"as cell {pane} -- the selected row would be invisible"
        )


def test_no_role_collapses_from_chromatic_to_a_neutral_cell(themes):
    """THE SURVEY, FROZEN. 52 role-colours across 23 themes used to lose their
    chroma to the greyscale ramp, and nothing caught it because every contrast
    assertion in this file looked at the hex.

    A colour that carries chroma is a colour somebody recognised, and the ramp
    cannot carry it: nord's background #2e3440 painted as 236 (#303030, grey),
    solarized-dark's #002b36 as 235, dracula's as 235, synthwave-84's rail
    #3e3e6d as 238. The whole Catppuccin lavender family, every Nord frost, the
    entire feature -- neutralised on the one terminal it shipped to, with a green
    test suite.

    The threshold is the catalog's own: below `th.NEUTRAL_CHROMA` a target really
    IS neutral and the ramp is the correct answer, and the darkest neutral role
    here sits at 0.019 while the lightest chromatic one sits at 0.021. Nothing
    lives in the gap, which is why the cut can be one number rather than a
    judgement call.
    """
    collapses = []
    for slug, label, colors in _rows(themes):
        for role, value in colors.items():
            target = th.oklab_hue(th.hex_to_rgb(value))
            cell = th.oklab_hue(th.ansi256_rgb(_index(value)))
            if target[1] > th.NEUTRAL_CHROMA and cell[1] < th.NEUTRAL_CHROMA:
                collapses.append(
                    f"{label}.{role} {value} (chroma {target[1]:.3f}) -> cell "
                    f"{_index(value)} {th.rgb_to_hex(th.ansi256_rgb(_index(value)))}")
    assert not collapses, (
        f"{len(collapses)} role-colours lost their chroma to a grey cell:\n  "
        + "\n  ".join(collapses)
    )


def test_a_genuinely_neutral_role_still_lands_on_the_greyscale_ramp(themes):
    """THE OTHER HALF, so the guard cannot be "fixed" by deleting itself.

    A role with no chroma to preserve must still resolve to the ramp: these are
    the colours that WERE grey on purpose, and pushing them onto the cube's
    saturated diagonal would be the same defect wearing the other shoe. This is
    also why the guard is a threshold and not a blanket ban on the ramp.
    """
    for slug, label, colors in _rows(themes):
        for role, value in colors.items():
            if th.oklab_hue(th.hex_to_rgb(value))[1] > th.NEUTRAL_CHROMA:
                continue
            rgb = th.ansi256_rgb(_index(value))
            assert rgb[0] == rgb[1] == rgb[2], (
                f"{label}.{role} {value} has no chroma but painted as "
                f"{th.rgb_to_hex(rgb)}, which has some"
            )
    # And at least a few really are neutral, or the loop above proves nothing.
    neutral = sum(
        1 for _slug, _label, colors in _rows(themes) for value in colors.values()
        if th.oklab_hue(th.hex_to_rgb(value))[1] <= th.NEUTRAL_CHROMA)
    assert neutral > 40, neutral


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
    """A nudge is a fix for a failing number, never a quiet restyling.

    TWO NUMBER SYSTEMS COUNT. A nudge is justified by whichever floor it was made
    for: the DECLARED hex, or the CELL that hex converts to. Before the converter
    refused to desaturate, 28 roles cleared every hex floor and painted under the
    same floor on a 256-colour terminal -- Catppuccin Frappe's meta at 1.99:1 on
    cell #5f87af against cell #005f5f, having been 3.07:1 on paper. Judging those
    on the hex alone would have called all 28 unjustified restyling and sent the
    next person to "revert" them.

    `POST_CONVERSION` names the roles that were moved for the cells, and this
    test is what keeps that list honest in both directions: an entry there with
    no cell shortfall fails, and a cell shortfall with no entry there fails too.
    """
    targets = {
        "text": (TEXT_TARGET, "bg"),
        "meta": (SUBTLE_TARGET, "bg"),
        "idle": (SUBTLE_TARGET, "bg"),
        "working": (SUBTLE_TARGET, "bg"),
        "rail": (RAIL_TARGET, "bg"),
        "sel_bg": None,      # measured against `text`, never against `bg`
        "bg": None,          # the surface the other five are measured on
    }
    for slug, moved in NUDGES.items():
        colors = themes[slug]["colors"]
        for role, (was, now) in moved.items():
            named = f"{slug}.{role}" in POST_CONVERSION
            if targets[role] is None:
                assert named, (
                    f"{slug}.{role} was nudged {was} -> {now}; only `text` and `bg` "
                    "have a floor against `bg`, so this one needs its own reason"
                )
                continue
            target, against = targets[role]
            shorts_hex, shorts_cell = [], []
            if role == "text":
                # text has to hold on the pane AND on the selected row; a nudge
                # is justified by whichever of the two it failed.
                for b in ("bg", "sel_bg"):
                    if _contrast(was, colors[b]) < target * HEADROOM:
                        shorts_hex.append(b)
                    if _contrast(_cell(was), _cell(colors[b])) < target * HEADROOM:
                        shorts_cell.append(b)
            else:
                if _contrast(was, colors[against]) < target * HEADROOM:
                    shorts_hex.append(against)
                if _contrast(_cell(was), _cell(colors[against])) < target * HEADROOM:
                    shorts_cell.append(against)
            assert shorts_hex or (named and shorts_cell), (
                f"{themes[slug]['label']}.{role} was nudged {was} -> {now} but the "
                f"published colour already cleared {target}:1 as a hex and as a "
                "cell -- it is a restyling, not a fix"
            )
            assert not named or shorts_cell, (
                f"{slug}.{role} is marked post-conversion but {was} cleared "
                f"{target}:1 on the cell too; drop it from POST_CONVERSION"
            )
            for b in shorts_hex:
                assert _contrast(now, colors[b]) >= target * HEADROOM
            if named:
                # Headroom on the CELLS only where the cell is the reason. A
                # hex-basis nudge is measured on the hex; the cell floor itself is
                # already enforced for every role, with no headroom, by
                # test_every_floor_holds_on_the_resolved_cell_not_only_on_the_hex.
                for b in shorts_cell:
                    assert _contrast(_cell(now), _cell(colors[b])) >= target * HEADROOM
            moved_away_from_bg = _luminance(colors["bg"]) < 0.5
            assert (_hsl(now)[1] > _hsl(was)[1]) == moved_away_from_bg, (
                f"{themes[slug]['label']}.{role} moved toward its background"
            )


def test_nudge_table_covers_every_shipped_palette():
    """Guard the guard: a new theme added to the catalog must arrive with its
    provenance here, or test_every_shipped_colour_is_published_or_a_hue_preserving
    _lightness_nudge starts failing on the new row for the right reason."""
    summary = _nudge_comment()
    assert len(NUDGES) == 21, f"nudged-theme count changed; re-read the table:\n{summary}"


# ---------------------------------------------------------------------------
# the one deliberate departure from a theme's own palette
# ---------------------------------------------------------------------------

def test_dracula_text_is_the_operators_parchment(themes):
    """dracula.text is #ffd7af, NOT Dracula's own #f8f8f2. Do not "fix" it.

    The operator asked for a warm parchment as the dominant text colour and
    dracula is the default theme, so the default pane reads parchment. Every
    other theme uses its own published foreground; this one value is the
    operator's call and is recorded in the catalog's `_note`.

    #ffd7af IS ansi 223 -- exactly, because 223 is a cube cell and the round trip
    is lossless. That is the same guarantee the `og` row's three hexes carry, and
    it is asserted here rather than in test_og_themes.py because this is the
    assertion about the CATALOG's declared value; the converter's own round trip
    is pinned over there.

    It is also neither of the two hexes this row has carried before, and both
    reasons matter. It is not Dracula's #f8f8f2 because the operator chose a
    warmer cream. It is not the previous #f0e6d2 because that hex's nearest
    256-colour cell is 254 -- a pure grey -- so on the terminal this actually
    ships to it painted the body text grey. That was a converter bug, and the
    converter is fixed; the hex is not coming back.
    """
    dracula = themes["dracula"]
    assert dracula["colors"]["text"] == "#ffd7af"
    assert dracula["colors"]["text"] != "#f8f8f2", "Dracula's Parchment leaked back in"
    assert dracula["colors"]["text"] != "#f0e6d2", (
        "the grey parchment is back; it paints as cell 254, which is a pure grey")
    assert _index(dracula["colors"]["text"]) == 223
    assert _cell(dracula["colors"]["text"]) == "#ffd7af"
    note = dracula.get("_note", "")
    assert "#ffd7af" in note and "parchment" in note.lower(), (
        f"the _note explaining the parchment exception is missing or vague: {note!r}")
    for phrase in ("#f8f8f2", "#f0e6d2", "223"):
        assert phrase in note, f"the _note does not mention {phrase!r}: {note!r}"
    # And it is only ever a *text* exception -- Dracula's other six roles are
    # upstream Dracula, which is what makes the pane still read as Dracula.
    assert dracula["colors"]["bg"] == "#282a36"
    assert dracula["colors"]["sel_bg"] == "#44475a"
    assert dracula["colors"]["idle"] == "#50fa7b"
    assert dracula["colors"]["working"] == "#f1fa8c"