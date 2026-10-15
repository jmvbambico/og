# Spec — `og start <mux>`, session adoption, and `og agents`

Status: **design, not built.** Everything here is either measured against a
live system (and says so) or an open decision (and says that too).

What exists today: `docs/HERDR.md` describes the bridge — `og herdr`, a daemon
that projects live Omnigent sessions into herdr spaces. That is built, merged
and running. This spec covers the three things on top of it:

1. a launcher, `og start herdr`, that starts a session *inside* a multiplexer;
2. an adoption handshake so the launcher and the bridge do not fight;
3. `og agents`, a TUI over the session tree.

Plus the framing that makes the first one extensible: **supported terminal
multiplexers** as a catalog, the way supported agents already are.

---

## 1. Command surface

```
og start [<mux>] [local|tunneled] [domain] [--use ngrok|tunnl]
        [--once] [--socket PATH] [--bin PATH]
```

`<mux>` is a multiplexer id from the registry (today: `herdr`). It is
orthogonal to the network mode, so every existing form keeps working and
composes:

```
og start herdr
og start herdr tunneled
og start herdr tunneled --use tunnl
og start herdr --once
og start tunneled                     # unchanged
```

### The launcher's flags, forwarded

`--once`, `--socket` and `--bin` are the **launcher's** flags
(`og_herdr_launch.build_parser`), and they reach it only from `cmd_start`'s
exec — which is a separate process with its own parser. They were originally
unreachable: the exec carried a fixed argv, so `og start herdr --once` died in
`parse_start_args` as an unknown option on its way to a parser that has had the
flag all along. `parse_start_args` now collects them into `OG_START_MUX_ARGS`
and the exec appends them after `--mux`/`--server`, so they can never displace
the two that command derives.

`--mux` and `--server` are deliberately NOT forwarded: `cmd_start` computes them
from the mux id and `$PORT`, and an override would point a pane at another space
or another server with nothing else to show for it. They stay unknown options
here.

Everything the launcher's parser does not have is still an unknown option, and
a launcher flag with no multiplexer is refused rather than accepted and dropped
— a flag the operator typed that nothing acts on is the same failure this whole
change is about.

### The parsing collision — this one bites

`cmd_start`'s positional handling ends in a catch-all:

```bash
*)  mode="tunneled"; domain="${pos[0]}"
    info "a bare argument is read as an ngrok domain; this is 'og start tunneled $domain'" ;;
```

So **today `og start herdr` already means "tunneled on ngrok reserved domain
`herdr`"**, and says so. A multiplexer keyword must therefore be recognised and
removed *before* that fallback runs, or the new command silently does something
else entirely.

**Rule:** scan all positionals for a known multiplexer id and strip the first
match, wherever it appears. Then interpret what remains exactly as today.

Scanning any position rather than only the first is deliberate: `og start
tunneled herdr` is a natural thing to type, and under a first-position-only rule
it would quietly become an ngrok domain — the exact footgun the rule exists to
close.

The cost is that an ngrok reserved domain literally named `herdr` becomes
unreachable positionally. Escape hatch, documented: `OG_NGROK_DOMAIN=herdr og
start tunneled`.

### Degradation when the multiplexer is absent

`og start herdr` on a machine without herdr: warn, then start normally.

```
! herdr not found on PATH — starting without it.
  Install it from https://herdr.dev/ and re-run to use spaces.
```

og is fully usable without any multiplexer. This is correctness, not courtesy:
the mux is a view, never the runtime.

Open, minor: a script that *depends* on getting a mux has no way to tell. If
that ever bites, add `--require-mux` rather than changing the default.

---

## 2. Launch sequence

Four situations, distinguished by two facts: are we already inside the mux, and
does this directory already have a live session.

A fifth, (E), sits outside that grid: it is reached by typing `og chat` rather
than `og start`, so neither fact selects it. See below.

