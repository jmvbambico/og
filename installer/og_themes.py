"""The `og agents` pane's colour THEMES: role -> colour, and the maths behind them.

THE BACKGROUND DECISION THIS FILE EXISTS TO MAKE. `og_agents` painted every colour
with `init_pair(n, fg, -1)` — a coloured FOREGROUND on the TERMINAL'S OWN
background. The `_VENDOR_COLOURS` comment block said what that costs, in as many
words: "on a light terminal every entry here inverts into the very problem the
floor exists to prevent ... That is a background decision, not a fourteen-colour
retune, so it is not made here." It is made here. A theme supplies a `bg`, the
pane paints that background, and the fourteen logo colours keep their indices
because the background they were chosen against is now the one they were picked
for.

A theme is DATA, not code — the same rule as registry.json. `themes.json` is the
catalog and this module is the reader, the validator and the maths. A theme maps
all SEVEN ROLES to a colour:

    bg       the pane background
    text     the dominant body text
    meta     secondary text (model ids, timestamps, status detail)
    sel_bg   the background of the SELECTED row
    idle     the idle status glyph
    working  the working status glyph
    rail     group rails and box borders

A role's value is EITHER a `#rrggbb` hex OR the literal string `"default"`,
which means curses `-1`: allocate no colour, inherit the terminal's own.
`"default"` is legal in ANY of the eight roles, not only `bg`. That is the single
most important structural decision here — it is what lets one theme reproduce
exactly what the pane painted before themes existed (`og`: every role either a
hex that converts back to today's index, or `"default"`), which in turn is what
makes the change cost nothing for an operator who wants none of it.

LOADING IS TWO-TIERED ON PURPOSE. An absent or unreadable `themes.json` falls
back SILENTLY to the one built-in theme below: a theme catalog is a preference,
and a missing preference must not stop the pane from opening. A catalog file
that is present, readable, valid JSON, and structurally WRONG — an empty or
list-valued `themes`, a theme missing a role, a role carrying something that is
neither a hex nor `"default"` — raises. That asymmetry is AGENTS.md's "a loud
refusal beats a silent breakage": the first case is an absent file and the
operator has simply never chosen, the second is a bad edit someone will look at
again and cannot look at unless told which row and which role.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "themes.json"

# The eight roles, in catalog order. Every theme defines ALL of them; there is no
# partial theme, because a partial one is a theme whose half is still hard-coded
# somewhere else. `failed` joined the seven when the operator asked for a red
# failed mark: it is a STATUS role beside `idle` and `working`, so it sits with
# them and every row carries it — the loader refuses a row missing one.
ROLES = ("bg", "text", "meta", "sel_bg", "idle", "working", "failed", "rail")

# The one value that is not a colour: curses' `-1`, "allocate nothing, inherit the
# terminal's own". Legal in every role (see the module docstring).
DEFAULT = "default"

# `og` is the DEFAULT because the operator asked for it: it is the pane as it
# painted before themes existed, and a fresh install should open in it rather than
# in a palette somebody else chose. The default is a preference, not a fact about
# colour: `--theme` and $OG_AGENTS_THEME both override it. dracula stays in the
# catalog as an ordinary row -- still the slug a stored preference can name, and
# still the operator's warm parchment (#ffd7af, ansi 223 EXACTLY, picked from the
# 256-colour cells; see the row's `_note`). Only the default moved; nothing was
# removed.
DEFAULT_THEME = "og"

HEX_RE = re.compile(r"^#[0-9a-f]{6}$")
# The converter is more permissive than the CATALOG on purpose: a catalog row is
# written by hand here and lowercase is a house rule, but `hex_to_ansi256` is also
# fed brand hexes copied out of a vendor's own stylesheet, and "#F3EF4E" is one of
# the two the `_VENDOR_COLOURS` comment asserts against. Rejecting it would be a
# guard against nothing.
ANY_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ThemeError(ValueError):
    """A catalog row that cannot be painted. Named, never skipped.

    Carries the theme SLUG and the offending ROLE in its message, because the
    question the next person asks is "which row, which cell" and a loader that
    answers "bad theme data" has saved them nothing.
    """


# ---------------------------------------------------------------------------
# the theme
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Theme:
    """One row of the catalog: a slug, a display label, a sort key, eight colours."""

    slug: str
    label: str
    order: int
    colors: dict

    def colour(self, role: str) -> str:
        """`role`'s value: a `#rrggbb` hex, or `"default"`."""
        return self.colors[role]

    def is_default(self, role: str) -> bool:
        """Whether `role` inherits the terminal's own colour."""
        return self.colors[role] == DEFAULT

    def background(self) -> Optional[str]:
        """The pane background as a hex, or None when it is the terminal's own.

        None is NOT the same as "no background": it means the background is
        unknowable here, which is why the logo readability guard is a no-op on
        it (see `readable_index`) and why the window background is left alone.
        """
        value = self.colors["bg"]
        return None if value == DEFAULT else value

    def index(self, role: str) -> int:
        """`role` as an ANSI-256 index, or `-1` (curses' "inherit") for `"default"`."""
        value = self.colors[role]
        return -1 if value == DEFAULT else hex_to_ansi256(value)


