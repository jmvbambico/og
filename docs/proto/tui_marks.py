#!/usr/bin/env python3
"""og agents - HARNESS MARKS prototype: one brand mark per harness vendor, in
three tiers, so the operator can see in THEIR OWN terminal whether a mark is
viable here and pick a tier.

The complaint this answers, in the operator's words: the pane says "Claude
Code" in letters, and letters are slow. "The orange starburst" is faster.

    python3 tui_marks.py              # every tier, width 46 (the real pane)
    python3 tui_marks.py --width 80   # a wider pane
    python3 tui_marks.py --width 20   # the narrow end, where marks get squeezed

THE TIERS, same agents, same layout, so they compare directly:

  TIER 0  OPERATOR PICKS. Fourteen glyphs the operator chose by hand, rendered
          exactly as given - not as proposals, not improved. EVERY VENDOR now
          has a mark here: no blanks remain. Measured with the same
          `unicodedata` this file lays out with, eleven are East Asian NARROW
          (one cell) and three are AMBIGUOUS (U+1F13A, U+26EF, U+2554 - one
          cell in a western terminal, two in a CJK-locale one). Dropping the
          crab and the whale removed BOTH WIDE picks, so tier 0 no longer has
          a single two-cell glyph - but the FIXED 2-COLUMN MARK SLOT is still
          required, because `slot_cells` charges Ambiguous two cells on
          purpose. See section 6 for which of these a stock monospace font
          will actually have.
  TIER 1  plain Unicode. One column, no emoji, a font shipped with every
          terminal since about 1990. Nine of the fifteen agents have an honest
          mark here. Kept whole and unchanged: it is the all-Narrow
          alternative to tier 0, and the thing to fall back to if a tier-0
          mark turns out to be tofu on the operator's terminal.
  TIER 2  the real brand glyph from the Nerd Font private-use area, where one
          exists. In a plain font this section is a row of TOFU BOXES, and
          that is the point: it is the operator's font test, not a proposal.
  TIER 3  `[CC]`. Four ASCII columns, no dependency at all, for `--once`
          output that gets piped into a log where a starburst is a mystery.

Plus six numbered sections the tiers cannot answer alone, and the two older
ones (A, the mark in the layouts beside the real capture; B, brand colour
with the real ANSI 256 escapes):

  1  THE TIER-0 ROSTER - every mark in its slot, its codepoint, its width
     class, and whether the operator named it or it came from tier 1.
  2  DELIBERATE BREAKAGE - the same rows WITHOUT the slot, printed on
     purpose, directly above the same rows WITH it. This is the section that
     justifies the slot: the shear is rendered, not asserted.
  3  THE MARKS IN CONTEXT - V1/V2/V5 with the tier-0 mark in the ROOT HEADER,
     plus W1 WEB-PARITY, the new variant from tui_beauty.py, rendered here
     with the tier-0 mark on the header AND on every worker row. Section A's
     per-worker comparison is kept below as the evidence for why V1/V2/V5
     stay root-only; W1 is the one layout the marks do earn a column in.
  4  U+2554 BESIDE A REAL BOX CORNER - a box-drawing glyph inside a pane made
     of box-drawing. Rendered for the operator to judge, with the risk stated.
  5  COLOUR INTERACTION - which tier-0 marks take the row attribute and which
     draw their own colour out of the font's colour table.
  6  FONT COVERAGE - every pick with its codepoint, its plane and its Unicode
     block, beside a deliberately UNASSIGNED codepoint as a control: a mark
     that looks like the control is tofu. The slot is what keeps that miss
     cosmetic.

WIDTH IS MEASURED, NOT ASSUMED. `len(line)` is the wrong ruler three times
over: it counts ANSI escapes as visible characters, it calls a double-width
glyph one column wide, and it calls every one of these glyphs ONE character.
`_visible_width` below strips escapes and asks `unicodedata.east_asian_width`,
the same approach `installer/og_agents.py` uses for its QR panel.

The slot itself is the one place where "one column" is not good enough: a
Narrow mark next to an Ambiguous one starts the name column at two different
offsets and every rule, box edge and right-aligned clock shears with it.
`mark_slot` pads each mark to exactly two display columns - a Narrow mark is
followed by one space, a blank vendor is two spaces, and an AMBIGUOUS mark is
charged two without being padded, because Ambiguous is one cell in a western
terminal and two in a CJK-locale one and only the pessimistic reading is safe
in both. `check_marks()` ASSERTS the invariant instead of trusting it: tier-1
marks must still be Narrow or Neutral, and a tier-0 mark is legal at any width
ONLY if it goes through the slot and lands on exactly two columns under the
pessimistic ruler.

Nothing here changes the live view. This is a design prototype.
"""
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
REGISTRY = os.path.join(REPO, "installer", "registry.json")


# ----------------------------------------------------------- display width
# The same `_ANSI_SGR` shape og_agents.py:639 strips, for the same reason: an
# escape is zero columns wide and `len()` cannot know that.
_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")

# East Asian Wide and Fullwidth are the two classes a terminal draws in TWO
# cells. `A` (Ambiguous) is one cell in a western terminal and two in a
# CJK-locale one, which is why tier 1 refuses it outright (see check_marks)
# and why tier 2, whose glyphs are all private use - all Ambiguous - is its own
# tier rather than a "better" tier 1.
_WIDE = ("W", "F")

# The slot's own ruler. Identical to `_WIDE` plus `A`: inside a mark slot an
# Ambiguous glyph is charged two cells, pessimistically. See `slot_cells`.
_WIDE_SLOT = ("W", "F", "A")

SLOT_COLS = 2


def _visible_width(line):
    """Display columns of one rendered line: escapes stripped, wide glyphs 2."""
    total = 0
    for ch in _ANSI_SGR.sub("", line):
        if unicodedata.combining(ch):
            continue                      # a combining mark adds no cell
        total += 2 if unicodedata.east_asian_width(ch) in _WIDE else 1
    return total