```
                    ┌─ inside the mux? (HERDR_ENV)
                    │
        ┌───────────┴───────────┐
       no                      yes
        │                       │
   ┌────┴────┐            ┌─────┴─────┐
 session    no           session     no
 in $PWD?   session      in $PWD?    session
   │          │             │           │
   │          │             │           │
  (B)        (A)           (C)         (D)
```

**(A) Not inside, no session — the ordinary first launch.**

1. `og start` proper: server up (and the tunnel first, if tunneled).
2. If the mux server is not running, start it headless (`herdr server`) and
   wait for its socket.
3. `workspace.create(label=<basename $PWD>, cwd=$PWD, focus=true)` — returns
   `workspace`, `tab` and `root_pane` in one reply (measured; the schema's own
   result listing omits the latter two, so trust the measurement).
4. `tab.rename(tab_id, <agent_name>)` — `hivemind`, from `og-install.json`.
5. `pane.send_text(root_pane, "og chat\n")`.
6. If `herdr_agents_pane` is on: `pane.split(root_pane, direction=right)` then
   `pane.send_text(new_pane, "og agents\n")`.
7. Record the space (§3) — but the session id is not known yet. See the
   deferred-write note in §3.
8. Start the bridge if it is not already running.
9. `exec herdr` — attach the TUI.

Ordering is load-bearing: `og chat` needs a live server, so steps 1 and 5 cannot
swap.

The space is labelled from the directory at this point because **no session
exists yet** — `og chat` is what mints one. The bridge renames the space to the
conversation title once it appears (`workspace.rename`).

### A degradation the operator can find out about

Step 6 is best effort, and correctly so: the chat pane is the session, and a
cosmetic second pane must never take it down. What it was *not* correctly doing
was failing silently — `pane.split` raising, herdr reporting no new pane id and
`pane_run` raising all `warn()`ed and then handed the terminal to `exec herdr`,
whose TUI enters the alternate screen within milliseconds. Every one of those
warnings was wiped before a human could read it, and the launcher wrote no log,
so "the agents pane never appeared" arrived as a report with no cause — from
another machine, where nobody could see what had been printed a moment earlier.

The trade is unchanged (a failed split is never fatal). What changed is that a
degradation now goes three places:

- **`~/.omnigent/logs/herdr-launch.log`**, one timestamped line per failure. A
  sibling of `herdr-server.log`, never that file: that one is herdr's own stdout
  and stderr, and og's sentence written there would read as the multiplexer's.
  This is the channel that answers "what happened?" afterwards.
- **A bounded hold before the exec** (6s, only when stdout is a terminal and
  something actually degraded). The log is what an operator finds tomorrow; the
  hold is what tells them today. Neither alone is enough — a log nobody knows to
  open is the same void with a file in it. It is skipped entirely under `--once`,
  which returns before the exec and so already leaves the text on screen.
- **The terminal**, as before, and re-surfaced at the hold: `info("opened space
  …")` is printed *after* the split, so the first copy has already scrolled off
  the top by exec time.

stdout is flushed explicitly before `execvp` as well: exec does not flush, so on
a pipe the warnings would be discarded by the exec rather than by the TUI — the
same loss by a different mechanism.

The same reasoning says what a degradation must never be: a non-zero exit, or any
exception out of `_split_agents`. Both are pinned by tests.

**(B) Not inside, session already in $PWD.**

The session is live somewhere; put the operator on it instead of starting a
second one.

- If that session has a space: `workspace.focus(ws)`, then `exec herdr`.
- If it does not: adopt it (§4), then focus and attach.

**Known gap, unfixed.** TWO things open the `og agents` pane, and neither is this
case: `_open_space` (a fresh launch) and `og_herdr._add_history_space` (a history
space for a past conversation, resumed from the spaces list). Both go through the
one implementation, `og_herdr_launch.split_agents_pane`; there is deliberately no
second copy of the split to drift. ADOPTING AN EXISTING SPACE is still the case
that never splits — and says nothing when the space it attaches to has no agents
pane (closed by hand, or created before the feature). At the keyboard that is
indistinguishable from case (A)'s split failing. It is not a small fix: the
split needs a tab id and a root pane, and an existing space's layout cannot be
trusted to match what the state file recorded, so "add the pane if it is missing"
is a design decision rather than a line of code. Splitting unconditionally is
the safe half, and has to be idempotent besides — the operator may have closed
the pane on purpose. Until then `og start herdr --once` is the diagnostic.