# The BUILT-IN fallback: ONE theme, the DEFAULT one (`og`), byte-for-byte the
# catalog row. It is the whole of what the pane can paint with no `themes.json` at
# all, which is a deliberate floor and not an oversight — a fallback that ships
# twenty-six palettes would be a second catalog nobody edits. It tracks the
# default so a machine with a missing or unreadable catalog opens on the same
# theme a fresh install does, rather than on a palette the default no longer
# names.
BUILTIN: dict = {
    "og": Theme(
        slug="og",
        label="OG",
        order=0,
        colors={
            "bg": "default",
            "text": "default",
            "meta": "#87afaf",
            "sel_bg": "default",
            "idle": "#00ff00",
            "working": "#ffff00",
            "failed": "#ff0000",
            "rail": "default",
        },
    ),
}

# The built-in `og` is checked against the catalog row by a test
# (`test_the_builtin_og_matches_the_catalog_row`), not by an assert here: an
# import-time assert kills the pane when someone is mid-edit on themes.json, and
# this file's whole argument is that a theme preference must never stop the view
# opening.


# ---------------------------------------------------------------------------
# colour maths
# ---------------------------------------------------------------------------
#
# The converter below is the one `_VENDOR_COLOURS`' comment block already reasons
# in, written down here so the comment and the code cannot drift apart. Two
# assertions in that comment are the converter's regression tests and both are
# reproduced in tests/test_og_themes.py: #F3EF4E -> 227 (kilo's lemon) and
# #A4BA96 -> 144 (the sage whose nearest index is NOT the one shipped — the table
# deliberately overrides 144 with 108, a hand decision this converter must not
# second-guess).

# The 6x6x6 colour cube: each channel takes one of six rungs, and 16 + 36r + 6g + b
# is the index. Named because the numbers are load-bearing and appear verbatim in
# the comment block above.
CUBE_BASE = 16
CUBE_RUNGS = (0, 95, 135, 175, 215, 255)

# The greyscale ramp: 24 neutrals at 8 + 10k.
GREY_BASE = 232
GREY_FIRST = 8
GREY_STEP = 10
GREY_COUNT = 24

# Curses' "use the terminal's own colour" for a colour pair.
CURSES_DEFAULT = -1


def _build_palette() -> tuple:
    """All 240 addressable indices, 16..255, as `(index, (r, g, b))`.

    240 and not 256: the first sixteen are the terminal's own ANSI slots, whose
    meaning is the terminal's (and whose xterm defaults are nothing like the
    names suggest), and the comment block above already treats "the whole
    palette" as this set.
    """
    rows = []
    index = CUBE_BASE
    for r in CUBE_RUNGS:
        for g in CUBE_RUNGS:
            for b in CUBE_RUNGS:
                rows.append((index, (r, g, b)))
                index += 1
    for step in range(GREY_COUNT):
        level = GREY_FIRST + GREY_STEP * step
        rows.append((GREY_BASE + step, (level, level, level)))
    return tuple(rows)


PALETTE = _build_palette()
assert len(PALETTE) == 240, len(PALETTE)


def _is_neutral(rgb: tuple) -> bool:
    """A palette cell with no chroma at all: the ramp, and the cube's diagonal."""
    return rgb[0] == rgb[1] == rgb[2]


# The candidate set for a CHROMATIC target: every cell that carries chroma. Built
# once at import because it is the whole of `hex_to_ansi256`'s search space for
# every colour the catalog actually has, and because a second, smaller set is the
# only way to say the rule below without changing what "nearest" is measured over.
CHROMATIC_PALETTE = tuple(row for row in PALETTE if not _is_neutral(row[1]))


