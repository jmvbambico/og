#!/usr/bin/env python3
"""og agents - TUI redesign prototype.

Renders LAYOUT variants and COMPONENT variants side by side so they can be
mixed and matched. Everything here is plain text at a fixed width; the real
pane is NARROW (the whole design problem), so every variant is shown at the
width it actually gets.

  python3 tui_proto.py              # everything
  python3 tui_proto.py layouts      # full-layout variants only
  python3 tui_proto.py components   # component variants only
  python3 tui_proto.py --width 46   # render at a given width
"""
import sys, json, os, collections

# ---------------------------------------------------------------- data
HERE = os.path.dirname(os.path.abspath(__file__))

def load():
    """Real sessions from the operator's server, captured to sessions.json."""
    try:
        rows = json.load(open(os.path.join(HERE, "sessions.json")))["data"]
    except Exception:
        return FALLBACK
    by_parent = collections.defaultdict(list)
    roots = []
    for r in rows:
        p = r.get("parent_session_id")
        (by_parent[p].append(r) if p else roots.append(r))
    out = []
    for r in roots:
        kids = by_parent.get(r["id"], [])
        if not kids and not (r.get("title") or "").strip():
            continue                      # empty husk, not worth a header
        out.append({
            "id": r["id"], "title": (r.get("title") or "").strip() or r["id"][:8],
            "harness": "Claude Code", "status": r.get("status"),
            "kids": [{
                "id": k["id"],
                "title": (k.get("title") or "").strip() or k["id"][:8],
                "status": k.get("status"),
                "elic": k.get("pending_elicitations_count") or 0,
            } for k in kids],
        })
    return out or FALLBACK

FALLBACK = [{"id": "5e894a23", "title": "Project Status Review",
             "harness": "Claude Code", "status": "running", "kids": []}]

def state(s):
    if s.get("elic"): return "blocked"
    return {"running": "working"}.get(s.get("status"), "idle")

def split_name(title):
    """`coder_cmdcode:fix-receive-atomicity` -> ('coder_cmdcode', 'fix-...')."""
    if ":" in title:
        a, _, t = title.partition(":")
        return a, t
    return "", title

def short_agent(a):
    return a.replace("coder_", "").replace("_", "") or a

# ------------------------------------------------------- component sets
GLYPHS = {
    "A unicode":  {"working": "●", "blocked": "⏸", "idle": "✓", "unknown": "?"},
    "B ascii":    {"working": "*", "blocked": "!", "idle": "+", "unknown": "?"},
    "C weight":   {"working": "◉", "blocked": "◍", "idle": "○", "unknown": "·"},
    "D arrows":   {"working": "▶", "blocked": "‖", "idle": "✓", "unknown": "·"},
    "E words":    {"working": "WORK", "blocked": "WAIT", "idle": "done", "unknown": "?"},
}
SEL = {
    "A caret":   lambda s: ("▸ " if s else "  "),
    "B bar":     lambda s: ("┃ " if s else "  "),
    "C bracket": lambda s: ("[" if s else " "),
    "D none":    lambda s: "  ",
}

def rule(w, ch="─"): return ch * w
def clip(s, w): return s if len(s) <= w else s[: max(0, w - 1)] + "…"

# ----------------------------------------------------------- LAYOUTS
def layout_flat(roots, w, g=GLYPHS["A unicode"]):
    """What ships today: group header, flat bullet rows."""
    out = []
    for r in roots:
        out.append(r["harness"])
        for k in r["kids"]:
            out.append(clip("  • %s  %s" % (g[state(k)], k["title"]), w))
    return out

def layout_tree(roots, w, g=GLYPHS["A unicode"]):
    """Hierarchy drawn with connectors; the root is a real node, not a label."""
    out = []
    for r in roots:
        out.append(clip("%s %s" % (g[state(r)], r["title"]), w))
        out.append(clip("   %s" % r["harness"], w))
        for i, k in enumerate(r["kids"]):
            last = i == len(r["kids"]) - 1
            agent, task = split_name(k["title"])
            out.append(clip("   %s %s %s" % ("└─" if last else "├─",
                                             g[state(k)], task or k["title"]), w))
            if agent:
                out.append(clip("   %s    %s" % ("  " if last else "│ ",
                                                 short_agent(agent)), w))
    return out

def layout_columns(roots, w, g=GLYPHS["A unicode"]):
    """Agent and task as aligned columns - scannable when names repeat."""
    out = []
    for r in roots:
        done = sum(1 for k in r["kids"] if state(k) == "idle")
        out.append(clip("%s  (%d/%d)" % (r["title"], done, len(r["kids"])), w))
        out.append(rule(w))
        aw = max([len(short_agent(split_name(k["title"])[0])) for k in r["kids"]] or [6])
        aw = min(aw, 10)
        for k in r["kids"]:
            agent, task = split_name(k["title"])
            out.append(clip(" %s %-*s %s" % (g[state(k)], aw,
                                             clip(short_agent(agent), aw), task), w))
    return out

def layout_dashboard(roots, w, g=GLYPHS["A unicode"]):
    """Counts first, then only what is not finished - triage view."""
    out = []
    for r in roots:
        c = collections.Counter(state(k) for k in r["kids"])
        out.append(clip(r["title"], w))
        out.append(clip("  %s %-2d  %s %-2d  %s %-2d" % (
            g["working"], c["working"], g["blocked"], c["blocked"],
            g["idle"], c["idle"]), w))
        live = [k for k in r["kids"] if state(k) != "idle"]
        if live:
            out.append("")
            for k in live:
                out.append(clip("  %s %s" % (g[state(k)], k["title"]), w))
        elif r["kids"]:
            out.append(clip("  all %d finished" % len(r["kids"]), w))
    return out