**(C) Inside the mux, session already in $PWD.**

This is the case that produced the rule. The operator pressed "new space", got a
shell, and typed `og start herdr` in a directory that already has a session.

```
workspace.focus(existing_space)
pane.close(HERDR_PANE_ID)
```

Both measured: focus moves, and closing a workspace's last pane removes the
workspace, so the space herdr just created for the operator disappears with the
pane. No orphan, no error path.

If the session has no space, adopt first (§4), then focus and close.

**(D) Inside the mux, no session in $PWD.**

A new space for a new conversation, which is what the operator asked for by
pressing "new space":

- `workspace.create(label=<basename $PWD>, cwd=$PWD, focus=true)`
- same tab rename / `og chat` / optional split as (A)
- do **not** re-attach; we are already attached.

The og server is normally already running in this case. Detect and skip rather
than restart.

**(E) Inside the mux, in a space herdr made, typing `og chat` directly.**

The operator presses "new space", gets a shell, `cd`s to the directory they
want, and types `og chat`. No `og start` runs, so none of (A)–(D) applies: the
space already exists and herdr, not og, created it.

`og chat` therefore does for itself the two things `_open_space` does for a
launched space, and nothing else:

- write the `pending` record keyed by `$PWD` (§3) **before** exec'ing the chat,
  so the bridge can claim and rename the space when the session appears
- if `herdr_agents_pane` is on, split and run `og agents`

Then `exec omnigent run` as it always did.

Three constraints this case brought with it, each of them a real difference
from (A)–(D) rather than a copy of it:

- **It must be a no-op outside herdr.** `og chat` in an ordinary terminal has
  to behave exactly as it always has. The gate is `HERDR_ENV=1` plus all three
  of `HERDR_WORKSPACE_ID` / `HERDR_TAB_ID` / `HERDR_PANE_ID` — all three,
  because a record naming a pane that does not exist is worse than no record.
- **The tab is not renamed.** (A) renames because `workspace.create` cannot
  label the tab it makes; here herdr made the tab, and the bridge renames it to
  the conversation title on adoption, so an interim basename would be visible
  for a moment and then replaced.
- **The split cannot assume it owns the tab.** `_split_pane_id` picks "any pane
  in this tab but the chat pane", which is exact when the launcher made the
  space — but this space is the operator's, and a pane they split themselves is
  theirs. The chat path passes the tab's panes as they were *before* the split,
  and when no new pane can be identified it starts `og agents` nowhere rather
  than guessing into one of theirs.

`og chat --dir <path>` sets the directory the session is rooted in without
`cd`-ing first. An absolute path is used as-is; a relative one resolves against
`projects_dir` from `og-install.json` (default `~/projects`). A path that does
not exist is refused, never created. `--dir` is consumed by `og chat`;
everything else is still forwarded verbatim to `omnigent run`, which is what
keeps `og chat --resume <id>` working.

### What counts as "a session in this directory"

A **root** session (`parent_session_id` is null) whose detail-row `workspace`
equals `$PWD`, and which is live:

- not `archived`
- `runner_online` is true
- `status` in (`running`, `idle`)

`idle` counts deliberately: an idle root is one waiting for the human to type,
which is precisely the session they want to be returned to. A failed or
runner-offline session must not block a fresh start.

`workspace` is on the **detail** row only — the listing row does not carry it.
The check therefore costs one detail fetch per live root, which is a handful.

---

## 3. The state file — the integration point

Launcher and bridge share one file, which is what stops them fighting.

`$OMNIGENT_HOME/og-herdr.json` exists today at version 1:

```json
{"version": 1, "workspaces": {"w9": "<session id>", "wA": "<session id>"}}
```

It is keyed by workspace id, which answers "is this space ours?" (what
`--cleanup` needs) but not "does this session have a space?" (what adoption
needs). Adoption wants the reverse lookup, so:

```json
{
  "version": 2,
  "spaces": {
    "<session_id>": {
      "workspace_id": "wA",
      "tab_id":       "wA:t1",
      "pane_id":      "wA:p1",
      "owner":        "launcher" | "bridge",
      "cwd":          "/Users/cryogenix/projects/og"
    }
  }
}
```

Rules:

- **v1 is upgraded in place, never discarded.** A v1 file may describe spaces
  that are live on screen right now; dropping it would orphan them from
  `--cleanup`. Read v1, rewrite as v2, keep going.
- **`cwd` is informational.** The authoritative directory is the session's
  `workspace` from the API. The cached value is a fast path, not a source of
  truth, and must be re-verified before anything is closed or focused.
- **`owner` records who created the space**, so a future `--cleanup` can offer
  "only what the bridge opened" if that ever matters. Today both are cleaned
  alike.
- **The file is not a lock, but every read-modify-write takes one.** Both
  processes write it whole, so any operation that means something is a
  read-modify-write, and re-reading immediately before writing (which both
  sides do) narrows the race without closing it — a second read only moves it.
  So load, change and save happen inside one `og_herdr.state_update`, holding
  `og_herdr.state_lock` — an advisory `flock` on a `og-herdr.json.lock` SIDECAR,
  never the state file itself, because `os.replace` swaps the inode a lock would
  ride on. Whoever commits first, the second writer reads the first's file and
  lands on top of it. It is advisory: a writer that ignores it is still
  last-writer-wins, which is what this file has always been.
- **Readers re-read, and merge per key — never wholesale.** The bridge folds the
  file in at each pass boundary (before `_prune_pending`, which saves), inside
  every save, and again at the moment it claims a pending record — the last of
  those *is* the save, in one transaction. A key the bridge has not changed
  since the file last held it belongs to whoever wrote the file last, *including
  a launcher that replaced it* under the same directory key. Memory wins for a
  key it holds an unsaved change to, and for a key it has finished with whose
  record the file still shows unchanged — that second half is what stops a
  claimed record from being offered to the next session. A key the file no
  longer has is never removed from memory, so an unreadable file changes nothing.

### The deferred write

In case (A) the space exists before the session does, so there is no key to
record it under. Two options, and this one is **open**:

1. **Launcher polls** for the session that appears in `$PWD` and writes the
   record once it sees it. Simple, self-contained, but the launcher has to
   stay alive past `exec herdr` — which it cannot.
2. **Bridge adopts by directory.** The launcher writes a pending record keyed
   by cwd; the bridge, on first seeing a session whose `workspace` matches a
   pending record, claims it and rewrites the entry keyed by session id.

(2) is the only one that survives `exec`, so it is the likely answer, but it
means the pending record needs its own shape and a staleness rule (a pending
entry whose directory never produces a session must expire, or it accumulates).

---

## 4. Adoption

**Trigger:** a live root session exists for a directory and has no space.

**Action:** create the space and attach to the *existing* session rather than
minting a new chat.

```
workspace.create(label=<session title>, cwd=<session workspace>, focus=…)
tab.rename(tab, <agent_name>)
pane.send_text(root_pane, "omnigent attach --server <url> <session_id>\n")
record in the state file, owner="launcher"
```

Note the pane command: an adopted session is **attached**, not `og chat`ed.
`og chat` would start a new conversation, which is the thing adoption exists to
avoid.

The bridge performs the same adoption when it sees a live root with no recorded
space — which is also what makes the bridge safe to restart. It holds its
session→pane map in memory, so after a restart every space looks unowned; the
state file is what tells it otherwise.

### Why this removes the duplicate-space problem