def ansi256_rgb(index: int) -> tuple:
    """The sRGB triple index `n` paints for, as 0-255 ints."""
    if index < CUBE_BASE:
        raise ValueError(f"index {index} is a terminal ANSI slot, not a palette cell")
    if index < GREY_BASE:
        step = index - CUBE_BASE
        return (CUBE_RUNGS[step // 36], CUBE_RUNGS[(step // 6) % 6],
                CUBE_RUNGS[step % 6])
    level = GREY_FIRST + GREY_STEP * (index - GREY_BASE)
    return (level, level, level)


def hex_to_rgb(value: str) -> tuple:
    """`#rrggbb` (or bare `rrggbb`) as three 0-255 ints."""
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb: Iterable[float]) -> str:
    """Three 0-255 floats as `#rrggbb`."""
    return "#%02x%02x%02x" % tuple(int(round(c)) for c in rgb)


# Below this chroma a colour is a neutral and has no hue to preserve. 0.02 of
# OKLab chroma is under one rung of the cube: (175,175,145) — `opencode-zen`'s
# mark — carries 0.055 and is NOT neutral (the table's comment makes a point of
# that exact figure), while the greyscale ramp is exactly 0.
#
# THE NUMBER IS ALSO THE CONVERTER'S THRESHOLD (`hex_to_ansi256`), and it is
# chosen from the catalog rather than from taste. Surveying all 175 measurable
# role-colours in installer/themes.json for the defect a nearest-cell search over
# the WHOLE palette produces — a chromatic colour resolving to a neutral cell —
# gives 52 collapses across 23 of the 25 themes, with target chroma running from
# 0.0212 (monokai-dark's rail #464538, which fell to 238) to 0.0788
# (synthwave-84's rail #3e3e6d, which also fell to 238). The catalog itself
# draws the line: the most chromatic role that IS neutral sits at 0.0193
# (monokai-light's rail #d3c5bd) and the least chromatic one that is NOT sits at
# 0.0212, with nothing measurable in between. 0.02 is inside that gap, so it
# catches all 52 and leaves every true neutral on the ramp where it belongs. It
# is also already the constant `oklab_hue` uses to decide whether a colour has a
# hue at all, so the guard and the hue test cannot disagree about what "neutral"
# means.
#
# It says WHEN a colour is worth protecting. `MAX_CHROMA_PENALTY`, below, says
# how far the converter may go to protect it, and neither number means anything
# without the other.
NEUTRAL_CHROMA = 0.02