LAYOUTS = [
    ("1 flat",      "today's shape: header + bullets", layout_flat),
    ("2 tree",      "connectors; root is a node",      layout_tree),
    ("3 columns",   "agent | task aligned",            layout_columns),
    ("4 dashboard", "counts, then only the live ones", layout_dashboard),
]

# ------------------------------------------------------------ FOOTERS
FOOTERS = {
    "A one line":  ["q quit  r refresh  ↑/↓ move  enter expand  a access"],
    "B two line":  ["↑/↓ move   enter expand   a access", "q quit    r refresh"],
    "C keys only": ["q  r  ↑↓  ⏎  a"],
    "D labelled":  ["[q]uit [r]efresh [a]ccess  ↑↓ ⏎"],
}
HEADERS = {
    "A plain":   lambda r, w: [r["harness"]],
    "B counted": lambda r, w: ["%s · %d workers" % (r["harness"], len(r["kids"]))],
    "C ruled":   lambda r, w: [r["title"], rule(w)],
    "D titled":  lambda r, w: ["%s  —  %s" % (clip(r["title"], w - 16), r["harness"])],
}
ACCESS = {
    "A full":    ["▀▀▀▀▀ QR ▀▀▀▀▀ (13 rows)", "open: http://192.168.100.176:6767",
                  "same wifi only"],
    "B compact": ["http://192.168.100.176:6767", "press A again for QR · wifi only"],
    "C boxed":   ["┌" + "─" * 30 + "┐", "│  ▀▀▀ QR ▀▀▀ (13 rows)       │",
                  "│  192.168.100.176:6767        │", "└" + "─" * 30 + "┘"],
}

# ------------------------------------------------------------- render
def frame(title, note, lines, w):
    top = "┌─ %s " % title
    top = top + "─" * max(0, w + 3 - len(top)) + "┐"
    out = [top]
    if note:
        out.append("│ " + clip(note, w).ljust(w) + " │")
        out.append("├" + "─" * (w + 2) + "┤")
    for ln in lines:
        out.append("│ " + clip(ln, w).ljust(w) + " │")
    out.append("└" + "─" * (w + 2) + "┘")
    return out

def side_by_side(blocks, gap=2):
    h = max(len(b) for b in blocks)
    blocks = [b + [" " * len(b[0])] * (h - len(b)) for b in blocks]
    return ["".join(b[i] + " " * gap for b in blocks).rstrip() for i in range(h)]

def show(title, blocks, per_row=2):
    print("\n" + "═" * 78)
    print("  " + title)
    print("═" * 78)
    for i in range(0, len(blocks), per_row):
        for ln in side_by_side(blocks[i:i + per_row]):
            print(ln)
        print()

MIDRUN = [{"id": "5e894a23", "title": "Project Status Review",
           "harness": "Claude Code", "status": "running", "kids": [
    {"title": "coder_zen:stock-concurrency-lock",      "status": "running", "elic": 0},
    {"title": "coder_cmdcode:fleet-accident-reports",  "status": "idle",    "elic": 2},
    {"title": "scout:scope-race-test-flake",           "status": "running", "elic": 0},
    {"title": "reviewer:review-br-2-payables",         "status": "idle",    "elic": 0},
    {"title": "coder_freebuff:kg-base-unit-read-auth", "status": "failed",  "elic": 0},
    {"title": "coder_cmdcode:admin-force-delete",      "status": "idle",    "elic": 0},
]}]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    w = 56
    if "--width" in sys.argv:
        w = int(sys.argv[sys.argv.index("--width") + 1])
    what = args[0] if args else "all"
    roots = MIDRUN if "--demo" in sys.argv else (
        [r for r in load() if r["kids"]][:1] or load()[:1])

    if what in ("all", "layouts"):
        show("LAYOUTS — pick one  (rendered at %d cols, the real pane width)" % w,
             [frame(n, d, f(roots, w)[:14], w) for n, d, f in LAYOUTS], 2)

    if what in ("all", "components"):
        r = roots[0]
        # A MIXED set: every state present, or the glyph variants look identical.
        k = [{"title": "coder_zen:stock-concurrency-lock", "status": "running", "elic": 0},
             {"title": "coder_cmdcode:fleet-accident-reports", "status": "idle", "elic": 2},
             {"title": "reviewer:review-br-2-payables", "status": "idle", "elic": 0},
             {"title": "coder_freebuff:kg-base-unit", "status": "failed", "elic": 0}]
        show("COMPONENT — status glyphs",
             [frame(n, "", ["%s %s" % (g[state(x)], clip(split_name(x["title"])[1], w - 6))
                            for x in k], w) for n, g in GLYPHS.items()], 2)
        show("COMPONENT — group header",
             [frame(n, "", f(r, w), w) for n, f in HEADERS.items()], 2)
        show("COMPONENT — selection marker",
             [frame(n, "", [f(i == 1) + "✓ " + clip(split_name(x["title"])[1], w - 6)
                            for i, x in enumerate(k)], w) for n, f in SEL.items()], 2)
        show("COMPONENT — footer  (must fit the pane; today's is 52 cols)",
             [frame(n, "%d cols" % max(len(l) for l in ls), ls, w)
              for n, ls in FOOTERS.items()], 2)
        show("COMPONENT — access panel",
             [frame(n, "", ls, w) for n, ls in ACCESS.items()], 2)

main()