Without it: the launcher creates a space and runs `og chat`, which mints a
session; the bridge then sees a brand-new root it has no record of and creates a
*second* space. Two spaces, one conversation.

With the shared file, the bridge's rule becomes: **a session already in the file
is adopted, never created.** No new mechanism — both sides read the file they
already share.

---

## 5. Layout and naming

Measured mapping from the sidebar:

```
○ <workspace label> · <tab label>
    <agent label>                    ← from pane.report_agent
```

| Element | Value | Source |
|---|---|---|
| Space label | the conversation title | session `title`, renamed from the directory once known |
| Space cwd | the session's directory | detail `workspace` |
| Orchestrator tab | `hivemind` | `agent_name` in `og-install.json` |
| Worker tab | `coder_zen:space-per-root` | session `title` — Omnigent already formats it `agent:task` |
| Agent label | `Claude Code`, `OpenCode (Zen)` | `harness` → `installer/registry.json` |
| Agent state | working / blocked / idle | `herdr_state()` |

The orchestrator tab is the one change from what is built: it currently has no
label, so herdr shows its default (`'1'`, which the UI hides).

---

## 6. `og agents`

A TUI over the Omnigent session tree. **Not herdr-specific** — it reads
Omnigent's API and runs in any terminal. `og start herdr` merely puts it in a
split pane.

### Data, all measured

| Element | Source |
|---|---|
| Tree | `parent_session_id` |
| Node title | session `title` |
| Status icon | `status` + `pending_elicitations_count` |
| Harness icon | `harness`, and `/v1/agents` for the agent catalog |
| Snippet line | last assistant message in the detail row's `items` |

The snippet is verbatim what the web UI shows: for `space-per-root` it is
*"The work was already committed (`573414d`) before the interruptio…"*.

### The cost problem, and the fix

A detail fetch is **121 KB** and the item list cannot be trimmed — `?items_limit=1`,
`?limit=1` and `?include=summary` all return the full 100 items. At ~16 workers
that is ~2 MB per refresh.

So the refresh is two-tier:

- **Listing** drives the live view — cheap, already paginated and filtered in
  `og_herdr_watch.py`, gives the tree, titles and statuses.
- **Detail** is fetched lazily for the snippet — on selection, or once when a
  worker goes idle, which is when its report exists and stops changing.

### Scope

**Open decision.** Two readings:

- **Current session's tree** — the orchestrator plus its workers. Matches the
  web UI panel, and matches being in that session's space.
- **Everything live** — all roots and their workers, one tree per root.