# How much WORSE than its own nearest cell the converter may answer, in OKLab
# distance, in order to keep a colour's chroma. The other half of the no-
# desaturation rule, and the boundary of it.
#
# `NEUTRAL_CHROMA` says WHEN a colour is worth protecting; this says HOW FAR the
# converter may go to protect it, and the two are the same kind of number on
# purpose: both are distances in the metric `hex_to_ansi256` already minimises.
# An unbounded guard is not a guard, it is a second objective. Measured over the
# chromatic role-colours in installer/themes.json, refusing neutral cells outright
# makes the converter 4.5x worse in exactly the metric it minimises, on the
# palette's darkest and most common ground:
#
#     tokyo-night.bg      #1a1b26  nearest 234 #1c1c1c 0.0214  -> 17 #00005f 0.1319
#     ayu-dark.sel_bg     #253340  nearest 236 #303030 0.0304  -> 23 #005f5f 0.1395
#     palenight.bg        #292d3e  nearest 236 #303030 0.0324  -> 52 #5f0000 0.1414
#     nord.bg             #2e3440  nearest 236 #303030 0.0275  -> 23 #005f5f 0.1350
#     dracula.bg          #282a36  nearest 235 #262626 0.0295  -> 52 #5f0000 0.1359
#
# The operator's DEFAULT pane background would be dark maroon and Nord's and Ayu
# Dark's would be teal: a more visible wrong than the grey the guard replaced.
#
# WHY IT HAPPENS, AND IT IS NOT A BUG IN THE SEARCH. The 6x6x6 cube's lowest
# non-zero rung is 95, and of the 240 cells only twenty carry a tint rather than a
# hue -- #5f5f87, #87afaf, #ffd7af, the operator's parchment among them -- and
# every one of the twenty sits above OKLab L 0.502. Below that the only chromatic
# cells are saturated primaries (#00005f, chroma 0.152; #005f5f, 0.075; #5f0000,
# 0.125), so a dark target asking for a chroma of 0.02 to 0.07 is asking for a
# colour the palette does not contain. The guard was reaching for it anyway.
#
# 68 of the 121 chromatic role-colours the catalog publishes pay NO penalty at
# all -- the chromatic cell IS the nearest cell, so the guard is free for them --
# and the rest are two populations rather than one. THE PENALTY AND THE LIGHTNESS
# AGREE ON WHICH IS WHICH: every chromatic role above OKLab L 0.44 pays at most
# 0.0328 (the heaviest is Bearded Dark's meta #515666, L 0.455), and every role
# paying more than 0.0393 sits at or below L 0.434 (the lightest is One Dark's rail
# #495162). Nothing lives between.
#
# THE NUMBER IS THE HOLE BETWEEN THOSE TWO POPULATIONS. The non-zero penalties run
# 0.0052 to 0.1105 with two gaps big enough to be a boundary, 0.0328-0.0393
# (0.0066) and 0.0428-0.0502 (0.0075). The second is the wider of the two and it
# is NOT the one: both its sides are dark targets, so it splits a single
# population and is arithmetic rather than a line between two kinds of colour. The
# first separates them, and 0.035 sits inside it -- 0.0022 above the heaviest
# cheap penalty and 0.0043 below the lightest expensive one. On the shipped,
# nudged catalog the same hole reads 0.0331-0.0357 and falls to the same
# partition, and test_the_cap_sits_in_the_gap_the_catalog_leaves pins both edges
# so a later nudge cannot slide a role across one of them unnoticed.
#
# NOT 0.03, AND THE TWO ROLES THAT DECIDE IT. On the published palettes a cap of
# 0.03 would refuse Bearded Dark's meta #515666 at L 0.455; on the shipped one it
# would refuse Monokai Light's rail #d3c5bc at L 0.832, a colour on a LIGHT pane
# that paints as cell 251 (#d7d7d7), a pure grey. Either is the original defect
# re-created in the band where it shows most, so the cut goes above both.
#
# This is not a retreat from NEUTRAL_CHROMA. It is that constant's boundary. A grey
# 0.03 away beats a maroon 0.14 away, and the z-ai row of `_VENDOR_COLOURS`
# already argues the shape of it: it pays 2.187 of OKLab to keep a hue. Bounded,
# not any price.
MAX_CHROMA_PENALTY = 0.035


def rgb_to_oklab(rgb: Iterable[float]) -> tuple:
    """An sRGB triple in OKLab, as `(L, a, b)`.

    Nearness is judged HERE rather than in sRGB because sRGB distance is the thing
    that goes wrong on a 240-cell palette: openai 36 and goose 37 are (0,175,135)
    and (0,175,175) — 40 of blue and nothing else — which sounds like almost
    nothing and is why the pair shipped unremarked until OKLab said the
    difference is barely visible at all.
    """
    def linear(channel: float) -> float:
        channel /= 255
        return (channel / 12.92 if channel <= 0.04045
                else ((channel + 0.055) / 1.055) ** 2.4)

    r, g, b = (linear(c) for c in rgb)
    # The cone response, then its cube root: the step that makes OKLab distances
    # match what the eye reports. All three sums are positive, so `**(1/3)` is
    # the same value without the domain check `cbrt` exists for.
    long_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    medium = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    short = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * long_ + 0.7936177850 * medium - 0.0040720468 * short,
            1.9779984951 * long_ - 2.4285922050 * medium + 0.4505937099 * short,
            0.0259040371 * long_ + 0.7827717662 * medium - 0.8086757660 * short)


# The palette in OKLab, and the chromatic half of it, built once at import. The
# converter searches one or the other for every colour on every call, and it does
# the arithmetic once more than it used to (see MAX_CHROMA_PENALTY), so the cells
# are converted here rather than inside a `key=` on each of the 240 candidates.
PALETTE_LAB = tuple((index, rgb_to_oklab(rgb)) for index, rgb in PALETTE)
CHROMATIC_LAB = tuple((index, rgb_to_oklab(rgb))
                      for index, rgb in CHROMATIC_PALETTE)


def oklab_hue(rgb: Iterable[float]) -> tuple:
    """`(L, chroma, hue_degrees)` — hue in 0..360, and chroma 0 means NO hue.

    A neutral carries no hue at all, so its hue angle is whatever the floating
    point noise says. Callers that move a colour in LIGHTNESS ONLY have to
    recognise that case, or they will "preserve" a hue that was never there.
    """
    lightness, a, b = rgb_to_oklab(rgb)
    chroma = math.hypot(a, b)
    if chroma < NEUTRAL_CHROMA:
        return lightness, chroma, None
    hue = math.degrees(math.atan2(b, a)) % 360.0
    return lightness, chroma, hue