def _cells(ch):
    """How many terminal cells one character occupies; 0 for a combining mark."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in _WIDE else 1


def slot_cells(ch):
    """How many cells one character occupies INSIDE THE MARK SLOT.

    Same as `_cells`, except East Asian AMBIGUOUS counts as two. `A` has no
    single true width - one cell in a western terminal, two in a CJK-locale one
    - so there is no number that is right in both, and the choice has to be
    made on which failure is survivable. Two is that choice: pessimistically a
    row can land one column NARROW (visible, cosmetic, and it only happens on
    the one ambiguous row), optimistically it lands one column PAST the pane's
    right edge and the terminal wraps it, which is the failure an operator
    reads as "the agents pane is broken". Every box-drawing glyph in U+2550-
    U+257F is `A`, so this is not a corner case - it is most of the block a
    pane is drawn from.
    """
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in _WIDE_SLOT else 1


def slot_width(s):
    """Display columns of a slot string under the PESSIMISTIC ruler.

    The ruler the slot invariant is asserted against. It differs from
    `_visible_width` on exactly one input class (Ambiguous), and that
    difference is the whole trade described in section 2: a line measured
    this way is never wider than the terminal will draw it.
    """
    return sum(slot_cells(c) for c in _ANSI_SGR.sub("", s))


def mark_slot(mark):
    """`mark` padded to exactly SLOT_COLS display columns. THE slot.

    An Ambiguous mark in one cell on the row above a Narrow mark in two makes
    the name column after it start at two different offsets, and with it every
    rule, box edge and right-aligned clock downstream shears - section 2
    prints that failure deliberately, because the operator should see it rather
    than take this function's word for it. Padding to a fixed slot is what
    removes it:

        NARROW     followed by one space   ⌘·         1 + 1
        BLANK      two spaces              ··         2
        AMBIGUOUS  NOT padded - `slot_cells` already charges it two, so the
                   slot holds one character that measures 1 here and 2 under
                   a CJK locale. That row is the one honest casualty of the
                   pessimistic ruler and section 2 names it out loud.

    There is no WIDE line any more: the crab and the whale were replaced in
    this round and tier 0 now has no glyph a western terminal draws two cells
    wide. The slot does not become optional - Ambiguous is still charged two -
    but the failure it now exists to prevent is the CJK one, not the emoji one.

    NEVER `len()` here, which is the mistake section 2's top half makes on
    purpose: every mark in tier 0 is ONE python character, whatever it draws
    as, so `len()` is precisely the ruler that cannot see this problem.
    """
    if not mark or mark == " ":
        return " " * SLOT_COLS
    return mark + " " * max(0, SLOT_COLS - slot_cells(mark[0]))


def slot_pad(s, w):
    """Pad to `w` slot columns, not `w` western columns - see `slot_cells`."""
    return s + " " * max(0, w - slot_width(s))


def width_class(mark):
    """One-word width class for the roster table: what the slot has to do."""
    if not mark or mark == " ":
        return "blank - 2 spaces in the slot"
    return {
        "W": "WIDE 2 cells",
        "F": "FULLWIDTH 2 cells",
        "A": "AMBIGUOUS - 1 or 2, slot assumes 2",
        "N": "NARROW 1 cell",
        "Na": "NARROW 1 cell",
        "H": "HALFWIDTH 1 cell",
    }.get(unicodedata.east_asian_width(mark),
           "%s 1 cell" % unicodedata.east_asian_width(mark))


def _tokens(s):
    """(text, is_escape) runs. Escapes are carried whole and never cut in half.

    Section B's marks arrive already wrapped in a real SGR escape, and a clip()
    that walked characters would count `\x1b[38;5;173m` as thirteen columns and
    cut the mark off the front of its own line. Stepping over escapes keeps one
    ruler for the whole file: the same measurement paints, clips and asserts.
    """
    pos = 0
    for m in _ANSI_SGR.finditer(s):
        if m.start() > pos:
            yield s[pos:m.start()], False
        yield m.group(0), True
        pos = m.end()
    if pos < len(s):
        yield s[pos:], False


def clip(s, w):
    if w <= 0:
        return ""
    if _visible_width(s) <= w:
        return s
    out, used = [], 0
    for text, is_escape in _tokens(s):
        if is_escape:
            out.append(text)
            continue
        for ch in text:
            cw = _cells(ch)
            if used + cw > w - 1:
                return "".join(out) + "…"
            out.append(ch)
            used += cw
    return "".join(out)


def pad(s, w):
    return clip(s, w) + " " * max(0, w - _visible_width(clip(s, w)))


def rule(w, ch="─"):
    return ch * max(0, w)


def wrap(text, width):
    if width <= 0:
        return [text]
    out, cur = [], ""
    for word in text.split():
        cw = _visible_width(word)
        if not cur:
            cur = word
        elif _visible_width(cur) + 1 + cw <= width:
            cur += " " + word
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out or [""]


def two_col(left, right, w):
    """`left` flush left, `right` flush right, exactly `w` display columns."""
    if w <= 0:
        return ""
    right = clip(right, w)
    room = w - _visible_width(right)
    if room <= 0:
        return right[:w]
    return pad(left, room) + right


def para(text, w, indent=2):
    """Prose wrapped to `w`, indented, never over the width.

    `wrap` cannot break a single word longer than the width - `installer/registry.json`
    at width 20 is one - so each segment is clipped after wrapping. That is the
    only place this file truncates text: a prose line, never a box row.
    """
    return [" " * indent + clip(seg, max(0, w - indent))
            for seg in wrap(text, max(1, w - indent))]


# ------------------------------------------------------------- the catalog
# One row per VENDOR, in installer/registry.json order, not per agent: gemini
# and agy are both `vendor: google`, so a mark keyed by agent would hand the
# same company two different glyphs and make the pane lie about cross-vendor
# review. The agent list under each vendor is printed so nothing is hidden by
# the grouping.
#
# A mark is only here if it is a recognisable SKELETON of the real logo. Where
# the brand has no shape that fits one cell the row is an honest BLANK, and
# the blank is more useful than a logo nobody would recognise in a 46-column
# pane - six of the fourteen vendors are blank, and that is a finding, not a
# gap to be papered over with invented symbols.
VENDORS = [
    {
        "vendor": "anthropic",
        "agents": ["claude"],
        # Claude Code is the orange starburst: a ring of tapered rays. Of the
        # asterisk family (✳ ✵ ✶ ✻ ⁂) U+2733 is the one that reads as a
        # STARBURST rather than as punctuation - U+2042 ASTERISM is a footnote
        # mark and U+273B's teardrop spokes read as a flower. An alien would be
        # a cute idea and a lie about what the pane is running.
        "t1": "✳", "t1_name": "U+2733 EIGHT SPOKED ASTERISK",
        "t1_why": "the orange starburst, the one mark the operator already says",
        # nf-cod-claude IS Anthropic's mark, shipped in the Codicons half of
        # the Nerd Fonts set (glyphnames.json, master).
        "t2": "\uec82", "t2_name": "nf-cod-claude  U+EC82",
        "t2_why": "Anthropic's own glyph, Codicons set",
        # Claude's documented brand coral, the "Claude orange" of claude.ai and
        # the Claude Code CLI prompt. See nearest_256 for what it lands on.
        "hex": "#D97757",
    },
    {
        "vendor": "opencode-zen",
        "agents": ["opencode"],
        # BLANK, and the reason matters more than the glyph would. OpenCode is
        # a multi-provider router: `opencode models` lists deepseek/... and
        # anthropic/... alongside Zen's own ids. Giving it a mark here - and
        # especially a Claude-orange one - would draw the operator's eye as
        # cross-vendor review assurance that the registry's `vendor` field does
        # not support. The pane is where that mistake would be read.
        "t1": " ", "t1_name": "-",
        "t1_why": "no honest mark: a multi-provider router has no one logo",
        "t2": " ", "t2_name": "-",
        "t2_why": "no PUA glyph in the Nerd Fonts set",
        # Zen's own brand has no published flat hex either; grey is honest.
        "hex": "#8B8B8B",
    },
    {
        "vendor": "google",
        "agents": ["agy", "gemini"],
        # Google's four-pointed sparkle - the Gemini mark since 2022, and the
        # only Google logo that is not the letter G. U+2726 rather than
        # U+2605 BLACK STAR: five points is a different company.
        "t1": "✦", "t1_name": "U+2726 BLACK FOUR POINTED STAR",
        "t1_why": "Google's four-pointed sparkle, shared by agy and gemini",
        "t2": "\uf02ad", "t2_name": "nf-md-google  U+F02AD",
        # The obvious trap, worth naming: nf-md-zodiac_gemini (U+F0A81) is the
        # ZODIAC sign Gemini. It looks right and means the wrong thing.
        "t2_why": "the G (nf-md-zodiac_gemini is the horoscope sign, not this)",
        # Google blue, the four-colour mark's dominant channel.
        "hex": "#4285F4",
    },
    {
        "vendor": "openai",
        "agents": ["codex"],
        # The hexagonal rosette, reduced to its outline: U+2B21 WHITE HEXAGON
        # is Neutral width, so it is one cell on every terminal. The interlocked
        # ribbons inside the hexagon are the part no Unicode glyph carries.
        "t1": "⬡", "t1_name": "U+2B21 WHITE HEXAGON",
        "t1_why": "the hexagonal rosette's outline; the knot inside is not drawable",
        "t2": "\uec81", "t2_name": "nf-cod-openai  U+EC81",
        "t2_why": "the real OpenAI glyph, Codicons set",
        # OpenAI's green.
        "hex": "#10A37F",
    },
    {
        "vendor": "deepseek",
        "agents": ["cline"],
        # DeepSeek's logo is a blue whale over three wave strokes. There is no
        # one-column whale in Unicode (the birds and whales are all emoji, and
        # emoji are double-width), so this is the honest half of the mark: the
        # water under the animal.
        "t1": "≋", "t1_name": "U+224B TRIPLE TILDE",
        "t1_why": "the wave strokes under the whale; the whale itself is emoji-only",
        "t2": " ", "t2_name": "-",
        "t2_why": "no PUA glyph in the Nerd Fonts set",
        # DeepSeek's blue.
        "hex": "#4D6BFE",
    },
    {
        "vendor": "kilo",
        "agents": ["kilo"],
        # BLANK: Kilo Code's mark is a rounded tile carrying the letter K. A
        # one-cell glyph that is a letter is just the letter, one column worse.
        "t1": " ", "t1_name": "-",
        "t1_why": "no honest mark: the logo is a letter K inside a rounded tile",
        "t2": " ", "t2_name": "-",
        "t2_why": "no PUA glyph in the Nerd Fonts set",
        "hex": "#6E56CF",
    },
    {
        "vendor": "aws-kiro",
        "agents": ["kiro"],
        # AWS's one logo that is not a wordmark is the smile arrow, pointing up
        # and to the right. U+21D7 rather than U+2197: ↗ is East Asian
        # AMBIGUOUS, which is one column on this machine and two on a
        # CJK-locale terminal - the exact silent misalignment check_marks()
        # exists to stop. Direction, not the smile.
        "t1": "⇗", "t1_name": "U+21D7 NORTH EAST DOUBLE ARROW",
        "t1_why": "the AWS smile arrow's direction; ↗ itself is Ambiguous-width",
        "t2": "\uf0e0f", "t2_name": "nf-md-aws  U+F0E0F",
        "t2_why": "AWS (also nf-fa-aws U+F0EF, dev-amazonwebservices U+E7AD)",
        # AWS orange.
        "hex": "#FF9900",
    },
    {
        "vendor": "cursor",
        "agents": ["cursor"],
        # Cursor's mark is a solid black cube in isometric. U+25F2 is the
        # closest one-cell skeleton: a square with its near corner filled, so
        # the glyph still reads as a BLOCK and not as a letter or a bullet.
        "t1": "◲", "t1_name": "U+25F2 WHITE SQUARE WITH LOWER RIGHT QUADRANT",
        "t1_why": "the cube's near corner; a solid block at one column",
        "t2": "\uec5c", "t2_name": "nf-cod-cursor  U+EC5C",
        "t2_why": "Cursor's own glyph, Codicons set",
        # Cursor is monochrome by design (#000 / #FFF). A neutral grey is the
        # only honest mapping into a 256-colour ramp; see the colour section.
        "hex": "#3C3C3C",
    },
    {
        "vendor": "goose",
        "agents": ["goose"],
        # BLANK: Block's goose is a hand-drawn bird, and every Unicode bird is
        # an emoji (double-width, banned by tier 1's own rule) or an obscure
        # Seti/duckdb icon that would mean "duckdb" to whoever sees it.
        "t1": " ", "t1_name": "-",
        "t1_why": "no honest mark: a drawn goose; every Unicode bird is emoji",
        "t2": " ", "t2_name": "-",
        "t2_why": "no PUA glyph in the Nerd Fonts set",
        "hex": "#101010",
    },
    {
        "vendor": "hermes",
        "agents": ["hermes"],
        # BLANK: the mark is a winged helmet. There is no one-cell winged
        # anything in Unicode, and ⌘ (a command key) would say "Apple".
        "t1": " ", "t1_name": "-",
        "t1_why": "no honest mark: a winged helmet has no one-cell glyph",
        "t2": " ", "t2_name": "-",
        "t2_why": "no PUA glyph in the Nerd Fonts set",
        "hex": "#6F42C1",
    },
    {
        "vendor": "xai",
        "agents": ["grok"],
        # xAI's mark is an X inside a broken circle: U+2297 CIRCLED TIMES is
        # that drawing, reduced to the two strokes that carry it.
        "t1": "⊗", "t1_name": "U+2297 CIRCLED TIMES",
        "t1_why": "the X in the broken circle",
        "t2": " ", "t2_name": "-",
        "t2_why": "no PUA glyph in the Nerd Fonts set",
        "hex": "#FFFFFF",
    },
    {
        "vendor": "cognition",
        "agents": ["devin"],
        # BLANK: Devin's mark is a wordmark and a glyph that only means
        # anything inside Cognition's own product chrome.
        "t1": " ", "t1_name": "-",
        "t1_why": "no honest mark: a product wordmark, not a shape",
        "t2": " ", "t2_name": "-",
        "t2_why": "no PUA glyph in the Nerd Fonts set",
        "hex": "#0F0F0F",
    },
    {
        "vendor": "z-ai",
        "agents": ["freebuff"],
        # BLANK: z.ai's mark is a Z wordmark. Z is a letter, and a one-column
        # letter standing in for a wordmark is worse than nothing - it reads as
        # a bullet, and the operator cannot tell it from a state glyph.
        "t1": " ", "t1_name": "-",
        "t1_why": "no honest mark: a Z wordmark would read as a bullet",
        "t2": " ", "t2_name": "-",
        "t2_why": "no PUA glyph in the Nerd Fonts set",
        "hex": "#2E6BE6",
    },
    {
        "vendor": "moonshotai",
        "agents": ["cmdcode"],
        # Moonshot AI's mark is the crescent. U+263E rather than U+263D: the
        # lit side faces away, which is the orientation the brand draws.
        "t1": "☾", "t1_name": "U+263E LAST QUARTER MOON",
        "t1_why": "the Moonshot crescent, the orientation the brand draws",
        "t2": " ", "t2_name": "-",
        "t2_why": "no PUA glyph in the Nerd Fonts set",
        "hex": "#1F1F1F",
    },
]

# ----------------------------------------------------------- tier 0: the
# operator's own picks. THESE ARE DECISIONS, NOT SUGGESTIONS: rendered exactly
# as given, with no substitution for a "better" glyph and no fallback to tier
# 1 when the glyph is inconvenient. Fourteen picks for fourteen vendors - this
# round closed the last four blanks - and NOT ONE of them is East Asian WIDE:
# the crab and the whale are gone, which removed both two-cell picks at once.
# What is left is eleven Narrow marks and three Ambiguous ones (kilo U+1F13A,
# cognition U+26EF, z-ai U+2554), measured with `unicodedata.east_asian_width`
# and asserted in check_marks(). The 2-column slot is still required, because
# `slot_cells` charges Ambiguous two on purpose; what changed is WHICH failure
# it prevents. Section 6 is the font-coverage bill for five of these fourteen.
#
# `why` says what the glyph IS, what it replaced, and what it costs at the
# slot. It does not argue that the glyph is better than the tier-1 pick on the
# row below it, and it does not argue the other way either: on several rows the
# operator's pick IS tier 1's mark (anthropic, google, openai), on one it is a
# different glyph for the same brand (cursor takes the arrow aws-kiro used),
# and on one it is a glyph tier 1 left honestly blank. All three are outcomes
# worth showing rather than resolving.
OPERATOR_PICKS = {
    "moonshotai": {
        "mark": "⌘", "why": "KEPT. the map place-of-interest sign, standing in "
        "for cmdcode; it is also the command key, so it reads as the CLI you "
        "type. Narrow"},
    "deepseek": {
        "mark": "\U0001319E", "why": "REPLACES the whale (U+1F40B). an Egyptian "
        "hieroglyph, K004 - a different skeleton for the brand than the animal "
        "tier 1 had to drop. Narrow, and astral: see section 6"},
    "anthropic": {
        "mark": "✳", "why": "REPLACES the crab (U+1F980), and returns tier 1's "
        "own starburst - the one mark the operator already says out loud. The "
        "slot now costs it the same two columns the crab did, with no emoji "
        "font involved"},
    "google": {
        "mark": "✦", "why": "KEPT, unchanged from tier 1 - the one row where "
        "the operator's pick and the safe default are the same glyph"},
    "openai": {
        "mark": "⬡", "why": "KEPT, unchanged from tier 1 - the hexagonal "
        "rosette's outline, Neutral width, so the slot costs it nothing"},
    "opencode-zen": {
        "mark": "▯", "why": "KEPT. a white vertical rectangle: a multi-provider "
        "router has no one logo, so the operator drew the empty slot itself"},
    "z-ai": {
        "mark": "╔", "why": "KEPT. a box-drawing corner standing in for a Z "
        "wordmark. Ambiguous width; section 4 renders it beside a real corner"},
    "kilo": {
        "mark": "\U0001F13A", "why": "NEW - tier 1 left this row blank for want "
        "of a one-cell mark of a letter K in a tile. AMBIGUOUS: one cell here, "
        "two under a CJK locale, so the slot charges it two. Astral, and "
        "outside the BMP: see section 6"},
    "aws-kiro": {
        "mark": "\U0001680C", "why": "REPLACES U+21D7, which tier 1 used and "
        "which the operator has now given to cursor. A Bamum letter, narrow "
        "but astral: see section 6"},
    "cursor": {
        "mark": "⇗", "why": "REPLACES U+25F2 (the cube's near corner). The "
        "double arrow is tier 1's own mark for aws-kiro - it moves across "
        "rather than being invented, and the two rows no longer collide "
        "because aws-kiro's pick is now U+1680C. Narrow"},
    "goose": {
        "mark": "\U0001316F", "why": "NEW - tier 1 left this row blank because "
        "every Unicode bird is an emoji or an obscure icon. G041 is a G-series "
        "bird glyph, so the mark is a bird without the emoji font. Narrow, and "
        "astral: see section 6"},
    "hermes": {
        "mark": "☤", "why": "NEW - tier 1 left this row blank because a winged "
        "helmet has no one-cell skeleton in Unicode. Caduceus, BMP, narrow"},
    "xai": {
        "mark": "\U0001D54F", "why": "REPLACES U+2297 CIRCLED TIMES. The "
        "double-struck X drops the circle and keeps the letter. Narrow, but "
        "astral and in Mathematical Alphanumeric Symbols: see section 6"},
    "cognition": {
        "mark": "⛯", "why": "NEW - tier 1 left this row blank because Devin's "
        "mark is a wordmark. The lighthouse is the brand's shape in one "
        "glyph. AMBIGUOUS: one cell here, two under a CJK locale, so the slot "
        "charges it two"},
}

# The same round, frozen as plain codepoints. `check_marks()` compares the table
# above against this, so the picks are a decision with a test on it rather than
# a comment that quietly stops matching. Written out rather than derived from
# OPERATOR_PICKS on purpose: derived from it, it would assert nothing at all.
EXPECTED_T0 = {
    "anthropic": "✳",
    "opencode-zen": "▯",
    "google": "✦",
    "openai": "⬡",
    "deepseek": "\U0001319E",
    "kilo": "\U0001F13A",
    "aws-kiro": "\U0001680C",
    "cursor": "⇗",
    "goose": "\U0001316F",
    "hermes": "☤",
    "xai": "\U0001D54F",
    "cognition": "⛯",
    "z-ai": "╔",
    "moonshotai": "⌘",
}

# The operator's own width measurement of the round, restated as data. Eleven
# Narrow and these three Ambiguous, which is what `east_asian_width` says about
# U+1F13A, U+26EF and U+2554 on this machine - verified, not assumed, and
# checked on every run so a Unicode data upgrade that changes a class is caught
# here instead of in somebody's terminal.
EXPECTED_AMBIGUOUS = {"kilo": "A", "cognition": "A", "z-ai": "A"}

# Emoji-presentation codepoints draw from the FONT's colour table and cannot be
# recoloured by a curses colour pair. Listed rather than inferred from
# `ord(m) >= 0x1F000` because that range also holds text-default glyphs, and
# because section 5's self-coloured/inherits split is only true if this set is
# exactly right. A hand-maintained list is the honest form: `unicodedata` does
# not expose Emoji_Presentation.
#
# IT IS EMPTY NOW, and that is a measurement rather than an oversight: the crab
# and the whale were the only two Wide picks tier 0 had, and the operator
# replaced both with Narrow glyphs. Every tier-0 mark is a monochrome text
# glyph, so every one of them takes the row attribute and none self-colours.
# The list stays because the rule stays: a tier-0 mark is legal at any width, so
# re-adding an emoji is one edit away, and an unclassified Wide mark would make
# section 5's numbers a coincidence. check_marks() asserts both directions.
EMOJI_PRESENTATION = ()

# Which tier-0 picks a stock monospace font is unlikely to carry, and why. This
# is not a judgement about the picks - it is a statement about font coverage.
# Five of the fourteen are outside the BMP, and outside the BMP is where the
# coverage cliff is: fonts are built block by block, and a project that carries
# Latin, Greek, Cyrillic, Box Drawing, Dingbats and Mathematical Operators has
# no reason whatsoever to carry Egyptian Hieroglyphs or Bamum.
#
# check_marks() asserts the list against the marks: every non-BMP pick must be
# declared here, and every entry here must really be non-BMP, so a future pick
# cannot quietly join the table without a coverage note attached.
FONT_RISK = {
    "deepseek": "an SMP historic script",
    "goose": "an SMP historic script",
    "aws-kiro": "an SMP historic script",
    "xai": "a styled-letter block, commonly missing",
    "kilo": "a squared-letter block, commonly missing",
}

# Range -> block name, for section 6. `unicodedata` exposes no block API, so
# the ranges that matter here are written out; only the blocks tier 0 actually
# touches are listed, and an unlisted codepoint prints as "BMP block".
_BLOCKS = (
    (0x2190, 0x21FF, "Arrows"),
    (0x2300, 0x23FF, "Miscellaneous Technical"),
    (0x2500, 0x257F, "Box Drawing"),
    (0x25A0, 0x25FF, "Geometric Shapes"),
    (0x2600, 0x26FF, "Miscellaneous Symbols"),
    (0x2700, 0x27BF, "Dingbats"),
    (0x2B00, 0x2BFF, "Miscellaneous Symbols and Arrows"),
    (0xE000, 0xF8FF, "Private Use Area"),
    (0x13000, 0x1342F, "Egyptian Hieroglyphs"),
    (0x16800, 0x16A3F, "Bamum Supplement"),
    (0x1D400, 0x1D7FF, "Mathematical Alphanumeric Symbols"),
    (0x1F100, 0x1F1FF, "Enclosed Alphanumeric Supplement"),
    (0x1F000, 0x1FAFF, "Emoji blocks"),
)


def plane_of(cp):
    """0 = BMP, 1 = SMP, 2 = SMP Supplement, 3 = planes 3-16."""
    if cp < 0x10000:
        return 0
    if cp < 0x20000:
        return 1
    if cp < 0x30000:
        return 2
    return 3


def plane_name(cp):
    return ("BMP", "SMP", "SMP ext", "plane %d" % plane_of(cp))[plane_of(cp)]


def block_of(cp):
    for lo, hi, name in _BLOCKS:
        if lo <= cp <= hi:
            return name
    return "?"


# U+0378 is the first codepoint the standard never assigns, so a terminal that
# has no glyph for it draws the same substitute it draws for every codepoint it
# does not have. Printing it beside each mark is the only font test that needs
# no font-config tooling and no second run: if the two cells match, the mark on
# the left is missing too. Some terminals draw unassigned as blank rather than
# as tofu, and on those the control reads as an empty cell - still not the mark,
# still a usable answer.
UNASSIGNED = "\u0378"


def codepoint_of(mark):
    """`U+XXXX` for one mark, or the empty string for a blank."""
    return "U+%04X" % ord(mark) if mark and mark != " " else "-"


def t0_of(vendor):
    """The tier-0 row for one vendor: operator pick, else tier 1, else blank.

    A vendor the operator did not name keeps whatever tier 1 already decided,
    which in tier 1 is an honest blank on six of the fourteen. Nothing here
    invents a glyph for them: an un-named row that quietly gained a mark would
    be the pane asserting a vendor claim nobody made. Every vendor IS named
    now, so the first branch is only reachable for a vendor that is not in the
    catalog at all - which is what a missing `installer/registry.json` looks
    like, and it gets the same honest blank rather than a crash.
    """
    row = by_id().get(vendor, {})
    pick = OPERATOR_PICKS.get(vendor)
    if pick:
        return {"mark": pick["mark"], "src": "OPERATOR PICK", "why": pick["why"]}
    if row.get("t1", " ") == " ":
        return {"mark": " ", "src": "DEFAULT - blank",
                "why": "the slot holds two spaces, not a glyph: no operator "
                       "pick, and no honest mark in tier 1 either"}
    return {"mark": row["t1"], "src": "DEFAULT - tier 1's",
            "why": "no operator pick, so tier 1's mark stands unchanged in the "
                   "slot"}


def slot_for(vendor):
    """The vendor's tier-0 mark, already in its two-column slot."""
    return mark_slot(t0_of(vendor)["mark"])


