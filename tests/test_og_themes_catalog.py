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

  * NO ROLE COLLAPSES TO NEUTRAL UNLESS THE PALETTE CANNOT HOLD ITS TINT. What a
    theme hex converts to is half the spec; what it converts to is the rest of
    it. The converter refuses neutral cells for any colour carrying chroma, and
    -- because the cube's dark cells are saturated primaries -- it stops refusing
    past a bounded price. See test_a_chromatic_role_only_collapses_where_the_palette
    _has_no_tint and test_the_cap_sits_in_the_gap_the_catalog_leaves.

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

ROLES = ("bg", "text", "meta", "sel_bg", "idle", "working", "failed", "rail")
HEX_RE = re.compile(r"^#[0-9a-f]{6}$")

# The literal a role carries when it means curses' `-1`: allocate no colour and
# inherit the terminal's own. Legal in ANY of the eight roles.
DEFAULT = "default"

# `og` is the row that is exempt from the contrast assertions, and the ONLY one.
# WHY IT HAS TO BE, because the next reader's first instinct will be to close this
# gap by inventing hexes for it:
#
#   * Four of its eight roles are the string "default". The point of the row is
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
        "sel_bg": "#1e252c", "idle": "#96e072", "working": "#f3d56e", "failed": "#ff5874", "rail": "#3a3f4c"},
    "ayu-dark": {"bg": "#0f1419", "text": "#e6e1cf", "meta": "#5c6773",
        "sel_bg": "#253340", "idle": "#aad94c", "working": "#ffb454", "failed": "#f07178", "rail": "#2d3640"},
    "ayu-light": {"bg": "#fafafa", "text": "#5c6773", "meta": "#abb0b6",
        "sel_bg": "#f0eee4", "idle": "#86b300", "working": "#eba400", "failed": "#f07178", "rail": "#d9d8d7"},
    "bearded-dark": {"bg": "#181a1f", "text": "#c8cbd4", "meta": "#515666",
        "sel_bg": "#34373d", "idle": "#37ae6f", "working": "#c9a022", "failed": "#d16d6d", "rail": "#0b0c0e"},
    "bearded-light": {"bg": "#f3f4f5", "text": "#0a1315", "meta": "#68909c",
        "sel_bg": "#c9e4ec", "idle": "#189433", "working": "#bb9600", "failed": "#d16d6d", "rail": "#c9ced2"},
    "catppuccin-frappe": {"bg": "#303446", "text": "#c6d0f5", "meta": "#737994",
        "sel_bg": "#414559", "idle": "#a6d189", "working": "#e5c890", "failed": "#e78284", "rail": "#51576d"},
    "catppuccin-latte": {"bg": "#eff1f5", "text": "#4c4f69", "meta": "#9ca0b0",
        "sel_bg": "#ccd0da", "idle": "#40a02b", "working": "#df8e1d", "failed": "#d20f39", "rail": "#bcc0cc"},
    "catppuccin-macchiato": {"bg": "#24273a", "text": "#cad3f5", "meta": "#8087a2",
        "sel_bg": "#363a4f", "idle": "#a6da95", "working": "#eed49f", "failed": "#ed8796", "rail": "#414559"},
    "catppuccin-mocha": {"bg": "#1e1e2e", "text": "#cdd6f4", "meta": "#7f849c",
        "sel_bg": "#313244", "idle": "#a6e3a1", "working": "#f9e2af", "failed": "#f38ba8", "rail": "#292c3c"},
    "dracula": {"bg": "#282a36", "text": "#ffd7af", "meta": "#6272a4",
        "sel_bg": "#44475a", "idle": "#50fa7b", "working": "#f1fa8c", "failed": "#ff5555", "rail": "#44475a"},
    "github-dark": {"bg": "#0d1117", "text": "#c9d1d9", "meta": "#8b949e",
        "sel_bg": "#163356", "idle": "#238636", "working": "#fcb32c", "failed": "#f85149", "rail": "#30363d"},
    "gruvbox": {"bg": "#282828", "text": "#ebdbb2", "meta": "#928374",
        "sel_bg": "#504945", "idle": "#b8bb26", "working": "#fabd2f", "failed": "#fb4934", "rail": "#3c3836"},
    "monokai-dark": {"bg": "#272822", "text": "#fcfcfa", "meta": "#727072",
        "sel_bg": "#49483e", "idle": "#a8dc76", "working": "#ffd966", "failed": "#ff6188", "rail": "#3e3d32"},
    "monokai-light": {"bg": "#faf4f2", "text": "#29242a", "meta": "#a59fa0",
        "sel_bg": "#eee5de", "idle": "#269d69", "working": "#cc7a0a", "failed": "#ff6188", "rail": "#e6ded9"},
    "nord": {"bg": "#2e3440", "text": "#d8dee9", "meta": "#616e88",
        "sel_bg": "#3b4252", "idle": "#a3be8c", "working": "#ebcb8b", "failed": "#bf616a", "rail": "#4c566a"},
    "one-dark": {"bg": "#282c34", "text": "#abb2bf", "meta": "#5c6370",
        "sel_bg": "#3e4451", "idle": "#98c379", "working": "#d19a66", "failed": "#e06c75", "rail": "#495162"},
    "one-light": {"bg": "#fafafa", "text": "#2a2b33", "meta": "#a0a1a7",
        "sel_bg": "#d0d0d0", "idle": "#3e953a", "working": "#d2b67b", "failed": "#e45649", "rail": "#d4d4d4"},
    "palenight": {"bg": "#292d3e", "text": "#bfc7d5", "meta": "#697098",
        "sel_bg": "#32374c", "idle": "#a9c77d", "working": "#ffcb6b", "failed": "#ff5370", "rail": "#676e95"},
    "panda": {"bg": "#292a2b", "text": "#e6e6e6", "meta": "#757575",
        "sel_bg": "#31353a", "idle": "#19f9d8", "working": "#ffb86c", "failed": "#ff75b5", "rail": "#3e4250"},
    "solarized-dark": {"bg": "#002b36", "text": "#93a1a1", "meta": "#586e75",
        "sel_bg": "#073642", "idle": "#859900", "working": "#b58900", "failed": "#dc322f", "rail": "#073642"},
    "solarized-light": {"bg": "#fdf6e3", "text": "#657b83", "meta": "#93a1a1",
        "sel_bg": "#eee8d5", "idle": "#859900", "working": "#b58900", "failed": "#dc322f", "rail": "#eee8d5"},
    "synthwave-84": {"bg": "#262335", "text": "#f2f2f2", "meta": "#848bbd",
        "sel_bg": "#34294f", "idle": "#72f1b8", "working": "#f3e70f", "failed": "#fe4450", "rail": "#1a1a2e"},
    "tailwind": {"bg": "#0f172a", "text": "#e2e8f0", "meta": "#64748b",
        "sel_bg": "#1e293b", "idle": "#22c55e", "working": "#eab308", "failed": "#ef4444", "rail": "#334155"},
    "tokyo-night": {"bg": "#1a1b26", "text": "#a9b1d6", "meta": "#565f89",
        "sel_bg": "#33467c", "idle": "#9ece6a", "working": "#e0af68", "failed": "#f7768e", "rail": "#1e202e"},
    "tokyo-night-light": {"bg": "#e6e7ed", "text": "#343b59", "meta": "#848cb5",
        "sel_bg": "#dcdee3", "idle": "#385f0d", "working": "#8f5e15", "failed": "#8c4351", "rail": "#c1c2c7"},
}

