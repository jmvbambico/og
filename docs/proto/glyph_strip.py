#!/usr/bin/env python3
"""og agents - FAILED status-glyph strip: every candidate mark for the failed
slot, painted in the failed RED, in the real gutter position, so the operator
picks by eye in their own terminal.

The ask, in the operator's words: the failed glyph is `✗` and it is PLAIN. Make
it a HEAVIER mark and colour it RED, the way the finished `✓` is coloured neon
green. This prints the candidates side by side with the measurements that decide
which are even legal, and leaves the look to the operator.

    python3 glyph_strip.py                  # the default theme's red
    python3 glyph_strip.py --theme nord      # every theme's own red is different
    python3 glyph_strip.py --plain           # no colour, for a pipe or a log

WHAT IS SHIPPED IS `×` U+00D7 MULTIPLICATION SIGN, marked SHIPPED below. The
saltires were the default choice and were passed over for a measured reason, not
a taste one - see the RULER note.

WHICH RULER COMPOSES THE GUTTER, because the answer decides what is safe and the
two rulers are in the same file (`installer/og_agents.py`):

  * the LEN-BASED ruler (`_visible_width`, `_clip`) charges an East Asian
    AMBIGUOUS glyph ONE column. It is what composes the gutter: the state glyph
    is written as `glyph + " "` into a fixed slot (`og_agents.py:1755-1760`) and
    every span is measured with `_visible_width` as it paints
    (`og_agents.py:2738,2758`), and the box width is spent as `width - GUTTER`.
  * `_display_width` charges Ambiguous TWO and is used ONLY by the footer
    (`og_agents.py:1887-1889`).

So under the ruler that actually composes this column, `×` (Ambiguous) and `✓`
(Narrow) cost the SAME one column, and the swap is column-neutral. The
`DISPLAY` column below shows what the OTHER ruler would say, and the gap is the
point: it is exactly why the gutter must not be moved onto `_display_width`.

THE SALTIRES are all Narrow, so they are width-safe under EITHER ruler - but
Geometric Shapes Extended (U+1F7AB..U+1F7AE) is missing from most monospace
fonts, and a terminal that lacks the glyph substitutes a fallback whose ADVANCE
WIDTH is not one cell no matter what the width table says. That is the same
shear the pane keeps re-learning, arriving from the FONT instead of the ruler.
`×` is Latin-1 and cannot be substituted away. That is the whole of the choice.

The LEFT column is the gutter as the pane draws it: the NODE `⦿`, a space, the
state glyph, a space, then the box edge, so a mark that costs a different number
of columns is visible as the box edge landing in a different place - which is
the failure, rendered instead of asserted.

Nothing here changes the live view. This is a design prototype.
"""
import argparse
import os
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "installer"))

import og_agents as a       # noqa: E402  (the gutter's own rulers)
import og_themes as th      # noqa: E402  (the catalog, and hex -> 256)

# The candidates, every one already measured for width. `SHIPPED` is the pick,
# `OLD` is what it replaces; the width columns are filled in from live
# measurement rather than trusted from this table.
CANDIDATES = [
    ("\U0001F7AB", "BOLD SALTIRE", ""),
    ("\U0001F7AC", "HEAVY SALTIRE", ""),
    ("\U0001F7AD", "VERY HEAVY SALTIRE", ""),
    ("\U0001F7AE", "EXTREMELY HEAVY SALTIRE", ""),
    ("\u2715", "MULTIPLICATION X", ""),
    ("\u00d7", "MULTIPLICATION SIGN", "SHIPPED"),
    ("\u2717", "BALLOT X", "OLD"),
]

# U+2713 CHECK MARK: the FINISHED glyph, the reference row. It is painted in the
# theme's IDLE colour, so the failed red can be judged against the green it has
# to sit beside down the gutter.
REFERENCE = ("\u2713", "CHECK MARK", "REFERENCE")


def _sgr(fg):
    """A 256-colour foreground escape, or nothing when colours are off."""
    return "" if fg is None else "\x1b[38;5;%dm" % fg


def _reset(on):
    return "" if not on else "\x1b[0m"


def failed_colour(theme):
    """The 256 index the failed mark paints under `theme`, or None for `og`.

    `og`'s failed red is 196 - the cube's pure-red corner, the same discipline
    that makes its green 46 and its yellow 226. Every other theme paints its own
    published red; the strip reads the same row the pane reads.
    """
    try:
        return th.hex_to_ansi256(theme.colour("failed"))
    except Exception:
        return None


def idle_colour(theme):
    try:
        return th.hex_to_ansi256(theme.colour("idle"))
    except Exception:
        return None


def row(mark, name, note, fg, plain, width=46):
    """One candidate: the gutter as the pane draws it, then its measurements.

    The gutter half is `⦿ ` + glyph + ` ` + `│` + a title, so the box edge's
    column is the thing under the eye: a mark one column too wide pushes it, and
    that is the shear this whole section exists to prevent.
    """
    gutter = "\u29bf " + mark + " \u2502 fix-auth-race"
    painted = _sgr(fg) + gutter + _reset(not plain)
    if plain:
        painted = gutter
    eaw = unicodedata.east_asian_width(mark)
    return "  %-*s  %-9s eaw=%s  len=%d  gutter=%d  display=%d  slot=%d  %s" % (
        width, painted, "U+%04X" % ord(mark), eaw,
        len(mark), a._visible_width(mark), a._display_width(mark),
        a._slot_width(mark), ("%-14s %s" % (note, name)).strip())


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--theme", default="dracula",
                    help="theme whose failed red to paint (default: dracula)")
    ap.add_argument("--width", type=int, default=46, help="pane width")
    ap.add_argument("--plain", action="store_true",
                    help="no colour: for a pipe or a log")
    args = ap.parse_args()

    catalog = th.load_catalog()
    theme = catalog.get(args.theme) or catalog[th.DEFAULT_THEME]
    red_idx, idle_idx = failed_colour(theme), idle_colour(theme)
    fg = None if args.plain else red_idx
    idle_fg = None if args.plain else idle_idx

    ruler = "  len-based `_visible_width` (the gutter's) and `_display_width` "\
            "(the footer's, shown to show the disagreement)"
    print()
    print("  og agents - FAILED glyph strip   theme=%s   red=cell %s" % (
        theme.slug, red_idx))
    print("  " + ruler)
    print("  " + "-" * (args.width + 34))
    for mark, name, note in CANDIDATES:
        print(row(mark, name, note, fg, args.plain, args.width))
    print(row(*REFERENCE, idle_fg, args.plain, args.width))
    print("  " + "-" * (args.width + 34))
    print("  The SHIPPED `\u00d7` costs the same gutter column as `\u2713` under "
          "the ruler that composes the")
    print("  gutter. The `display` column is the OTHER ruler and is expected to "
          "disagree on an")
    print("  Ambiguous glyph - that disagreement is why the gutter does not use "
          "it.")
    print()


if __name__ == "__main__":
    main()