# How far a replacement's hue may sit from the original's, in degrees. Loose
# enough that the 6x6x6 cube's coarse hue lattice still has something to pick,
# tight enough that no reader would call the result a different colour: the
# catalog's own nudge rule caps hue drift at 12.
HUE_TOLERANCE = 12.0

# A brand mark below this contrast against the pane is not a mark. The floor the
# `_VENDOR_COLOURS` comment records, and the same 3.0 the catalog requires of a
# theme's `meta`/`idle`/`working`.
MARK_MIN_CONTRAST = 3.0


def _nearest(pool: tuple, target: tuple) -> tuple:
    """`(index, distance)` for the cell of `pool` nearest `target` in OKLab.

    Ties go to the LOWER index, which is what makes the converter reproducible
    on any machine: `min` over a tuple is stable, so the answer cannot depend on
    dict or set iteration order.
    """
    return min(((index, math.dist(target, lab)) for index, lab in pool),
               key=lambda pair: (pair[1], pair[0]))


def hex_to_ansi256(value: str) -> int:
    """The palette index NEAREST `value`, judged in OKLab over all 240 cells —
    or over the subset of them that can HONOUR the colour, and only within a
    bounded extra distance.

    THE RULE, IN TWO CLAUSES. A target whose OKLab chroma exceeds
    `NEUTRAL_CHROMA` is searched against `CHROMATIC_LAB` instead of the whole
    palette, so it can never resolve to a neutral cell — UNLESS the nearest
    chromatic cell costs more than `MAX_CHROMA_PENALTY` of OKLab distance over
    the nearest cell overall, in which case the palette plainly cannot hold this
    colour's tint and the nearest cell wins. A target at or below the chroma
    threshold really is neutral, has no hue to preserve, and is searched against
    the whole palette exactly as before: the greyscale ramp is the correct
    answer for it. Both constants are the ones `oklab_hue` uses to decide
    whether a colour has a hue at all, so the converter and the hue test cannot
    disagree about what "neutral" means.

    WHY THE FIRST CLAUSE ALONE WAS NOT ENOUGH. The greyscale ramp is spread
    across the entire lightness axis and so is usually the nearest cell to any
    dim, low-chroma colour, so refusing it outright handed 52 role-colours across
    23 of the 25 catalog themes to the cube's saturated primaries: dracula's
    background became 52 (#5f0000, a dark maroon), nord's 23 (#005f5f, teal),
    solarized-dark's 17 (#00005f). Every one of those is 4.5x further from the
    colour that was asked for than the grey it replaced, in the metric this
    function exists to minimise — a more visible wrong than the one it fixed.
    The cube's lowest non-zero rung is 95, so a dark chromatic target has no
    unsaturated chromatic cell near it at all; `MAX_CHROMA_PENALTY` is what says
    so out loud instead of shipping the primary. See that constant for the
    measurement, and NEUTRAL_CHROMA for the defect the first clause exists to
    fix: dracula's parchment #f0e6d2 painting as 254, a pure grey, 96 degrees of
    hue from the colour that was asked for.

    Deterministic: the cells are walked in index order and ties go to the lower
    index, so the same hex always answers with the same number on every machine.
    Raises on anything that is not a hex — a caller that passes `"default"` has a
    bug, and `Theme.index` is where that case is meant to be caught.
    """
    if not ANY_HEX_RE.match(value):
        raise ThemeError(f"{value!r} is not a #rrggbb colour")
    target = rgb_to_oklab(hex_to_rgb(value))
    nearest, distance = _nearest(PALETTE_LAB, target)
    if math.hypot(target[1], target[2]) > NEUTRAL_CHROMA:
        tinted, tinted_distance = _nearest(CHROMATIC_LAB, target)
        if tinted_distance - distance <= MAX_CHROMA_PENALTY:
            return tinted
    return nearest