# Themes whose published colour could not clear its contrast target and were
# nudged in HSL lightness only, by the least amount that clears it. The comment
# on each line is that amount (HSL lightness delta).
#
# THE TWO ROUNDS. A line marked `post-conversion` was moved by the SECOND run of
# the floors -- the one that measures the RESOLVED 256-colour CELLS rather than
# the declared hexes. Most of the table is that round: 39 of the 42. Every one of
# them passed on paper and failed on the terminal, which is the whole point: the
# hex is half the spec and the cell it converts to is the other half, and the hex
# alone is not what anyone looks at. A colour can clear 3.07:1 on paper and paint
# at 1.99:1, because the cell it lands on is a different colour.
#
# THE MOVEMENT IS MOSTLY `meta` and `rail`, for two reasons that happen to agree:
# comment colours are dim by design and most sat under 3.0, and structural greys
# were effectively invisible against their own background.
#
# WHAT THE CAP CHANGED, AND IT IS WORTH THE SPACE. This table was re-derived from
# scratch when `MAX_CHROMA_PENALTY` bounded the converter, and three things moved
# that are easier to state than to rediscover:
#
#   * `bg` is no longer nudged anywhere, and `sel_bg` is not either. Under the
#     unbounded guard a dark background was forced onto a saturated cube cell --
#     Catppuccin Frappe's #303446 became #005f5f, so its meta painted at 1.99:1
#     and the background itself had to move +0.096 in lightness to give the meta
#     something to read on. With the cap both roles land on the ramp one rung
#     apart, which is what a pane and a selected row actually want, and Nord's
#     and Tailwind's two sel_bg nudges exist no more.
#   * Solarized Dark's body text stops moving at all. Its #93a1a1 has chroma 0.016,
#     below the threshold, so it is a neutral and took the ramp either way; what
#     the unbounded guard did was drag the BACKGROUND onto #00005f, 0.1311 of
#     OKLab away where the grey it replaced is 0.0486, which is why the text had to
#     climb to +0.184 to clear 4.5:1 against it. On the capped mapping it clears
#     at 5.65:1 un-nudged.
#   * Bearded Dark's rail is the longest move left in the table, +0.214, and it
#     has to be: #0b0c0e is nearly the same colour as its own background, so a
#     rail you can see is a long way from the published one no matter what.
NUDGES = {
    "ayu-dark": {
        "rail": ("#2d3640", "#2e3741"),  # L +0.004, post-conversion
    },
    "ayu-light": {
        "idle": ("#86b300", "#739a00"),  # L -0.049, post-conversion
        "meta": ("#abb0b6", "#888f97"),  # L -0.129, post-conversion
        "rail": ("#d9d8d7", "#cdcbca"),  # L -0.049, post-conversion
        "working": ("#eba400", "#bd8400"),  # L -0.090, post-conversion
    },
    "bearded-dark": {
        "meta": ("#515666", "#6c7388"),  # L +0.120, post-conversion
        "rail": ("#0b0c0e", "#3b414b"),  # L +0.214, post-conversion
    },
    "bearded-light": {
        "rail": ("#c9ced2", "#bbc1c6"),  # L -0.051, post-conversion
        "working": ("#bb9600", "#9c7d00"),  # L -0.061, post-conversion
    },
    "catppuccin-frappe": {
        "meta": ("#737994", "#797f98"),  # L +0.020, post-conversion
    },
    "catppuccin-latte": {
        "idle": ("#40a02b", "#3e9c2a"),  # L -0.010, post-conversion
        "meta": ("#9ca0b0", "#777d93"),  # L -0.129, post-conversion
        "text": ("#4c4f69", "#4a4d66"),  # L -0.010, post-conversion
        "working": ("#df8e1d", "#b47317"),  # L -0.096, post-conversion
    },
    "catppuccin-mocha": {
        "rail": ("#292c3c", "#3a3e54"),  # L +0.080, post-conversion
    },
    "gruvbox": {
        "rail": ("#3c3836", "#494542"),  # L +0.049, post-conversion
    },
    "monokai-dark": {
        "meta": ("#727072", "#737173"),  # L +0.004, post-conversion
        "rail": ("#3e3d32", "#464539"),  # L +0.029, post-conversion
    },
    "monokai-light": {
        "idle": ("#269d69", "#259966"),  # L -0.010, post-conversion
        "meta": ("#a59fa0", "#8e8687"),  # L -0.094, post-conversion
        "rail": ("#e6ded9", "#d3c5bc"),  # L -0.094, post-conversion
        "working": ("#cc7a0a", "#c2740a"),  # L -0.020, post-conversion
    },
    "nord": {
        "meta": ("#616e88", "#717e99"),  # L +0.065, post-conversion
    },
    "one-dark": {
        "meta": ("#5c6370", "#6e7786"),  # L +0.078, post-conversion
    },
    "one-light": {
        "meta": ("#a0a1a7", "#8c8d95"),  # L -0.075, post-conversion
        "rail": ("#d4d4d4", "#cccccc"),  # L -0.031, post-conversion
        "working": ("#d2b67b", "#ae893b"),  # L -0.196, post-conversion
    },
    "palenight": {
        "meta": ("#697098", "#6f769c"),  # L +0.020, post-conversion
    },
    "panda": {
        "rail": ("#3e4250", "#424756"),  # L +0.020
    },
    "solarized-dark": {
        "meta": ("#586e75", "#5d757c"),  # L +0.024, post-conversion
        "rail": ("#073642", "#0a4b5b"),  # L +0.055, post-conversion
    },
    "solarized-light": {
        "idle": ("#859900", "#839600"),  # L -0.006
        "meta": ("#93a1a1", "#809090"),  # L -0.071, post-conversion
        "rail": ("#eee8d5", "#d3c392"),  # L -0.184, post-conversion
        "text": ("#657b83", "#576970"),  # L -0.065, post-conversion
        "working": ("#b58900", "#b28700"),  # L -0.006
    },
    "synthwave-84": {
        "rail": ("#1a1a2e", "#3e3e6e"),  # L +0.196, post-conversion
    },
    "tokyo-night": {
        "meta": ("#565f89", "#6872a0"),  # L +0.080, post-conversion
        "rail": ("#1e202e", "#393d58"),  # L +0.135, post-conversion
        "text": ("#a9b1d6", "#bec4e0"),  # L +0.061, post-conversion
    },
    "tokyo-night-light": {
        "meta": ("#848cb5", "#6771a4"),  # L -0.090, post-conversion
        "rail": ("#c1c2c7", "#b5b6bc"),  # L -0.045, post-conversion
    },
}


