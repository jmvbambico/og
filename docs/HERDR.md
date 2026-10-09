# Running og from herdr instead of the browser

[herdr](https://herdr.dev/) is a terminal multiplexer for coding agents — "tmux
for agents", with workspaces, tabs, panes, agent-state detection and a local
socket API. Omnigent is normally driven from a browser tab. This document
records what it takes to drive it from herdr instead, which parts are verified,
and which parts cannot work.

Everything below was measured against **Omnigent 0.17.0** and **herdr 0.9.3**
on macOS. Where a claim comes from Omnigent's source, the path is relative to
`<brew prefix>/Cellar/omnigent/0.17.0/libexec/lib/python3.14/site-packages/omnigent`.

---

## The shape that works

One herdr pane per Omnigent session, running `omnigent attach <session_id>`.
A bridge watches Omnigent's HTTP API and keeps herdr in sync:

```
   Omnigent server :6767                       herdr server
   ├ GET  /v1/sessions?kind=any  ──┐       ┌── pane.report_agent   (state badge)
   ├ GET  /v1/sessions/{id}/stream─┤       ├── pane.report_metadata (title)
   └ POST /v1/sessions/{id}/events │       ├── tab.create / tab.close
                                   │       └── pane.run "omnigent attach <id>"
                                   │                 │
                          ┌────────▼─────────────────▼────────┐
                          │  og herdr   (installer/og_herdr.py)│
                          │  watcher ──events──► bridge ──────►│
                          └────────────────────────────────────┘
```

- `installer/og_herdr_client.py` — herdr socket API client (NDJSON over a Unix
  socket). Knows nothing about Omnigent.
- `installer/og_herdr_watch.py` — Omnigent session watcher (poll + SSE). Knows
  nothing about herdr.
- `installer/og_herdr.py` — the bridge and the `og herdr` CLI. The only module
  that knows about both.

The browser and the ngrok tunnel keep working unchanged. herdr becomes the desk
interface; the web UI stays the away-from-desk one.

---

## Why the pane command is `omnigent attach`

`omnigent attach` is a thin HTTP client: it "joins an already-running
conversation on a server and streams its I/O" and never spawns a server,
runner or harness. Three properties make it the right pane process:

1. **It works for sub-agent sessions.** Attach gates only on liveness
   (`cli.py:8284-8325`); the server's `get_session` has no `kind` or
   `parent_session_id` guard (`routes_core.py:1196-1263`); and `POST /events`
   explicitly handles `conv.kind == "sub_agent"`, even healing a dead child
   runner through the ancestor chain (`routes_events.py:2212-2234`). So every
   delegated worker can have its own pane.
2. **It covers ACP workers, which have no terminal at all.** See below.
3. **It carries the approval prompt.** The REPL installs an
   `on_elicitation_request` hook that renders a `y`/`a`/`n` prompt
   (`repl/_repl.py:996-1122`) and POSTs the verdict itself
   (`repl/_repl.py:2654-2660`).

### One caveat on discovery

`GET /v1/sessions` defaults to `kind="default"`, which returns **root sessions
only** (`routes_core.py:1364-1370`). A watcher that forgets `?kind=any` silently
never sees a single delegated worker. This is the easiest thing to get wrong in
the whole design.

---

## Why multiple clients are safe

Co-drive is a designed-for case, not a tolerated one.
`runtime/session_stream.py:415-418` states it directly: "Multiple concurrent
subscribers to the same conversation each see every event independently — there
is no contention between them." `attach` describes itself as a "pure co-drive
client … exactly like the web UI co-drive" (`chat.py:571-578`). Opening a
stream registers you as a viewer and emits `session.presence` to co-viewers
(`routes_events.py:2793-2800`). There is no takeover, lease, eviction or
exclusivity logic anywhere in the package.

The terminal-attach websocket is likewise non-exclusive: write attach needs
`LEVEL_OWNER`, read attach only `LEVEL_READ` (`terminal_attach.py:288-357`),
and the tmux bridge attaches viewers with `-r` so "a viewer can't resize the
owner's pane" (`terminals/control_bridge.py:565`).

### But the bridge must never answer an approval

Elicitations are broadcast to every subscriber, while the parked Future is
single (`server/_elicitation_registry.py:21`). **The first resolver wins and
every later one gets `not_found`.** A bridge that auto-answered would race the
user's own browser or phone. So it reports `blocked` and stops there.

---

## Agent state: pushed, not detected

herdr classifies agents into `working` / `blocked` / `done` / `idle` /
`unknown` by foreground process plus screen manifests. Neither mechanism
recognises Omnigent, and neither can be taught to:

- The `--kind` list in `herdr agent start` is a compiled-in enum of 24 kinds.
  A bogus kind is rejected at runtime (`unsupported interactive agent kind`).
- Detection manifests only *patch* known agents. herdr's own docs: "Remote
  manifests patch detection rules for agents Herdr already knows how to
  identify. Adding a completely new agent still requires a Herdr binary update
  for process detection, labels, and integration behavior."
- The local override path is `~/.config/herdr/agent-detection/<agent>.toml`.
  (`~/.local/state/herdr/agent-detection/remote/*.toml` is the *download
  cache*, not a place to author anything.)

The escape hatch is `pane.report_agent`, whose `--agent` is a **free-form
label**, not a member of the enum:

```bash
herdr pane report-agent w1:pM --source og-bridge --agent omnigent --state working
```

Verified: the pane then appears in `herdr agent list` and `herdr agent get` as
a first-class agent with a real `agent_status`, and `herdr agent rename` will
even give it a live name. This is **better** than detection would have been —
the state comes from Omnigent's own API rather than from pattern-matching a
TUI.

The mapping the bridge applies, elicitations taking priority over status:

| Omnigent session | herdr state |
|---|---|
| `pending_elicitation_count > 0` | `blocked` |
| `status == "running"` | `working` |
| `status` in idle / completed / done / closed | `idle` |
| anything else, or malformed | `unknown` |

### What a reported agent cannot do

Reported agents are observable but **not drivable through the agent API**:
`herdr agent prompt` refuses with `agent_not_ready: agent <pane> is not an
active named agent`. Input must go through `pane.send_text` / `pane.send_keys`
/ `pane.run`, which are unrestricted. This costs nothing when a human types in
the pane; it only means the bridge drives panes rather than agents.

---

## What cannot work

### Replacing Omnigent's terminal layer with herdr

Omnigent hosts every native-harness agent in tmux, and that is not negotiable
from outside. `"tmux"` is a hardcoded literal plus `shutil.which("tmux")`
(`inner/terminal.py:745-766, 1489`). There is no backend class, driver,
strategy interface or config key — `grep` for `Mux|Multiplexer|TerminalBackend`
returns nothing, and the only related env var, `OMNIGENT_TMUX_SOCK`, is
*popped* for security (`inner/terminal.py:1531`), not read as a selector.

Each terminal is its own private tmux server on an isolated socket:

```
tmux -S <socket> -f /dev/null <option cmds…> ; new-session -d -s main -x 80 -y 24 -c <cwd> <cmd>
```

(`inner/terminal.py:1630-1655`, spawned at `:1660`.) `send` is `send-keys`
(`:1679-1732`), `read` is `capture-pane` (`:1734-1769`). There is no pty or
pexpect path; `terminals/registry.py:1-34` documents the deliberate move off
the legacy pexpect manager.

Because herdr exposes no tmux-compatible CLI, there is nothing for a PATH shim
to impersonate. Making herdr the host for Omnigent's agents requires an
upstream change to Omnigent.

### Auto-detecting an agent through a nested tmux attach

Attaching a herdr pane to one of those private sockets *displays* beautifully —
tested, and the real Claude Code TUI renders live, spinner and status bar
included, with `-r` keeping the viewer from resizing the owner's pane. But
herdr sees the pane's foreground process as `tmux`, not `claude`, so its
process gate never selects a manifest and `agent_status` stays `unknown`.

Useful as an optional high-fidelity view for a native worker. Useless as a
detection strategy, and inapplicable to ACP workers.

### ACP workers in a terminal

`acp:*` harnesses are plain stdio JSON-RPC children —
`asyncio.create_subprocess_exec(launch_path, *argv, stdin=PIPE, stdout=PIPE,
stderr=PIPE, …)` (`inner/acp_executor.py:514`) — with no pty and no tmux pane.
Confirmed empirically: with two native `claude` sessions live, there were
exactly two `omnigent-terminal-*` sockets and none for any ACP worker.

For `og`'s roster that is `coder_kilo`, `coder_freebuff`, `coder_cmdcode`,
`scout` and `integrator`. Their panes show the `omnigent attach` event stream;
there is no TTY to mirror.

### Free-form approvals

The REPL does not prompt for complex free-form elicitation schemas — it
declines them with a "use the web UI" message (`repl/_repl.py:2631-2647`).
Those specific approvals always need the browser. This is the one place where
"purely herdr, no browser" is literally false.

---

## Open items

- **Upstream ask:** an `omnigent` kind in herdr proper, which would bring real
  process detection and integration behaviour instead of pushed state.
- **Install-time wiring.** `og herdr` currently takes its settings from flags
  and defaults; it is not yet a question in `og-install.json`.
- **Remote.** herdr's remote story is SSH (`herdr --machine`, `herdr session
  attach`); Herdr Cloud is not released. Phone access stays on the Omnigent
  tunnel.
- **Persistence.** The bridge keeps its session→pane mapping in memory, so a
  restart re-reconciles from scratch rather than re-adopting existing panes.
