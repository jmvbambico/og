"""Contract tests for installer/og_themes.py -- the theme model, the converter, the
readability guard and the persistence.

THESE ARE THE TESTS THAT DECIDE WHETHER THE BACKGROUND DECISION WAS MADE
CORRECTLY, so they are written against the things the rest of the repo already
recorded rather than against whatever the code happens to do:

  * the CONVERTER is pinned on the two indices `_VENDOR_COLOURS`' own comment
    block asserts (#F3EF4E -> 227 and #A4BA96 -> 144), because that comment is
    where the conversion this file implements was reasoned out in the first
    place. 144 is the CONVERTER's answer and NOT what the table ships -- the
    table overrides it with 108 on purpose -- so a test that asserted 108 here
    would be asserting the hand decision twice and the maths not at all.
  * the GUARD is pinned on all fourteen marks being byte-identical on dracula,
    and on hue holding (not exact lightness) when they move.
  * the OG ROUND TRIP is pinned on #87afaf -> 109, #00ff00 -> 46, #ffff00 ->
    226. If the converter does not reproduce the indices the pane already
    shipped, `og` is not today's colours and the regression guard is a fake.
  * the LOADER is pinned twice over: silent on an absent or unreadable file, and
    LOUD — naming the slug AND the role — on a structurally wrong row.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import og_agents as a
import og_themes as th


# ---------------------------------------------------------------------------
# the converter
# ---------------------------------------------------------------------------

def test_the_palette_is_the_240_addressable_cells():
    """16..255, and not 256: the first sixteen are the terminal's own ANSI slots
    whose xterm defaults are nothing like their names, so including them would
    let a brand colour land on "system red"."""
    assert len(th.PALETTE) == 240
    assert th.PALETTE[0][0] == 16
    assert th.PALETTE[-1][0] == 255
    assert th.CUBE_RUNGS == (0, 95, 135, 175, 215, 255)


def test_ansi256_rgb_agrees_with_the_index_arithmetic():
    """The cube is 16 + 36r + 6g + b over the six rungs; the ramp is 8 + 10k."""
    assert th.ansi256_rgb(16) == (0, 0, 0)
    assert th.ansi256_rgb(46) == (0, 255, 0)      # the neon green
    assert th.ansi256_rgb(226) == (255, 255, 0)    # the neon yellow
    assert th.ansi256_rgb(227) == (255, 255, 95)   # kilo's lemon
    assert th.ansi256_rgb(108) == (135, 175, 135)  # freebuff's sage
    assert th.ansi256_rgb(109) == (135, 175, 175)  # META_COLOR
    assert th.ansi256_rgb(231) == (255, 255, 255)
    assert th.ansi256_rgb(232) == (8, 8, 8)
    assert th.ansi256_rgb(255) == (238, 238, 238)


def test_kilos_brand_hex_converts_to_227():
    """THE ASSERTION `_VENDOR_COLOURS`' COMMENT BLOCK MAKES.

    "the brand is #F3EF4E, a bright lemon, and only the TOP rung of the 6x6x6
    cube can hold it ... the colour is 227 (255,255,95)". The neighbouring cube
    cells the comment rules out (221 and 191) must genuinely be further in OKLab,
    or the comment is describing a converter this one is not.
    """
    assert th.hex_to_ansi256("#F3EF4E") == 227
    assert th.ansi256_rgb(227) == (255, 255, 95)
    brand = th.rgb_to_oklab(th.hex_to_rgb("#F3EF4E"))
    for neighbour in (221, 191):
        assert math.dist(brand, th.rgb_to_oklab(th.ansi256_rgb(neighbour))) \
            > math.dist(brand, th.rgb_to_oklab(th.ansi256_rgb(227)))


def test_the_sage_converts_to_144_even_though_the_table_ships_108():
    """THE OTHER HALF OF THE SAME COMMENT, AND THE EASIEST THING TO GET WRONG.

    "The NEAREST index in the whole palette is 144, (175,175,135), at OKLab 3.087
    and sRGB 21.6 -- but 108, (135,175,135), is shipped instead." 144 is what the
    CONVERTER must return. 108 is a deliberate hand override the table justifies
    on hue grounds, and it is not this converter's business.
    """
    assert th.hex_to_ansi256("#A4BA96") == 144
    assert th.ansi256_rgb(144) == (175, 175, 135)
    assert a._VENDOR_COLOURS["z-ai"] == 108, \
        "the hand override is a table decision, and the table is unchanged"
    brand = th.rgb_to_oklab(th.hex_to_rgb("#A4BA96"))
    assert math.dist(brand, th.rgb_to_oklab(th.ansi256_rgb(144))) \
        < math.dist(brand, th.rgb_to_oklab(th.ansi256_rgb(108)))


def test_the_converter_is_deterministic_and_exact_on_cube_cells():
    # Every cell maps to ITSELF: the converter is a nearest-cell search over a set
    # that contains the cell, so a distance of zero has to win.
    for index, rgb in th.PALETTE:
        assert th.hex_to_ansi256(th.rgb_to_hex(rgb)) == index, (index, rgb)
    # And it is deterministic: same input, same answer, on any machine, because
    # ties break on the lower index rather than on iteration order.
    assert th.hex_to_ansi256("#7f7f7f") == th.hex_to_ansi256("#7f7f7f")
    assert th.hex_to_ansi256("#7f7f7f") in {row[0] for row in th.PALETTE}


def test_the_converter_never_spends_more_than_the_cap_of_oklab_error():
    """THE CONVERTER MUST NOT DESATURATE — AND MUST NOT BUY CHROMA AT ANY PRICE.

    THE DEFECT. The greyscale ramp spans the whole lightness axis, so a
    nearest-cell search over all 240 cells hands it any dim, low-chroma colour:
    dracula's background #282a36 (OKLab chroma 0.022) resolved to 235, nord's
    #2e3440 to 236, and solarized-dark's #002b36 (chroma 0.049) to 235.
    Fifty-two role-colours across twenty-three of the twenty-five catalog themes
    lost their chroma that way, and on the terminal this actually ships to --
    TERM=tmux-256color, so curses.COLORS is 256 and init_color does not exist --
    the entire feature rendered grey. So a chromatic target is searched against
    the chromatic cells only.

    THE CONSEQUENCE NOBODY DREW AT FIRST, WHICH IS WHY THIS TEST HAS TWO HALVES.
    The cube's lowest non-zero rung is 95, so a dark chromatic target has NO
    unsaturated chromatic cell anywhere near it and the nearest chromatic cell is
    a saturated primary: refusing the ramp outright made dracula's background 52
    (#5f0000, a dark maroon) and nord's 23 (#005f5f, teal), each about 0.13 from
    the colour asked for against the grey's 0.03. That is 4.5x worse in exactly the
    metric `hex_to_ansi256` minimises, on the DEFAULT theme's background.

    So the guard is bounded by `MAX_CHROMA_PENALTY`: past that much extra OKLab
    error the palette plainly cannot hold the colour's tint, and the nearest cell
    wins. One rule, every colour, no per-theme exceptions.

    THE RULE, over a grid of real colours rather than five of them: for a
    chromatic target the resolved cell is neutral if and only if the nearest
    chromatic cell costs more than the cap over the nearest cell overall. Both
    directions are asserted, so neither a loosened cap nor a hard ban on the ramp
    can pass.
    """
    neutral = {index for index, rgb in th.PALETTE if rgb[0] == rgb[1] == rgb[2]}
    assert len(neutral) == 30, "24 ramp cells plus the cube's own six-cell diagonal"
    chromatic_pool = [(index, th.rgb_to_oklab(rgb))
                      for index, rgb in th.PALETTE if index not in neutral]
    every_pool = [(index, th.rgb_to_oklab(rgb)) for index, rgb in th.PALETTE]
    kept = refused = 0
    for r in range(0, 256, 17):
        for g in range(0, 256, 17):
            for b in range(0, 256, 17):
                value = "#%02x%02x%02x" % (r, g, b)
                index = th.hex_to_ansi256(value)
                target = th.rgb_to_oklab(th.hex_to_rgb(value))
                if target[1] ** 2 + target[2] ** 2 <= th.NEUTRAL_CHROMA ** 2:
                    assert index in neutral, (value, index)
                    continue
                nearest = min(math.dist(target, lab) for _i, lab in every_pool)
                tinted = min(math.dist(target, lab) for _i, lab in chromatic_pool)
                over_cap = tinted - nearest > th.MAX_CHROMA_PENALTY
                assert (index in neutral) == over_cap, (value, index)
                refused += over_cap
                kept += not over_cap
    # Neither half may be empty: a converter that refused everything, or one that
    # never refused anything, would both satisfy the loop above.
    assert kept > 400 and 0 < refused < 100, (kept, refused)


def test_the_cap_sends_draculas_background_to_its_nearest_cell():
    """THE CAP ON A LOW-CHROMA DARK BACKGROUND, WHERE IT MATTERS MOST.

    dracula's background is the canonical low-chroma dark case, and its hex
    quantises on every terminal that reports 256 colours. #282a36's OKLab chroma
    is 0.022, just above NEUTRAL_CHROMA, and at lightness 0.288 the only chromatic
    cells within reach are the cube's dark saturated ones: 52 (#5f0000, a dark
    maroon) at 0.1359 and 17 (#00005f, a navy) are both candidates the ramp-less
    search wanted, and both are 0.106+ further from the colour than 235 (#262626,
    a charcoal grey) at 0.0295. The palette has no charcoal-blue cell to give it;
    235 is the honest answer and MAX_CHROMA_PENALTY is what says so out loud
    rather than shipping the maroon.

    Every number in that paragraph is asserted below, so the test fails loudly if
    the palette, the cap or the converter moves.
    """
    target = th.rgb_to_oklab(th.hex_to_rgb("#282a36"))
    nearest = min((math.dist(target, th.rgb_to_oklab(rgb)), index)
                  for index, rgb in th.PALETTE)
    tinted = min((math.dist(target, th.rgb_to_oklab(rgb)), index)
                 for index, rgb in th.PALETTE
                 if rgb[0] != rgb[1] or rgb[1] != rgb[2])
    assert nearest == (pytest.approx(0.0295, abs=5e-4), 235), nearest
    assert tinted[1] == 52 and tinted[0] == pytest.approx(0.1359, abs=5e-4), tinted
    assert tinted[0] - nearest[0] > th.MAX_CHROMA_PENALTY, "the cap moved and this is no longer an example"
    assert th.hex_to_ansi256("#282a36") == nearest[1]
    # And the two it must NOT be: the saturated primaries the unbounded guard
    # reached for. dracula's pane background was going to be maroon.
    assert th.ansi256_rgb(52) == (95, 0, 0)
    assert th.ansi256_rgb(17) == (0, 0, 95)
    assert th.hex_to_ansi256("#282a36") not in (17, 52)


def test_a_chromatic_colour_the_cap_allows_still_keeps_its_hue():
    """THE OTHER HALF OF THE CAP, so it cannot be "fixed" by deleting itself.

    The cap is a bound on the PRICE of chroma, not a licence to desaturate
    anything dark. nord's background #2e3440 (chroma 0.023, lightness 0.324) is
    the same shape as dracula's and does get refused the ramp -- but
    synthwave-84's rail #3e3e6d (chroma 0.079) is only 0.023 from the nearest
    cell overall, so it keeps a chromatic cell, and the old parchment #f0e6d2
    that used to paint as 254, a pure grey, is 0.015 from its nearest cell and
    keeps cell 224 (#ffd7d7, a pale pink).
    """
    for value, index in (("#3e3e6d", 24), ("#f0e6d2", 224), ("#3e3e6d", 24)):
        assert th.hex_to_ansi256(value) == index, value
        rgb = th.ansi256_rgb(th.hex_to_ansi256(value))
        assert rgb[0] != rgb[1] or rgb[1] != rgb[2], (value, rgb)
    # Both are inside the cap, so this is the guard, not the fallback.
    for value in ("#3e3e6d", "#f0e6d2"):
        target = th.rgb_to_oklab(th.hex_to_rgb(value))
        nearest = min(math.dist(target, th.rgb_to_oklab(rgb)) for _i, rgb in th.PALETTE)
        tinted = min(math.dist(target, th.rgb_to_oklab(rgb)) for _i, rgb in th.PALETTE
                     if not (rgb[0] == rgb[1] == rgb[2]))
        assert tinted - nearest <= th.MAX_CHROMA_PENALTY, (value, tinted - nearest)


def test_a_neutral_colour_still_lands_on_the_ramp():
    """THE OTHER HALF OF THE SAME RULE, so the guard cannot eat the greys.

    A colour with no chroma to preserve must keep resolving to the greyscale
    ramp: those are the colours that WERE grey on purpose, and moving them onto
    the cube's saturated diagonal would be the same defect the other way round.
    This is why the rule is a THRESHOLD and not a blanket ban on the ramp.
    """
    for value in ("#000000", "#7f7f7f", "#fefefe", "#fafafa", "#282828", "#e6e6e6"):
        index = th.hex_to_ansi256(value)
        rgb = th.ansi256_rgb(index)
        assert rgb[0] == rgb[1] == rgb[2], (value, index, rgb)
    assert th.hex_to_ansi256("#7f7f7f") == 244
    assert th.hex_to_ansi256("#282828") == 235
    assert th.hex_to_ansi256("#fefefe") == 231


def test_the_threshold_is_the_constants_own_and_the_catalog_straddles_it():
    """WHY 0.02, FROM THE DATA RATHER THAN FROM TASTE.

The catalog's own chroma distribution straddles the cut: the most chromatic
    NEUTRAL role sits at 0.0196 (Monokai Dark's rail #464539) and the least
    chromatic CHROMATIC one at 0.0201 (Monokai Light's rail #d3c5bc, which a
    post-conversion nudge pushed just over the line), with nothing in between.
    `NEUTRAL_CHROMA` is already the constant `oklab_hue` uses to decide whether a
    colour has a hue at all, so reusing it here means the converter and the hue
    test cannot disagree about what neutral means.

    The margin is worth watching rather than trusting: it is about 0.0005 wide on
    the neutral side, so a future hex nudged to land near 0.02 could move it. The
    assertion below is what will say so.
    """
    catalog = th.load_catalog()
    chromatic, neutral = [], []
    for slug, theme in catalog.items():
        if slug == "og":
            continue
        for role, value in theme.colors.items():
            chroma = th.oklab_hue(th.hex_to_rgb(value))[1]
            (chromatic if chroma > th.NEUTRAL_CHROMA else neutral).append(chroma)
    assert max(neutral) < th.NEUTRAL_CHROMA < min(chromatic), (
        f"the catalog no longer straddles the threshold: the most chromatic "
        f"neutral is {max(neutral):.4f} and the least chromatic chromatic one "
        f"is {min(chromatic):.4f}")


def test_the_converter_accepts_uppercase_brand_hexes_and_refuses_junk():
    """The catalog is lowercase BY HOUSE RULE; a brand hex pasted out of a vendor's
    stylesheet is not, and rejecting "#F3EF4E" would guard against nothing."""
    assert th.hex_to_ansi256("#F3EF4E") == th.hex_to_ansi256("#f3ef4e")
    for bad in ("#fff", "#GGGGGG", "f3ef4e", "", "default"):
        with pytest.raises(th.ThemeError):
            th.hex_to_ansi256(bad)


def test_contrast_is_the_wcag_formula():
    """The same arithmetic as tests/test_og_themes_catalog.py, on purpose: the
    catalog is judged on the WCAG floor, so the code that trusts it has to
    compute it identically or the two disagree about the same colour."""
    assert th.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=1e-9)
    assert th.contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0, abs=1e-9)
    assert th.contrast_ratio("#282a36", "#282a36") == pytest.approx(1.0, abs=1e-9)
    assert th.relative_luminance("#000000") == 0.0
    assert th.relative_luminance("#ffffff") == pytest.approx(1.0, abs=1e-9)


def test_curses_value_is_minus_one_for_default_and_a_palette_cell_otherwise():
    """`init_pair(n, fg, bg)` takes colour NUMBERS. There is no truecolor path and
    there never was a correct one here: `curses.init_extended_pair` — the only API
    through which a packed RGB value could become a colour — is ABSENT from this
    build's ncurses (6.0.20150808), as is `init_extended_color`. The branch that
    packed one was handing a 24-bit integer to a slot whose meaning is a palette
    index, and it was unreachable besides: the terminal this ships to is
    `TERM=tmux-256color` with no `ccc`, so `init_color` fails there too.

    So `colors` CANNOT change the answer, and that is the assertion: a terminal
    claiming truecolor paints the SAME cell as one that does not, and no value
    this returns is anything but `-1` or one of the 240.
    """
    assert th.curses_value("default", 256) == -1
    assert th.curses_value("default", 1 << 24) == -1
    assert th.curses_value("#87afaf", 256) == 109
    for colors in (0, 8, 16, 256, 1 << 24, 1 << 32):
        assert th.curses_value("#ffd7af", colors) == 223, colors
        assert th.curses_value("#282a36", colors) == 235, colors


def test_no_value_curses_value_returns_can_be_a_packed_rgb_triple():
    """The defect, pinned over the whole catalog rather than on two hexes.

    The old truecolor branch returned `(r << 16) | (g << 8) | b` — 16.7 million at
    most, and `init_pair` was handed it as a colour NUMBER. If any role in any
    theme could still produce one, `colours.attr` would put it in a pair and the
    terminal would paint whatever slot that number happens to own, which is a
    colour nobody chose. Every value is `-1` or a cell of the 240.
    """
    for slug, theme in th.load_catalog().items():
        for role, value in theme.colors.items():
            answer = th.curses_value(value, 1 << 24)
            assert answer == -1 or 0 <= answer < 256, (slug, role, value, answer)


# ---------------------------------------------------------------------------
# the OG round trip -- today's colours, provably
# ---------------------------------------------------------------------------

OG = {"meta": "#87afaf", "idle": "#00ff00", "working": "#ffff00",
      "failed": "#ff0000"}


def test_og_hexes_convert_back_to_the_indices_the_pane_already_shipped():
    """IF THIS FAILS, `og` IS NOT TODAY'S COLOURS.

    The four hexes in the `og` row are the exact sRGB of ansi 109 (META_COLOR),
    46 (idle), 226 (working) and 196 (the failed mark's neon red). They were
    resolved from the cube, not eyeballed, and the converter has to reproduce them
    exactly — otherwise the theme that exists to prove this change costs nothing
    is a different palette wearing its name, and every "OG looks the same" claim
    built on it is false.
    """
    assert th.hex_to_ansi256(OG["meta"]) == 109
    assert th.hex_to_ansi256(OG["idle"]) == 46
    assert th.hex_to_ansi256(OG["working"]) == 226
    assert th.hex_to_ansi256(OG["failed"]) == 196
    # And the module's own constants ARE those round trips, not literals again:
    # `META_COLOR` and `_STATE_COLOURS` are the `og` theme's rows read back
    # through the converter, so the catalog is the source and these are its
    # record. They still evaluate to exactly 109 / 46 / 226 / 196.
    og = th.load_catalog()["og"]
    assert a.META_COLOR == th.hex_to_ansi256(og.colour("meta")) == 109
    assert a._STATE_COLOURS == {
        state: th.hex_to_ansi256(og.colour(state))
        for state in ("idle", "working", "failed")
    } == {"idle": 46, "working": 226, "failed": 196}


def test_the_parchment_round_trips_to_the_cell_the_operator_chose():
    """#ffd7af -> 223, EXACTLY, for the same reason `og`'s three hexes do.

    The operator was shown the 256-colour cells near the original parchment and
    picked 223 ("warm cream"): the nearest chromatic cell, 17.9 degrees of hue
    from where it started, and the closest in lightness to the target -- which is
    why it was taken over the brighter 230 and the more muted 187. So the catalog
    stores the hex and the converter must reproduce 223 from it, with no drift
    and no rounding, exactly as it reproduces 109 / 46 / 226 for `og`.

    It replaces #f0e6d2, whose nearest cell is 254 -- a pure grey, 96 degrees of
    hue away. That was the defect this whole commit is about, and a round trip
    that merely came out "near 223" would let it back in.
    """
    assert th.hex_to_ansi256("#ffd7af") == 223
    assert th.ansi256_rgb(223) == (255, 215, 175)
    assert th.rgb_to_hex(th.ansi256_rgb(223)) == "#ffd7af"
    dracula = th.load_catalog()["dracula"]
    assert dracula.colour("text") == "#ffd7af"
    assert dracula.index("text") == 223
    # And it stays warm on the 256 path, which is the whole reason for the pin.
    assert th.curses_value("#ffd7af", 256) == 223


def test_the_og_row_is_todays_colours_including_a_transparent_background():
    theme = th.load_catalog()["og"]
    assert theme.label == "OG" and theme.order == 0
    assert theme.background() is None, \
        "og must keep the terminal's own background; a hex here would end that"
    assert all(theme.is_default(role)
               for role in ("bg", "text", "sel_bg", "rail"))
    assert theme.index("meta") == 109
    assert theme.index("idle") == 46
    assert theme.index("working") == 226
    assert theme.index("failed") == 196
    # And the module's own record of them matches the catalog row byte for byte.
    assert a._OG_COLOURS == theme.colors


# ---------------------------------------------------------------------------
# the loader
# ---------------------------------------------------------------------------

def _write(path: Path, themes: dict) -> Path:
    path.write_text(json.dumps({"version": 1, "themes": themes}), encoding="utf-8")
    return path


def _good_row(**overrides) -> dict:
    row = {
        "label": "Example",
        "order": 1,
        "colors": {role: "#112233" for role in th.ROLES},
    }
    row.update(overrides)
    return row


def test_the_real_catalog_loads_with_every_theme_defining_all_eight_roles():
    catalog = th.load_catalog()
    assert len(catalog) == 26
    assert "dracula" in catalog and "og" in catalog
    # `failed` joined the seven when the operator asked for a red failed mark; it
    # is a STATUS role, and every row carries it or the loader refuses the catalog.
    assert "failed" in th.ROLES
    for slug, theme in catalog.items():
        assert theme.slug == slug
        # The EIGHT required roles, plus at most the OPTIONAL `strip` (only `og`
        # declares it — its background is the terminal's own, so its footer band has
        # no colour to derive from and the operator named one).
        assert set(th.ROLES) <= set(theme.colors) <= set(th.ALL_ROLES), slug
        assert theme.colour("failed") != "default", slug


def test_every_theme_defines_a_failed_red_that_resolves_above_the_named_16():
    """The failed mark is coloured on EVERY theme, and its red is a real cell.

    `_STATE_COLOURS` only knows the `og` red; under any other theme `_role_colour`
    asks THAT theme's `failed` row, so every row must define one — the loader
    enforces that, and this is the colour half. The red must convert to a palette
    index ABOVE 16, because the named 8 are the dull, terminal-remappable colours
    and a red landing there would fall back to whatever the terminal put there:
    the gutter would stop painting the colour the catalog chose. `og`'s is 196,
    the cube's pure-red corner, well past them.
    """
    catalog = th.load_catalog()
    for slug, theme in catalog.items():
        index = theme.index("failed")
        assert index > 16, (slug, theme.colour("failed"), index)
    # And the failed red is its OWN colour, not the idle or working one reused.
    dracula = catalog["dracula"]
    assert dracula.index("failed") not in (dracula.index("idle"),
                                           dracula.index("working"))


def test_ordered_themes_sorts_by_the_rows_own_order_not_by_file_order():
    rows = th.ordered_themes(th.load_catalog())
    assert [t.order for t in rows] == sorted(t.order for t in rows)
    assert rows[0].slug == "og", "order 0 sorts first in the picker"
    # dracula sorts where its own order says, not next to `og`: its row keeps the
    # reference site's order, so the picker reads the same as the site that
    # supplied the 25.
    assert [t.slug for t in rows].index("dracula") == 10
    assert all(a < b for (a, _x), (b, _y) in
               zip([(t.order, t.slug) for t in rows],
                   [(t.order, t.slug) for t in rows][1:]))


def test_default_is_legal_in_every_role_not_just_bg(tmp_path):
    """THE STRUCTURAL DECISION. `"default"` in ANY of the eight roles is what
    lets `og` reproduce the pre-theme pane; a loader that only accepted it in `bg`
    could not express "rails paint PLAIN today" and the row would be a fiction."""
    row = _good_row(colors={role: "default" for role in th.ROLES})
    theme = th.load_catalog(_write(tmp_path / "t.json", {"example": row}))["example"]
    assert all(theme.is_default(role) for role in th.ROLES)
    assert theme.index("text") == -1
    assert theme.background() is None


def test_a_missing_file_falls_back_silently(tmp_path):
    """NOT AN ERROR. A theme catalog is a preference; an operator who has never
    chosen one still gets a pane, and a raised error would take that away."""
    assert th.load_catalog(tmp_path / "nope.json") == th.BUILTIN


def test_an_unreadable_or_unparseable_file_falls_back_silently(tmp_path):
    """NOT AN ERROR. A theme catalog is a preference; an operator who has never
    chosen one still gets a pane, and a raised error would take that away.

    All three cases here are the file saying NOTHING — absent, unreadable, or not
    JSON at all. A file that parses and is still wrong is the other half of the
    asymmetry, and it raises; see the empty-and-wrong-shaped cases below.
    """
    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all", encoding="utf-8")
    assert th.load_catalog(broken) == th.BUILTIN
    zero_length = tmp_path / "zero-length.json"
    zero_length.write_text("", encoding="utf-8")
    assert th.load_catalog(zero_length) == th.BUILTIN, \
        "an EMPTY file is a file that said nothing, not a bad catalog"


@pytest.mark.parametrize("document,expected", [
    ({"version": 1, "themes": {}}, "EMPTY"),
    ({"version": 1, "themes": []}, "not an object"),
    ({"version": 1}, "'themes' key"),
    ({"themes": "dracula"}, "not an object"),
    ([{"label": "Example"}], "top level"),
    ("dracula", "top level"),
])
def test_a_catalog_that_parsed_and_is_still_wrong_is_refused(tmp_path, document,
                                                            expected):
    """THE LOUD HALF, and the case the docstring had been claiming all along.

    `{"themes": {}}` is present, readable, valid JSON and structurally wrong, and
    it used to fall back to dracula in silence. So did `{"themes": []}`, a
    document with no `themes` key at all, and a top level that is not an object —
    all four arrived through the same `except` as the missing file, which is why
    the module's own claim and its code disagreed.

    Falling back is the wrong answer here. The absent file is an operator who has
    never chosen; these are files somebody WROTE, and a catalog that parses and
    then quietly paints dracula reports a one-theme menu to someone with
    twenty-six rows in the file, which is the silent breakage this module exists
    to refuse. A refusal names the shape, which is what somebody looking at the
    file again needs.
    """
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(th.ThemeError) as caught:
        th.load_catalog(path)
    assert expected in str(caught.value), caught.value
    assert str(path) in str(caught.value), \
        "the refusal has to name the FILE, or it is nobody's bad edit"


def test_the_builtin_fallback_is_exactly_one_theme_and_it_is_og():
    """ONE, deliberately. A fallback that shipped twenty-six palettes would be a
    second catalog nobody edits; the whole of what the pane can paint with no
    themes.json is the default theme and no more."""
    assert list(th.BUILTIN) == [th.DEFAULT_THEME]
    assert th.DEFAULT_THEME == "og"


def test_the_builtin_og_matches_the_catalog_row_exactly():
    """The fallback and the file must not drift. A drift here means a machine
    with no themes.json paints a different pane from a machine with one, and
    there is no way to tell which is right."""
    assert th.BUILTIN["og"].colors == th.load_catalog()["og"].colors
    assert th.BUILTIN["og"].label == th.load_catalog()["og"].label
    assert th.BUILTIN["og"].order == th.load_catalog()["og"].order


@pytest.mark.parametrize("role", list(th.ROLES))
def test_a_missing_role_is_refused_by_name(tmp_path, role):
    colors = {r: "#112233" for r in th.ROLES if r != role}
    with pytest.raises(th.ThemeError) as caught:
        th.load_catalog(_write(tmp_path / "t.json",
                               {"solarized-light": _good_row(colors=colors)}))
    assert "solarized-light" in str(caught.value), caught.value
    assert role in str(caught.value), caught.value


@pytest.mark.parametrize("value", ["#FFF", "#gggggg", "00ff00", "#00ff00ff", 7])
def test_a_malformed_value_is_refused_by_slug_and_role(tmp_path, value):
    with pytest.raises(th.ThemeError) as caught:
        th.load_catalog(_write(tmp_path / "t.json", {"gruvbox": _good_row(
            colors={**{r: "#112233" for r in th.ROLES}, "working": value})}))
    message = str(caught.value)
    assert "gruvbox" in message and "working" in message, message


def test_a_typo_role_is_refused_rather_than_ignored(tmp_path):
    colors = {r: "#112233" for r in th.ROLES}
    colors["selection"] = "#445566"
    with pytest.raises(th.ThemeError) as caught:
        th.load_catalog(_write(tmp_path / "t.json", {"nord": _good_row(colors=colors)}))
    assert "nord" in str(caught.value) and "selection" in str(caught.value)


def test_the_optional_strip_role_is_validated_like_any_other(tmp_path):
    """`strip` is OPTIONAL, but when a row declares it, it goes through the SAME
    validation a required role does — the loader never routes it around the hex
    check. An ABSENT strip is fine (the 25 derived themes); a MALFORMED one is
    refused by slug and role, exactly like any other colour."""
    # Absent: allowed, and resolves to no declared strip.
    theme = th.load_catalog(_write(tmp_path / "absent.json", {"example": _good_row()}))
    assert theme["example"].strip() is None

    # Declared and valid: kept verbatim.
    row = _good_row(colors={**{r: "#112233" for r in th.ROLES}, "strip": "#232625"})
    theme = th.load_catalog(_write(tmp_path / "valid.json", {"example": row}))
    assert theme["example"].strip() == "#232625"

    # Declared and MALFORMED: refused, naming the slug AND the role.
    row = _good_row(colors={**{r: "#112233" for r in th.ROLES}, "strip": "#FFF"})
    with pytest.raises(th.ThemeError) as caught:
        th.load_catalog(_write(tmp_path / "bad.json", {"gruvbox": row}))
    message = str(caught.value)
    assert "gruvbox" in message and "strip" in message, message


@pytest.mark.parametrize("label,order", [(None, 1), ("  ", 1), ("X", "1"),
                                         ("X", True)])
def test_a_row_without_a_usable_label_or_order_is_refused(tmp_path, label, order):
    with pytest.raises(th.ThemeError):
        th.load_catalog(_write(tmp_path / "t.json", {"panda": _good_row(
            label=label, order=order)}))


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _home(tmp_path, **env) -> dict:
    return {"OMNIGENT_HOME": str(tmp_path / ".omnigent"), **env}


def test_the_chosen_slug_round_trips(tmp_path):
    env = _home(tmp_path)
    th.save_theme("nord", env)
    assert th.load_theme(env) == "nord"


def test_the_state_lives_beside_og_install_json_and_not_inside_it(tmp_path):
    """Two writers, one file, is a race nobody here is paid to debug: the
    installer rewrites og-install.json from its own state, and `og agents` is
    standalone. So the preference gets its own file under the same home."""
    path = th.theme_state_path(_home(tmp_path))
    assert path.name == "agents-theme.json"
    assert path.parent.name == ".omnigent"


def test_the_env_override_beats_the_stored_value(tmp_path):
    env = _home(tmp_path)
    th.save_theme("nord", env)
    assert th.load_theme({**env, th.THEME_ENV: "gruvbox"}) == "gruvbox"
    # An empty export is not an opinion.
    assert th.load_theme({**env, th.THEME_ENV: "  "}) == "nord"


def test_an_unknown_or_corrupt_stored_slug_falls_back_and_never_raises(tmp_path):
    """`load_theme` READS the preference; `theme_for` is what opens on the default
    when that preference names a row the catalog no longer has. Both are here
    because the split is the point: reading a stale slug must never be an error,
    and resolving one must never crash the pane."""
    env = _home(tmp_path)
    assert th.load_theme(env) == th.DEFAULT_THEME          # never chosen
    assert th.theme_for(environ=env).slug == th.DEFAULT_THEME

    th.save_theme("a-theme-that-was-removed", env)
    assert th.load_theme(env) == "a-theme-that-was-removed"   # read verbatim
    assert th.theme_for(environ=env).slug == th.DEFAULT_THEME  # resolved safely

    path = th.theme_state_path(env)
    path.write_text("{{{ not json", encoding="utf-8")
    assert th.load_theme(env) == th.DEFAULT_THEME
    assert th.theme_for(environ=env).slug == th.DEFAULT_THEME
    path.write_text('{"theme": 42}', encoding="utf-8")
    assert th.load_theme(env) == th.DEFAULT_THEME


def test_saving_never_raises_on_an_unwritable_home(tmp_path):
    """The pane repaints either way. A read-only $OMNIGENT_HOME is not a reason
    to throw away the theme the operator just chose, and a TUI that dies on exit
    over a write is worse than one that forgets."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    th.save_theme("nord", {"OMNIGENT_HOME": str(blocker)})


def _strays(home: Path) -> list:
    """Every file under `home` that is not the state file."""
    return [p.name for p in home.iterdir() if p.name != th.STATE_FILE]


@pytest.mark.parametrize("where", ["replace", "write"])
def test_a_save_that_cannot_finish_removes_its_own_temporary_file(tmp_path,
                                                                  monkeypatch,
                                                                  where):
    """THE LEAK. `mkstemp` has already made a file by the time the write or the
    replace can go wrong, and swallowing the failure while leaving that file
    behind is how a picker driven by muscle memory fills a home that is read-only
    or full with `tmp*.tmp` nobody will ever read.

    Both failure points are covered. `where="replace"` fails at the LAST step,
    after a complete file exists — the shape a full disk takes, and the one that
    leaks most: `json.dump` succeeded, so the temp is a whole, valid, useless
    file. `where="write"` fails inside the dump, which is the one where the file
    is still empty and looks like nothing happened. Either way `save_theme` stays
    best effort: it returns, it does not raise, and it leaves the home as it
    found it.
    """
    home = tmp_path / ".omnigent"
    home.mkdir()
    monkeypatch.setattr(th.json if where == "write" else th.os,
                        "dump" if where == "write" else "replace",
                        _explode)
    th.save_theme("nord", {"OMNIGENT_HOME": str(home)})     # must not raise
    assert _strays(home) == [], (where, _strays(home))


def _explode(*_args, **_kwargs):
    """What a full disk and a read-only home both look like from inside."""
    raise OSError(28, "No space left on device")


def test_a_save_that_cannot_finish_leaves_the_previous_choice_readable(tmp_path,
                                                                     monkeypatch):
    """And the file it was REPLACING is still there, untouched. The whole of the
    atomic write is that a failure leaves the previous theme rather than half of
    one, and the cleanup must not become a second way to lose it."""
    home = tmp_path / ".omnigent"
    env = {"OMNIGENT_HOME": str(home)}
    th.save_theme("nord", env)
    monkeypatch.setattr(th.os, "replace", _explode)
    th.save_theme("gruvbox", env)
    assert th.load_theme(env) == "nord", \
        "the failed save did not write, so the old slug is still what loads"
    assert _strays(home) == []


def test_theme_for_falls_back_for_a_stale_slug_but_a_typed_slug_is_checked(tmp_path):
    """The typed slug is checked; the stale one and the configured one both fall
    back to the default. The last assert reads `OMNIGENT_HOME`, so it is pinned to
    a tmp home like its neighbours: left to the real home it asserts against
    whatever theme this machine happens to have stored, which passes on the machine
    that wrote the default and fails on any other."""
    env = _home(tmp_path)
    assert th.theme_for("nord", environ=env).slug == "nord"
    assert th.theme_for("a-theme-that-was-removed",
                        environ=env).slug == th.DEFAULT_THEME
    assert th.theme_for(environ=env).slug == th.DEFAULT_THEME


# ---------------------------------------------------------------------------
# the readability guard: the fourteen logo colours survive
# ---------------------------------------------------------------------------

# How far a replacement's hue may sit from the original's. The same 12 the
# catalog's nudge rule caps hue drift at, and the same tolerance the guard itself
# searches with: a mark that moved more than this reads as a different colour,
# which is the failure the z-ai row's whole comment is about.
HUE_TOLERANCE = 12.0


def _hue(rgb):
    return th.oklab_hue(rgb)[2]


def _gap(left, right):
    return min(abs(left - right), 360.0 - abs(left - right))


def test_all_fourteen_marks_paint_exactly_their_table_index_on_dracula():
    """THE OPERATOR'S LOGO COLOURS, UNCHANGED, ON THE DEFAULT THEME.

    `_VENDOR_COLOURS` carries eighty lines of recorded reasoning and ships
    byte-identical. The guard is a PAINT-TIME fallback, so on a background dark
    enough for a mark it must return the table index for every single vendor --
    not most of them, and not "the ones that happen to be bright".
    """
    theme = th.load_catalog()["dracula"]
    for vendor, index in a._VENDOR_COLOURS.items():
        assert a._role_colour(a.mark_role(vendor), theme) == index, (
            vendor, "the guard moved a mark on the default theme")
    assert len(a._VENDOR_COLOURS) == 14


def test_the_guard_is_a_no_op_when_the_background_is_the_terminals_own():
    """`bg` is `"default"` means this process cannot see the background, so there
    is no contrast to compute. GUESSING would silently recolour fourteen logos on
    the one theme that promises today's colours exactly -- which is the opposite
    of what the `og` row is for."""
    theme = th.load_catalog()["og"]
    assert theme.background() is None
    for vendor, index in a._VENDOR_COLOURS.items():
        assert th.readable_index(index, None) == index, vendor
        assert a._role_colour(a.mark_role(vendor), theme) == index, vendor


def test_on_a_light_background_the_invisible_marks_move_and_keep_their_hue():
    """THE FAILURE `_VENDOR_COLOURS` PREDICTED, ANSWERED.

    "on a light terminal every entry here inverts into the very problem the floor
    exists to prevent -- 227 is the second-brightest mark in the table (only xai's
    white is brighter) and would wash out against white." On Solarized Light it
    does: kilo's 227 and xai's white 231 both fall under 3.0 against #fdf6e3, so
    the guard moves them -- walking LIGHTNESS and holding HUE, because hue is the
    channel a brand mark is recognised by.
    """
    theme = th.load_catalog()["solarized-light"]
    background = theme.background()
    assert background == "#fdf6e3"

    moved = {v: th.readable_index(i, background)
             for v, i in a._VENDOR_COLOURS.items()}
    assert moved["kilo"] != 227, "kilo's lemon washes out on white and must move"
    assert moved["xai"] != 231, "xai's white is invisible on white and must move"
    assert moved["cognition"] == 32, "a mark that already reads must not move"

    for vendor, index in a._VENDOR_COLOURS.items():
        replacement = moved[vendor]
        was, now = th.ansi256_rgb(index), th.ansi256_rgb(replacement)
        assert th.contrast_ratio(th.rgb_to_hex(now), background) >= 3.0, vendor
        if replacement == index:
            continue
        was_hue, now_hue = _hue(was), _hue(now)
        if was_hue is None:
            # A neutral stays neutral -- "the same hue" for a colour that has
            # none is the only honest reading, and it is what keeps xai's white
            # from acquiring a tint it never had.
            assert _hue(now) is None, vendor
            continue
        assert now_hue is not None and _gap(was_hue, now_hue) <= HUE_TOLERANCE, (
            vendor, was_hue, now_hue, "the guard changed the brand's hue")


def test_the_guard_never_paints_a_value_further_from_the_palette():
    """The replacement is always a REAL cell of the 256 palette — the guard walks
    the same 240 candidates the converter does, so nothing can name an index the
    terminal has never heard of."""
    for theme in th.ordered_themes(th.load_catalog()):
        background = theme.background()
        if background is None:
            continue
        for index in a._VENDOR_COLOURS.values():
            replacement = th.readable_index(index, background)
            assert replacement in {row[0] for row in th.PALETTE}, (theme.slug, replacement)
            assert th.contrast_ratio(th.rgb_to_hex(th.ansi256_rgb(replacement)),
                                     background) >= 3.0 or replacement == index


def test_the_guard_walks_lightness_and_holds_hue_across_every_light_theme():
    """The claim is about the CATALOG, not about one row: every light theme in
    the shipped set moves what has to move and nothing else."""
    worst = 0.0
    for theme in th.ordered_themes(th.load_catalog()):
        background = theme.background()
        if background is None:
            continue
        for vendor, index in a._VENDOR_COLOURS.items():
            replacement = th.readable_index(index, background)
            if replacement == index:
                continue
            was_hue, now_hue = _hue(th.ansi256_rgb(index)), \
                _hue(th.ansi256_rgb(replacement))
            if was_hue is None or now_hue is None:
                continue
            worst = max(worst, _gap(was_hue, now_hue))
    assert worst <= HUE_TOLERANCE, worst


def test_a_dark_theme_moves_nothing_at_all():
    """Dracula is the operator's default, so the invariant is stated about it and
    then checked across the rest of the dark set: if a dark row ever moved a
    logo, that would be a bug in the guard, not a feature of the theme."""
    dark = [t for t in th.ordered_themes(th.load_catalog())
            if t.background() and th.relative_luminance(t.background()) < 0.15]
    assert len(dark) >= 15, dark
    untouched = [t.label for t in dark
                 if all(th.readable_index(i, t.background()) == i
                        for i in a._VENDOR_COLOURS.values())]
    # Every dark theme except the three whose bg is light enough to hide hermes
    # or deepseek; the claim is that they are the ONLY exceptions, and they are
    # exactly the rows the guard exists for.
    assert set(untouched) >= set(t.label for t in dark) - {
        "Catppuccin Frappe", "Nord", "One Dark", "Palenight"}, \
        sorted(set(t.label for t in dark) - set(untouched))


def test_the_guard_never_writes_to_the_table():
    """The table is the operator's logo colours and it ships byte-identical. The
    guard is a paint-time fallback; anything that mutated the table would make
    the result depend on which theme was loaded FIRST."""
    before = dict(a._VENDOR_COLOURS)
    for theme in th.ordered_themes(th.load_catalog()):
        if theme.background():
            for index in before.values():
                th.readable_index(index, theme.background())
    assert a._VENDOR_COLOURS == before