The split pane argues for the first (it sits in one session's space). A
standalone `og agents` argues for the second. A `--all` flag covers both but
defers the question of what the default is.

### Reuse

The watcher is already multiplexer-agnostic and already does pagination,
filtering, truncation handling, token matching and backoff. `og agents` should
import it rather than re-implement any of that.

---

## 7. Install question

The question list in `og_install.py` is flat and already has a `bool` type
(`auto_update`). Following the existing convention — ask always, note when it
does not apply, the way `tunnl_ssh_key` does:

```python
{"key": "herdr_agents_pane", "type": "bool", "default": True,
 "ask": "In a herdr session, should `og agents` open beside `og chat` as a "
        "split pane? No means the session starts with `og chat` alone. "
        "Ignored if herdr is not installed."},
```

Lands in `og-install.json`; `bin/og` reads it at launch. It does not touch the
orchestrator prompt, so the 13.3 KB argv ceiling is not in play.

---

## 8. Supported terminal multiplexers

### Why a catalog

Of the three modules built, two are already portable:

| Module | Portable |
|---|---|
| `og_herdr_watch.py` | **fully** — its docstring already says it knows nothing about herdr |
| bridge logic (projection, tree, adoption, cleanup) | yes |
| `og_herdr_client.py` | no — herdr's socket protocol |

### What does not port

`pane.report_agent` — the live working/blocked/idle badges. tmux has no
equivalent; a tmux backend gives named windows and nothing else.

So the backend contract is **capability-based, not lowest-common-denominator**:

- **Required:** create space, create tab, run command, close, focus, rename
- **Optional:** report agent state

The table says so per row rather than implying parity.

### The tmux naming hazard

tmux is **already a required dependency** — Omnigent runs every native agent
terminal in a private tmux server (`tmux -S <socket>`, one per terminal). A
user-facing `og start tmux` would make tmux mean two different things in one
tool: the invisible substrate, and a window manager.

No technical conflict — separate servers, separate sockets — but it is a
documentation hazard precisely where a reader is most likely to be confused.
Worth either wording around it explicitly, or proving the abstraction with a
second backend that does not collide (zellij, wezterm) and adding tmux once the
seam is known.

### What to build now, and what not to

**Now** (cheap, locks nothing in):

- the registry row, matching `registry.json`'s "adding a vendor is a row, not
  code" convention. `{id, label, binary, capabilities}` — detection and listing
  genuinely are data.
- README sections (§9).
- graceful degradation.

**Not now:** extracting the backend interface. There is exactly one
implementation, and an interface designed against one implementation ends up
shaped like it. Extract when the second backend is written — that is when the
seam becomes knowable rather than guessable.

Be explicit in the docs that a registry row buys detection and the table; the
backend code still has to exist per multiplexer, exactly as an agent row needs a
harness plugin behind it.

---

## 9. README changes

**1. The "Per workflow" table** — herdr fits the established
"only for X; without it everything else runs as normal" shape, beside `gh`,
`ngrok`, `ssh` and `qrencode`:

> | `herdr` | only for `og start herdr`: runs the orchestrator inside [herdr](https://herdr.dev/), one space per session with each delegated worker as a tab. Without it `og start herdr` says so and starts normally; nothing else changes. |

**2. A new section parallel to "Supported agents":**

> ## Supported terminal multiplexers
>
> | Multiplexer | Session → | Worker → | Agent status | Status |
> |---|---|---|---|---|
> | herdr | workspace ("space") | tab | **yes** — live badges | verified against 0.9.3 |
> | tmux | session | window | no — window names only | not built |

The "Agent status" column is the honest part: it names what each backend gives
up instead of implying they are interchangeable.

---

## 10. Open decisions

All three are now **resolved**; the sections above describe the resolution.

| # | Decision | Resolved as |
|---|---|---|
| 1 | Deferred write in case (A) (§3) | Launcher writes a `pending` record keyed by cwd. The bridge claims it when a session appears in that directory. **Staleness is by existence, not by clock**: a pending record whose `workspace_id` is no longer in `workspace.list` is pruned on the next poll. Deterministic, self-cleaning, nothing to tune. |
| 2 | `og agents` scope (§6) | Scope by cwd, the same rule the launcher uses: a session whose `workspace` is `$PWD` → that tree; otherwise all live roots. `--all` forces the wide view. This also means the TUI needs no session id passed in, which matters because at split-pane time there is not one yet. Where MORE THAN ONE session has this workspace, the most recently active one wins — recency, not liveness: a previous session in the same repo stays "live" forever because it still has workers, and picking by liveness showed the operator yesterday's run. |
| 3 | Missing multiplexer (§1) | Warn and proceed. `--require-mux` only if a script ever needs the distinction. |

### Why decision 1 could not go the other way

`og chat` is `exec omnigent run "$AGENT_DIR" --server …` — the session is minted
by `omnigent run` inside the pane, so the id does not exist when the launcher
needs it, and the launcher ends in `exec herdr` so it cannot wait around to
learn it. Creating the session over the API instead would mean duplicating what
`omnigent run` does for worker scoping and environment, which is a far worse
trade than a pending record.

## 11. Explicitly out of scope

- A second multiplexer backend.
- Extracting the backend interface (§8).
- Changing how the bridge projects sessions — that is built and specified in
  `docs/HERDR.md`.
- Anything that makes a multiplexer required to use og.