def _t0_marks():
    """Every non-blank tier-0 mark, blanks excluded (they are not glyphs)."""
    return [t0_of(v["vendor"])["mark"] for v in VENDORS
            if t0_of(v["vendor"])["mark"] != " "]


def _t0_emoji():
    return sum(1 for m in _t0_marks() if m in EMOJI_PRESENTATION)


def _t0_monochrome():
    return sum(1 for m in _t0_marks() if m not in EMOJI_PRESENTATION)


# Tier 3 is keyed by AGENT rather than vendor, because a bracketed tag is
# allowed to be a short name of the PRODUCT (the thing the operator says out
# loud) and two agents on one vendor must stay distinguishable - [GEM] and
# [AGY] are the whole reason tier 3 is not `[GOO]`.
# Which vendor's mark the ROOT header carries in section A. og installs
# `claude` as the orchestrator; a different bundle changes this one line.
ORCHESTRATOR_VENDOR = "anthropic"

AGENT_TAGS = {
    "claude": "CC", "opencode": "OC", "agy": "AGY", "codex": "CX",
    "cline": "CLN", "kilo": "KIL", "kiro": "KIR", "cursor": "CRS",
    "gemini": "GEM", "goose": "GOO", "hermes": "HER", "grok": "GRK",
    "devin": "DEV", "freebuff": "FB", "cmdcode": "CMD",
}

# The worker rows in the capture name an AGENT (`coder_zen:task`) or a ROLE
# (`reviewer:task`). A role is not a harness and must never borrow one: a mark
# on a reviewer row would claim a vendor the row does not have.
ROLES = {"coder", "reviewer", "scout", "integrator", "orchestrator"}


def load_registry():
    """The agent catalog, plus a vendor label per harness.

    A missing or broken catalog is a cosmetic failure and must not take the
    prototype down: rows degrade to the raw harness id, exactly as
    `harness_label` (installer/og_agents.py:528) degrades when the registry
    loader cannot read the file.
    """
    try:
        agents = json.load(open(REGISTRY))["agents"]
    except Exception:
        return [], "registry.json unreadable: labels degrade to raw harness ids"
    by_vendor = {v["vendor"]: v for v in VENDORS}
    rows = []
    for a in agents:
        rows.append({
            "id": a.get("id", "?"),
            "harness": a.get("harness", "?"),
            "label": a.get("label") or a.get("harness", "?"),
            "vendor": a.get("vendor", "?"),
            # `worker: coder_zen` names the session-title prefix `zen` once the
            # `coder_` is stripped - the same split `split_title` does on a
            # captured title, so the lookup key matches what a row really says.
            "worker": re.sub(r"^coder_", "", (a.get("worker") or "").partition(":")[0]),
        })
    missing = [r["id"] for r in rows if r["vendor"] not in by_vendor]
    if missing:
        return rows, ("vendors with no mark row: " + ", ".join(missing))
    return rows, None


# ------------------------------------------------------------------ data
ORDER = ("working", "blocked", "failed", "idle")
GLYPH = {"working": "●", "blocked": "⏸", "failed": "✗", "idle": "○"}
SPIN = "⠋⠙⠹⠸⠼⠴"
# Box weights borrowed from tui_beauty.py so section A's renders are that
# prototype's geometry, not a lookalike. Only the three the marked layouts use.
LIGHT = ("┌", "─", "┐", "│", "└", "┘")
ROUND = ("╭", "─", "╮", "│", "╰", "╯")

# The capture is untracked (a dump of the operator's own server), so the file
# has to run without it. Mirrors tui_beauty.py's FALLBACK row-for-row, which
# keeps section A comparable with that prototype's V1/V2/V5.
FALLBACK = [{"id": "5e894a23", "title": "Project Status Review",
             "harness": "Claude Code", "status": "working", "kids": [
                 {"title": "coder_zen:stock-concurrency-lock",
                  "status": "working", "elic": 0, "age": 134},
                 {"title": "reviewer:review-br-2-payables",
                  "status": "blocked", "elic": 1, "age": 60},
                 {"title": "coder_freebuff:kg-base-unit-read-auth",
                  "status": "failed", "elic": 0, "age": 30},
                 {"title": "coder_cmdcode:fleet-accident-reports",
                  "status": "idle", "elic": 0, "age": 360},
                 {"title": "scout:scope-race-test-flake",
                  "status": "working", "elic": 0, "age": 12},
             ]}]


def _state(status, elic):
    if elic:
        return "blocked"
    s = (status or "").strip().lower()
    if s in ("running", "working", "in_progress", "active"):
        return "working"
    if s in ("failed", "error", "errored", "cancelled", "canceled"):
        return "failed"
    return "idle"


def age(sec):
    total = max(0, int(sec))
    if total < 60:
        return "%ds" % total
    m, r = divmod(total, 60)
    if m < 60:
        return "%dm%02ds" % (m, r)
    h, m = divmod(m, 60)
    return "%dh%02dm" % (h, m)


