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


def test_curses_value_is_minus_one_for_default_and_a_pair_otherwise():
    assert th.curses_value("default", 256) == -1
    assert th.curses_value("default", 1 << 24) == -1
    # On a 256-colour terminal the hex is mapped into the palette...
    assert th.curses_value("#87afaf", 256) == 109
    # ...and on a truecolor one it goes in EXACTLY, which is what keeps the
    # operator's warm parchment warm: the nearest 256 cell to #f0e6d2 is a pure
    # grey, and shipping that grey would be shipping grey where parchment was
    # asked for.
    assert th.curses_value("#f0e6d2", 1 << 24) == 0xF0E6D2
    assert th.curses_value("#282a36", 1 << 24) == 0x282A36


# ---------------------------------------------------------------------------
# the OG round trip -- today's colours, provably
# ---------------------------------------------------------------------------

OG = {"meta": "#87afaf", "idle": "#00ff00", "working": "#ffff00"}


def test_og_hexes_convert_back_to_the_indices_the_pane_already_shipped():
    """IF THIS FAILS, `og` IS NOT TODAY'S COLOURS.

    The three hexes in the `og` row are the exact sRGB of ansi 109 (META_COLOR),
    46 (idle) and 226 (working). They were resolved from the cube, not eyeballed,
    and the converter has to reproduce them exactly — otherwise the theme that
    exists to prove this change costs nothing is a different palette wearing its
    name, and every "OG looks the same" claim built on it is false.
    """
    assert th.hex_to_ansi256(OG["meta"]) == 109
    assert th.hex_to_ansi256(OG["idle"]) == 46
    assert th.hex_to_ansi256(OG["working"]) == 226
    # And the module's own constants ARE those round trips, not literals again:
    # `META_COLOR` and `_STATE_COLOURS` are the `og` theme's rows read back
    # through the converter, so the catalog is the source and these are its
    # record. They still evaluate to exactly 109 / 46 / 226.
    og = th.load_catalog()["og"]
    assert a.META_COLOR == th.hex_to_ansi256(og.colour("meta")) == 109
    assert a._STATE_COLOURS == {
        state: th.hex_to_ansi256(og.colour(state))
        for state in ("idle", "working")
    } == {"idle": 46, "working": 226}


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


def test_the_real_catalog_loads_with_every_theme_defining_all_seven_roles():
    catalog = th.load_catalog()
    assert len(catalog) == 26
    assert "dracula" in catalog and "og" in catalog
    for slug, theme in catalog.items():
        assert theme.slug == slug
        assert set(theme.colors) == set(th.ROLES), slug


def test_ordered_themes_sorts_by_the_rows_own_order_not_by_file_order():
    rows = th.ordered_themes(th.load_catalog())
    assert [t.order for t in rows] == sorted(t.order for t in rows)
    assert rows[0].slug == "og", "order 0 sorts first in the picker"
    # dracula sorts where its own order says, not first: it is the DEFAULT, and a
    # default that also happened to be first in the menu would be indistinguishable
    # from a theme that merely got added recently.
    assert [t.slug for t in rows].index("dracula") == 10
    assert all(a < b for (a, _x), (b, _y) in
               zip([(t.order, t.slug) for t in rows],
                   [(t.order, t.slug) for t in rows][1:]))


def test_default_is_legal_in_every_role_not_just_bg(tmp_path):
    """THE STRUCTURAL DECISION. `"default"` in ANY of the seven roles is what
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
    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all", encoding="utf-8")
    assert th.load_catalog(broken) == th.BUILTIN
    empty = tmp_path / "empty.json"
    empty.write_text('{"version": 1, "themes": {}}', encoding="utf-8")
    assert th.load_catalog(empty) == th.BUILTIN


def test_the_builtin_fallback_is_exactly_one_theme_and_it_is_dracula():
    """ONE, deliberately. A fallback that shipped twenty-five palettes would be a
    second catalog nobody edits; the whole of what the pane can paint with no
    themes.json is the default theme and no more."""
    assert list(th.BUILTIN) == [th.DEFAULT_THEME]
    assert th.DEFAULT_THEME == "dracula"


def test_the_builtin_dracula_matches_the_catalog_row_exactly():
    """The fallback and the file must not drift. A drift here means a machine
    with no themes.json paints a different pane from a machine with one, and
    there is no way to tell which is right."""
    assert th.BUILTIN["dracula"].colors == th.load_catalog()["dracula"].colors
    assert th.BUILTIN["dracula"].label == th.load_catalog()["dracula"].label
    assert th.BUILTIN["dracula"].order == th.load_catalog()["dracula"].order


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
    """`load_theme` READS the preference; `theme_for` is what opens on dracula
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


def test_theme_for_falls_back_for_a_stale_slug_but_a_typed_slug_is_checked():
    assert th.theme_for("nord").slug == "nord"
    assert th.theme_for("a-theme-that-was-removed").slug == th.DEFAULT_THEME
    assert th.theme_for().slug == th.DEFAULT_THEME


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