def curses_value(value: str, colors: int) -> int:
    """`value` as the int `init_pair(n, fg, bg)` wants on a terminal with `colors`.

    `"default"` is `-1` whatever the terminal, which is the whole point of it.
    Every other value is a PALETTE INDEX, mapped by `hex_to_ansi256`.

    THE DEGRADATION RULES LIVE IN THE CALLER — `_Colours.attr` range-checks both
    ends against `colors` and paints the glyph bare rather than wrong on an 8- or
    16-colour terminal — so `colors` is not consulted here, and that is
    deliberate: one place decides what a terminal can hold, and this one only
    says what a theme hex means.

    THERE IS NO TRUECOLOR PATH, AND THERE WAS NEVER A CORRECT ONE HERE. It used to
    hand a packed 24-bit int to `init_pair`, whose second and third arguments are
    colour NUMBERS — indices into the palette `alloc_color` would have had to
    register first. Nothing in this build registers one: the module that could,
    `curses.init_extended_pair`, is absent, as is `init_extended_color`, and
    `curses.ncurses_version` is 6.0.20150808. So the value was never a colour;
    it was a large number in a slot whose meaning is the terminal's. And the path
    was unreachable anyway: the terminal this ships to is `TERM=tmux-256color`
    with `infocmp` reporting no `ccc`, so `init_color` fails there too and
    `curses.COLORS` stays at 256. Dropping it is not a narrowing — the 256 path
    was ALWAYS the one that ran, and it is now the only one, so the pane paints
    the SAME cell on every terminal instead of a different wrong colour on the
    rare one.

    WHICH IS THE POINT. A theme colour there MUST be one of the 240 existing
    cells, which is why the converter refuses to resolve a chromatic colour onto a
    neutral one and why dracula's parchment is the cell 223 rather than a hex
    whose nearest cell is a grey. A hex that only survives as an exact RGB triple
    is a hex that renders grey everywhere else.
    """
    if value == DEFAULT:
        return CURSES_DEFAULT
    return hex_to_ansi256(value)