POST_CONVERSION = {
    "ayu-dark.rail",
    "ayu-light.idle",
    "ayu-light.meta",
    "ayu-light.rail",
    "ayu-light.working",
    "bearded-dark.meta",
    "bearded-dark.rail",
    "bearded-light.rail",
    "bearded-light.working",
    "catppuccin-frappe.meta",
    "catppuccin-latte.idle",
    "catppuccin-latte.meta",
    "catppuccin-latte.text",
    "catppuccin-latte.working",
    "catppuccin-mocha.rail",
    "gruvbox.rail",
    "monokai-dark.meta",
    "monokai-dark.rail",
    "monokai-light.idle",
    "monokai-light.meta",
    "monokai-light.rail",
    "monokai-light.working",
    "nord.meta",
    "one-dark.meta",
    "one-light.meta",
    "one-light.rail",
    "one-light.working",
    "palenight.meta",
    "solarized-dark.meta",
    "solarized-dark.rail",
    "solarized-light.meta",
    "solarized-light.rail",
    "solarized-light.text",
    "synthwave-84.rail",
    "tokyo-night-light.meta",
    "tokyo-night-light.rail",
    "tokyo-night.meta",
    "tokyo-night.rail",
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

# What counts as a TINT on this palette, as opposed to a hue: the twenty cells
# that carry some chroma without carrying a primary's worth of it. The bound the
# collapse inventory is judged against is derived from them rather than written
# down -- see _darkest_unsaturated_cell.
UNSATURATED_CELL_CHROMA = 0.07


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
    # because order 0 puts it first in the picker. `og` is not one of the
    # reference site's themes, so it is checked separately rather than folded into
    # EXPECTED_LABELS.
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
        "failed": "#ff0000", "rail": "default",
        # The OPTIONAL ninth role: `og` is the one row that must DECLARE a footer
        # strip, because it has no `bg` to derive one from and the operator picked
        # this colour. See `og_agents.footer_strip`.
        "strip": "#232625",
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


def test_every_row_defines_all_eight_roles(themes):
    """Eight roles on every row -- and a role is EITHER `#rrggbb` or `"default"`.

    The lowercase-hex half of this used to be the whole rule. `"default"` is now
    legal in ANY of the eight roles, which is the single structural decision that
    lets one theme reproduce exactly what the pane painted before themes existed;
    so the shape test accepts both and the CONTRAST tests are the ones that
    decide which rows may use `"default"` at all (they may not, except `og` --
    see OG_EXEMPT). `failed` is the eighth: the operator asked for a red failed
    mark, so every row has to carry one beside `idle` and `working`.

    A row MAY also declare the OPTIONAL `strip` role (its own footer-band colour),
    which is why the shape is `ROLES <= roles <= ALL_ROLES` rather than equality:
    `og` declares one, the other 25 derive their strip from `bg` and do not.
    """
    for slug, theme in themes.items():
        colors = theme["colors"]
        assert set(ROLES) <= set(colors) <= set(ROLES) | {"strip"}, \
            f"{slug}: roles {sorted(colors)}"
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
# existing palette cells and NOT its hex. That is now true of EVERY terminal, not
# just this one: `curses_value` has no truecolor branch at all, because
# `init_pair` takes colour numbers and nothing in this ncurses turns a hex into
# one. That makes the two sets of numbers different questions, and only the
# second one is about the pane.
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
    selected row would have been invisible, in three themes, on every terminal.
    A hex test cannot see that and this one can.
    """
    for slug, label, colors in _rows(themes):
        pane, selected = _index(colors["bg"]), _index(colors["sel_bg"])
        assert pane != selected, (
            f"{label}: bg {colors['bg']} and sel_bg {colors['sel_bg']} both paint "
            f"as cell {pane} -- the selected row would be invisible"
        )


def _penalty(value: str) -> tuple:
    """`(penalty, nearest, tinted)` for `value`, in OKLab distance.

    `penalty` is what the guard costs: how much further the best CHROMATIC cell is
    than the best cell of any kind. Zero means the converter had no choice to
    make; a large one means the palette does not hold this colour's tint at any
    price and the ramp is the honest answer.
    """
    target = th.rgb_to_oklab(th.hex_to_rgb(value))
    best = min(th.PALETTE_LAB, key=lambda row: (math.dist(target, row[1]), row[0]))
    tinted = min(th.CHROMATIC_LAB, key=lambda row: (math.dist(target, row[1]), row[0]))
    return (math.dist(target, tinted[1]) - math.dist(target, best[1]),
            best[0], tinted[0])


def _darkest_unsaturated_cell() -> float:
    """The lightness of the DARKEST cell that carries a tint rather than a hue.

    Computed from the palette rather than written down, because it is a fact about
    the 6x6x6 cube and the cube does not change. Of the 240 cells, twenty carry
    chroma without carrying a primary's worth of it -- #5f5f87, #87afaf, #ffd7af,
    the operator's parchment among them -- and every one of the twenty sits above
    OKLab L 0.502. Below that there is no such cell: the cube's lowest two rungs
    are 0 and 95, so the only chromatic cells down there are #00005f (chroma
    0.152), #005f5f (0.075) and #5f0000 (0.125), and a dark role asking for a
    chroma of 0.02 to 0.07 is asking for a tint the palette simply does not have.
    """
    tints = [lightness for lightness, chroma, _hue in
             (th.oklab_hue(rgb) for _index, rgb in th.PALETTE)
             if th.NEUTRAL_CHROMA <= chroma < UNSATURATED_CELL_CHROMA]
    assert tints, "no cell carries a tint; the palette changed underneath this"
    return min(tints)


def test_a_chromatic_role_only_collapses_where_the_palette_has_no_tint(themes):
    """THE SURVEY, INVENTORIED. The guard is bounded, so some collapses are CORRECT.

    52 role-colours across 23 themes used to lose their chroma to the greyscale
    ramp, and nothing caught it because every contrast assertion in this file
    looked at the hex. Refusing neutral cells outright fixed that and created the
    opposite fault: the cube's darkest cells are saturated primaries, so dracula's
    background became 52 (#5f0000, a dark maroon) and nord's 23 (#005f5f, teal) --
    each about 0.13 from the colour that was asked for, where the grey was 0.03.
    Four and a half times worse, in the metric the converter minimises, on the
    operator's DEFAULT pane background.

    `th.MAX_CHROMA_PENALTY` is the boundary between those two mistakes, and THIS
    is the test that says which side of it the catalog is on. The claim is
    structural, not numerical: a role may only collapse to a grey if it is DARKER
    than every cell on the palette that carries a tint. `_darkest_unsaturated_cell`
    puts that line at OKLab L 0.502, and the lightest role the cap refuses is at
    0.434 -- 0.068 below it, in a band where six other Catppuccin, Dracula and
    Nord selection cells sit too.

    Thirty-three roles collapse. Nothing above L 0.434 collapses, which is the
    assertion that matters: a mid- or high-lightness colour going grey is the
    original defect, and this test fails the moment one does.
    """
    collapses = []
    for slug, label, colors in _rows(themes):
        for role, value in colors.items():
            lightness, chroma, _hue = th.oklab_hue(th.hex_to_rgb(value))
            cell = th.oklab_hue(th.ansi256_rgb(_index(value)))
            if chroma > th.NEUTRAL_CHROMA and cell[1] < th.NEUTRAL_CHROMA:
                penalty, nearest, tinted = _penalty(value)
                assert penalty > th.MAX_CHROMA_PENALTY, (
                    f"{label}.{role} {value} collapsed with a penalty of "
                    f"{penalty:.4f}, inside the cap -- that is the ramp winning a "
                    "colour it should have lost, not the cap allowing one"
                )
                assert lightness < _darkest_unsaturated_cell(), (
                    f"{label}.{role} {value} collapsed at OKLab L {lightness:.3f}, "
                    "which is NOT darker than every tinted cell on the palette "
                    f"({_darkest_unsaturated_cell():.3f}) -- the palette could have "
                    f"held this tint and did not: nearest cell {nearest}, tinted "
                    f"{tinted}, penalty {penalty:.4f}"
                )
                collapses.append(
                    f"{label}.{role} {value} (L {lightness:.3f}, chroma "
                    f"{chroma:.3f}, penalty {penalty:.4f}) -> cell {_index(value)}")
    assert len(collapses) == 33, (
        "the collapse set moved; re-read it before accepting it\n  "
        + "\n  ".join(collapses))


def test_the_cap_sits_in_the_gap_the_catalog_leaves(themes):
    """THE NUMBER IS THE DATA'S, and this is what will say so if it stops being so.

    Across the 147 chromatic role-colours here, 94 have no penalty at all -- the
    chromatic cell IS the nearest cell and the guard is free. The rest are
    bimodal: a cheap band from 0.005 to 0.033, which is what paying for a tint
    actually costs, and an expensive band from 0.036 to 0.111, which is what
    reaching for a saturated primary costs. `MAX_CHROMA_PENALTY` sits in the hole
    between them, so moving it either way changes which side a role is on only
    when a theme is nudged again.

    This is the margin. A cap that had been picked to make today's catalog look
    right would have no room here: nudge a hex by one lightness step and it slides
    across. 0.035 has 0.0019 above the heaviest keeper and 0.0007 below the
    lightest collapse, and both of those numbers are small enough that this test is
    worth having on every run rather than once.
    """
    kept = []
    for _slug, _label, colors in _rows(themes):
        for value in colors.values():
            if th.oklab_hue(th.hex_to_rgb(value))[1] <= th.NEUTRAL_CHROMA:
                continue
            kept.append((_penalty(value)[0], value, _index(value)))
    kept.sort()
    refused = [(p, v, i) for p, v, i in kept if p > th.MAX_CHROMA_PENALTY]
    cheap = [(p, v, i) for p, v, i in kept if p <= th.MAX_CHROMA_PENALTY]
    assert len(kept) == 147, len(kept)
    assert len(refused) == 33, len(refused)
    assert max(p for p, _v, _i in cheap) == pytest.approx(0.0331, abs=5e-4), \
        "the heaviest keeper moved; re-check the cap"
    assert min(p for p, _v, _i in refused) == pytest.approx(0.0357, abs=5e-4), \
        "the lightest collapse moved; re-check the cap"
    # The cut is between them, which is the whole claim.
    assert max(p for p, _v, _i in cheap) < th.MAX_CHROMA_PENALTY \
        < min(p for p, _v, _i in refused)
    # And the cap is not the 0.03 that would push Monokai Light's rail -- a
    # LIGHT-pane colour at L 0.832 -- onto the ramp, which is the defect the
    # guard exists to prevent, in the band where it shows most.
    assert th.MAX_CHROMA_PENALTY > 0.0331, "a mid-lightness colour would go grey"
    assert th.hex_to_ansi256("#d3c5bc") == 181, "monokai-light's rail lost its tint"


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
    assert len(NUDGES) == 20, f"nudged-theme count changed; re-read the table:\n{summary}"
    assert sum(len(m) for m in NUDGES.values()) == 42, summary


# ---------------------------------------------------------------------------
# the one deliberate departure from a theme's own palette
# ---------------------------------------------------------------------------

def test_dracula_text_is_the_operators_parchment(themes):
    """dracula.text is #ffd7af, NOT Dracula's own #f8f8f2. Do not "fix" it.

    The operator asked for a warm parchment as the dominant text colour, and
    dracula is the row that carries the choice. Every other theme uses its own
    published foreground; this one value is the operator's call and is recorded
    in the catalog's `_note`.

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