def ago(sec):
    total = max(0, int(sec))
    if total < 3600:
        return "%dm ago" % (total // 60)
    return "%dh ago" % (total // 3600)


def _meta(state, elic, created, updated, now):
    if state == "blocked":
        return "waiting on you"
    if state == "failed":
        return "failed"
    if state == "working":
        return age(now - created) if created is not None else "working"
    return "done " + ago(now - updated) if updated is not None else "idle"


def split_title(title):
    """`coder_cmdcode:fix-x` -> (`cmdcode`, `fix-x`), as the shipped view."""
    text = title if isinstance(title, str) else ""
    prefix, sep, task = text.partition(":")
    if not sep:
        return None, text
    agent = prefix[len("coder_"):] if prefix.startswith("coder_") else prefix
    return agent or None, task.strip() or text


def _kid(k, now):
    """One capture row in the shape the views draw: task, agent, state, clock."""
    st = _state(k.get("status"), k.get("pending_elicitations_count"))
    agent, task = split_title((k.get("title") or "").strip() or k.get("id", "")[:8])
    return {
        "title": task or (k.get("title") or k.get("id", "")[:8]),
        "agent": agent,
        "state": st,
        "meta": _meta(st, k.get("pending_elicitations_count"),
                      k.get("created_at"), k.get("updated_at"), now),
        "elic": k.get("pending_elicitations_count") or 0,
    }


def load(limit=5):
    """The SAME root + worker selection tui_beauty.py picks.

    State-diverse root, state-diverse workers, `now` pinned to the newest
    `updated_at` in the capture so the clocks do not move between runs.
    """
    try:
        rows = json.load(open(os.path.join(HERE, "sessions.json")))["data"]
    except Exception:
        rows = FALLBACK

    by_parent = defaultdict(list)
    roots = []
    for r in rows:
        parent = r.get("parent_session_id")
        if parent:
            by_parent[parent].append(r)
        else:
            roots.append(r)
    now = max((r.get("updated_at") or 0) for r in rows)

    candidates = [r for r in roots if by_parent.get(r["id"])]
    if not candidates:
        # Same kid shape as the real path, including the title split, so
        # section A draws `stock-concurrency-lock` and not the raw
        # `coder_zen:stock-concurrency-lock` it would otherwise show.
        f = FALLBACK[0]
        return {"title": f["title"], "harness": f["harness"],
                "state": _state(f["status"], 0),
                # `age` backdates the synthetic rows against the capture's own
                # clock, so the fallback shows the same shape of meta line the
                # real capture does instead of a bare "working".
                "kids": [_kid({"title": k["title"], "status": k["status"],
                               "pending_elicitations_count": k.get("elic"),
                               "created_at": now - k.get("age", 0),
                               "updated_at": now - k.get("age", 0),
                               "id": k["title"][:8]}, now)
                         for k in f["kids"]]}, now

    def diversity(r):
        return len({_state(k.get("status"), k.get("pending_elicitations_count"))
                    for k in by_parent.get(r["id"], [])})

    root = max(candidates, key=lambda r: (diversity(r), r["id"]))
    picked, seen = [], set()
    for k in by_parent[root["id"]]:
        st = _state(k.get("status"), k.get("pending_elicitations_count"))
        if st not in seen:
            seen.add(st)
            picked.append(k)
    picked_ids = {k["id"] for k in picked}
    for k in by_parent[root["id"]]:
        if len(picked) >= limit:
            break
        if k["id"] not in picked_ids:
            picked.append(k)
            picked_ids.add(k["id"])
    picked = picked[:limit]
    picked.sort(key=lambda k: ORDER.index(
        _state(k.get("status"), k.get("pending_elicitations_count"))))

    kids = [_kid(k, now) for k in picked]
    return {
        "title": (root.get("title") or "").strip() or root["id"][:8],
        # No `harness` field in this capture; the bundle name stands in, as in
        # tui_beauty.py, and section A says so where it matters.
        "harness": root.get("harness") or root.get("agent_name") or "agent",
        "state": _state(root.get("status"), root.get("pending_elicitations_count")),
        "kids": kids,
    }, now


# ------------------------------------------------------------- resolving
def mark_for(agent):
    """The tier-1 mark for a worker row's agent name, or a blank.

    Three outcomes and they are not interchangeable:
      - an agent in the catalog      -> its vendor's mark
      - a role (`reviewer`, `scout`) -> BLANK, never a borrowed vendor mark
      - an unknown name              -> BLANK
    A blank is the honest answer: the pane must not claim a vendor it cannot
    read out of the registry.
    """
    v = vendor_for(agent)
    return by_id().get(v, {}).get("t1", " ") if v else " "


def by_id():
    return {v["vendor"]: v for v in VENDORS}


def vendor_for(agent):
    """The registry vendor behind a worker row's agent name, or None.

    Matches the agent id, the harness id, and the `worker:` name the registry
    carries (`coder_zen` -> zen -> opencode). The worker name is the one that
    actually appears in a session title, so a table keyed only on agent ids
    would resolve zen and freebuff to nothing.
    """
    if not agent:
        return None
    base = re.sub(r"_\d+$", "", agent)          # reviewer_2 -> reviewer
    if base in ROLES:
        return None
    for row in REGISTRY_ROWS:
        if base in (row["id"], row["worker"], row["harness"]):
            return row["vendor"]
    return None


def tier3_for(agent):
    base = re.sub(r"_\d+$", "", agent or "")
    return "[%s]" % AGENT_TAGS[base] if base in AGENT_TAGS else "[--]"


# ----------------------------------------------------------------- colour
# The xterm 256 palette: 16 system colours, a 6x6x6 colour cube on the levels
# {0,95,135,175,215,255}, and a 24-step grey ramp. Reproduced here because the
# question "which 256 index is this brand colour" has to be answerable offline
# and the pane will hard-code the answer.
_CUBE = (0, 95, 135, 175, 215, 255)
_SYSTEM = (
    (0, 0, 0), (170, 0, 0), (0, 170, 0), (170, 85, 0),
    (0, 0, 170), (170, 0, 170), (0, 170, 170), (170, 170, 170),
    (85, 85, 85), (255, 85, 85), (85, 255, 85), (255, 255, 85),
    (85, 85, 255), (255, 85, 255), (85, 255, 255), (255, 255, 255),
)


def _palette():
    out = list(_SYSTEM)
    for r in _CUBE:
        for g in _CUBE:
            for b in _CUBE:
                out.append((r, g, b))
    for i in range(24):
        v = 8 + i * 10
        out.append((v, v, v))
    return out


PALETTE = _palette()


def nearest_256(hexcol):
    """The 256 index whose RGB is closest to `hexcol`, by squared distance.

    Nearest-RGB rather than a guessed index, because the cube is coarse: the
    operator's "Claude orange is about 208" is 208 = (255,135,0), a saturated
    orange, while Claude's actual brand coral #D97757 lands in the same
    neighbourhood but not on 208. Both are printed in the colour section so
    the choice is visible rather than assumed.
    """
    h = hexcol.lstrip("#")
    want = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    best, best_d = 0, None
    for i, (r, g, b) in enumerate(PALETTE):
        d = (r - want[0]) ** 2 + (g - want[1]) ** 2 + (b - want[2]) ** 2
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def paint(idx, text):
    return "\x1b[38;5;%dm%s\x1b[0m" % (idx, text)


# The three attributes W1 uses, as raw SGR. Named rather than inlined because
# the header bar and the selection treatment are both about WHICH attribute is
# spent where, and that is a thing the operator is choosing, not a detail.
#   A_REVERSE - the filled header bar, and nowhere else (see `w1_header`)
#   A_DIM     - the rail and the report snippet
#   A_BOLD    - the selected worker's title, with the caret
REV, NOREV = "\x1b[7m", "\x1b[27m"
DIM, UNDIM = "\x1b[2m", "\x1b[22m"
BOLD, UNBOLD = "\x1b[1m", "\x1b[22m"


def _hex_spread(hexcol):
    """How far a brand hex sits from grey: max channel minus min channel."""
    h = hexcol.lstrip("#")
    r, g, b = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return max(r, g, b) - min(r, g, b)


def _check_w1_parity(w=46):
    """The W1 renderers in this file and in tui_beauty.py must agree, exactly.

    Neither prototype can import the other - tui_beauty needs the marks and
    this file needs the layout - so both carry the same geometry and this
    renders the same worker rows through both and compares. It is the check
    that makes "not a lookalike" a fact instead of an intention: if the two
    rungs of the degradation ladder ever disagree, or one file starts padding a
    gutter differently, the render differs and this fails.

    Skipped, not faked, if tui_beauty.py cannot be imported - a prototype that
    refuses to run because its sibling moved is worse than one that runs.
    """
    try:
        import tui_beauty
    except Exception:
        return
    kid = {"title": "stock-concurrency-lock", "agent": "cmdcode",
           "state": "working", "meta": "2m14s", "elic": 0}
    root = {"title": "Project Status Review", "harness": "hivemind",
            "state": "working", "kids": [kid]}
    mine = a_web(root, w, slot_for("moonshotai"),
                 lambda k: t0_mark_for(k["agent"]))
    theirs = tui_beauty.v_web(root, w,
                              mark_of=lambda k: t0_mark_for(k["agent"]),
                              root_mark=slot_for("moonshotai"))
    # The header bar differs by construction (this file passes the operator's
    # mark, the other defaults to a tile), and so does the trailing grade
    # paragraph, which is prose rather than geometry. The worker rows are the
    # geometry under test, and their count is exact: two lines a worker plus a
    # gap row between workers, so `3n - 1` of them. Slicing by that count
    # rather than to the end means the check does not silently stop covering
    # the rows if the grade paragraph ever changes length.
    nrows = 3 * len(root["kids"]) - 1
    mine_rows, their_rows = mine[2:2 + nrows], theirs[2:2 + nrows]
    assert mine_rows == their_rows, (
        "the W1 renderers in tui_marks.py and tui_beauty.py disagree:\n  %r\n  %r"
        % (mine_rows, their_rows))
    assert [w1_layout(x) for x in (46, 80, 20)] == \
        [tui_beauty.w1_layout(x) for x in (46, 80, 20)], (
        "the W1 degradation ladder differs between the two prototypes")


# -------------------------------------------------------------- checking
def check_marks():
    """Assert the mark set: tier 1 all-Narrow, tier 0 through the slot.

    Tier 1 keeps its old, stricter rule - every mark one character, Narrow or
    Neutral - because it is still offered as the all-Narrow alternative and a
    wide glyph in it would shear exactly the boxes it exists to keep straight.

    Tier 0's rule is deliberately different: a WIDE mark is LEGAL, because the
    operator may pick one, and the thing that has to be true instead is that
    the mark goes through `mark_slot` and lands on exactly two columns. So the
    assertions here are about the slot, not about the glyph: one character, not
    a combining mark, one or two cells under the pessimistic ruler, unique, and
    - the property that actually protects every box below - the same column
    before the vendor name on every row.

    On top of the geometry, the DECISION is asserted too: `EXPECTED_T0` is the
    operator's round as a frozen codepoint table, so a later edit that changes a
    pick - deliberately or by typo - fails here rather than shipping quietly.
    Same for `FONT_RISK`: every non-BMP pick must be declared in it and every
    declaration must really be non-BMP, which is what stops the coverage note
    in section 6 from drifting away from the marks it describes.

    The failure messages name the row, the glyph and the width class, because
    the failure this check prevents ("the pane shears by one column") is
    invisible in a diff and obvious only once something has already shipped.
    """
    seen = {}
    for v in VENDORS:
        m = v["t1"]
        if m == " ":
            continue
        assert len(m) == 1, "tier-1 mark is not one character: %r (%s)" % (
            m, v["vendor"])
        w = unicodedata.east_asian_width(m)
        assert w in ("N", "Na"), (
            "tier-1 mark %r (%s) is East Asian %s, not Narrow: it is two "
            "columns in a CJK terminal" % (m, v["vendor"], w))
        assert not unicodedata.combining(m), (
            "tier-1 mark %r (%s) is a combining mark and adds no cell"
            % (m, v["vendor"]))
        assert ord(m) < 0x1F000, (
            "tier-1 mark %r (%s) is in the emoji planes and is double-width"
            % (m, v["vendor"]))
        assert m not in seen, "tier-1 mark %r used twice: %s and %s" % (
            m, seen.get(m), v["vendor"])
        seen[m] = v["vendor"]

    # ---- tier 0: the operator's picks, slotted
    unknown = sorted(set(OPERATOR_PICKS) - {v["vendor"] for v in VENDORS})
    assert not unknown, "operator picked a mark for no catalog vendor: %s" % (
        ", ".join(unknown))

    seen0 = {}
    marks = {v["vendor"]: t0_of(v["vendor"])["mark"] for v in VENDORS}

    # Every vendor has an operator mark now. This round closed the last four
    # blanks (kilo, goose, hermes, cognition), and the claim the tier-0 table
    # makes in its own header - "no blanks remain" - is checked here rather
    # than trusted, because a blank row in that table is exactly what makes a
    # reader think the table is unfinished.
    assert not [v for v in VENDORS if marks[v["vendor"]] == " "], (
        "tier-0 blanks remain: %s"
        % sorted(v["vendor"] for v in VENDORS if marks[v["vendor"]] == " "))
    assert len(OPERATOR_PICKS) == len(VENDORS), (
        "tier 0 is meant to be complete: %d operator picks for %d vendors"
        % (len(OPERATOR_PICKS), len(VENDORS)))

    # The operator's round as a frozen table. Every vendor, the codepoint they
    # named, and nothing derived: the point is that a diff which changes a pick
    # has to change this line too, in the same commit, on purpose.
    assert marks == EXPECTED_T0, (
        "tier-0 picks differ from the operator's round. expected %s, got %s"
        % (sorted("%s=%s" % (k, codepoint_of(v)) for k, v in EXPECTED_T0.items()),
           sorted("%s=%s" % (k, codepoint_of(v)) for k, v in marks.items())))

    for vid, m in marks.items():
        slot = mark_slot(m)
        if m == " ":
            assert slot == "  ", (
                "tier-0 blank for %s is not two spaces: %r" % (vid, slot))
            continue
        assert len(m) == 1, (
            "tier-0 mark for %s is %d characters (%r), not 1: a sequence like a "
            "ZWJ emoji cannot be slotted, its width is not the sum of its parts"
            % (vid, len(m), m))
        assert not unicodedata.combining(m), (
            "tier-0 mark %r (%s) is a combining mark and adds no cell, so the "
            "slot cannot be two columns" % (m, vid))
        cells = slot_cells(m)
        assert cells in (1, 2), (
            "tier-0 mark %r (%s) is %d cells, which no 2-column slot can hold"
            % (m, vid, cells))
        assert slot_width(slot) == SLOT_COLS, (
            "tier-0 slot for %s is %d columns, not %d: %r"
            % (vid, slot_width(slot), SLOT_COLS, slot))
        # The pessimistic guarantee, stated as the thing it prevents: a slot is
        # never WIDER than two cells on the ruler a western terminal uses, so a
        # row can never be pushed past the pane's right edge.
        assert _visible_width(slot) <= SLOT_COLS, (
            "tier-0 slot for %s is %d columns wide in a western terminal, past "
            "the %d-column slot: %r"
            % (vid, _visible_width(slot), SLOT_COLS, slot))
        assert m not in seen0, "tier-0 mark %r used twice: %s and %s" % (
            m, seen0.get(m), vid)
        seen0[m] = vid

    # The one row allowed to come up short is the Ambiguous one, and only
    # because the slot is deliberately pessimistic about it. Anything else
    # measuring under two columns means the slot stopped being the ruler.
    short = {vid for vid, m in marks.items()
             if _visible_width(mark_slot(m)) < SLOT_COLS}
    ambiguous = {vid for vid, m in marks.items()
                 if m != " " and unicodedata.east_asian_width(m) == "A"}
    assert short == ambiguous, (
        "rows whose slot is under %d columns do not match the Ambiguous rows: "
        "%s (expected exactly %s)" % (SLOT_COLS, sorted(short), sorted(ambiguous)))
    assert ambiguous, "no Ambiguous mark left to exercise the pessimistic slot"

    # Section 5's self-coloured/inherits split is derived from these two sets at
    # render time, and asserted here so the two can never drift apart: a glyph
    # declared self-colouring must be Wide (an emoji that drew in one cell would
    # make section 5's "the wide ones self-colour" a coincidence), and every
    # Wide mark must be DECLARED one way or the other. The declaration is a hand-
    # maintained list on purpose - `unicodedata` does not expose
    # Emoji_Presentation - so a new Wide pick has to be classified deliberately,
    # and this is the line that makes it deliberate.
    #
    # Both sets are EMPTY this round, and the rule is still armed: tier 0 has no
    # two-cell glyph left, so section 5's honest answer is "every mark takes the
    # row attribute", but a future Wide pick cannot be added without being
    # classified here. The two assertions below are the same ones as before with
    # the non-emptiness requirement removed - not weakened, just no longer
    # demanding a Wide mark exists.
    wide = {vid for vid, m in marks.items()
            if m != " " and unicodedata.east_asian_width(m) in _WIDE}
    selfcolour = {vid for vid, m in marks.items() if m in EMOJI_PRESENTATION}
    assert selfcolour <= wide, (
        "glyphs declared emoji-presentation that are not Wide, so section 5's "
        "'the wide ones self-colour, the rest do not' is not what the pane "
        "would draw: %s" % sorted(selfcolour - wide))
    assert wide == selfcolour, (
        "tier-0 has Wide marks that section 5 does not account for. Classify "
        "them in EMOJI_PRESENTATION if they are emoji-presentation, or expect "
        "section 5's numbers to be wrong: %s"
        % sorted(wide - selfcolour))
    assert EMOJI_PRESENTATION == (), (
        "EMOJI_PRESENTATION must be empty: the crab and the whale left tier 0 "
        "this round, so a leftover entry here would be a ghost that section 5 "
        "counts as a self-coloured mark nothing draws. Add the new pick to the "
        "list in the same commit that adds it to OPERATOR_PICKS.")

    # The operator's measurement of their own round, restated as an assertion:
    # everything is Narrow except kilo, cognition and z-ai, which are Ambiguous.
    # Ambiguous is the only non-Narrow class left - if a future pick arrives Wide
    # this fails and the tier-0 header's "no Wide picks" line has to be rewritten.
    non_narrow = {vid: unicodedata.east_asian_width(m) for vid, m in marks.items()
                  if unicodedata.east_asian_width(m) not in ("N", "Na")}
    assert non_narrow == EXPECTED_AMBIGUOUS, (
        "tier-0 width classes changed. expected exactly %s, got %s - the header "
        "text and section 6 both quote that measurement"
        % (sorted("%s=%s" % kv for kv in EXPECTED_AMBIGUOUS.items()),
           sorted("%s=%s" % kv for kv in non_narrow.items())))
    assert not wide, (
        "tier-0 has a Wide mark again (%s). That is allowed by the slot, but the "
        "tier-0 header, section 2 and section 5 all say tier 0 has no two-cell "
        "pick, and an emoji there also has to go in EMOJI_PRESENTATION"
        % sorted(wide))

    # Section 6's coverage table and FONT_RISK describe the same set, so the two
    # are checked against each other: every pick outside the BMP is declared a
    # coverage risk, and every declared risk really is outside the BMP.
    astral = {vid for vid, m in marks.items() if ord(m) > 0xFFFF}
    assert astral == set(FONT_RISK), (
        "section 6's coverage risk list and the tier-0 table disagree. "
        "outside the BMP: %s; declared: %s"
        % (sorted(astral), sorted(FONT_RISK)))
    assert astral, "no non-BMP tier-0 pick left for section 6 to be about"

    # `_t0_marks` walks the table a second time, by a different route than
    # `marks` above, so comparing the two costs nothing and catches a vendor
    # that fell out of the loop entirely.
    assert sorted(_t0_marks()) == sorted(marks.values()), (
        "t0_of and the mark table disagree about which vendors have a mark")
    assert len(set(_t0_marks())) == len(VENDORS), (
        "two vendors share a tier-0 mark: %s"
        % sorted(m for m in set(_t0_marks())
                 if list(_t0_marks()).count(m) > 1))

    # The ambiguous call itself, asserted so it cannot drift silently.
    assert mark_slot("╔") == "╔" and slot_width(mark_slot("╔")) == SLOT_COLS, (
        "Ambiguous handling changed: U+2554 must fill the slot unpadded")

    ids = {a for v in VENDORS for a in v["agents"]}
    catalog = {r["id"] for r in REGISTRY_ROWS}
    assert ids == catalog, "mark table and registry disagree: %s" % (
        sorted(ids ^ catalog))
    assert len(set(AGENT_TAGS.values())) == len(AGENT_TAGS), \
        "two agents share a tier-3 tag"
    assert set(AGENT_TAGS) == catalog, "tier-3 tags do not cover the catalog"

    # The two prototypes draw W1 from the same geometry; prove it rather than
    # assume it. Last, because it is the only check that touches the sibling.
    _check_w1_parity()


# -------------------------------------------------------------- sections
def hdr(w, text):
    """A heading line that is always exactly within the width."""
    return "  " + clip(text, max(0, w - 2))


def head(w, title, sub):
    out = [rule(w, "═"), "  " + clip(title, w - 2)]
    for seg in para(sub, w):
        out.append(seg)
    out.append(rule(w, "═"))
    return out


def section(w, name, rationale):
    out = ["", rule(w, "─"), "  " + clip(name, w - 2)]
    out += para(rationale, w)
    out.append(rule(w, "─"))
    return out


def roster_block(w, rows, note):
    out = []
    if note:
        out += para(note, w)
    out.append("  " + two_col(pad("agent", 8) + " " + pad("vendor", 10),
                              "product", w - 2))
    for r in rows:
        out.append("  " + two_col(pad(r["id"], 8) + " " + pad(r["vendor"], 10),
                                  r["label"], w - 2))
    return out


def tier_block(w, rows, tier, rationale, mark_w=5, mark_padder=pad):
    """One tier's table: same rows, same order, same geometry; only the mark.

    The mark cell is padded to the same width in EVERY tier so the tables are
    aligned with each other and the operator can compare a mark down the page
    instead of re-reading which column it was in. Tier 0 passes the slot's own
    width and the slot's own padder, because its marks are not one cell of
    text but a two-cell region that `pad` cannot measure.
    """
    out = section(w, tier["name"], rationale)
    out.append("  " + two_col(mark_padder(tier["head"], mark_w) + " "
                              + pad("vendor", 10), tier["tail_head"], w - 2))
    for r in rows:
        out.append("  " + two_col(mark_padder(tier["mark"](r), mark_w) + " "
                                  + pad(r["vendor"], 10), tier["tail"](r), w - 2))
        note, why = tier["note"](r), tier["why"](r)
        # A blank mark has no glyph name to lead with; "- - no honest mark"
        # reads like a rendering bug, which is the opposite of the point.
        out += para(why if note == "-" else "%s - %s" % (note, why), w, indent=6)
    return out


# ---- section A: the mark in the layouts tui_beauty.py proposed
def _box(lines, bw, chars=LIGHT):
    tl, h, tr, v, bl, br = chars
    bw = max(4, bw)
    return ([tl + h * (bw - 2) + tr]
            + [v + pad(ln, bw - 2) + v for ln in lines]
            + [bl + h * (bw - 2) + br])


def a_nested(r, w, root_mark, row_mark):
    """tui_beauty V1 (nested-container) with the mark in the header."""
    out = []
    head_txt = root_mark + " " + r["harness"]
    name = clip(r["title"], max(4, w - 12))
    left = "╔═ " + head_txt + " " + name + " "
    fill = max(0, w - _visible_width(left) - 1)
    out.append(clip(left + rule(fill, "═") + "╗", w))
    out.append("║ " + two_col("%s  %d workers" % (GLYPH[r["state"]], len(r["kids"])),
                              "v1 nested", w - 4) + " ║")
    out.append("╠" + rule(w - 2, "═") + "╣")
    for i, k in enumerate(r["kids"]):
        extra = "⏸ %d" % k["elic"] if k["state"] == "blocked" and k["elic"] else ""
        lead = row_mark(k) + " " if row_mark else ""
        inner = _box([
            ("▸" if i == 0 else " ") + " " + lead + GLYPH[k["state"]] + " " + k["title"],
            "    " + ((k["agent"] + " · ") if k["agent"] else "") + k["meta"]
            + (("  " + extra) if extra else "")], max(4, w - 4), LIGHT)
        out += ["║ " + ln + " ║" for ln in inner]
    out.append("╚" + rule(w - 2, "═") + "╝")
    return out


def a_banner(r, w, root_mark, row_mark):
    """tui_beauty V2 (title-bar-badge): mark inside the heavy bar."""
    badge = "%s %s · %d" % (root_mark, r["harness"], len(r["kids"]))
    left = "━━ " + clip(r["title"], max(4, w - _visible_width(badge) - 6)) + " "
    right = " " + clip(badge, max(2, w - _visible_width(left) - 2)) + " "
    fill = max(0, w - _visible_width(left) - _visible_width(right))
    out = [clip(left + "━" * fill + right, w), ""]
    for i, k in enumerate(r["kids"]):
        lead = row_mark(k) + " " if row_mark else ""
        lines = [
            ("▸" if i == 0 else " ") + " " + lead + GLYPH[k["state"]] + " " + k["title"],
            "  " + ((k["agent"] + " · ") if k["agent"] else "") + k["meta"]]
        out += _box(lines, max(4, w - 1), LIGHT)
    return out


def a_dense(r, w, root_mark, row_mark):
    """tui_beauty V5 (dense-rows): one row per worker, the tightest layout."""
    out = _box([clip(r["title"], w - 4),
                two_col(root_mark + " " + r["harness"], "%d workers" % len(r["kids"]),
                        w - 4)], w, ROUND)
    for i, k in enumerate(r["kids"]):
        g = SPIN[0] if k["state"] == "working" else GLYPH[k["state"]]
        right = k["meta"]
        if k["state"] == "blocked" and k["elic"]:
            right = "⏸" + str(k["elic"]) + "  " + right
        lead = row_mark(k) + " " if row_mark else ""
        out.append(two_col(("▸" if i == 0 else " ") + " " + lead + g + " "
                            + k["title"], right, w))
    return out


# ---- W1 web-parity: the geometry lives here, and tui_beauty.py copies it
# ---------------------------------------------------------------------
# WHY IT LIVES IN THIS FILE AND NOT IN THE OTHER ONE. W1's mark column is two
# cells wide under the PESSIMISTIC ruler - the same `mark_slot` as the tier-0
# table - so the one renderer of W1 has to be able to call `mark_slot`, and
# `mark_slot` lives here. Rather than import one prototype from the other
# (which is a cycle: tui_beauty needs the marks, this file needs the layout),
# both files carry the same small geometry and `check_marks()` asserts they
# still agree - see `_check_w1_parity`. The earlier sections set the precedent:
# a_nested/a_banner/a_dense are re-implementations of tui_beauty's V1/V2/V5,
# not a lookalike, and the parity check is how "not a lookalike" is enforced
# rather than merely intended.
W1_CARET = "▸"
# U+2299 CIRCLED DOT OPERATOR for the node, U+2502 for the rule joining them.
# Both are Ambiguous, like every box-drawing glyph the pane already draws, so
# W1 introduces no new class of width risk - it adds more of a class the pane
# has always had.
W1_DOT = "⊙"
W1_RULE = "│"
# The avatar tile in the header bar when no vendor mark is available. U+25A1,
# Neutral-ish and one cell in both rulers, which matters in a bar that is
# filled to the full pane width.
W1_TILE = "□"
# Below this many columns for the title, the three gutters are not worth their
# columns and the form degrades. The title is the pane's content; the gutters
# are aids to reading it.
W1_MIN_TITLE = 18
W1_MIN_TITLE_SMALL = 11

# The web shows a green check for done and a red exclamation for failed. The
# TUI keeps its OWN state vocabulary instead - ● ⏸ ✗ ○, the same four glyphs
# every other variant in this file uses - so the chip column reads as the same
# language as the rest of the pane and a state never changes meaning between
# layouts. Colour is not available to it either way: the chip is one cell of
# monochrome text glyph, so it inherits the row attribute exactly as the mark
# does. That divergence from the web shot is deliberate and it is the only one.
#
# The capture carries NO report text - a kid row has a title, a status, an
# agent name, two timestamps and an elicitation count - so the snippet below is
# SYNTHESISED from the fields that do exist, and the section says so where the
# line is printed. What is under test is the second LINE: always present,
# always dim, truncated with an ellipsis at the pane edge, never expanded on
# demand. The words in it are placeholders of the right length to make that
# visible.
SNIPPET = {
    "working": "in progress, no report line yet",
    "blocked": "holding an elicitation, needs a decision",
    "failed": "stopped on an error, nothing reported",
    "idle": "finished, report at the tail of the log",
}


def snippet_for(k):
    """The DIM second line W1 shows under every worker."""
    who = k.get("agent") or "role"
    return "%s · %s · %s" % (who, SNIPPET[k["state"]], k["meta"])


def w1_cols(rail_on, mark_on):
    """Columns the W1 gutter occupies: caret, [rail], chip, [mark] + spaces."""
    return 4 + 2 * rail_on + 3 * mark_on


def w1_layout(w):
    """`(grade, rail_on, mark_on)` for W1 at pane width `w`.

    The three gutters are dropped in a fixed order when the pane is too narrow
    to carry them, and the order is the design, not an accident: the timeline
    rail goes first because it is decoration on top of a single cell; then the
    harness mark, which is the one the rest of this file argues hardest for;
    then the chip, whose information is the state word the title already
    carries. The title is the last thing to go, and the grade string is printed
    with every render, so a degraded W1 always says so on the same screen.
    """
    for grade, rail, mk, need in (
            ("full - timeline rail, status chip and harness mark",
             True, True, W1_MIN_TITLE),
            ("DEGRADED - the timeline rail is dropped", False, True,
             W1_MIN_TITLE_SMALL),
            ("DEGRADED - rail and mark dropped, chip kept", False, False,
             W1_MIN_TITLE_SMALL)):
        if w - w1_cols(rail, mk) >= need:
            return grade, rail, mk
    return "DEGRADED - caret only, no gutters", False, False


def w1_prefix(caret, rail_char, rail_on, chip, mark, mark_on):
    """The gutter in front of every W1 line. Identical in both prototypes.

    The rail column is DIM here and nowhere else, which is what makes the rail
    read as a rail: a full-brightness vertical rule beside a list is a border,
    and the whole point of W1 is that this layout has no borders.
    """
    s = caret + " "
    if rail_on:
        s += DIM + rail_char + UNDIM + " "
    s += chip + " "
    if mark_on:
        s += mark + " "
    return s


def w1_header(r, w, mark):
    """The filled bar: A_REVERSE across the full pane, tile plus bold name.

    THE ONE PLACE REVERSE VIDEO BELONGS. W1 marks the selected worker with a
    caret (and A_BOLD on its title), so the attribute is not competing with
    selection for the same channel: one thing is a filled bar at the top, the
    other is an arrow in the gutter, and the operator can see both at once.
    """
    lead = " %s " % ((mark or "").strip() or W1_TILE)
    name = clip(r["harness"], max(2, w - _visible_width(lead) - 1))
    return REV + pad(lead + BOLD + name + UNBOLD, w) + NOREV


def a_web(r, w, root_mark, row_mark):
    """tui_beauty V8 (W1 web-parity): the web view's shape, in this pane.

    Flat list, no boxes, a filled header bar, a DIM timeline rail down the
    left, a status chip, the harness mark in its own gutter, two lines a
    worker, and a blank row between workers. Selection is the caret alone.
    """
    grade, rail_on, mark_on = w1_layout(w)
    mark_of = row_mark or (lambda k: " ")
    cols = w1_cols(rail_on, mark_on)
    room = max(0, w - cols)
    out = [w1_header(r, w, root_mark), ""]
    for i, k in enumerate(r["kids"]):
        if i:
            # The blank row is not blank: it carries the rail's vertical rule,
            # which is what joins one node to the next.
            out.append(w1_prefix(" ", W1_RULE, rail_on, " ", "  ", mark_on))
        slot = mark_slot(mark_of(k) or " ")
        title = BOLD + k["title"] + UNBOLD if i == 0 else k["title"]
        out.append(w1_prefix(W1_CARET if i == 0 else " ", W1_DOT, rail_on,
                             GLYPH[k["state"]], slot, mark_on)
                   + clip(title, room))
        out.append(w1_prefix(" ", W1_RULE, rail_on, " ", "  ", mark_on)
                   + DIM + clip(snippet_for(k), room) + UNDIM)
    # Wrapped, not clipped: a degraded W1 that cannot say which gutter it
    # dropped has failed at the one job the degradation ladder has.
    out += para("W1 at width %d: %s. %d columns of gutter, %d left for the "
                "title and the snippet." % (w, grade, cols, room), w,
               indent=2)
    return out


def t0_mark_for(agent):
    """The TIER-0 mark for a worker row's agent name, or a blank.

    Same three outcomes as `mark_for` - a catalog agent gets its vendor's pick,
    a role and an unknown name get a blank - because a role row has no vendor
    and the pane must not invent one. Where `mark_for` returns tier 1's glyph,
    this returns the operator's pick for the same vendor.
    """
    v = vendor_for(agent)
    return t0_of(v)["mark"] if v else " "


CONTEXTS = [
    ("V1 nested-container", "tui_beauty.py", a_nested,
     "root mark only: +2 cols in the container's title row, once per tree"),
    ("V1 nested-container", "tui_beauty.py", a_nested,
     "+ a mark on EVERY worker row: +2 cols a row"),
    ("V2 title-bar-badge", "tui_beauty.py", a_banner,
     "root mark only: +2 cols in the bar's badge, once per tree"),
    ("V2 title-bar-badge", "tui_beauty.py", a_banner,
     "+ a mark on EVERY worker row: +2 cols a row"),
    ("V5 dense-rows", "tui_beauty.py", a_dense,
     "root mark only: +2 cols on the header row, once per tree"),
    ("V5 dense-rows", "tui_beauty.py", a_dense,
     "+ a mark on EVERY worker row: +2 cols a row"),
    ("W1 web-parity", "tui_beauty.py", a_web,
     "+ a mark on EVERY worker row, in its OWN gutter"),
]


def context_block(w, root, root_mark, with_worker_marks):
    row_mark = (lambda k: mark_for(k["agent"])) if with_worker_marks else None
    out = []
    for name, src, fn, cap in CONTEXTS:
        want = cap.startswith("+")
        if want != with_worker_marks:
            continue
        out.append(hdr(w, "%s (%s) - %s" % (name, src, cap)))
        out += fn(root, w, root_mark, row_mark)
        out.append("")
    return out


# ---- section 2: the deliberate breakage
# The rows the demo uses: every Ambiguous pick, plus enough Narrow ones to show
# that the Narrow rows are the ones the naive padder gets RIGHT. That ratio is
# the section's finding now - the crab and the whale are gone, so no tier-0
# mark draws two cells in a western terminal any more, and the shear that
# remains is entirely the Ambiguous three, which is only visible under the
# other ruler.
BREAKAGE = ("moonshotai", "deepseek", "anthropic", "kilo", "cognition",
            "z-ai", "goose")
# What the pane would plausibly put on a worker row, so the demo is a real row
# and not a column of glyphs with nothing to shear.
DEMO_TASKS = {
    "moonshotai": ("fix-auth", "2m ago"),
    "deepseek": ("fix-auth", "2m ago"),
    "anthropic": ("fix-auth", "2m ago"),
    "kilo": ("fix-auth", "2m ago"),
    "cognition": ("fix-auth", "2m ago"),
    "z-ai": ("fix-auth", "2m ago"),
    "goose": ("fix-auth", "2m ago"),
}


def _naive_row(mark, task, clock, cell, body):
    """A row laid out the way `len()`-based padding lays one out.

    `ljust` on characters, `len()` for the right-alignment arithmetic, a
    str slice for the clip. This is not a strawman - it is what a first pass
    at this pane looks like, and it is why the slot exists. Every function is
    deliberately one line and deliberately wrong.
    """
    left = mark.ljust(cell) + " " + task
    right = clock
    return (left + " " * max(1, body - len(left) - len(right)) + right)[:body]


def _cjk_width(s):
    """Display columns under the CJK-locale ruler: Ambiguous counts two.

    The other half of the measurement in section 2. `slot_cells` already is
    that ruler; this is it applied to a whole line, so the table can show both
    readings of the same row side by side instead of asserting that one of them
    is hypothetical.
    """
    return sum(slot_cells(c) for c in _ANSI_SGR.sub("", s))


def breakage_block(w):
    """Print the shear, then the fix, from the same rows. Section 2.

    Deliberate breakage: the top block is what the pane does if the mark is
    padded as TEXT, the bottom block is what it does if the mark goes through
    `mark_slot`. The two are printed from the same data one after the other,
    with the right-hand ruler under each, so the drift is a thing the operator
    looks at rather than a sentence they have to believe.

    Both blocks are measured under the ruler of THIS terminal, where an
    Ambiguous glyph is one cell. That is not a flattering choice for the
    argument - it hides the worst of the naive render - so the MEASURED table
    underneath prints the same rows under BOTH rulers, and the prose says
    which one this terminal is.
    """
    cell = 3
    dw = max(12, w - 2)
    # No headroom, deliberately: the naive block is a column WIDER than the
    # pane wherever a mark draws two cells, and the point of the demo is that
    # the row does not fit, not that it fits crookedly. The naive rows are
    # clipped to fit THIS file's budget (the gate forbids a long line), which is
    # a kinder failure than the one a terminal gives them, which is a wrap.
    body = max(10, dw)
    out = section(
        w, "2. DELIBERATE BREAKAGE - the same rows without the slot, and with it",
        "Read the two blocks as one table: the column the task name starts in, "
        "and the right-hand edge of each row. THE TOP BLOCK IS SUPPOSED TO BE "
        "BROKEN. It is printed on purpose. Nothing about it is a bug in this "
        "file; it is what the pane renders if the mark is padded as text, and "
        "it is the whole justification for mark_slot(). Both blocks are drawn "
        "with THIS terminal's ruler, where an Ambiguous glyph is one cell - "
        "the kinder reading. The table under them prints both.")
    out.append("")

    def ruler(label):
        return "  " + label.ljust(dw - 1)[:dw - 1] + "║"

    for label, naive in (("NAIVE - mark.ljust(3), right column by len():", True),
                         ("SLOTTED - mark_slot(mark), measured in columns:", False)):
        out.append(hdr(w, label))
        for vid in BREAKAGE:
            mark = t0_of(vid)["mark"]
            task, clock = DEMO_TASKS[vid]
            row = (_naive_row(mark, task, clock, cell, body) if naive else
                   two_col(mark_slot(mark) + " " + task, clock, body))
            out.append("  " + clip(row, dw))
        out.append(ruler("pane's right edge"))
        out.append("")

    # Measured, so the claim above is arithmetic the operator can check rather
    # than a sentence they have to believe. "believed" is what the padder
    # computed from len(); "drawn" is where the terminal actually puts it, under
    # each of the two rulers a real terminal could be using.
    out.append(hdr(w, "MEASURED - where the task name actually lands, under "
                      "BOTH rulers"))
    out += para("W = this terminal's ruler, Ambiguous counts 1. C = a CJK-"
                "locale terminal, Ambiguous counts 2. `slot_cells` is C; the "
                "file's own width check is W.", w, indent=6)
    believed = 2 + cell + 1
    drifts, ends, ends_c = {}, {}, {}
    out.append("  " + clip(pad("vendor", 11) + " " + "  " + " "
                           + pad("name W", 8) + " " + pad("name C", 8)
                           + " " + pad("end W", 5) + " " + pad("end C", 5), dw))
    for vid in BREAKAGE:
        mark = t0_of(vid)["mark"]
        naive = _naive_row(mark, DEMO_TASKS[vid][0], DEMO_TASKS[vid][1],
                           cell, body)
        drawn = 2 + _visible_width(mark.ljust(cell)) + 1
        drawn_c = 2 + _cjk_width(mark.ljust(cell)) + 1
        drifts[vid] = (drawn - believed, drawn_c - believed)
        ends[vid] = 2 + _visible_width(naive)
        ends_c[vid] = 2 + _cjk_width(naive)
        out.append("  " + clip(
            pad(vid, 11) + " " + slot_for(vid) + " "
            + pad("%d->%d %+d" % (believed, drawn, drawn - believed), 8) + " "
            + pad("%d->%d %+d" % (believed, drawn_c, drawn_c - believed), 8)
            + " " + pad(str(ends[vid]), 5) + " " + pad(str(ends_c[vid]), 5),
            dw))
    out.append("")
    narrow_drift = [v for v in BREAKAGE if drifts[v][0]]
    amb_drift = [v for v in BREAKAGE if drifts[v][1]]
    out += para(
        "Read the name column. The padder BELIEVES the task name starts at "
        "column %d on every row, because len(mark) is %d for every glyph in "
        "tier 0 - the hieroglyphs, the arrow and the place-of-interest sign are "
        "all single python characters, whatever they draw as. In THIS terminal "
        "that belief is %s - it is the accident the previous round relied on, "
        "and it held only because the crab and the whale were there to break "
        "it. With them gone, every glyph in tier 0 draws one cell here, "
        "Ambiguous included. Under the CJK ruler the belief is wrong on %d of "
        "the %d rows: %s draw two cells there and land a column PAST the slot. "
        "Same padder, same glyph, two terminals, two different truths - and "
        "the naive version cannot be right in both, which is the whole reason "
        "`slot_cells` picks the worse one on purpose."
        % (believed, len(t0_of("anthropic")["mark"]),
           "accidentally right on all %d" % len(BREAKAGE) if not narrow_drift
           else "wrong on %d" % len(narrow_drift),
           len(amb_drift), len(BREAKAGE), ", ".join(amb_drift)), w, indent=6)
    over_w = {v for v in BREAKAGE if ends[v] > w}
    over_c = {v for v in BREAKAGE if ends_c[v] > w}
    out += para(
        "And at the right edge, which is where the pane actually fails: the "
        "naive rows end at %s under this terminal's ruler and at %s under the "
        "CJK one. The pane is %d columns wide. %s under this ruler; %d of the "
        "%d rows end PAST it under the other%s - and a row that does not fit is "
        "not clipped by a terminal, it is WRAPPED, and a wrapped row in a "
        "right-aligned clock column is the failure that gets reported as 'the "
        "agents pane is broken'. The slotted block needs no clipping at all: "
        "it ends at column %d on every row, in both locales."
        % (", ".join("%s %d" % (v, ends[v]) for v in BREAKAGE),
           ", ".join("%s %d" % (v, ends_c[v]) for v in BREAKAGE), w,
           ("no row overruns" if not over_w
            else "%d overrun (%s)" % (len(over_w), ", ".join(sorted(over_w)))),
           len(over_c), len(BREAKAGE),
           " (%s)" % ", ".join(sorted(over_c)) if over_c else "", w), w,
        indent=6)
    out += para(
        "THE ROWS THE SLOT DOES NOT FULLY FIX, named rather than buried: the "
        "Ambiguous ones. `slot_cells` charges Ambiguous two cells, so the slot "
        "leaves them unpadded - padding them would make the slot three cells "
        "wide under a CJK locale, and a three-cell slot shears everything "
        "after it, which is the worse failure. The bill for that choice falls "
        "on these %d rows alone: in a western terminal they land %d cell of the "
        "%d-column slot, one column short, which is why the kilo, cognition "
        "and z-ai rows of the tier-0 table carry one space less than their "
        "neighbours. The other %d rows are exact in both locales. Note what is "
        "NOT in this section any more: the crab and the whale used to be the "
        "two rows that overran the edge on every terminal. They are gone from "
        "tier 0, and the slot's remaining job is the one that is invisible "
        "until you change locale."
        % (len(EXPECTED_AMBIGUOUS), _visible_width(mark_slot("╔")), SLOT_COLS,
           len(BREAKAGE) - len(EXPECTED_AMBIGUOUS)), w, indent=6)
    return out


# ---- section 3: tier-0 mark in the root header only
def tier0_context_block(w, root):
    """V1/V2/V5 with the tier-0 mark in the root header, then W1 with it on
    every worker row too.

    V1, V2 and V5 stay ROOT HEADER ONLY. The previous run measured what a
    per-worker mark costs in those three layouts - section A below still
    carries that comparison, and it has not been deleted, because a verdict
    with its evidence removed is just an assertion.

    W1 is the exception, and it is an exception for a reason the other three
    cannot supply: there the mark sits in its OWN ALIGNED GUTTER, a column of
    its own that lines up down the whole list, instead of being jammed in front
    of the title where it costs the title its first three columns and lands
    against whatever the previous row's title happened to end with. Same mark,
    same two columns, different place - and that is the whole of the reversal.
    FOR W1 ONLY: section A's verdict does not transfer.
    """
    slot = slot_for(ORCHESTRATOR_VENDOR)
    out = section(
        w, "3. THE MARKS IN CONTEXT - tui_beauty V1 / V2 / V5 in the ROOT "
           "HEADER, then W1 with the mark on EVERY row",
        "The operator's %s mark for the orchestrator vendor (%s), through "
        "mark_slot, in the three tui_beauty layouts that place a label in a "
        "header. ROOT HEADER ONLY. The capture names the root %r - the bundle, "
        "not a harness - so the mark drawn here is the mark for the "
        "orchestrator this repo installs (`claude`, harness `claude-native`); "
        "the pane resolves the real harness id per session and draws a blank "
        "when the registry does not name it. W1 follows, drawn twice: once "
        "here with the tier-0 mark on every worker row, and once in section A "
        "with tier 1's."
        % (codepoint_of(t0_of(ORCHESTRATOR_VENDOR)["mark"]), ORCHESTRATOR_VENDOR,
           root["harness"]))
    for name, src, fn, cap in CONTEXTS:
        if cap.startswith("+"):
            continue
        out.append(hdr(w, "%s (%s) - %s" % (name, src, cap)))
        out += fn(root, w, slot, None)
        out.append("")
    out.append(hdr(w, "W1 web-parity (tui_beauty.py) - tier-0 mark on the "
                      "header AND on every worker row"))
    out += a_web(root, w, slot, lambda k: t0_mark_for(k["agent"]))
    out.append("")
    out += para("WHY W1 AND NOT V1/V2/V5: the mark costs the same two columns "
                "in all four, and in the first three it is spent out of the "
                "title's three leading columns on every row, against a ragged "
                "left edge. In W1 it is a gutter column that aligns down the "
                "list, next to a chip column and a rail column, so the title "
                "starts in the same place on every row by construction rather "
                "than by a promise. This REVERSES the verdict in section A for "
                "this variant only; section A is kept whole, and its arithmetic "
                "- a column blank on %d of %d rows under tier 1 - is also what "
                "tier 0's completeness has changed underneath it."
                % (len(root["kids"]) - sum(
                    1 for k in root["kids"] if mark_for(k["agent"]) != " "),
                   len(root["kids"])), w, indent=2)
    out += para("The mark is %d columns of a %d-column pane, once per tree, on "
                "the one label in the pane that names a company. Tier 0 has no "
                "WIDE pick left, so every mark in the header costs exactly what "
                "a Narrow one costs - and the slot is still what makes that "
                "true, because Ambiguous is charged two cells here rather than "
                "the one it happens to draw as."
                % (slot_width(slot), w), w, indent=2)
    return out


# ---- section 4: U+2554 beside a real box corner
def corner_block(w):
    """The U+2554 pick rendered against real box-drawing, for judgement.

    The risk, stated once and not editorialised past: U+2554 BOX DRAWINGS
    DOUBLE DOWN AND RIGHT is drawn from the SAME block as the pane's own
    corners, and a pane is made almost entirely of that block. A mark the
    operator can read as a corner is not a mark. This renders it in three
    positions the pane actually uses - in the gutter beside a vertical edge,
    hard against a corner, and in a header made of double rules - and the
    verdict is the operator's to make from what they see.
    """
    m = "╔"
    out = section(
        w, "4. U+2554 BESIDE A REAL BOX CORNER - the risk, rendered",
        "z-ai's pick is U+2554 BOX DRAWINGS DOUBLE DOWN AND RIGHT. It is the "
        "only mark in tier 0 that comes from the box-drawing block the pane "
        "itself is drawn from, so it is the only pick that can be misread as "
        "part of the frame instead of as a brand. The pane cannot recolour it "
        "differently from the frame to keep them apart - U+2554 is a "
        "monochrome text glyph, so it takes the row attribute, which is the "
        "same attribute the frame is drawn with. Here it is in three "
        "positions, next to tier 1's blank row for comparison.")
    bw = max(16, min(w - 2, 46))
    inner = bw - 2

    def row(text):
        return "│ " + pad(clip(text, inner - 2), inner - 2) + "│"

    task = "kg-base-unit"
    gutter = " │ %s freebuff · %s" % (m, task)
    demo = [
        ("in the row gutter, beside a vertical edge",
         [row(gutter),
          row(" │    freebuff · %s  <- blank" % task),
          row(" │ ▯ freebuff · %s  <- narrow" % task)]),
        ("in a corner position, where a corner belongs",
         ["╭" + "─" * 12 + m + "─" * (inner - 14) + "╮",
          row("the mark drawn where the frame itself turns"),
          "╰" + "─" * inner + "╯"]),
        ("inside a double-rule header (V1's shape)",
         ["╔═ " + m + " hivemind "
          + "═" * max(1, inner - 14 - _visible_width(m + " hivemind")) + "╗",
          "║" + "═" * inner + "║",
          "╚" + "═" * inner + "╝"]),
    ]
    for cap, lines in demo:
        out.append(hdr(w, cap))
        for ln in lines:
            out.append("  " + clip(ln, w - 2))
        out.append("")
    out += para(
        "WHAT TO LOOK FOR, not a verdict: whether the mark reads as a glyph "
        "standing in the gutter or as the frame continuing. It is one cell of "
        "the SAME weight and the SAME colour as the corner beside it, and "
        "nothing in the pane distinguishes them. If it reads as frame, the "
        "options are (a) drop the pick and let z-ai go back to tier 1's blank, "
        "or (b) accept it, on the grounds that it is drawn at a position where "
        "no real corner would ever be drawn - inside a row's text, never on "
        "the frame itself. Both are defensible; only (b) is a design "
        "decision, which is why it is the operator's.", w, indent=6)
    return out


# ---- section 5: colour interaction
def twotone_block(w):
    """Which marks take the row attribute and which bring their own colour.

    This section used to be about two marks in a fourteen-glyph table: the
    whale and the crab were Emoji_Presentation codepoints, the terminal drew
    them from the FONT's colour table, and a SGR foreground - which is all
    init_pair produces - did not touch them. The operator replaced both with
    Narrow glyphs, so the split this section exists to show is now empty: all
    %d marks are monochrome text glyphs and every one of them takes the row
    attribute.

    It is kept, and kept honest, for two reasons. The number it prints is
    derived from the marks rather than typed in, so it now says "0 of %d
    self-colour" instead of quietly claiming a whale it no longer has. And the
    rule that made the old numbers right is still armed: `check_marks()` fails
    if a Wide mark is ever added without being classified in
    EMOJI_PRESENTATION, at which point this section starts counting again
    without a code change.
    """
    out = section(
        w, "5. COLOUR INTERACTION - which marks bring their own colour, and "
           "which take the row's",
        "Painted with the same init_pair-derived escape on every row, brand "
        "colour for the vendor, exactly as section B does. Watch which rows "
        "change colour: NONE of them refuse it. An Emoji_Presentation "
        "codepoint draws from the font's colour table and ignores the escape; "
        "tier 0 had two of those - the whale and the crab - and the operator "
        "replaced both. Every mark in the table below is a monochrome text "
        "glyph and obeys the escape like a letter does.")
    out.append("")
    # The two counts come from `_t0_emoji`/`_t0_monochrome`, which walk the
    # table a second time by a different route, and the rows are painted from
    # a third. If any of the three ever disagrees, this section's prose - which
    # is generated from them - is generated from the wrong one.
    n_emoji, n_mono = _t0_emoji(), _t0_monochrome()
    selfcol, inherit = [], []
    for v in VENDORS:
        mark = t0_of(v["vendor"])["mark"]
        if mark == " ":
            continue
        idx = nearest_256(v["hex"])
        left = paint(idx, mark) + " " + paint(idx, v["vendor"])
        out.append("  " + clip(two_col(left, codepoint_of(mark), w - 2), w - 2))
        (selfcol if mark in EMOJI_PRESENTATION else inherit).append(v["vendor"])
    picks_inherit = [v for v in inherit if v in OPERATOR_PICKS]
    defaults = [v for v in inherit if v not in OPERATOR_PICKS]
    # Every row is an operator pick now, so the "and the N default rows" tail
    # is normally empty - said rather than printed as "the 0 default rows ()".
    tail = (" and the %d default rows (%s)" % (len(defaults), ", ".join(defaults))
            if defaults else
            " - and there are no default rows left, because the operator named "
            "all %d of them" % len(OPERATOR_PICKS))
    # How much of that list is actually a hue. Counted rather than asserted:
    # "brand colour" on a 256-colour terminal is sometimes a grey, and saying
    # eight brand colours when two of them are #3C3C3C would be the same kind
    # of thing this file exists to avoid claiming.
    hues = sum(1 for v in inherit
               if _hex_spread(by_id()[v]["hex"]) >= 20)
    out.append("")
    out += para(
        "SELF-COLOURED (%d of %d rows): %s. That is the change this round "
        "made to the pane, and it is invisible until you were looking for it: "
        "the whale and the crab used to ignore the escape entirely, so a "
        "tier-0 header was never one brand colour - it was two emoji in "
        "whatever colours the font's table gave them, standing beside %d "
        "monochrome glyphs that took the brand colour properly. Now every "
        "glyph in the header is on the same footing, and `paint()` above "
        "reaches all of them."
        % (n_emoji, n_emoji + n_mono,
           "none" if not selfcol else ", ".join(selfcol), n_mono), w,
        indent=2)
    out += para(
        "INHERITS THE ROW ATTRIBUTE (%d rows): the operator's %d picks (%s)%s. "
        "These are monochrome text glyphs. They take the brand colour from the "
        "row, or the row's dim/reverse attribute when the terminal has no "
        "colour - so these are the marks that carry branding, and also the "
        "ones that lose it entirely on a monochrome terminal."
        % (n_mono, len(picks_inherit),
           ", ".join(codepoint_of(t0_of(v)["mark"]) for v in picks_inherit),
           tail), w, indent=2)
    out += para(
        "THE TRADE, PLAINLY: it used to be a trade, and it is not any more. "
        "Two marks the pane could not colour were two marks that would not "
        "have taken a brand colour if it could, so losing them cost the "
        "operator the recognisability of a whale and a crab in exchange for a "
        "header where every mark obeys `init_pair`: %d hues and %d greys, all "
        "of them paintable, none of them self-coloured behind the pane's "
        "back. What the swap did NOT buy is width: U+1319E and U+1D54F are as "
        "wide as the whale in the only sense that matters now, which is "
        "'present in your font' - section 6."
        % (hues, n_mono - hues), w, indent=2)
    out += para(
        "STILL ARMED: check_marks() asserts that every Wide tier-0 mark is "
        "declared in EMOJI_PRESENTATION and that no Narrow one is, so a "
        "future emoji cannot slip into the table and quietly turn this section "
        "back into a two-tone pane without the numbers here changing.", w,
        indent=2)
    return out


# ---- section 6: does your font have these?
def font_block(w):
    """The operator's picks, with codepoint, plane and block, beside a control.

    Not a recommendation and not a review. A statement about font coverage,
    because five of the fourteen picks live outside the BMP and that is where
    fonts end: a project that ships Latin, Greek, Cyrillic, Box Drawing,
    Dingbats and Mathematical Operators has no reason at all to carry Egyptian
    Hieroglyphs or Bamum, and the terminal will draw whatever substitute it has
    for a codepoint the font does not.

    The left column is the pick; the RIGHT column of that same pair is
    `UNASSIGNED`, a codepoint the standard never assigns to anything. Any
    terminal renders both of them with the same substitute when it has no
    glyph, so the pair is a self-contained test: look at the two cells side by
    side on your own terminal, and if they match, the mark is missing. No
    second run, no font config, no `fc-list`.
    """
    out = section(
        w, "6. FONT COVERAGE - does your terminal's font have these?",
        "FIVE of the operator's %d picks are outside the BMP: the two Egyptian "
        "Hieroglyphs (U+1319E for deepseek, U+1316F for goose) and the Bamum "
        "letter (U+1680C for aws-kiro) are SMP historic scripts that a stock "
        "monospace font is unlikely to carry, and U+1D54F (xai, Mathematical "
        "Alphanumeric Symbols) and U+1F13A (kilo, Enclosed Alphanumeric "
        "Supplement) are outside the BMP and commonly missing too. The other "
        "nine are in blocks a monospace font has carried since about 1990. "
        "Each row prints the glyph beside U+0378, a codepoint the standard "
        "never assigns: if the two cells look the same, the font is missing "
        "the glyph."
        % len(VENDORS))
    out.append("")
    # The mark goes through `slot_pad` before it is padded again, so this
    # table is measured on the same pessimistic ruler as the pane - a CJK
    # terminal does not see a different offset here than it would there.
    out.append("  " + clip(pad("glyph", 5) + " " + pad("ctrl", 4) + " "
                           + pad("codepoint", 10) + pad("plane", 6)
                           + "vendor", w - 2))
    risky = []
    for v in VENDORS:
        vid = v["vendor"]
        mark = t0_of(vid)["mark"]
        cp = ord(mark)
        line = ("  " + pad(slot_pad(mark, SLOT_COLS), 5) + " "
                + pad(UNASSIGNED, 4) + " " + pad(codepoint_of(mark), 10)
                + pad(plane_name(cp), 6) + vid)
        out.append(clip(line, w - 2))
        block = block_of(cp)
        if vid in FONT_RISK:
            risky.append(vid)
            note = ("%s - %s. Outside the BMP: very likely drawn as a "
                    "substitute." % (block, FONT_RISK[vid]))
        else:
            note = ("%s - inside the BMP, in a block a monospace font has "
                    "carried for decades." % block)
        out += para(note, w, indent=6)
    out.append("")
    out += para(
        "WHAT A MISS COSTS: one glyph, in its own %d-column slot, on one row. "
        "The slot is charged before the terminal is asked anything, so a "
        "substitute box, a hex-digit box or a blank cell all land in the same "
        "two columns the mark would have - the vendor column does not move, "
        "nothing after it shears, and the row is still readable as a row. That "
        "is the whole argument for `mark_slot`: it is what makes a font miss "
        "cosmetic rather than structural. What a miss does NOT do is pretend: "
        "a tofu cell reads as 'this terminal has no glyph here', which is true."
        % SLOT_COLS, w, indent=2)
    tier1_have = len([v for v in VENDORS if v["t1"] != " "])
    out += para(
        "THE ONE PLACE A MISS COULD BITE: a terminal that falls back to an "
        "EMOJI font for a codepoint outside the BMP may draw it TWO cells wide "
        "even when the primary font had nothing, which would push that row one "
        "column past the slot. Two cells is what the slot has already "
        "reserved, so even that case lands inside the reservation - the "
        "pessimistic ruler is doing its job for a reason that is not about "
        "East Asian width at all. TIER 1 above is the escape hatch for a "
        "terminal where several of these are missing at once: %d marks, every "
        "one asserted Narrow, none of them outside the BMP."
        % tier1_have, w, indent=2)
    out += para(
        "WHAT THIS TEST CANNOT SEE: font FALLBACK. Most terminals - "
        "Terminal.app and iTerm2 among them - do not stop at the pane's font; "
        "they walk a fallback list per character, so a glyph the primary font "
        "lacks can still arrive from another installed font, drawn in that "
        "font's style rather than the pane's. A font-file scan is therefore a "
        "different question from 'will I see tofu', and this section is "
        "deliberately the terminal question: look at the two cells on your own "
        "screen. What a scan does answer, and it is worth knowing, is whether "
        "the pick exists on this machine at all.", w, indent=2)
    out += para(
        "WHAT IS NOT CLAIMED HERE: nothing about whether these are the right "
        "marks. They are the operator's decisions, this section measures "
        "whether a given terminal can draw them, and the answer to that is "
        "per-terminal and not per-vendor. %d of %d picks are outside the BMP; "
        "the rest are in blocks with no meaningful coverage risk."
        % (len(risky), len(VENDORS)), w, indent=2)
    return out


# ------------------------------------------------------------------ build
def build(root, w, rows, note):
    by_vendor = {v["vendor"]: v for v in VENDORS}

    def tier1(r):
        return by_vendor[r["vendor"]]["t1"]

    # Vendor order follows the registry's agent order, so adding an agent to
    # installer/registry.json moves its vendor's row rather than appending one.
    order, seen_v = [], set()
    for r in rows:
        if r["vendor"] in by_vendor and r["vendor"] not in seen_v:
            seen_v.add(r["vendor"])
            order.append(r["vendor"])
    tier1_rows = [{"vendor": v, "agents": by_vendor[v]["agents"]} for v in order]

    tier1_blanks = [v["vendor"] for v in VENDORS if v["t1"] == " "]
    tier2_have = [v["vendor"] for v in VENDORS if v["t2"] != " "]

    out = head(w, "tui_marks - harness brand marks for `og agents`",
               "width %d - %d agents, %d vendors, from installer/registry.json "
               "(untracked capture: %r + %d workers)"
               % (w, len(rows), len(VENDORS), root["title"], len(root["kids"])))

    out += ["", hdr(w, "ROSTER - a mark is keyed by VENDOR, because gemini "
                       "and agy are both google"), ""]
    out += roster_block(w, rows, note)
    out += para("Blank marks are not omissions: %d of %d vendors have no "
                "one-cell skeleton of their real logo, and an invented symbol "
                "would read as a fact the registry does not support."
                % (len(tier1_blanks), len(VENDORS)), w, indent=2)

    # ---- tier 0, first, because the operator picked it
    out += tier_block(w, tier1_rows, {
        "name": "TIER 0 - OPERATOR PICKS  (decided, not proposed)",
        "head": "", "tail_head": "agents",
        "mark": lambda r: slot_for(r["vendor"]),
        "tail": lambda r: " ".join(r["agents"]),
        "note": lambda r: "%s %s · %s" % (
            codepoint_of(t0_of(r["vendor"])["mark"]),
            " ".join(unicodedata.name(t0_of(r["vendor"])["mark"]).lower().split())
            if t0_of(r["vendor"])["mark"] != " " else "no mark",
            t0_of(r["vendor"])["src"]),
        "why": lambda r: (
            "%s. %s" % (width_class(t0_of(r["vendor"])["mark"]),
                        t0_of(r["vendor"])["why"])
            if t0_of(r["vendor"])["mark"] != " " else t0_of(r["vendor"])["why"]),
    }, "COMPLETE: %d picks for %d vendors, so no row in this table is blank - "
       "the previous round left four (kilo, goose, hermes, cognition) and this "
       "one closed them. COST %d columns for the slot plus the space that "
       "separates it from the name: %d columns a row, whatever the glyph is - "
       "that is the entire point of mark_slot(), and the reason this table is "
       "not simply tier 1 with different characters in it. WIDTHS: %d Narrow, "
       "%d Ambiguous, 0 Wide - dropping the crab and the whale removed BOTH "
       "two-cell picks, so every mark in this table now draws in one cell here "
       "or in two under a CJK locale, and never in two here. THE SLOT IS STILL "
       "REQUIRED: `slot_cells` charges Ambiguous two on purpose, so kilo, "
       "cognition and z-ai hold their two columns in both locales while a "
       "naive padder would let all three shear the column after them. "
       "DEPENDS a font carrying the codepoints listed in section 6 - no "
       "private use area, no patched font, and, since the whale and the crab "
       "went, no emoji font at all: every glyph here is a monochrome text "
       "glyph that takes the row attribute. FONT RISK: %d of the %d are "
       "outside the BMP, in blocks a stock monospace font often does not "
       "carry, and will likely draw as tofu; section 6 is the test, and the "
       "slot is what keeps such a miss cosmetic. TIER 1 below is unchanged "
       "and keeps its %d honest blanks: it is the all-Narrow alternative, and "
       "the thing to fall back to if a mark here turns out to be missing."
       % (len(OPERATOR_PICKS), len(VENDORS), SLOT_COLS, SLOT_COLS + 1,
          len(VENDORS) - len(EXPECTED_AMBIGUOUS), len(EXPECTED_AMBIGUOUS),
          len(FONT_RISK), len(VENDORS), len(tier1_blanks)),
       mark_w=SLOT_COLS, mark_padder=slot_pad)

    out += tier_block(w, tier1_rows, {
        "name": "TIER 1 - PLAIN UNICODE  (the safe default)",
        "head": "mark", "tail_head": "agents",
        "mark": tier1,
        "tail": lambda r: " ".join(r["agents"]),
        "note": lambda r: by_vendor[r["vendor"]]["t1_name"],
        "why": lambda r: by_vendor[r["vendor"]]["t1_why"],
    }, "COST 1 column for the mark plus the space that separates it from the "
       "name: 2 columns a row. DEPENDS a font carrying Geometric Shapes, "
       "Dingbats and Misc Technical - true of every monospace font shipped "
       "since about 1990, and nothing else: no colour, no private use area, no "
       "font install. DEGRADES never in alignment: a font missing the glyph "
       "draws tofu in exactly the cell the mark occupied, so boxes, rules and "
       "right-aligned clocks stay put and only the glyph is lost. %d marks, %d "
       "honest blanks (%s), every one asserted Narrow."
       % (len(VENDORS) - len(tier1_blanks), len(tier1_blanks),
          ", ".join(tier1_blanks)))

    out += tier_block(w, tier1_rows, {
        "name": "TIER 2 - NERD FONT  (READ THIS SECTION TO TEST YOUR FONT)",
        "head": "mark", "tail_head": "agents",
        "mark": lambda r: by_vendor[r["vendor"]]["t2"],
        "tail": lambda r: " ".join(r["agents"]),
        "note": lambda r: by_vendor[r["vendor"]]["t2_name"],
        "why": lambda r: by_vendor[r["vendor"]]["t2_why"],
    }, "If the marks above render as EMPTY BOXES, your terminal font has no "
       "Nerd Font glyphs and tier 2 is dead for you. That is what this section "
       "is for: a font test the operator runs on their own machine, in their "
       "own terminal, before anyone writes a line of the pane. COST 1 column, "
       "same as tier 1 - but only for the %d of %d vendors that have a real "
       "glyph in the Nerd Fonts set. DEPENDS a patched font (Nerd Fonts or "
       "equivalent, 200KB+ of additions) and a terminal that does not shaper "
       "PUA into emoji. DEGRADES to tofu for the other %d vendors, because no "
       "brand glyph exists to render - and every PUA codepoint is East Asian "
       "AMBIGUOUS, so even with the font installed a CJK-locale terminal "
       "draws these two cells wide and the boxes shear."
       % (len(tier2_have), len(VENDORS), len(VENDORS) - len(tier2_have)))

    out += tier_block(w, rows, {
        "name": "TIER 3 - ASCII TAGS  (pipes, logs, and everyone else's terminal)",
        "head": "mark", "tail_head": "agent",
        "mark": lambda r: tier3_for(r["id"]),
        "tail": lambda r: r["id"],
        "note": lambda r: AGENT_TAGS[r["id"]],
        "why": lambda r: "%s = %s (%s)" % (r["label"], r["id"], r["vendor"]),
    }, "COST 4 columns for [CC] against tier 1's 1: 3 columns a row, and "
       "nothing else - no font, no colour, no width class. DEPENDS nothing at "
       "all: ASCII survives `og agents --once | tee og.log`, a CI transcript, "
       "a pager, a latin-1 locale and every font ever shipped. DEGRADES never; "
       "this is the only tier that is still legible where the pane is not "
       "being watched by a person who already knows the logos. Keyed by AGENT "
       "here rather than vendor, so [AGY] and [GEM] stay apart.")

    # ---- 2: the slot, justified by the failure it removes
    out += breakage_block(w)

    # ---- 3: the operator's mark in the layouts, root header only
    out += tier0_context_block(w, root)

    # ---- A: the tier-1 mark in context, and the per-worker comparison.
    # Kept whole on purpose. Section 3 is the proposal; this is the evidence
    # for why a mark on EVERY worker row was rejected, and deleting evidence
    # because the conclusion is already settled is how a prototype starts
    # lying. It is not promoted: nothing above points at it as the answer.
    # The capture's root names the BUNDLE (`hivemind`), not a harness id, so
    # there is nothing in it to resolve. The orchestrator this repo installs is
    # `claude` (installer/registry.json, harness `claude-native`), and that is
    # the mark the header carries here. The pane resolves the real per-session
    # harness id and draws a blank when the registry does not name it.
    root_mark = by_vendor[ORCHESTRATOR_VENDOR]["t1"]
    out += ["", rule(w, "═"),
            hdr(w, "A. THE TIER-1 MARK IN CONTEXT - tui_beauty's V1 / V2 / V5, "
                   "real capture (kept: the per-worker evidence)")]
    out += para("Section 3 above is the proposal - the operator's mark in the "
                "root header. This section is the previous run's comparison, "
                "left intact: the same layouts with tier 1's mark, then the "
                "same layouts with a mark on EVERY worker row. It is here as "
                "the measurement behind the verdict that per-worker marks do "
                "not earn their column, not as an alternative to section 3.",
                w)
    out += para("The capture names the root %r - the bundle, not a harness - "
                "so the root mark drawn here is %s, the orchestrator this "
                "repo installs (registry row `claude`, harness `claude-native`). "
                "The pane resolves the real harness id per session via "
                "`harness_label` (og_agents.py:528) and draws a BLANK when the "
                "registry does not name it - never a guess."
                % (root["harness"], root_mark), w)
    out.append("")
    out += context_block(w, root, root_mark, with_worker_marks=False)
    out += context_block(w, root, root_mark, with_worker_marks=True)

    # ---- the measurement that answers "is the worker mark worth its column"
    out += [hdr(w, "MEASURED - what the per-worker mark costs, on THIS "
                   "capture")]
    # The indent is capped by the width: at 12 columns a fixed 15-column
    # gutter is wider than the pane, which is the whole thing this file checks.
    col = min(16, max(0, w - 6))     # 2 indent + 8 agent + 1 + 5 mark
    out.append("  " + clip(pad("agent", 8) + " " + pad("mark", 5), col - 2)
               + clip(" task", max(0, w - col)))
    role_rows, vendor_rows, drawn = [], [], 0
    for k in root["kids"]:
        m = mark_for(k["agent"])
        base = re.sub(r"_\d+$", "", k["agent"] or "")
        if m != " ":
            drawn += 1
            why = "the vendor mark, 2 columns a row"
        elif base in ROLES:
            role_rows.append(k["agent"])
            why = "BLANK: %s is a role, not a harness - it has no vendor" % base
        else:
            vname = vendor_for(k["agent"]) or "?"
            vendor_rows.append("%s -> %s" % (k["agent"], vname))
            why = "BLANK: vendor %s has no honest one-column mark" % vname
        out.append("  " + clip(pad(k["agent"] or "(none)", 8) + " " + pad(m, 5),
                               col - 2)
                   + clip(k["title"], max(0, w - col)))
        out += para(why, w, indent=col)
    blanks = len(root["kids"]) - drawn
    out += para("VERDICT at width %d: %d of the %d worker rows in this capture "
                "draw a mark; %d draw a blank%s%s. A column that is blank on "
                "%d rows in %d is not a mark, it is a gutter with a logo in it, "
                "and the rows it costs are exactly the rows whose task title "
                "gets two columns shorter. The ROOT mark is the one that earns "
                "its place: drawn once per tree, beside the only label in the "
                "pane that names a company."
                % (w, drawn, len(root["kids"]), blanks,
                   " - %s name a role (reviewer, scout), which has no vendor to "
                   "mark" % ", ".join(sorted(set(role_rows))) if role_rows else "",
                   "; %s have a vendor with no honest mark" % ", ".join(vendor_rows)
                   if vendor_rows else "",
                   blanks, len(root["kids"])), w)
    # Two things changed underneath that verdict this round, and a verdict whose
    # inputs have moved is not a verdict any more - it is a fossil. Both changes
    # are stated here rather than by deleting the section above.
    out += para(
        "WHAT THIS ROUND CHANGES ABOUT IT, both ways. (1) The arithmetic above "
        "is a TIER-1 measurement and tier 1 still has %d blank vendors, but "
        "tier 0 has none: the operator named a mark for all %d, so a per-worker "
        "column drawn in tier 0 would be full on this capture rather than blank "
        "on %d rows. (2) The other half of the verdict - that the mark costs "
        "the title its first columns - still stands for V1, V2 and V5, because "
        "in those three the mark is jammed in FRONT of the title and lands "
        "against whatever the row above ended with. W1 (section 3) is the "
        "layout where that half is false: there the mark is a gutter column of "
        "its own, aligned down the whole list. So the verdict is REVERSED FOR "
        "W1 ONLY, on the strength of the render above rather than a new "
        "opinion, and unchanged everywhere else."
        % (len([v for v in VENDORS if v["t1"] == " "]), len(VENDORS), blanks),
        w)

    # ---- B: colour
    out += ["", rule(w, "═"),
            hdr(w, "B. BRAND COLOUR - real ANSI 256 escapes, rendered for "
                   "real")]
    out += para("Each tier-1 mark painted in its vendor's own colour with a "
                "real escape, then the escape itself as text and the hex it "
                "was derived from. If your terminal shows no colour here, the "
                "escape is being stripped and tier B is unavailable to you.",
                w)
    out += para("WHY COLOUR IS AFFORDABLE NOW: the view calls "
                "curses.use_default_colors() (og_agents.py:1366), so "
                "init_pair(n, <colour>, -1) paints a coloured FOREGROUND on "
                "the terminal's own background. Brand colour and a "
                "transparent pane are not in conflict any more - the opaque "
                "black pair 0 that made colour a background fill is gone.",
                w)
    out += para("WHAT IT COSTS: the view's FIRST EVER init_pair calls. It "
                "paints with A_NORMAL, A_REVERSE and A_DIM and pair 0 alone "
                "(curses.wrapper has already called start_color(), so the "
                "colour machinery is live but unused). Branding %d vendors "
                "means %d pairs, a has_colors()/COLOR_PAIRS branch for the "
                "8- and 16-colour terminals, and a fourth paint role for the "
                "mark so a row can carry both its row attribute and its vendor "
                "colour. It also has to degrade: a terminal with no colour "
                "falls back to tier 1's glyph, which is exactly what tiers 1-3 "
                "are for. Piping is already safe: --once output is stripped of "
                "SGR escapes before it is printed (og_agents.py:639)."
                % (len(VENDORS), len(VENDORS)), w)
    out.append("")
    for v in VENDORS:
        if v["t1"] == " ":
            continue
        idx = nearest_256(v["hex"])
        left = paint(idx, v["t1"]) + "  " + v["vendor"]
        # The glyph NAME only if it fits beside the vendor. Tier 1 already
        # printed it, and a right column long enough to starve the left one
        # deletes the mark this section exists to show - which is exactly what
        # a 39-character name did at width 46.
        name = v["t1_name"] if _visible_width(left) + 2 + len(v["t1_name"]) <= w - 2 else ""
        out.append("  " + two_col(left, name, w - 2))
        out.append("  " + clip("\\x1b[38;5;%dm" % idx, w - 2))
        detail = "%s -> nearest 256 index %d" % (v["hex"], idx)
        if _visible_width("  " + detail) > w:
            detail = "%s 256:%d" % (v["hex"], idx)
        out.append("  " + clip(detail, w - 2))
    out += para("The operator's 'Claude orange is about 208' is 208 = "
                "(255,135,0), a saturated orange. Claude's actual brand coral "
                "#D97757 lands on %d by nearest-RGB, not 208: the 6x6x6 cube "
                "has six levels per channel and cannot hold a terracotta. AWS "
                "#FF9900 is the vendor that really does land on 208. 208 reads "
                "better on a dark pane than the literal match, and both are "
                "defensible; neither is the brand colour exactly, and that is a "
                "property of a 256-colour terminal rather than of this table."
                % nearest_256("#D97757"), w)
    out += para("Monochrome brands (cursor #3C3C3C, xAI #FFFFFF, cognition, "
                "goose, moonshot, opencode-zen) are the honest greys here: "
                "there is no hue to reproduce, so a neutral ramp is all the "
                "256 palette can say. Three of those are DARK brands (cursor "
                "237, moonshot 234) and this pane is transparent, so on a dark "
                "terminal the mark is drawn in the same darkness as the "
                "background it sits on: correct, and invisible. A dark brand "
                "colour needs a light stand-in chosen per terminal, which is "
                "the first thing colour buys that a glyph cannot.", w)

    # ---- 4: the U+2554 risk, rendered
    out += corner_block(w)

    # ---- 5: what emoji do to that colour, named
    out += twotone_block(w)

    # ---- 6: whether a stock font has any of this, said plainly
    out += font_block(w)

    picks = len(OPERATOR_PICKS)
    out += ["", rule(w, "═"),
            hdr(w, "%d agents - tier 0: %d operator picks in a %d-column slot, "
                   "tier 1: %d marks, tier 2: %d, tier 3: %d ASCII tags - no "
                   "line over %d cols"
                % (len(rows), picks, SLOT_COLS, len(VENDORS) - len(tier1_blanks),
                   len(tier2_have), len(AGENT_TAGS), w)),
            rule(w, "═")]
    return out


REGISTRY_ROWS = []


def main():
    global REGISTRY_ROWS
    argv = sys.argv[1:]
    w = 46
    if "--width" in argv:
        try:
            w = max(12, int(argv[argv.index("--width") + 1]))
        except (ValueError, IndexError):
            w = 46

    REGISTRY_ROWS, note = load_registry()
    if not REGISTRY_ROWS:
        REGISTRY_ROWS = [{"id": k, "harness": k, "label": k, "vendor": "unknown",
                         "worker": ""} for k in AGENT_TAGS]
    check_marks()

    root, _now = load()
    lines = build(root, w, REGISTRY_ROWS, note)

    over = [(i, _visible_width(ln)) for i, ln in enumerate(lines)
            if _visible_width(ln) > w]
    for ln in lines:
        print(ln)
    if over:
        for i, got in over:
            sys.stderr.write("tui_marks: line %d is %d cols, width %d\n"
                             % (i + 1, got, w))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
