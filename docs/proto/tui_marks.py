#!/usr/bin/env python3
"""og agents - HARNESS MARKS prototype: one brand mark per harness vendor, in
three tiers, so the operator can see in THEIR OWN terminal whether a mark is
viable here and pick a tier.

The complaint this answers, in the operator's words: the pane says "Claude
Code" in letters, and letters are slow. "The orange starburst" is faster.

    python3 tui_marks.py              # every tier, width 46 (the real pane)
    python3 tui_marks.py --width 80   # a wider pane
    python3 tui_marks.py --width 20   # the narrow end, where marks get squeezed

THE THREE TIERS, same agents, same layout, so they compare directly:

  TIER 1  plain Unicode. One column, no emoji (emoji are double-width and
          would break every box), a font shipped with every terminal since
          about 1990. Nine of the fifteen agents have an honest mark here.
  TIER 2  the real brand glyph from the Nerd Font private-use area, where one
          exists. In a plain font this section is a row of TOFU BOXES, and
          that is the point: it is the operator's font test, not a proposal.
  TIER 3  `[CC]`. Four ASCII columns, no dependency at all, for `--once`
          output that gets piped into a log where a starburst is a mystery.

Plus two sections the tiers cannot answer alone: A, the mark placed in the
real layouts beside the real capture, and B, the brand colour with the real
ANSI 256 escapes the pane would use.

WIDTH IS MEASURED, NOT ASSUMED. `len(line)` is the wrong ruler twice over: it
counts ANSI escapes as visible characters, and it calls a double-width glyph
one column wide. `_visible_width` below strips escapes and asks
`unicodedata.east_asian_width`, the same approach `installer/og_agents.py`
uses for its QR panel. `check_marks()` ASSERTS every tier-1 mark is Narrow or
Neutral, so a double-width glyph cannot slip in and quietly misalign every box
downstream of it.

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


# -------------------------------------------------------------- checking
def check_marks():
    """Assert the tier-1 set: one character, Narrow or Neutral, unique.

    This is the check the whole prototype exists to make safe. A tier-1 mark
    that is East Asian Wide or Fullwidth occupies TWO cells in a box that was
    laid out in columns, and every rule, box edge and right-aligned clock
    below it shears by one - which reads as "the pane is broken" and is
    blamed on the pane. Ambiguous is refused for the same reason with a
    smaller blast radius: it is two cells only on a CJK-locale terminal, which
    is exactly the kind of thing nobody checks.
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

    ids = {a for v in VENDORS for a in v["agents"]}
    catalog = {r["id"] for r in REGISTRY_ROWS}
    assert ids == catalog, "mark table and registry disagree: %s" % (
        sorted(ids ^ catalog))
    assert len(set(AGENT_TAGS.values())) == len(AGENT_TAGS), \
        "two agents share a tier-3 tag"
    assert set(AGENT_TAGS) == catalog, "tier-3 tags do not cover the catalog"


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


def tier_block(w, rows, tier, rationale):
    """One tier's table: same rows, same order, same geometry; only the mark.

    The mark cell is padded to four columns in EVERY tier so the three tables
    are byte-for-byte aligned with each other and the operator can compare a
    mark down the page instead of re-reading which column it was in.
    """
    out = section(w, tier["name"], rationale)
    out.append("  " + two_col(pad(tier["head"], 5) + " " + pad("vendor", 10),
                              tier["tail_head"], w - 2))
    for r in rows:
        out.append("  " + two_col(pad(tier["mark"](r), 5) + " " + pad(r["vendor"], 10),
                                  tier["tail"](r), w - 2))
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

    # ---- A: the mark in context
    # The capture's root names the BUNDLE (`hivemind`), not a harness id, so
    # there is nothing in it to resolve. The orchestrator this repo installs is
    # `claude` (installer/registry.json, harness `claude-native`), and that is
    # the mark the header carries here. The pane resolves the real per-session
    # harness id and draws a blank when the registry does not name it.
    root_mark = by_vendor[ORCHESTRATOR_VENDOR]["t1"]
    out += ["", rule(w, "═"),
            hdr(w, "A. THE MARK IN CONTEXT - tui_beauty's V1 / V2 / V5, real "
                   "capture")]
    out += para("Root header marked, then the same layouts with a mark on every "
                "worker row, so the cost of the second column is visible rather "
                "than argued about.", w)
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

    out += ["", rule(w, "═"),
            hdr(w, "%d agents - %d marked in tier 1, %d in tier 2, %d ASCII "
                   "tags in tier 3 - no line over %d cols"
                % (len(rows), len(VENDORS) - len(tier1_blanks),
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
