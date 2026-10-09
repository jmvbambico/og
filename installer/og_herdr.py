#!/usr/bin/env python3
"""og herdr — project live Omnigent sessions into herdr panes.

Omnigent runs coding agents and is normally driven from a browser; herdr is a
terminal multiplexer for coding agents. This bridge watches the running Omnigent
server for sessions coming and going and opens one herdr tab per session whose
pane runs `omnigent attach <session_id>` — a thin co-drive client that streams
that session's I/O, so the user can work from herdr instead of the browser.

The low-level work lives in two sibling modules, `og_herdr_client` (a herdr
socket client) and `og_herdr_watch` (a session watcher). They are imported
LAZILY, inside the functions that need them, so importing this module never
requires them to exist — which is what lets the tests run with fakes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_SERVER = "http://127.0.0.1:6767"
DEFAULT_SOURCE = "og-bridge"


# ---------------------------------------------------------------------------
# lazy seams to the sibling modules
#
# Nothing here imports og_herdr_client / og_herdr_watch at module load. Tests
# replace these two functions with fakes and never need the real modules.
# ---------------------------------------------------------------------------

def _state(session: dict) -> str:
    """herdr's state (idle|working|blocked|unknown) for an Omnigent session.

    WHY the bridge stops at "blocked" and never answers: Omnigent broadcasts an
    elicitation to every connected client but parks a SINGLE Future for the
    answer, so the first resolver wins and every other client gets `not_found`.
    Auto-answering an elicitation here would race the user's own browser or
    phone for that one Future — so the bridge only REPORTS the blocked state and
    leaves the decision to a real client.
    """
    from og_herdr_watch import herdr_state

    return herdr_state(session)


def _error_cls():
    """The sibling client's HerdrError, resolved lazily so this module imports
    without it. Used as the `except` type in reconcile."""
    from og_herdr_client import HerdrError

    return HerdrError


def _error_line(sid: str, exc) -> str:
    """One `reconcile`-style error line for a failed herdr call."""
    code = getattr(exc, "code", "herdr-error")
    message = getattr(exc, "message", str(exc))
    return f"error {sid}: {code}: {message}"


def _id_of(entry) -> str | None:
    """The id out of a tab_create result's `tab` / `root_pane` sub-object.

    herdr 0.9.3 names these `tab_id` and `pane_id` respectively; there is no
    generic `id` key, so checking one would only ever match a shape the server
    never sends.
    """
    if isinstance(entry, dict):
        for key in ("pane_id", "tab_id"):
            if entry.get(key):
                return entry[key]
    return None


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------

class Bridge:
    """Reconciles Omnigent session events into herdr tabs.

    State is the in-memory map session_id -> {tab_id, pane_id, title}. It lives
    for the process lifetime: the bridge projects the sessions that are live
    while it runs.
    """

    def __init__(self, watcher, client, workspace=None, dry_run=False,
                 source=DEFAULT_SOURCE):
        self.watcher = watcher
        self.client = client
        self.workspace = workspace
        self.dry_run = dry_run
        self.source = source
        self._tabs: dict[str, dict] = {}

    # -- public surface -----------------------------------------------------

    def reconcile(self, events) -> list:
        """Apply a batch of session events, returning human-readable action
        lines. One bad pane is recorded and skipped, never fatal: the bridge
        must survive a single failing herdr call and keep projecting the rest.
        """
        lines: list[str] = []
        for event in events:
            try:
                lines.extend(self._apply(event))
            except _error_cls() as exc:
                lines.append(_error_line(event.session_id, exc))
        return lines

    def run_once(self) -> list:
        """Poll the watcher once and reconcile whatever it returned."""
        return self.reconcile(self.watcher.poll_once())

    def run_forever(self) -> None:
        """Consume watcher.watch() indefinitely, reconciling each event."""
        for event in self.watcher.watch():
            for line in self.reconcile([event]):
                print(line, flush=True)

    # -- event handlers -----------------------------------------------------

    def _apply(self, event) -> list:
        kind = event.kind
        sid = event.session_id
        if kind == "added":
            return self._added(sid, event.session)
        if kind == "changed":
            return self._changed(sid, event.session, event.previous or {})
        if kind == "removed":
            return self._removed(sid)
        return []

    def _added(self, sid: str, session: dict) -> list:
        rec = self._tabs.get(sid)
        if rec is not None:
            # One tab per session: a redelivered `added` either finds setup
            # already complete (no-op) or a previous attempt that died part-way
            # — in which case the pane already exists and the remaining steps
            # resume on it instead of a second `tab_create` opening a duplicate
            # tab for the one session.
            if rec["ready"]:
                return []
            state = _state(session)
            self._setup_pane(sid, rec, state)
            return [f"resume add {sid} → pane {rec['pane_id']} "
                    f"'omnigent attach {sid}' [{state}]"]
        title = session.get("title")
        cwd = session.get("workspace") or str(Path.home())
        label = "og:" + (title or sid[:8])
        state = _state(session)
        if self.dry_run:
            self._tabs[sid] = {"tab_id": None, "pane_id": None, "title": title,
                               "step": 3, "ready": True}
            return [f"dry-run: add {label} ({cwd}) → omnigent attach {sid} "
                    f"[{state}]"]
        result = self.client.tab_create(self.workspace, cwd=cwd, label=label)
        tab_id = _id_of(result.get("tab"))
        pane_id = _id_of(result.get("root_pane"))
        if tab_id is None or pane_id is None:
            return self._reject_partial_create(sid, tab_id, pane_id)
        # Record the mapping the moment the ids are known, BEFORE the rest of
        # the setup. If a setup call then raises, this record keeps the pane
        # tracked so a redelivered `added` resumes on it; recording only on
        # full success would leave the session looking unmapped and the retry
        # would open a second tab.
        rec = {"tab_id": tab_id, "pane_id": pane_id, "title": title,
               "step": 0, "ready": False}
        self._tabs[sid] = rec
        self._setup_pane(sid, rec, state)
        return [f"add {sid} → tab {tab_id} pane {pane_id} "
                f"'omnigent attach {sid}' [{state}]"]

    def _setup_pane(self, sid: str, rec: dict, state: str) -> None:
        """Run the post-`tab_create` setup steps for a session's pane.

        `rec["step"]` counts the steps that have already succeeded, so a failure
        mid-setup leaves the record pointing at the failed step. A redelivered
        `added` then resumes from there on the SAME pane rather than creating a
        second tab, and a step that already succeeded is not repeated —
        re-running pane.run would submit the attach command into the pane again.
        """
        pane_id = rec["pane_id"]
        if rec["step"] <= 0:
            self.client.pane_run(pane_id, "omnigent attach " + sid)
            rec["step"] = 1
        if rec["step"] <= 1:
            self.client.report_agent(pane_id, self.source, agent="omnigent",
                                     state=state)
            rec["step"] = 2
        if rec["step"] <= 2:
            self.client.report_metadata(pane_id, self.source, title=rec["title"])
            rec["step"] = 3
            rec["ready"] = True

    def _reject_partial_create(self, sid: str, tab_id, pane_id) -> list:
        """Reject a tab_create reply that carried no usable ids.

        Stores nothing: a `pane_id` of None in the mapping would make every
        later `changed` report against a pane that does not exist. When a tab id
        WAS obtained but its pane id was not, close it best-effort so the user's
        workspace does not keep an orphan tab; a failure of that cleanup is
        recorded, not raised, so the rest of the batch still runs.
        """
        if tab_id is None and pane_id is None:
            detail = "no tab or pane id"
        elif tab_id is None:
            detail = "no tab id"
        else:
            detail = "no pane id"
        lines = [f"error {sid}: bad_response: tab.create reply had {detail}"]
        if tab_id is not None:
            try:
                self.client.tab_close(tab_id)
            except _error_cls() as exc:
                lines.append(_error_line(sid, exc))
        return lines

    def _changed(self, sid: str, session: dict, previous: dict) -> list:
        rec = self._tabs.get(sid)
        if rec is None:
            # A session we never opened: nothing to update, ignore it safely.
            return []
        state = _state(session)
        title = session.get("title")
        title_changed = previous.get("title") != title
        if self.dry_run:
            rec["title"] = title
            extra = " title" if title_changed else ""
            return [f"dry-run: update {sid} → state={state}{extra}"]
        self.client.report_agent(rec["pane_id"], self.source, agent="omnigent",
                                 state=state)
        if title_changed:
            self.client.report_metadata(rec["pane_id"], self.source, title=title)
        rec["title"] = title
        return [f"update {sid} → state={state}" + (" (title)" if title_changed
                                                   else "")]

    def _removed(self, sid: str) -> list:
        rec = self._tabs.get(sid)
        if rec is None:
            return []
        if self.dry_run:
            self._tabs.pop(sid, None)
            return [f"dry-run: remove {sid} (release pane, close tab)"]
        pane_id, tab_id = rec["pane_id"], rec["tab_id"]
        try:
            self.client.release_agent(pane_id, self.source, agent="omnigent")
            self.client.tab_close(tab_id)
        except _error_cls() as exc:
            if getattr(exc, "code", None) == "not_found":
                # herdr says the pane or tab is already gone: the outcome we
                # wanted has happened, so drop the mapping instead of retrying a
                # removal that can never succeed.
                self._tabs.pop(sid, None)
                return [f"remove {sid} (pane {pane_id}, tab {tab_id}; "
                        f"already gone)"]
            # Any other error keeps the mapping: popping it here would leave the
            # pane and tab untracked with no way to retry, leaking them for
            # good. The error is recorded by `reconcile` and a later `removed`
            # retries this cleanup.
            raise
        self._tabs.pop(sid, None)
        return [f"remove {sid} (pane {pane_id}, tab {tab_id})"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="og herdr", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true",
                    help="poll the server once, reconcile, and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the actions without calling herdr at all")
    ap.add_argument("--server", default=DEFAULT_SERVER, metavar="URL",
                    help=f"Omnigent server URL (default: {DEFAULT_SERVER})")
    ap.add_argument("--socket", default=None, metavar="PATH",
                    help="herdr socket path (default: the client's own)")
    ap.add_argument("--workspace", default=None, metavar="ID",
                    help="herdr workspace to open tabs in")
    ap.add_argument("--source", default=DEFAULT_SOURCE, metavar="NAME",
                    help=f"reporting source name (default: {DEFAULT_SOURCE})")
    return ap


def _load_token(server_url: str) -> str | None:
    """Best-effort bearer token for the server, read from Omnigent's own token
    store exactly as bin/og does. Missing file or entry reads as no token, which
    the watcher handles like any unauthenticated request."""
    path = Path(os.environ.get("OMNIGENT_HOME", Path.home() / ".omnigent")) \
        / "auth_tokens.json"
    try:
        store = json.loads(path.read_text())
    except Exception:
        return None
    rec = store.get(server_url) if isinstance(store, dict) else None
    if isinstance(rec, dict):
        token = rec.get("token")
        return token if isinstance(token, str) else None
    return None


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    from og_herdr_watch import SessionWatcher

    watcher = SessionWatcher(base_url=args.server, token=_load_token(args.server))
    client = None
    if not args.dry_run:
        # Constructed only for a real run: --dry-run must make zero herdr calls,
        # and building the client is the one step that could touch the socket.
        from og_herdr_client import HerdrClient

        client = HerdrClient(socket_path=args.socket)

    bridge = Bridge(watcher, client, workspace=args.workspace,
                    dry_run=args.dry_run, source=args.source)

    if args.once:
        for line in bridge.run_once():
            print(line)
        return 0
    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
