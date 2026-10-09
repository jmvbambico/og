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

## The socket's real contract (measured, not assumed)

Three facts about the herdr socket were discovered only by talking to a running
server. Every one of them had already passed a full test suite against a fake
built on the opposite assumption, and the first would have broken the feature
outright — so they are recorded here rather than left in a commit message.

### A connection serves exactly ONE request

The server answers one request, then closes. Measured across three trials, one
connection each:

```
workspace.list, workspace.list, workspace.list  ->  reply, EOF, BrokenPipeError
ping, agent.list                                ->  pong, BrokenPipeError
```

The method does not matter. Since the bridge issues four calls per session
(`tab.create`, `pane.run`, `pane.report_agent`, `pane.report_metadata`), a
reused connection fails on every *second* call — alternating failure on every
event. `og_herdr_client.py` therefore connects, sends, reads and closes per
request. `events.subscribe` is the sole exception: there the subscription *is*
the connection.

### `events.subscribe` takes `subscriptions`, not a list of type names

```
{}                                              -> missing field `subscriptions`
{"types": [...]}                                -> missing field `subscriptions`
{"subscriptions": ["pane.agent_status_changed"]} -> invalid type: string …,
                                                    expected internally tagged
                                                    enum Subscription
{"subscriptions": [{"type": "…"}]}              -> missing field `pane_id`
{"subscriptions": []}                           -> {"type":"subscription_started"}
```

So each entry is an internally-tagged object, a per-type entry also needs a
`pane_id`, and the empty list is the accepted catch-all.

### An unparseable request comes back with `"id": ""`

herdr cannot echo an id it failed to read, so a malformed request answers with
an empty id. A reply loop that skips frames on id mismatch therefore discards
the server's real message, reads EOF, and reports a dropped connection instead
of `missing field 'subscriptions'`. The client carves out empty-id error frames
for exactly this reason.

## The HTTP API's real shape (measured)

The session listing was originally documented here from `sys_session_get_info`'s
MCP output. That was wrong, and the wrong version reached the implementation.
What `GET /v1/sessions` actually returns, measured against 0.17.0:

```json
{"object":"list","data":[ … ],"has_more":true,"first_id":"<id>","last_id":"<id>"}
```

One row carries exactly these keys:

```
agent_id  agent_name  archived  comments_count  created_at  external_session_id
id  labels  owner  parent_session_id  pending_elicitations_count
permission_level  runner_id  status  title  updated_at  viewer_unread
```

Four traps, each of which had already shipped into the code:

- **`pending_elicitations_count` is PLURAL.** The MCP tool spells it singular.
  Reading the singular key against a REST row silently never matches, so
  `blocked` — the state a human most needs to see — never fires.
- **The id is `id`, not `session_id`**, and the array is `data`, not
  `sessions`. There is no `kind` field on a row at all; root versus sub-agent
  is `parent_session_id` being null or not.
- **There is no `workspace` field**, so a session does not tell you its
  directory. `og herdr` takes `--cwd`, defaulting to its own working directory.
- **The listing is paginated**: default page 20, newest-first, cursor via
  `after=<last_id>`, with `has_more` saying whether more remain. There is **no
  server-side status filter** — `status=`, `statuses=` and `state=` are all
  ignored — so filtering is the client's job.

### Why pagination is a correctness problem, not a performance one

Sessions are never retired from the listing: this machine held 100+ with 91
idle and 8 failed. Reading only the first page means the root session — the
conversation the human is actually driving — is pushed off by newer sub-agents
and never seen. Worse, a session that falls off the page boundary between polls
is indistinguishable from one that ended, so a naive diff reports `removed` and
the bridge closes a live pane.

The watcher therefore walks the cursor to the end, bounded by
`SESSION_PAGE_LIMIT` / `MAX_SESSION_PAGES` / `MAX_LISTED_SESSIONS`; and when a
walk stops short for any reason it suppresses `removed` events for that poll and
merges rather than replaces its retained state. Suppression alone would be a
trap: without the merge, the next complete poll re-reports every suppressed
session as `added` and the bridge opens a second tab for a pane already on
screen.

That suppression is only sound **because the listing is newest-first**, so the
rows that go unseen are old ones already known. If the ordering ever flips, a
genuinely new session could land beyond the cap and never be announced.

### What gets a pane

Projecting every live session would mean ~100 tabs, nearly all dead. So:

- a **root** session is projected while it is listed and not archived — it
  reads `idle` the whole time it waits for the human to type, so retiring it on
  idle would close the pane they are typing into;
- a **sub-agent** is projected only while `running` or holding a pending
  elicitation, and its pane retires when it goes idle or fails.

### One consequence worth generalising

A fake server encodes the beliefs of whoever wrote it, and a contract document
encodes the beliefs of whoever wrote *that*. Eight of the fourteen blocking
defects in this feature were wrong beliefs about herdr or Omnigent, not wrong
code — they passed review, 223 tests, and a different-vendor reviewer, because
every one of those checks was reasoning from the same wrong premise. The suite
even contained a test (`test_peer_close_is_reported_not_retried`) that modelled
the *real* hangup behaviour while the comment beside it asserted the opposite.

Probe the real thing before writing the contract, not after the tests pass.

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
- **Duplicate session ids** in one listing: the last entry wins. The server
  does not emit duplicates, so this is theoretical.
- **A buffered partial SSE frame is lost** if `resp.read()` raises, because
  `flush()` only runs on a clean end-of-stream.
- **`pane.read` tolerates five payload spellings** (a bare string, or
  `text`/`output`/`content`/`data`, or a `lines` list). Only one is in use;
  the tolerance is unverified guesswork and could be narrowed once the real
  shape is confirmed against a live pane.
- **A permanently truncated listing** (session count above the caps forever)
  degrades the watcher to add-and-change only: removals never resume, so a
  finished worker beyond the cap keeps a stale pane. The stderr notice on
  entering that state is the operator's signal.
- **`_seen` does not evict.** During a long truncated period it grows to the
  server's session count. Bounded and small per row, but there is no eviction
  policy, because "which session do we forget" is a consumer decision.
- **A never-fetched session retires only when it comes back into view.** A
  session that finishes while sitting past the cap keeps its pane until a
  listing fits in one walk. Inherent: without the row there is no evidence to
  retire on, and inventing one is the original defect. A session that *was*
  fetched and stopped qualifying retires immediately, truncated or not.
- **A retired session that later reappears arrives as `added`, not `changed`**,
  so the bridge opens a fresh tab rather than reviving the old one.
- **Duplicate rows for one id are last-wins, verdict included.** A server
  sending `[projectable, archived]` for the same id would close a live
  session's pane. First-wins has the mirror failure; the choice is pinned in
  tests in both directions so it is deliberate.
- **`_announce_listing` sets its flag before writing to stderr**, so a failed
  write loses the truncation notice permanently. Observability only.
- **`watch()` retries every `Exception`**, so a programming error (`TypeError`,
  `AssertionError`) becomes a log-and-retry loop rather than surfacing.
  Narrowing the catch risks reintroducing "the daemon dies on one unexpected
  error", which is the defect the broad catch exists to prevent — a judgement
  call, left to the owner.

---

## Operational notes

Things the bridge has to do because of how long it runs:

- **`watch()` survives a failed poll.** A transient HTTP error, a server
  restart or a malformed listing would otherwise end the generator, and
  `Bridge.run_forever` does not wrap it — so one blip would silently stop
  projecting anything. It retries with bounded backoff (`poll_interval` up to
  `MAX_POLL_BACKOFF`, reset on recovery) and reports to stderr. `poll_once`
  stays strict and still raises; only the loop is forgiving. Because
  `self._seen` is assigned last in `poll_once`, a failed poll cannot corrupt
  it, so recovery diffs against the last state actually observed instead of
  emitting a storm of `removed` + `added`.
- **The SSE buffers are capped** (`MAX_SSE_LINE_BYTES`, `MAX_SSE_FRAME_BYTES`).
  A stream that never sends a newline, or a `data:` run never closed by a blank
  line, would otherwise grow memory without limit in a process meant to run for
  days. Over the cap the junk is discarded and the stream continues.
- **Tokens are matched to the server.** `discover_token(base_url, …)` returns a
  token only when the store key names that server. It used to prefer a loopback
  key and then fall through to the first usable token, which would have sent a
  remote server's bearer token to `127.0.0.1`.
- **A partially set-up pane is resumable.** The session→pane record is written
  the moment `tab.create` returns ids, with a step counter, so a herdr call
  that fails mid-setup leaves exactly one tab that a redelivered `added`
  finishes — rather than a second tab, or an untracked orphan.
