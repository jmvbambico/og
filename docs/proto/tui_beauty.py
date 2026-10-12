#!/usr/bin/env python3
"""og agents - BEAUTY prototype: eight treatments for the root header and the
view at large, rendered on the SAME real data so they can be compared.

The complaint this answers, in the operator's words: the ROOT is a naked line
of text while every worker under it is a full box, so the top session reads as
a stray label instead of the parent of the tree; and the view is boring.

Everything here is plain text at a fixed NARROW width, exactly like
`tui_proto.py` - the pane is the design problem, so every variant is shown at
the width it actually gets.

    python3 tui_beauty.py              # every variant, width 46
    python3 tui_beauty.py --width 80   # same variants, wider pane

WHY GLYPHS AND NOT COLOUR. The live view paints through curses with only
A_NORMAL, A_REVERSE and A_DIM, and the pane is about to become TRANSPARENT (no
background of its own). So every variant below earns its interest from
box-drawing weight (heavy vs light vs rounded vs double), a gutter column,
separator rules, blank breathing rows and those three attributes - never from a
background fill. V7 says out loud where A_REVERSE/A_DIM would be used, and the
one place a variant would genuinely want colour is called out in its rationale
rather than assumed.

V8 (W1) is the exception and is drawn, not asserted: the operator sent a
screenshot of the WEB view of this same pane and asked for the TUI to get as
close to it as possible, and the web has a FILLED header bar. So this file now
emits real SGR escapes - in V8's header bar and in the selection comparison
below - which is why `clip`, `pad_clip` and `two_col` measure display columns
with the escapes stripped and `unicodedata.east_asian_width` consulted instead
of counting characters. V1-V7 contain neither an escape nor a non-BMP glyph, so
they render identically under the old ruler and under the new one.

Two of W1's details are decisions, not transcription, and both are argued where
they are implemented: the harness mark gets its OWN aligned gutter (which is
what makes a per-worker mark earn its column in this layout and only this one),
and selection is the caret alone rather than a full-block A_REVERSE.

Each variant is a COMPLETE view of the same root + the same worker selection
(read from sessions.json), each carries a one-line rationale naming what it
fixes and what it costs in rows, and no emitted line ever exceeds the width.
"""
import sys, json, os, collections

# ---------------------------------------------------------------- data
HERE = os.path.dirname(os.path.abspath(__file__))

# The display states, in the order the groups are drawn in. State, not the raw
# server status: `blocked` is not a status, it is a worker holding an
# elicitation, and it must never be sorted away from the top.
ORDER = ("working", "blocked", "failed", "idle")

GLYPH = {"working": "\u25cf", "blocked": "\u23f8", "failed": "\u2717", "idle": "\u25cb"}
# The braille spinner, top-two rows only (dots 1-6) so the cell height never
# jitters as the frame advances - the same six frames the shipped view uses.
SPIN = "\u280b\u2819\u2839\u2838\u283c\u2834"
STATE_WORD = {"working": "working", "blocked": "blocked - waiting on you",
              "failed": "failed", "idle": "finished"}

