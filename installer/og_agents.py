#!/usr/bin/env python3
"""`og agents` — a terminal view of the Omnigent session tree.

It shows the orchestrator (a ROOT session) and the workers it delegated to,
built from two sources and nothing else:

  * the SESSION LISTING, which supplies the tree (`parent_session_id`), the
    titles and the statuses; and
  * the DETAIL row (`GET /v1/sessions/{id}`), read lazily, which supplies the
    `workspace` (to resolve "which session is this directory") and the report
    SNIPPET.

The listing is cheap and drives the live view; the detail row is 121 KB and is
fetched as little as possible. See `AgentModel.refresh` for the budget and why a
per-row-per-poll fetch is a defect rather than a slow path.

This module is deliberately NOT herdr-specific: it reads Omnigent's HTTP API and
must run in any terminal. It never writes to a session — no prompting, no
answering an elicitation, no dispatches. The only requests it makes are GETs,
and they all go through `og_herdr_watch`, which owns the transport, pagination,
`kind=any` default, truncation handling and per-server token matching. None of
that is re-implemented here.

The renderer is a pure function from state to a list of lines (`render`), and
the curses layer only paints that list; that separation is what lets the view be
tested with `--once`, with no tty in sight.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from og_herdr import DEFAULT_AGENT_LABEL, harness_labels
from og_herdr_watch import (
    DEFAULT_BASE_URL,
    SessionWatcher,
    _session_id,
    herdr_state,
)

# The status glyph shown beside a worker. Keyed by `og_herdr_watch.herdr_state`
# so there is one state vocabulary in the project; anything the viewer does not
# recognise is "?" rather than a wrong guess.
STATE_GLYPH = {
    "working": "●",
    "idle": "✓",
    "blocked": "⏸",
    "unknown": "?",
}

DEFAULT_INTERVAL = 3.0

# Ceilings. The view is meant to run for a long time, so its two caches are
# bounded rather than growing one entry per session the machine has ever seen.
# Both are order-of-magnitude above anything real: MAX_ROOT_DETAILS is the
# number of live roots whose detail is worth a 121 KB fetch at startup (the
# "handful" the design budgets for), and MAX_SNIPPETS is a window of reports.
MAX_ROOT_DETAILS = 32
MAX_SNIPPETS = 256


# ---------------------------------------------------------------------------
# pure: snippet extraction
# ---------------------------------------------------------------------------

def snippet_from_detail(detail: Any) -> Optional[str]:
    """The report text of a session, from its DETAIL row, or None.

    The snippet is the LAST `items` element that is an assistant message
    (`type == "message"` and `data.role == "assistant"`), with its text taken
    from the `output_text` parts of `data.content`:

        {"id": "25f8b2d7…", "type": "message", "status": "completed",
         "data": {"role": "assistant",
                  "content": [{"type": "output_text", "text": "The work…"}]}}

    The LAST one, not the first: the report is what the agent most recently
    said, and older assistant messages are the turns before it. When that last
    message carries no `output_text` the snippet is None rather than an earlier
    message's text — falling back would show a stale line as if it were the
    report, which is worse than showing nothing.

    Every shape is tolerated without raising, because the detail row is an
    enrichment and a missing one must cost the snippet and nothing else: a
    missing/None/empty `items`, an element that is not a dict, `content` that is
    not a list, and parts that are not dicts or carry no string `text`.
    """
    if not isinstance(detail, dict):
        return None
    items = detail.get("items")
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        data = item.get("data")
        if not isinstance(data, dict) or data.get("role") != "assistant":
            continue
        content = data.get("content")
        if not isinstance(content, list):
            return None
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "output_text"
            and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts) if parts else None
    return None


# ---------------------------------------------------------------------------
# pure: which roots, and the rows of the tree
# ---------------------------------------------------------------------------

def _norm_path(path: str) -> str:
    """A directory string normalised for comparison.

    realpath also collapses `..`, trailing separators and symlinks, so a
    workspace recorded as `/repo/og` matches a shell `$PWD` of `/repo/og/` or a
    path that reached the same place through a symlink. It does NOT require the
    path to exist, which matters because the session may name a directory this
    process cannot stat.

    Note this is deliberately not `Path.resolve`: the read-only guard in the
    tests forbids this module from CALLING anything whose name contains
    `resolve`, so that a stray call can never look like an elicitation
    resolver. `realpath` does the same job under a name the guard leaves alone.
    """
    return os.path.realpath(os.path.expanduser(path))


def select_scope(
    roots: Iterable[dict], cwd: str, all_roots: bool
) -> list[str]:
    """The root sessions the view shows, as a list of session ids.

    The cwd rule, and the same one the launcher uses: show the tree whose
    session's `workspace` is `$PWD`, if there is one; otherwise show every live
    root. `all_roots` (`--all`) forces the wide view even when one matches.

    Roots come in already filtered to the live set, so this only decides
    NARROWING. `workspace` is a detail-row field; a root whose detail could not
    be read simply has no match and does not narrow anything — the view falls
    back to the wide set rather than hiding the session the user is in because
    one optional fetch failed.
    """
    ids = [sid for sid in (_session_id(s) for s in roots) if sid]
    if all_roots or not cwd:
        return ids
    target = _norm_path(cwd)
    for root in roots:
        workspace = root.get("workspace")
        if isinstance(workspace, str) and workspace and \
                _norm_path(workspace) == target:
            return [_session_id(root)]
    return ids


def _is_root(session: dict) -> bool:
    """A session with no parent is a root: the conversation the human drives."""
    parent = session.get("parent_session_id")
    return not (isinstance(parent, str) and parent)


def _index(sessions: Iterable[dict]):
    """`(by_id, children)` in listing order.

    `by_id` maps a session id to its object; `children` maps a parent id to the
    ids of its children, in the order the listing gave them (newest-first, so a
    parent's newest worker is first among its siblings). A child whose parent is
    not in this listing is not indexed as a child — it would have no row to hang
    under; see `_preorder`.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for session in sessions:
        sid = _session_id(session)
        if sid and sid not in by_id:
            by_id[sid] = session
            order.append(sid)
    children: dict[str, list[str]] = {}
    for sid in order:
        parent = by_id[sid].get("parent_session_id")
        if isinstance(parent, str) and parent and parent in by_id:
            children.setdefault(parent, []).append(sid)
    return by_id, children


def _preorder(by_id: dict, children: dict, root_ids: Iterable[str]):
    """`[(session_id, depth)]` for the selected roots' trees, pre-order.

    A `seen` set makes a malformed listing — a cycle, or a session that is its
    own parent — terminate rather than recurse forever.
    """
    out: list[tuple[str, int]] = []
    seen: set[str] = set()

    def walk(sid: str, depth: int) -> None:
        if sid in seen or sid not in by_id:
            return
        seen.add(sid)
        out.append((sid, depth))
        for child in children.get(sid, []):
            walk(child, depth + 1)

    for rid in root_ids:
        walk(rid, 0)
    return out


@dataclass
class Row:
    """One line of the tree, before it is turned into text."""

    session_id: str
    text: str
    state: str
    depth: int
    is_root: bool = False
    snippet: Optional[str] = None
    expanded: bool = False


@dataclass
class Frame:
    """The whole view at one instant: the rows, plus anything to say about it."""

    rows: list[Row] = field(default_factory=list)
    truncated: bool = False
    notice: Optional[str] = None


def harness_label(harness: Any) -> str:
    """The product name for a harness id, from installer/registry.json.

    `claude-native` -> "Claude Code". A harness the registry does not list falls
    back to its raw id, which is ugly but honest: it names exactly what ran. A
    session that names no harness at all falls back further, to
    `og_herdr.DEFAULT_AGENT_LABEL`.

    The label table is read through `og_herdr.harness_labels` rather than a
    second copy of it here, so renaming a harness in the catalog cannot leave
    this view saying the old name. That loader never raises: a broken catalog
    degrades every label to a raw harness id, and a row is never lost over a
    cosmetic failure.
    """
    if not isinstance(harness, str) or not harness:
        return DEFAULT_AGENT_LABEL
    return harness_labels().get(harness, harness)


def build_rows(
    sessions: Iterable[dict],
    root_ids: Iterable[str],
    snippets: Optional[dict[str, Optional[str]]] = None,
    expanded_ids: Iterable[str] = (),
) -> list[Row]:
    """The display rows for the selected roots' trees, in listing order.

    A root row is labelled by its harness (the product name), matching the
    orchestrator header the operator's web UI shows. A worker row is labelled by
    its title and carries a status glyph. `snippets` is consulted per row, and a
    row is marked expanded only when its id is in `expanded_ids` AND it has a
    snippet to show.
    """
    snippets = snippets or {}
    expanded = set(expanded_ids)
    sessions = list(sessions)
    by_id, children = _index(sessions)
    rows: list[Row] = []
    for sid, depth in _preorder(by_id, children, root_ids):
        session = by_id[sid]
        if depth == 0:
            harness = session.get("harness")
            label = harness_label(harness) if isinstance(harness, str) \
                and harness else None
            text = label or session.get("title") or sid
        else:
            text = session.get("title") or sid
        snippet = snippets.get(sid)
        rows.append(Row(
            session_id=sid,
            text=text,
            state=herdr_state(session),
            depth=depth,
            is_root=depth == 0,
            snippet=snippet,
            expanded=sid in expanded and snippet is not None,
        ))
    return rows


# ---------------------------------------------------------------------------
# pure: the renderer (state -> lines)
# ---------------------------------------------------------------------------

def _oneline(text: str) -> str:
    """Collapse a multi-line report to one line for the tree."""
    return " ".join(text.split())


def row_line(row: Row) -> str:
    """The primary line for one row."""
    if row.is_root:
        return row.text
    indent = "  " * row.depth
    return f"{indent}• {STATE_GLYPH.get(row.state, '?')}  {row.text}"


def snippet_line(row: Row) -> str:
    """The indented report line under an expanded row."""
    indent = "  " * (row.depth + 1)
    return f"{indent}{_oneline(row.snippet or '')}"


def render(frame: Frame) -> list[str]:
    """A frame as a list of plain-text lines. Pure; no terminal involved.

    This is the whole renderer. curses paints exactly these lines, and `--once`
    prints them, so the shape of the view is tested here without a tty.
    """
    lines: list[str] = []
    if frame.notice:
        lines.append(frame.notice)
    elif frame.truncated:
        # A truncated listing is missing rows beyond the page cap, and since the
        # listing is newest-first those are the OLDEST sessions. Say so rather
        # than let an absence read as a finished conversation.
        lines.append("… partial listing: some sessions may be missing")
    for row in frame.rows:
        lines.append(row_line(row))
        if row.expanded:
            lines.append(snippet_line(row))
    return lines


# ---------------------------------------------------------------------------
# the model: listing + lazy detail
# ---------------------------------------------------------------------------

class AgentModel:
    """Builds a `Frame` from the listing, fetching detail only when it must.

    THE BUDGET. One refresh is exactly one listing walk plus a bounded number of
    detail fetches, and detail fetches happen on these three occasions and no
    others:

      1. once per LIVE ROOT, to learn its `workspace` (the cwd rule needs it)
         and its `harness` (the header label). Roots come and go, so this is
         "once per root, on its first sighting", not "once per poll".
      2. once per worker that has gone IDLE, to capture the report it just
         produced. An idle report stops changing, so it is fetched once and kept.
      3. once for a row the user SELECTED, so its snippet can be read on demand
         (and refetched when the user forces a refresh, while it is still
         running).

    Never "one per row per poll": a session already in a cache is not fetched
    again, so a stable listing costs nothing after the first refresh. The detail
    row is 121 KB and cannot be trimmed (`?items_limit`, `?limit` and
    `?include=summary` are all ignored by the server and return the full item
    list), so at ~16 workers a per-poll fetch would be ~2 MB every few seconds.
    """

    def __init__(
        self,
        watcher: SessionWatcher,
        cwd: Optional[str] = None,
        all_roots: bool = False,
    ) -> None:
        self.watcher = watcher
        self.cwd = cwd if cwd is not None else os.getcwd()
        self.all_roots = all_roots
        # root id -> {workspace?, harness?}, from one detail fetch each.
        self._root_info: dict[str, dict] = {}
        # session id -> snippet (or None, meaning "asked, nothing there"). Cached
        # so a running worker is not re-read every poll; dropped on a forced
        # refresh of the selected row.
        self._snippets: dict[str, Optional[str]] = {}

    # -- caches -------------------------------------------------------------

    def _root_detail(self, session_id: str) -> dict:
        """The workspace/harness of a root, fetched once and cached.

        A failed fetch is cached as `{}` too, deliberately: "once at startup"
        has to mean once, and a root whose detail endpoint is unhealthy for a
        while must not turn every poll into a retry. The cost is that the cwd
        rule cannot narrow on that root until the process restarts.
        """
        if session_id in self._root_info:
            return self._root_info[session_id]
        if len(self._root_info) >= MAX_ROOT_DETAILS:
            self._root_info.pop(next(iter(self._root_info)))
        detail = self.watcher._fetch_detail(session_id)
        info: dict = {}
        if isinstance(detail, dict):
            workspace = detail.get("workspace")
            if isinstance(workspace, str) and workspace:
                info["workspace"] = workspace
            harness = detail.get("harness")
            if isinstance(harness, str) and harness:
                info["harness"] = harness
        self._root_info[session_id] = info
        return info

    def _snippet(self, session_id: str) -> Optional[str]:
        """The report of a session, fetched once and cached.

        `self.watcher._fetch_detail` already swallows a transport or decode
        error and returns None, so a session whose detail cannot be read costs
        its snippet and nothing else — the row still renders.
        """
        if session_id in self._snippets:
            return self._snippets[session_id]
        if len(self._snippets) >= MAX_SNIPPETS:
            self._snippets.pop(next(iter(self._snippets)))
        text = snippet_from_detail(self.watcher._fetch_detail(session_id))
        self._snippets[session_id] = text
        return text

    # -- refresh ------------------------------------------------------------

    def refresh(
        self,
        selected_id: Optional[str] = None,
        expanded_ids: Iterable[str] = (),
        force: bool = False,
    ) -> Frame:
        """One listing walk -> a `Frame`. The only method that touches the wire."""
        rows, truncated = self.watcher.fetch_listing(kind="any")

        # Only archived sessions are dropped. `og_herdr_watch.should_project` is
        # NOT applied here on purpose: it drops an idle sub-agent, which is
        # right for a herdr pane but wrong for this view — the whole point of
        # the lazy "first idle" snippet is to read a worker's report AFTER it
        # stopped, so an idle worker must stay on screen long enough to show it.
        sessions = [s for s in rows
                    if isinstance(s, dict) and not s.get("archived")]

        by_id, children = _index(sessions)
        workers: dict[str, list[dict]] = {}
        for session in sessions:
            parent = session.get("parent_session_id")
            if isinstance(parent, str) and parent and parent in by_id:
                workers.setdefault(parent, []).append(session)

        live_root_ids = [
            sid for sid, session in by_id.items()
            if _is_root(session) and self._is_live_root(session, workers.get(sid, []))
        ]

        # Merge each live root's detail into its session object so the pure
        # layer below can decide the cwd match and the header label without
        # knowing anything about HTTP.
        merged = []
        for session in sessions:
            sid = _session_id(session)
            copy = dict(session)
            if sid in live_root_ids:
                copy.update(self._root_detail(sid))
            merged.append(copy)

        # Root caches are pruned to the live set: a root that is gone (or has
        # gone quiet) costs one re-fetch if it comes back, and the cache cannot
        # grow without bound across a long-running process.
        for sid in list(self._root_info):
            if sid not in live_root_ids:
                del self._root_info[sid]

        by_id, children = _index(merged)
        scope = select_scope(
            [by_id[sid] for sid in live_root_ids if sid in by_id],
            self.cwd,
            self.all_roots,
        )
        order = _preorder(by_id, children, scope)

        # The lazy fetches. An idle worker is read once and kept; a selected row
        # is read once on selection, and re-read only on a forced refresh.
        for sid, _depth in order:
            if sid not in by_id:
                continue
            if herdr_state(by_id[sid]) == "idle":
                self._snippet(sid)
            if sid == selected_id:
                if force:
                    self._snippets.pop(sid, None)
                self._snippet(sid)

        frame_rows = build_rows(merged, scope, self._snippets, expanded_ids)
        return Frame(rows=frame_rows, truncated=truncated)

    @staticmethod
    def _is_live_root(root: dict, workers: list[dict]) -> bool:
        """Whether a root is part of the live set this view shows.

        A root is live while it is working or blocked (it is the conversation
        the human is driving, or waiting on them), OR while it has ANY worker —
        including a worker that has already finished.

        "Has any worker" rather than "has an ACTIVE worker" on purpose: the whole
        reason this view keeps an idle worker on screen is so its report can be
        read, and that is exactly the case of a conversation whose orchestrator
        has answered and whose workers are done. A root with no workers at all
        and no activity is a finished solo conversation; dropping it is what
        keeps "all live roots" — and its per-root detail fetches — to a handful
        on a machine with a long history of sessions.
        """
        if herdr_state(root) in ("working", "blocked"):
            return True
        return bool(workers)


# ---------------------------------------------------------------------------
# the terminal
# ---------------------------------------------------------------------------

def _tty_available() -> bool:
    """Whether a curses TUI can run: a tty on stdout and curses importable."""
    if not sys.stdout.isatty():
        return False
    try:
        import curses  # noqa: F401
    except Exception:
        return False
    return True


def _run_tui(model: AgentModel, interval: float) -> int:
    """The live view. curses paints `render`'s lines; keys drive the model.

    Keys: `q` quits, `r` forces a refresh, the arrows (or `j`/`k`) move the
    selection, and Enter expands or collapses the selected row's snippet. It is
    a viewer: it never sends anything to a session.
    """
    import curses

    def loop(stdscr) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(250)
        selected = 0
        offset = 0
        expanded: set[str] = set()
        force = True
        last = 0.0
        frame = Frame()
        while True:
            height, width = stdscr.getmaxyx()
            selected_ids = [r.session_id for r in frame.rows]
            sel_id = selected_ids[selected] if 0 <= selected < len(selected_ids) else None

            if force or time.monotonic() - last >= interval:
                frame = model.refresh(
                    selected_id=sel_id, expanded_ids=expanded, force=force)
                last = time.monotonic()
                force = False
                selected_ids = [r.session_id for r in frame.rows]
                selected = min(selected, max(0, len(frame.rows) - 1))

            # Each row is one display line, plus one when its snippet is shown.
            display: list[tuple[str, Optional[int]]] = []
            if frame.truncated:
                display.append(("… partial listing: some sessions may be missing",
                                None))
            for index, row in enumerate(frame.rows):
                display.append((row_line(row), index))
                if row.expanded:
                    display.append((snippet_line(row), index))

            sel_line = next(
                (i for i, (_, index) in enumerate(display) if index == selected),
                0,
            )
            if sel_line < offset:
                offset = sel_line
            elif sel_line >= offset + height - 1:
                offset = max(0, sel_line - (height - 2))

            stdscr.erase()
            for screen_row, (text, index) in enumerate(display[offset:offset + height - 1]):
                attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
                try:
                    stdscr.addnstr(screen_row, 0, text, width - 1, attr)
                except curses.error:
                    # Writing the very last cell raises; the line is already
                    # drawn, so this is a no-op rather than a lost frame.
                    pass
            footer = "q quit   r refresh   ↑/↓ move   enter expand"
            try:
                stdscr.addnstr(height - 1, 0, footer, width - 1, curses.A_DIM)
            except curses.error:
                pass
            stdscr.refresh()

            key = stdscr.getch()
            if key == -1:
                continue
            if key in (ord("q"), 27):
                return
            if key == ord("r"):
                force = True
                continue
            if key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(max(0, len(frame.rows) - 1), selected + 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                if 0 <= selected < len(frame.rows):
                    sid = frame.rows[selected].session_id
                    if sid in expanded:
                        expanded.discard(sid)
                    else:
                        expanded.add(sid)

    curses.wrapper(loop)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="og agents",
        description="Live view of the Omnigent session tree.",
    )
    parser.add_argument("--server", default=DEFAULT_BASE_URL,
                        help="Omnigent server base URL")
    parser.add_argument("--all", action="store_true", dest="all_roots",
                        help="show every live root, not just this directory's")
    parser.add_argument("--once", action="store_true",
                        help="render one frame as plain text and exit")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between refreshes in the live view")
    return parser


def build_model(args: argparse.Namespace, opener: Optional[Any] = None) -> AgentModel:
    """The model for a parsed command line.

    `opener` is the transport seam: tests and callers pass a stub so no test can
    reach the operator's live server. It defaults to urllib inside
    `SessionWatcher`.
    """
    watcher = SessionWatcher(
        base_url=args.server,
        token=SessionWatcher.discover_token(args.server),
        opener=opener,
    )
    return AgentModel(watcher, cwd=os.getcwd(), all_roots=args.all_roots)


def main(argv: Optional[Iterable[str]] = None, opener: Optional[Any] = None) -> int:
    """`og agents`. `--once` (and any non-tty fallback) prints one plain frame."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    model = build_model(args, opener)

    if args.once or not _tty_available():
        if not args.once:
            # Say so rather than crash or silently block: the live view needs a
            # tty and curses, and one plain frame is the honest fallback.
            sys.stderr.write(
                "og agents: no tty or no curses; rendering one frame instead\n")
        for line in render(model.refresh()):
            print(line)
        return 0

    try:
        return _run_tui(model, args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