def relative_luminance(value: str) -> float:
    """WCAG relative luminance of a hex: 0 (black) to 1 (white).

    The same arithmetic as tests/test_og_themes_catalog.py, deliberately: the
    catalog is judged on the WCAG floor, so the code that trusts it has to compute
    it the same way or the two can disagree about the same colour.
    """
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c / 255) for c in hex_to_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _luminance_rgb(rgb: Iterable[float]) -> float:
    def channel(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    """The WCAG contrast ratio between two hexes: 1.0 to 21.0."""
    la, lb = relative_luminance(a), relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def readable_index(index: int, background: Optional[str],
                   floor: float = MARK_MIN_CONTRAST) -> int:
    """`index`, or the SAME-HUE index nearest it in lightness that reads on `background`.

    THE LOGO COLOUR GUARD. `_VENDOR_COLOURS` is fourteen brand colours picked and
    argued for against a DARK pane, and its own comment predicts the failure this
    answers: on a light terminal 227 (kilo's lemon, the second-brightest mark in
    the table) washes out against white. So the table is not edited — a brand
    colour is not a taste, and thirteen of fourteen rows are right for the theme
    they were chosen for — the PAINT-TIME ANSWER moves.

    HUE IS THE CHANNEL A BRAND MARK IS RECOGNISED BY, and that is the argument the
    z-ai row already makes in the table: at 144 the sage reads OLIVE, at 108 it
    reads green, and "a sage gone olive is simply the wrong colour". So the search
    holds hue and walks LIGHTNESS, picking the candidate whose contrast clears
    `floor` and which is nearest the original in L. A neutral mark (xai's white,
    moonshotai's 252) has no hue to hold, so it stays neutral and walks lightness
    too.

    `background` None means the theme's `bg` is `"default"` — the terminal's own,
    which this process cannot know. It is a NO-OP there, deliberately: guessing
    would recolour fourteen logos behind the operator's back on the one theme that
    promises today's colours exactly.

    Falls back to `index` itself if nothing on the palette holds the hue and
    clears the floor. There is always something (the greys do), so this is a
    belt-and-braces path, not a branch anyone should expect to see.
    """
    if background is None:
        return index
    source = ansi256_rgb(index)
    here = rgb_to_hex(source)
    if contrast_ratio(here, background) >= floor:
        return index

    lightness, chroma, hue = oklab_hue(source)
    ranked = []
    for candidate, rgb in PALETTE:
        if contrast_ratio(rgb_to_hex(rgb), background) < floor:
            continue
        cand_l, cand_c, cand_hue = oklab_hue(rgb)
        if hue is None:
            # A neutral stays neutral: "the same hue" for a colour that has none
            # is the only honest reading, and it is what keeps xai's white from
            # acquiring a tint it never had.
            if cand_c >= NEUTRAL_CHROMA:
                continue
        elif cand_hue is None or _hue_gap(hue, cand_hue) > HUE_TOLERANCE:
            continue
        ranked.append((abs(cand_l - lightness), candidate))
    if not ranked:
        return index
    return min(ranked)[1]


def _hue_gap(left: float, right: float) -> float:
    """The shorter way round the colour wheel between two hue angles."""
    return min(abs(left - right), 360.0 - abs(left - right))


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def _parse_theme(slug: str, raw) -> Theme:
    """One catalog row into a `Theme`, or a `ThemeError` naming the slug AND role.

    Every check below is loud on purpose (see the module docstring). The two a
    hand edit actually makes are a role deleted from a row and a hex typed in
    uppercase or with a `#` missing; both would otherwise reach curses as a colour
    number nobody chose.
    """
    if not isinstance(raw, dict):
        raise ThemeError(f"theme {slug!r} is not an object")
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ThemeError(f"theme {slug!r} has no usable 'label'")
    order = raw.get("order")
    if not isinstance(order, int) or isinstance(order, bool):
        raise ThemeError(f"theme {slug!r} has a non-integer 'order': {order!r}")
    colors = raw.get("colors")
    if not isinstance(colors, dict):
        raise ThemeError(f"theme {slug!r} has no 'colors' object")

    for role in ROLES:
        if role not in colors:
            raise ThemeError(f"theme {slug!r} is missing the role {role!r}")
        value = colors[role]
        if not isinstance(value, str):
            raise ThemeError(
                f"theme {slug!r} role {role!r} is {value!r}, not a string")
        if value == DEFAULT:
            continue
        if not HEX_RE.match(value):
            raise ThemeError(
                f"theme {slug!r} role {role!r} is {value!r}: expected "
                f"'default' or lowercase #rrggbb")
    for role in sorted(set(colors) - set(ROLES)):
        raise ThemeError(f"theme {slug!r} has no role {role!r} — a typo costs a colour")

    return Theme(slug=slug, label=label, order=order, colors=dict(colors))


def load_catalog(path: Optional[Path] = None) -> dict:
    """Every theme, slug -> `Theme`, from `themes.json` or the built-in fallback.

    SILENT on an absent, unreadable or unparseable FILE — a preference nobody
    set is not an error, and the pane still has to open. Those three are the
    cases where the file said NOTHING.

    LOUD on a file that said something WRONG, and an empty `themes` is one of
    those things. `{"themes": {}}` and `{"themes": []}` are present, readable,
    valid JSON and structurally wrong: there is no row to paint, and falling back
    to the default says "your catalog is one theme, from nowhere" instead of "your
    catalog is empty and that is why the menu you are looking at is not the one
    you wrote". It took a deliberate decision to put that on this side of the line
    rather than the silent one, and the argument is the module's own: the absent
    file is an operator who has never chosen, and this is a bad edit somebody
    will look at again and cannot look at unless told which shape is wrong. The
    same is true of a document that is not an object and of one with no `themes`
    key — all three used to arrive here through the same `except` as the missing
    file, which is why this docstring and the code once disagreed.
    """
    source = Path(path or CATALOG)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(BUILTIN)
    if not isinstance(document, dict):
        raise ThemeError(f"{source} is not a themes document: "
                         f"top level is {type(document).__name__}, not an object")
    if "themes" not in document:
        raise ThemeError(f"{source} has no 'themes' key")
    themes = document["themes"]
    if not isinstance(themes, dict):
        raise ThemeError(f"{source} has a 'themes' that is "
                         f"{type(themes).__name__}, not an object")
    if not themes:
        raise ThemeError(f"{source} has an EMPTY 'themes': a catalog with no rows "
                         f"in it paints nothing, so it is refused rather than "
                         f"quietly replaced by {DEFAULT_THEME!r}")
    return {slug: _parse_theme(slug, raw) for slug, raw in themes.items()}


def ordered_themes(themes: Optional[dict] = None) -> list:
    """The catalog in `order` — what the picker lists, top to bottom.

    Sorted by the row's own `order` rather than by the file's key order, so a
    hand-edited catalog that happens to be alphabetical still shows the operator
    the order they wrote down. A tie falls back to the slug so the list is
    stable across runs.
    """
    catalog = load_catalog() if themes is None else themes
    return sorted(catalog.values(), key=lambda t: (t.order, t.slug))


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
#
# THE PREFERENCE LIVES BESIDE og-install.json, NOT INSIDE IT. That file is the
# INSTALLER's state and the source of truth for the generated bundle; `og agents`
# is standalone, runs in any terminal with no dependency on `og` being installed,
# and must not write into a file the installer rewrites from its own state — two
# writers and one file is a race nobody here is paid to debug. A separate small
# file under the same $OMNIGENT_HOME (the resolution `access_info` already uses)
# keeps the precedence and loses the coupling.

STATE_FILE = "agents-theme.json"
THEME_ENV = "OG_AGENTS_THEME"


def omnigent_home(environ: Optional[dict] = None) -> Path:
    """`$OMNIGENT_HOME`, or `~/.omnigent` — the same rule `access_info` follows."""
    env = os.environ if environ is None else environ
    return Path(env.get("OMNIGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".omnigent"))


def theme_state_path(environ: Optional[dict] = None) -> Path:
    """Where the chosen slug is kept."""
    return omnigent_home(environ) / STATE_FILE


def load_theme(environ: Optional[dict] = None) -> str:
    """The slug to start in, and NEVER a reason the pane cannot open.

    Precedence, most specific first: `$OG_AGENTS_THEME` (a shell export, set
    before the command so it applies to a run the operator did not type the theme
    into), then the stored slug, then the default (`og`). An unknown or corrupt
    stored slug is not an error either — a stale slug left by a catalog that has
    since dropped that theme is the NORMAL consequence of editing `themes.json`,
    and the right response is to open on the default rather than to refuse.
    """
    env = os.environ if environ is None else environ
    override = (env.get(THEME_ENV) or "").strip()
    if override:
        return override
    try:
        stored = json.loads(theme_state_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_THEME
    slug = stored.get("theme") if isinstance(stored, dict) else None
    return slug.strip() if isinstance(slug, str) and slug.strip() else DEFAULT_THEME


def save_theme(slug: str, environ: Optional[dict] = None) -> None:
    """Persist `slug`, replacing the file atomically.

    Atomic because the write is tiny and the crash window is real: a theme picker
    is the one part of this view an operator drives by muscle memory, and a
    half-written file read on the next launch is a corrupt slug, which then falls
    back to the default and looks like the setting was lost. `os.replace` within
    the same directory cannot leave a partial file behind.

    Best effort by design. A read-only `$OMNIGENT_HOME` is not a reason to throw
    away the theme the operator just chose — the pane repaints either way — so a
    failure to persist is swallowed rather than raised. Nothing here is load
    bearing enough to crash a TUI over.

    AND IT CLEANS UP AFTER ITSELF WHEN IT CANNOT FINISH. `mkstemp` has already
    made a file by the time the write can go wrong, and swallowing the failure
    while leaving that file behind is how a picker driven by muscle memory — the
    operator picks a theme, nothing happens, they pick another — fills a home that
    is read-only or full with `tmp*.tmp` nobody will ever read. The unlink is in a
    `finally`, is itself swallowed, and is skipped once `os.replace` has consumed
    the name.
    """
    path = theme_state_path(environ)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "theme": slug}, stream)
        os.replace(temporary, path)
        temporary = None                    # consumed; nothing left to remove
    except (OSError, ValueError):
        return
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def theme_for(slug: Optional[str] = None, environ: Optional[dict] = None) -> Theme:
    """The `Theme` for `slug`, falling back to the default when it is unknown.

    NAMED `theme_for`, NOT `resolve`, because `og_agents`' read-only AST guard
    (test_source_only_makes_read_only_calls) refuses any CALLED identifier that
    looks like a session writer, and `resolve` is one of the names it refuses —
    for herdr's `session.resolve`. Sharing a name with the thing a guard exists to
    keep out of this module is how you end up deleting the guard.

    `slug` None means "whatever is configured". An unknown slug ALSO falls back,
    for the reason `load_theme` gives: the catalog is editable and a stored slug
    can outlive its row. `--theme` does NOT fall back — it refuses by name,
    because a flag the operator typed and got silently ignored is worse than one
    that told them the list.
    """
    catalog = load_catalog()
    wanted = slug if slug is not None else load_theme(environ)
    theme = catalog.get(wanted)
    if theme is not None:
        return theme
    return catalog.get(DEFAULT_THEME) or next(iter(BUILTIN.values()))


def valid_slugs(themes: Optional[dict] = None) -> list:
    """Every slug, in catalog order — the list `--theme` names when it refuses."""
    return [theme.slug for theme in ordered_themes(themes)]