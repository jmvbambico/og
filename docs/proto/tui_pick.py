#!/usr/bin/env python3
"""og agents — the chosen combination.

  tree · ascii glyphs · 6-dot braille spinner · plain header · caret
  · one-line footer · full QR+URL · boxed agents

  python3 tui_pick.py                 # stills, every box style
  python3 tui_pick.py --live          # animate (ctrl-c to stop)
  python3 tui_pick.py --live B4       # animate one style
  python3 tui_pick.py --width 64
"""
import sys, time, itertools

W = 64
if "--width" in sys.argv:
    W = int(sys.argv[sys.argv.index("--width") + 1])

# 6-dot braille: only dots 1-6, never the 7/8 descenders, so the glyph never
# changes height and the row never jitters. 6 frames, one cycle ≈ 0.7s at 120ms.
SPIN6 = "⠋⠙⠹⠸⠼⠴"
SPIN10 = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

GLYPH = {"idle": "+", "blocked": "!", "failed": "x", "unknown": "?"}

WORKERS = [
    ("stock-concurrency-lock",  "zen",      "working", "2m14s"),
    ("fleet-accident-reports",  "cmdcode",  "blocked", "waiting on you"),
    ("scope-race-test-flake",   "scout",    "working", "41s"),
    ("review-br-2-payables",    "reviewer", "idle",    "done 6m ago"),
    ("kg-base-unit-read-auth",  "freebuff", "failed",  "boot failed"),
]
SELECTED = 0

def mark(state, f):
    return SPIN6[f % len(SPIN6)] if state == "working" else GLYPH[state]

def clip(s, w): return s if len(s) <= w else s[: max(0, w - 1)] + "…"
def pad(s, w):  return clip(s, w).ljust(w)

# Every box style returns rows ALREADY prefixed with the tree connector and the
# caret column, so the tree reads top to bottom whatever the box costs in rows.
def rows(i, w, f, style):
    name, agent, state, meta = WORKERS[i]
    last = i == len(WORKERS) - 1
    elbow = "└─" if last else "├─"
    spine = "  " if last else "│ "
    car = "▸" if i == SELECTED else " "
    g, info = mark(state, f), "%s · %s" % (agent, meta)
    inner = w - 6                       # connector(2) + caret(1) + space + 2 borders

    if style == "B1":                   # full box, 4 rows
        return [elbow + " " + car + "┌" + "─" * inner + "┐",
                spine + "  " + "│" + pad(" %s %s" % (g, name), inner) + "│",
                spine + "  " + "│" + pad("   " + info, inner) + "│",
                spine + "  " + "└" + "─" * inner + "┘"]
    if style == "B2":                   # one line
        left = " %s %s" % (g, name)
        return [elbow + " " + car + "[" + pad(left, inner - len(agent) - 1)
                + agent + " ]"]
    if style == "B3":                   # two rows, rounded, no bottom rule
        return [elbow + " " + car + "╭" + "─" * inner + "╮",
                spine + "  " + "│" + pad(" %s %s" % (g, name), inner) + "│",
                spine + "  " + "╰" + pad(" " + info, inner) + "╯"]
    bar = "┃" if i == SELECTED else "│"   # B4 left edge, 2 rows
    return [elbow + " " + car + bar + pad(" %s %s" % (g, name), inner + 1),
            spine + "  " + bar + pad("   " + info, inner + 1)]

def _qr():
    """The REAL code for the LAN URL, ANSI stripped. 13 rows x 27 cols — a mock
    QR that is shorter than the real one would hide the layout cost."""
    import os, re as _re, subprocess as _sp
    here = os.path.dirname(os.path.abspath(__file__))
    cached = os.path.join(here, "qr_real.txt")
    if os.path.exists(cached):
        return open(cached).read().rstrip("\n").split("\n")
    out = _sp.run(["qrencode", "-t", "ANSIUTF8", "-m", "1", "-s", "1", URL],
                  capture_output=True, text=True).stdout
    return [_re.sub(r"\x1b\[[0-9;]*m", "", l) for l in out.splitlines()]

URL = "http://192.168.100.176:6767"
QR = _qr()
FOOTER = "q quit  r refresh  ↑/↓ move  enter expand  a access"

def screen(style, w, f, access=True):
    out = ["Claude Code"]
    for i in range(len(WORKERS)):
        out += rows(i, w, f, style)
    if access:
        out += [""] + access_box(w)
    return out + ["", clip(FOOTER, w)]


def access_box(w):
    """The access panel as its own box, titled, flush left.

    Flush left on purpose: it is not a worker, so putting it under the tree
    spine would read as one more agent. The QR is 27 cols; a pane narrower than
    that gets the URL alone rather than a wrapped code nobody can scan.
    """
    inner = w - 2
    top = "┌─ access " + "─" * max(0, inner - 9) + "┐"
    body = []
    if max(len(q) for q in QR) + 2 <= inner:
        body += ["│ " + pad(q, inner - 1) + "│" for q in QR]
        body += ["│" + " " * inner + "│"]
    body += ["│ " + pad(URL, inner - 1) + "│",
             "│ " + pad("same wifi only — not reachable outside",
                        inner - 1) + "│"]
    return [top] + body + ["└" + "─" * inner + "┘"]

def framed(title, lines, w):
    top = "┌─ %s " % title
    top += "─" * max(0, w + 3 - len(top)) + "┐"
    return [top] + ["│ " + pad(l, w) + " │" for l in lines] + \
           ["└" + "─" * (w + 2) + "┘"]

def side(blocks, gap=2):
    h = max(len(b) for b in blocks)
    blocks = [b + [" " * len(b[0])] * (h - len(b)) for b in blocks]
    return ["".join(b[i] + " " * gap for b in blocks).rstrip() for i in range(h)]

if "--live" in sys.argv:
    at = sys.argv.index("--live")
    style = sys.argv[at + 1] if len(sys.argv) > at + 1 and \
        sys.argv[at + 1] in ("B1", "B2", "B3", "B4") else "B1"
    try:
        for f in itertools.count():
            sys.stdout.write("\033[H\033[J" + "\n".join(screen(style, W, f)) + "\n")
            sys.stdout.flush(); time.sleep(0.12)
    except KeyboardInterrupt:
        pass
    sys.exit()

if "--one" in sys.argv:
    at = sys.argv.index("--one")
    style = sys.argv[at + 1] if len(sys.argv) > at + 1 else "B1"
    print("\n".join("  " + l for l in screen(style, W, 2)))
    sys.exit()

print("\n  SPINNER — 6-dot braille, all six frames")
print("    " + "   ".join(SPIN6) + "        (10-frame alt: " + " ".join(SPIN10) + ")\n")
print("  BOX STYLES — tree + caret + spinner, frozen mid-cycle\n")
for i in (0, 2):
    pair = [framed(n, screen(n, W - 2, 2, access=False), W)
            for n in ("B1", "B2", "B3", "B4")[i:i + 2]]
    print("\n".join(side(pair)) + "\n")