# Box weights. The contrast between these IS the visual interest.
LIGHT = ("\u250c", "\u2500", "\u2510", "\u2502", "\u2514", "\u2518")   # ----
HEAVY = ("\u250f", "\u2501", "\u2513", "\u2503", "\u2517", "\u251b")   # ----
ROUND = ("\u256d", "\u2500", "\u256e", "\u2502", "\u2570", "\u256f")   # ----
DOUBLE = ("\u2554", "\u2550", "\u2557", "\u2551", "\u255a", "\u255d")  # ====


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
    """`41s`, `2m14s`, `1h04m` - a running figure, watched ticking."""
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
    if total < 60:
        return "%ds ago" % total
    if total < 3600:
        return "%dm ago" % (total // 60)
    return "%dh ago" % (total // 3600)


def split_title(title):
    """`coder_cmdcode:fix-x` -> (`cmdcode`, `fix-x`); mirrors the shipped view."""
    text = title if isinstance(title, str) else ""
    prefix, sep, task = text.partition(":")
    if not sep:
        return None, text
    agent = prefix[len("coder_"):] if prefix.startswith("coder_") else prefix
    return agent or None, task.strip() or text


def _meta(state, elic, created, updated, now):
    if state == "blocked":
        return "waiting on you"
    if state == "failed":
        return "failed"
    if state == "working":
        return age(now - created) if created is not None else "working"
    return "done " + ago(now - updated) if updated is not None else "idle"


FALLBACK = [{"id": "5e894a23", "title": "Project Status Review", "harness": "Claude Code",
             "state": "working", "kids": [
                 {"title": "coders", "agent": "zen", "state": "working", "meta": "2m14s", "elic": 0},
                 {"title": "reviewers", "agent": "cmdcode", "state": "blocked", "meta": "waiting on you", "elic": 1},
                 {"title": "scouts", "agent": "freebuff", "state": "failed", "meta": "failed", "elic": 0},
                 {"title": "docs", "agent": "cmdcode", "state": "idle", "meta": "done 6m ago", "elic": 0},
             ]}]


def load(limit=5):
    """The SAME root + worker selection every variant draws.

    Picks the root with the most STATE DIVERSITY (so every variant is exercised
    on working / blocked / failed / idle at once), then a deterministic
    state-diverse worker selection, then orders it by state. `now` is the newest
    `updated_at` in the capture, not the wall clock, so the clocks are stable
    run to run.
    """
    try:
        rows = json.load(open(os.path.join(HERE, "sessions.json")))["data"]
    except Exception:
        return FALLBACK[0], 0.0

    by_parent = collections.defaultdict(list)
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
        return FALLBACK[0], now

    def diversity(r):
        states = {_state(k.get("status"), k.get("pending_elicitations_count"))
                  for k in by_parent.get(r["id"], [])}
        return len(states)

    root = max(candidates, key=lambda r: (diversity(r), r["id"]))

    # State-diverse pick, in listing order, then fill to the limit.
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

    kids = []
    for k in picked:
        st = _state(k.get("status"), k.get("pending_elicitations_count"))
        agent, task = split_title((k.get("title") or "").strip() or k["id"][:8])
        kids.append({
            "title": task or (k.get("title") or k["id"][:8]),
            "agent": agent,
            "state": st,
            "meta": _meta(st, k.get("pending_elicitations_count"),
                          k.get("created_at"), k.get("updated_at"), now),
            "elic": k.get("pending_elicitations_count") or 0,
        })

    return {
        "title": (root.get("title") or "").strip() or root["id"][:8],
        # The listing row has no `harness` field in this capture; the agent name
        # stands in so the badge names the orchestrator truthfully.
        "harness": root.get("harness") or root.get("agent_name") or "agent",
        "state": _state(root.get("status"), root.get("pending_elicitations_count")),
        "kids": kids,
    }, now


# ------------------------------------------------------------ primitives
# WIDTH IS MEASURED, NOT ASSUMED - the same rule tui_marks.py follows, for the
# same reason. V8 (W1) is the first variant here that emits a real SGR escape
# (the filled header bar) and the first to draw a non-BMP glyph (the harness
# mark), so `len()` would now be wrong twice over on one line: it counts an
# escape as thirteen visible characters, and it calls every glyph one column
# whatever it draws as. `_visible_width` strips escapes and asks
# `unicodedata.east_asian_width`. Nothing before V8 changed output under the
# new ruler, because no earlier line contains either.
import re
import unicodedata

_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")

# East Asian Wide and Fullwidth are the two classes a terminal draws in TWO
# cells. `A` (Ambiguous) is one cell in a western terminal and two in a
# CJK-locale one; the mark column is charged pessimistically for it, in
# `mark_slot` below, which is the same rule tui_marks.py applies.
_WIDE = ("W", "F")
_WIDE_SLOT = ("W", "F", "A")

SLOT_COLS = 2


def _visible_width(line):
    """Display columns of one rendered line: escapes stripped, wide glyphs 2."""
    total = 0
    for ch in _ANSI_SGR.sub("", line):
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in _WIDE else 1
    return total


def _cells(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in _WIDE else 1


def _tokens(s):
    """(text, is_escape) runs. Escapes are carried whole and never cut in half."""
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
                return "".join(out) + "\u2026"
            out.append(ch)
            used += cw
    return "".join(out)


def pad_clip(s, w):
    c = clip(s, w)
    return c + " " * max(0, w - _visible_width(c))


def rule(w, ch="\u2500"):
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
    """`left` flush left, `right` flush right, exactly `w` wide, never longer."""
    if w <= 0:
        return ""
    right = clip(right, w)
    room = w - _visible_width(right)
    if room <= 0:
        return clip(right, w)
    return pad_clip(left, room) + right


def slot_cells(ch):
    """Cells one character occupies inside the mark slot: Ambiguous counts 2.

    A copy of `tui_marks.slot_cells`, kept here so this prototype stands alone
    - it has no registry and no tier-0 table of its own - and kept honest by
    that file's `check_marks()`, which renders W1 through both and compares.
    """
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in _WIDE_SLOT else 1


def mark_slot(mark):
    """`mark` padded to exactly two columns under the pessimistic ruler.

    Narrow gets one space, a blank vendor gets two, Ambiguous gets none -
    it has already been charged two. A mark the font does not have draws in
    the same two cells, which is what keeps a tofu box from shearing the row.
    """
    if not mark or mark == " ":
        return " " * SLOT_COLS
    return mark + " " * max(0, SLOT_COLS - slot_cells(mark[0]))


def box(lines, bw, chars=LIGHT):
    """`lines` in a box of EXACTLY `bw` columns."""
    tl, h, tr, v, bl, br = chars
    bw = max(4, bw)
    return ([tl + h * (bw - 2) + tr]
            + [v + pad_clip(ln, bw - 2) + v for ln in lines]
            + [bl + h * (bw - 2) + br])


def wbox(k, w, indent=0, chars=LIGHT, caret=" ", extra=""):
    """One worker as a full 4-row box, optionally inset by `indent` columns."""
    bw = max(4, w - indent)
    l1 = caret + " " + GLYPH[k["state"]] + " " + k["title"]
    who = (k["agent"] + " \u00b7 ") if k["agent"] else ""
    l2 = "  " + who + k["meta"] + (("  " + extra) if extra else "")
    pre = " " * indent
    return [pre + ln for ln in box([l1, l2], bw, chars)]


def rule_label(label, w, ch="\u2500"):
    """A rule with `label` inlaid near the left: `== blocked ==========`."""
    lab = " %s " % label
    if _visible_width(lab) + 2 >= w:
        return clip(lab, w)
    return clip(ch * 2 + lab + ch * (w - 2 - _visible_width(lab)), w)


def state_counts(kids):
    return collections.Counter(k["state"] for k in kids)


def group_by_state(kids):
    return [(st, [k for k in kids if k["state"] == st])
            for st in ORDER if any(k["state"] == st for k in kids)]


def gauge(done, total, width):
    filled = 0 if not total else int(round(done / total * width))
    return "\u2588" * filled + "\u2591" * (width - filled)


# ------------------------------------------------------------- variants
def v_nested(r, w):
    """Root as a double-line CONTAINER; worker boxes visibly nest inside."""
    out = []
    name = clip(r["title"], max(4, w - 10))
    out.append("\u2554\u2550 " + name + " " + rule(max(0, w - len(name) - 5), "\u2550") + "\u2557")
    counts = state_counts(r["kids"])
    summary = "  ".join("%s %d" % (GLYPH[st], counts[st]) for st in ORDER if counts.get(st))
    out.append("\u2551 " + two_col(summary, "%d workers" % len(r["kids"]), w - 4) + " \u2551")
    out.append("\u2560" + rule(w - 2, "\u2550") + "\u2563")
    for i, k in enumerate(r["kids"]):
        extra = ("\u23f8 %d" % k["elic"]) if k["state"] == "blocked" and k["elic"] else ""
        inner = box([("\u25b8" if i == 0 else " ") + " " + GLYPH[k["state"]] + " " + k["title"],
                     "    " + ((k["agent"] + " \u00b7 ") if k["agent"] else "") + k["meta"]
                     + (("  " + extra) if extra else "")],
                    max(4, w - 4), LIGHT)
        out.extend("\u2551 " + ln + " \u2551" for ln in inner)
    out.append("\u255a" + rule(w - 2, "\u2550") + "\u255d")
    return out


def v_banner(r, w):
    """A heavy title bar with the name inlaid and the badge pinned right."""
    badge = "%s \u00b7 %d" % (r["harness"], len(r["kids"]))
    name = clip(r["title"], max(4, w - len(badge) - 6))
    left = "\u2501\u2501 " + name + " "
    right = " " + clip(badge, max(2, w - len(left) - 2)) + " "
    fill = max(0, w - len(left) - len(right))
    out = [(left + "\u2501" * fill + right)[:w], ""]
    for i, k in enumerate(r["kids"]):
        extra = ("\u23f8 %d" % k["elic"]) if k["state"] == "blocked" and k["elic"] else ""
        out += wbox(k, w, 1, LIGHT, "\u25b8" if i == 0 else " ", extra)
    return out


def v_gutter(r, w):
    """A left gutter rail carries the tree and a per-row status glyph."""
    cw = max(4, w - 2)
    out = []
    out.append("\u258c " + pad_clip(r["title"], cw))
    counts = state_counts(r["kids"])
    summary = "  ".join("%s %d %s" % (GLYPH[st], counts[st], st) for st in ORDER if counts.get(st))
    out.append("\u258c " + pad_clip(two_col(summary, "%d workers" % len(r["kids"]), cw), cw))
    for i, k in enumerate(r["kids"]):
        mark = "\u25b8" if i == 0 else " "
        line = mark + " " + GLYPH[k["state"]] + " " + two_col(k["title"], k["meta"], max(0, cw - 4))
        out.append("\u2502 " + pad_clip(line, cw))
        who = (k["agent"] + " \u00b7 " + k["meta"]) if k["agent"] else k["meta"]
        out.append("\u2502 " + pad_clip("   " + who, cw))
    return out


def v_contrast(r, w):
    """Double-line root box over single-line workers; weight + badges."""
    out = box([clip(r["title"], w - 4),
               two_col(r["harness"], "%d workers" % len(r["kids"]), w - 4)], w, DOUBLE)
    groups = group_by_state(r["kids"])
    for gi, (st, ks) in enumerate(groups):
        out.append(rule_label("%s %s" % (GLYPH[st], st), w, "\u2550"))
        for i, k in enumerate(ks):
            extra = ("\u23f8 %d" % k["elic"]) if k["state"] == "blocked" and k["elic"] else ""
            out += wbox(k, w, 0, LIGHT, "\u25b8" if (gi == 0 and i == 0) else " ", extra)
    return out


def v_dense(r, w):
    """Compact rows: one line per worker, right-aligned clock, inline badges."""
    out = box([clip(r["title"], w - 4),
               two_col(r["harness"], "%d workers" % len(r["kids"]), w - 4)], w, ROUND)
    for i, k in enumerate(r["kids"]):
        g = SPIN[0] if k["state"] == "working" else GLYPH[k["state"]]
        right = k["meta"]
        if k["state"] == "blocked" and k["elic"]:
            right = "\u23f8" + str(k["elic"]) + "  " + right
        out.append(two_col(("\u25b8" if i == 0 else " ") + " " + g + " " + k["title"], right, w))
    return out


def v_gauge(r, w):
    """Heavy bar + done-gauge, a status gutter, breathing rows between groups."""
    out = []
    done = sum(1 for k in r["kids"] if k["state"] == "idle")
    out.append(rule_label(r["title"], w, "\u2501"))
    out.append(two_col("%s  %d/%d done" % (gauge(done, len(r["kids"]), 10), done, len(r["kids"])),
                       r["harness"], w))
    out.append("")
    prev = None
    for i, k in enumerate(r["kids"]):
        if prev is not None and k["state"] != prev:
            out.append("")
            out.append(" " + clip(STATE_WORD[k["state"]], max(0, w - 1)))
        prev = k["state"]
        g = SPIN[0] if k["state"] == "working" else GLYPH[k["state"]]
        right = (gauge(0, 1, 6) + " " + k["meta"]) if k["state"] == "working" else k["meta"]
        out.append(" " + g + " " + two_col(k["title"], right, max(0, w - 4)))
    return out


def v_weights(r, w):
    """State told by box weight alone: rounded, double, heavy, light - no colour."""
    out = box([clip(r["title"], w - 4),
               two_col(r["harness"], "%d workers" % len(r["kids"]), w - 4)], w, ROUND)
    out.append("")
    for i, k in enumerate(r["kids"]):
        chars = {"failed": DOUBLE, "blocked": HEAVY}.get(k["state"], LIGHT)
        out += wbox(k, w, 0, chars, "\u25b8" if i == 0 else " ")
    out.append("")
    out.append(clip("\u256d rounded root  \u2554\u2550\u2557 failed  \u250f\u2501\u2513 blocked  "
                    "\u250c\u2500\u2510 idle/working  \u00b7 rev = selection, dim = meta", w))
    return out


# ---- V8 W1 web-parity -----------------------------------------------------
# The operator sent a screenshot of the web version of this view and asked for
# the TUI to get as close to it as possible. What the web has, itemised:
#   a FILLED header bar across the full pane, carrying a square avatar tile and
#     the orchestrator name in bold, with clear space under it;
#   NO BOXES AT ALL around workers - the list is flat;
#   a LEFT TIMELINE RAIL: a small circled dot in a narrow gutter, joined down
#     the list by a faint vertical line, drawn dim;
#   a STATUS CHIP in a second narrow column;
#   the HARNESS MARK in a third narrow column (the web's per-agent icon);
#   TWO LINES per worker - the title, then a dim report snippet truncated with
#     an ellipsis at the pane edge, ALWAYS shown rather than expanded;
#   a blank row between workers.
#
# Two of those are decisions rather than decorations, and they are why W1 is
# the one layout in the whole file where a per-worker mark earns its column:
#
#   THE MARK GETS ITS OWN GUTTER. In V1/V2/V5 the mark is jammed in front of
#   the title, so it costs the title three columns a row and lands against
#   whatever the row above happened to end with. Here it is a column of its own
#   that lines up down the whole list, which is the difference between a mark
#   and a logo glued to a ragged edge. That reverses the earlier "per-worker
#   marks do not earn their column" verdict - FOR THIS VARIANT ONLY; the other
#   seven still jam it in front, and the measurement behind that verdict is
#   kept in tui_marks.py section A.
#
#   SELECTION IS THE CARET. The operator does not want the selected worker in
#   full reverse video. A_REVERSE therefore survives here in exactly one place,
#   the header bar, which is a different job from marking the cursor. The
#   comparison section below shows what that replaces.
#
# The geometry is shared with tui_marks.py, which renders the same variant with
# the operator's real tier-0 marks; that file's `check_marks()` renders W1
# through both and fails if the two ever disagree.
REV, NOREV = "\x1b[7m", "\x1b[27m"
DIM, UNDIM = "\x1b[2m", "\x1b[22m"
BOLD, UNBOLD = "\x1b[1m", "\x1b[22m"

W1_CARET = "\u25b8"
# U+2299 for the node, U+2502 for the rule joining them. Both are Ambiguous,
# like every box-drawing glyph this pane already draws, so W1 introduces no new
# class of width risk - it adds more of a class the pane has always had.
W1_DOT = "\u2299"
W1_RULE = "\u2502"
# The avatar tile when no vendor mark is available: one cell under both rulers,
# which matters in a bar filled to the full pane width.
W1_TILE = "\u25a1"
# Below this many columns for the title the three gutters are not worth their
# columns and the form degrades. The title is the pane's content; the gutters
# are aids to reading it.
W1_MIN_TITLE = 18
W1_MIN_TITLE_SMALL = 11

# The web shows a green check for done and a red exclamation for failed. The
# TUI keeps its OWN state vocabulary - \u25cf \u23f8 \u2717 \u25cb, the four glyphs every other
# variant here uses - so a state never changes meaning between layouts, and
# because the chip is one cell of monochrome text glyph it inherits the row
# attribute exactly as the mark does. Colour is not available to it either way;
# that divergence from the web shot is deliberate and it is the only one.
#
# The capture carries NO report text - a worker row has a title, a status, an
# agent name, two timestamps and an elicitation count - so the snippet below is
# SYNTHESISED from the fields that do exist, and the variant says so. What is
# under test is the second LINE: always present, always dim, truncated with an
# ellipsis at the pane edge, never expanded on demand. The words are
# placeholders of the length needed to make that visible at width 46.
SNIPPET = {
    "working": "in progress, no report line yet",
    "blocked": "holding an elicitation, needs a decision",
    "failed": "stopped on an error, nothing reported",
    "idle": "finished, report at the tail of the log",
}

# This prototype has no registry and no tier-0 table of its own, so the mark
# column is filled from the three vendors that actually appear in the fallback
# capture. The real table is tui_marks.py's; the parity check described above
# is what keeps the two honest. An agent not in here draws the same honest
# blank slot a role row draws there.
DEMO_MARKS = {"cmdcode": "\u2318", "zen": "\u25af", "freebuff": "\u2554"}


def snippet_for(k):
    """The dim second line W1 shows under every worker. Synthesised; see above."""
    who = k.get("agent") or "role"
    return "%s \u00b7 %s \u00b7 %s" % (who, SNIPPET[k["state"]], k["meta"])


def w1_cols(rail_on, mark_on):
    """Columns the W1 gutter occupies: caret, [rail], chip, [mark] + spaces."""
    return 4 + 2 * rail_on + 3 * mark_on


def w1_layout(w):
    """`(grade, rail_on, mark_on)` for W1 at pane width `w`.

    The drop order is the design, not an accident: the timeline rail goes first
    because it is decoration on top of a single cell; then the harness mark,
    which the rest of tui_marks.py argues hardest for; then the chip, whose
    information the state word in the title already carries. The title is the
    last thing to go, and the grade is printed with every render, so a degraded
    W1 always says so on the same screen.
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
    and the whole point of this layout is that it has none.
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

    THE ONE PLACE REVERSE VIDEO BELONGS. Selection is the caret, so the
    attribute is not competing with selection for the same channel: one thing
    is a filled bar at the top, the other is an arrow in the gutter, and the
    operator can see both at once.
    """
    lead = " %s " % ((mark or "").strip() or W1_TILE)
    name = clip(r["harness"], max(2, w - _visible_width(lead) - 1))
    return REV + pad_clip(lead + BOLD + name + UNBOLD, w) + NOREV


def v_web(r, w, mark_of=None, root_mark=None):
    """V8: the web view's shape, in this pane. Flat, two lines a worker."""
    grade, rail_on, mark_on = w1_layout(w)
    mark_of = mark_of or (lambda k: DEMO_MARKS.get(k.get("agent"), " "))
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
    out += [" " + seg for seg in wrap(
        "W1 at width %d: %s. %d columns of gutter, %d left for the title and "
        "the snippet." % (w, grade, cols, room), max(1, w - 1))]
    return out


VARIANTS = [
    ("nested-container",
     "Root is a double-line container the worker boxes visibly sit inside, so the top "
     "session reads as the parent of the tree and not a stray label. "
     "Cost: +4 rows (rails, title, closing edge) around the whole group.",
     v_nested),
    ("title-bar-badge",
     "One heavy rule with the root name inlaid and the harness \u00b7 worker-count badge "
     "pinned right: the root announces the tree in a single row. "
     "Cost: +1 row for the bar, plus a breathing row before the workers.",
     v_banner),
    ("gutter-rail",
     "A left gutter rail carries the hierarchy and a per-row status glyph, and the root "
     "caps the rail - ownership is a column, not a box. "
     "Cost: 2 rows per worker (no four-row boxes at all).",
     v_gutter),
    ("double-vs-single",
     "A double-line root box over single-line worker boxes, with heavy per-state "
     "separator rules and \u23f8 elicitation badges: weight, not colour, marks the parent "
     "and the rows that need you. Cost: +4 root rows, +1 separator row per state group.",
     v_contrast),
    ("dense-rows",
     "One row per worker with a right-aligned elapsed clock and inline badges, and a "
     "braille spinner on the live rows: the whole tree fits a short pane. "
     "Cost: 3 root rows; 1 row per worker instead of 4 (saves 3 rows each).",
     v_dense),
    ("gauge-breathing",
     "A heavy title bar with a done-gauge over a status gutter, with blank breathing rows "
     "between state groups: scanned top-down instead of read box by box. "
     "Cost: 3 header rows, plus one blank and one label row per state group.",
     v_gauge),
    ("weight-encoding",
     "State is told by box weight alone - rounded root, double for failed, heavy for "
     "blocked, light for the rest - with no colour anywhere. "
     "Cost: +4 root rows and a trailing legend row.",
     v_weights),
    ("W1 web-parity",
     "The WEB view, in the pane: a filled A_REVERSE header bar, NO boxes, a DIM "
     "timeline rail down the left, a status chip, the harness mark in its own "
     "aligned gutter, two lines a worker with the report snippet always shown, and a "
     "blank row between workers. Selection is the caret alone. "
     "Cost: 3 header rows and 2 rows a worker; below %d columns the gutters are "
     "dropped in order, rail first."
     % W1_MIN_TITLE,
     v_web),
]


# ---------------------------------------------------------------- selection
# The operator's words: "the arrow pointing to it is enough". W1 is drawn that
# way, and this section puts the old treatment next to the new one so the
# replacement is visible rather than asserted - including what is given up.
def _sel_line(k, w, caret, reverse=False, bold=False):
    """One flat worker row under each selection treatment, same geometry."""
    body = k["title"] if not bold else BOLD + k["title"] + UNBOLD
    line = pad_clip(caret + " " + GLYPH[k["state"]] + " " + body, w)
    return REV + line + NOREV if reverse else line


def selection_block(w, root):
    """Full-block reverse (what the pane does today) against caret + bold.

    Drawn on the SAME rows as every variant above, one per state, so the two
    treatments can be compared like for like rather than described.
    """
    kids, seen = [], set()
    for k in root["kids"]:
        if k["state"] not in seen:
            seen.add(k["state"])
            kids.append(k)
    kids = kids[:4] or FALLBACK[0]["kids"][:4]
    out = ["", rule(w, "\u2500"),
           clip("  SELECTION - the caret alone, and what it replaces", w)]
    out += [" " + seg for seg in wrap(
        "The operator does not want the selected worker drawn in full reverse "
        "video: 'the arrow pointing to it is enough'. This section shows the "
        "treatment being replaced and the one replacing it, on the same rows "
        "and the same width, so the difference is a thing you look at. W1 "
        "above uses the caret; A_REVERSE survives in exactly one place in that "
        "layout, the filled header bar, which is a different thing from a "
        "selection indicator and never has to be told apart from one.", w - 1)]
    out.append(rule(w, "\u2500"))
    out.append("")
    out.append(clip("  REPLACED - A_REVERSE across the whole row, today:", w))
    out.append("")
    for i, k in enumerate(kids):
        out.append("  " + _sel_line(k, w - 2, "\u25b8" if i == 0 else " ",
                                    reverse=(i == 0)))
    out.append("")
    out.append(clip("  REPLACING - the caret alone, plus A_BOLD on the title:", w))
    out.append("")
    for i, k in enumerate(kids):
        out.append("  " + _sel_line(k, w - 2, "\u25b8" if i == 0 else " ",
                                    bold=(i == 0)))
    out.append("")
    out += [" " + seg for seg in wrap(
        "WHAT THE OLD TREATMENT BOUGHT: a selected row is unmissable at any "
        "glance, because it is the only block of inverted colour on the screen "
        "and the eye finds inverted colour without being aimed at it. It also "
        "survives a screenshot, a scrollback dump and a pane with no colour at "
        "all, because reverse is an attribute rather than a hue.", w - 1)]
    out += [" " + seg for seg in wrap(
        "WHAT IT GIVES UP: everything else. The block covers the row's own "
        "text, so a selected title in reverse reads as a label the pane is "
        "holding rather than a title you can read; it fights the header bar if "
        "one is added (two inverted regions on one screen, and only one of "
        "them means 'you are here'); and it is the pane's loudest possible "
        "signal for the least important fact in the list, which is where the "
        "operator's cursor happens to be.", w - 1)]
    out += [" " + seg for seg in wrap(
        "WHAT THE CARET BUYS BACK: the title stays in normal weight and "
        "normal colour, the inverted header bar is the only inverted thing on "
        "screen, and the selection costs one column that no other row is "
        "using. Its real cost is visibility: a caret is one character wide and "
        "one character bright, so a long list scanned for a half-remembered "
        "title can lose it. W1's answer is the blank row between workers - "
        "each item is its own two-line block, so the caret has a row of its "
        "own to live in rather than sharing a line with five other rows. A "
        "pane that wants both can keep the caret and spend A_DIM on the "
        "unselected rows instead, which is the same idea one step quieter.",
        w - 1)]
    return out


def build_all(root, w):
    out = [rule(w, "\u2550"),
           clip("  tui_beauty \u2014 beauty variants for `og agents`  \u00b7  width %d" % w, w),
           clip("  same data every variant: root %r + %d workers from sessions.json"
                % (root["title"], len(root["kids"])), w),
           rule(w, "\u2550")]
    for i, (name, why, fn) in enumerate(VARIANTS, 1):
        out.append("")
        out.append(rule(w, "\u2500"))
        out.append(clip("V%d  %s" % (i, name), w))
        for seg in wrap(why, max(1, w - 1)):
            out.append(" " + seg)
        out.append(rule(w, "\u2500"))
        out += fn(root, w)
        out.append("")
    out += selection_block(w, root)
    out.append("")
    out.append(rule(w, "\u2550"))
    out.append(clip("  %d variants \u00b7 no line exceeds %d cols \u00b7 A_NORMAL / "
                    "A_DIM / A_BOLD everywhere, and A_REVERSE in exactly two "
                    "places: V8's header bar and the selection comparison"
                    % (len(VARIANTS), w), w))
    out.append(rule(w, "\u2550"))
    return out


def main():
    argv = sys.argv[1:]
    w = 46
    if "--width" in argv:
        try:
            w = max(12, int(argv[argv.index("--width") + 1]))
        except (ValueError, IndexError):
            w = 46
    root, _now = load()
    lines = build_all(root, w)
    # Escapes stripped, east-asian width consulted: `len()` would count V8's
    # header bar's thirteen SGR bytes as thirteen columns of text.
    over = [ln for ln in lines if _visible_width(ln) > w]
    for ln in lines:
        print(clip(ln, w))
    if over:
        for ln in over:
            sys.stderr.write("tui_beauty: %d cols, width %d: %r\n"
                             % (_visible_width(ln), w, ln))
        sys.stderr.write("tui_beauty: %d line(s) exceeded width %d\n"
                         % (len(over), w))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
