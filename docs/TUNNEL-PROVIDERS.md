# Pluggable tunnel providers — ngrok and tunnl.gg

Feasibility study, design record, and implementation notes. Written 2026-10-06.

**Status: Phases 0–2 implemented** on `feature/tunnel-providers` — the provider
seam and tunnl.gg support in `bin/og`, and the `tunnel_provider` /
`tunnl_ssh_key` knobs in the installer. Phase 3 (automatic fallback, reconnect
supervisor, `og setup` preflight) is **not** implemented; see "Phase 3" below
and the open question at the end of "Decisions".

Everything under "Verdict" is probe-verified against the live service rather
than taken from the vendor docs. Keep it: the stdin trap in particular is the
kind of finding that is cheap to read and expensive to rediscover.

## Why

`og start tunneled` is hardwired to ngrok. Two failure modes bite in practice:

1. **ngrok free bandwidth runs out.** The tunnel still comes up; traffic dies.
2. **The machine is already running an ngrok agent for something else.** ngrok's
   free plan allows one agent, and `og`'s URL discovery reads the *singleton*
   local API at `127.0.0.1:4040` (`bin/og:66`). A second agent either refuses to
   start or binds 4041, at which point `tunnel_url()` reads the *other*
   project's tunnel and `og` configures itself with a URL that is not its own.
   That second half is a correctness bug, not just an inconvenience.

Both are properties of ngrok, not of tunnelling. A second provider removes the
single point of failure.

## Verdict

**Feasible, verified by probe, and a good fit — with one hard constraint and one
implementation trap that must not be missed.** tunnl.gg is a better match for
`og` than it first appears, because the existing design already isolates the
provider behind a small seam.

### What makes it easy

`og` is closer to pluggable than it looks. The public origin reaches the server
through **provider-agnostic environment variables** (`bin/og:765-768`):

```bash
export OMNIGENT_ACCOUNTS_BASE_URL="$url"
export OMNIGENT_WS_ALLOWED_ORIGINS="$url"
```

Local mode already feeds the same variables a non-tunnel URL
(`url="http://$ip:$PORT"`, `bin/og:706`). Nothing downstream of `$url` knows or
cares what produced it. The provider-specific surface is only four behaviours:

| Seam | ngrok today |
|---|---|
| preflight | `ngrok` on PATH (`:680`) + `ngrok config check` (`:712-715`) |
| spawn | `nohup ngrok http "$PORT" …` + pidfile (`:722-726`) |
| URL discovery | poll `http://127.0.0.1:4040/api/tunnels` (`:536-550`) |
| teardown | kill pidfile pid, grace, `kill -9` (`:1207-1216`) |

That is the whole abstraction. Everything else — the ~20s wait loop
(`:729-742`), `show_access`, `print_qr` (`:80-85`), `og url` (`:1426-1431`),
`og status` (`:1310-1313`) — is reusable verbatim.

### What makes it different

| | ngrok | tunnl.gg |
|---|---|---|
| Client | `ngrok` binary, must be installed | `ssh` — already on macOS, Linux, Win10+ |
| Auth | authtoken required | **none** |
| URL discovery | local HTTP API, JSON | **stdout under a PTY only** |
| Stable URL | reserved `--domain` (paid) | `stable@proxy.tunnl.gg`, **free**, tied to SSH key |
| Chosen name | reserved domain (paid) | `pro@` + dashboard (paid) |
| Monthly bandwidth | 1 GB free — *the user's pain* | no monthly cap; per-second rate limits instead |
| Concurrency | 1 agent free — *the user's other pain* | 3 tunnels per IP |
| Local port use | binds 4040 (conflicts) | none |
| Tunnel lifetime | unbounded | **24 h hard cap, 2 h idle cap** |
| Interstitial | ngrok warning page (free) | warning page once/day, skippable by header |

tunnl.gg resolves **both** stated pains directly: no monthly bandwidth quota,
no singleton local API to collide with, and 3 concurrent tunnels per IP.

### The hard constraint: lifetime caps make `stable@` mandatory

tunnl.gg closes a tunnel after **2 h of no requests** and at **24 h** regardless.
ngrok has no equivalent. An `og` server is long-lived, so the tunnel *will*
die under it — a regression ngrok never exposed us to.

Reconnecting is easy. Reconnecting *to the same URL* is the problem:
`OMNIGENT_ACCOUNTS_BASE_URL` and `OMNIGENT_WS_ALLOWED_ORIGINS` are baked into
the server process at launch (`:765-768`). If a reconnect yields a new random
subdomain, the running server's auth-cookie base URL and WebSocket origin
allowlist are both stale — sessions break and the UI's socket is rejected, with
no restart to repair it.

`stable@proxy.tunnl.gg` makes the subdomain a pure function of the SSH key, so
reconnects are URL-invariant and the baked env stays correct. **So for `og`,
`stable@` is not an optional nicety — it is the default, and random mode should
be opt-in only.** Two consequences worth designing for now:

- Pin the identity explicitly (`-i <key> -o IdentitiesOnly=yes`). If the user's
  `ssh-agent` offers a different key on a later connect, the "stable" URL
  silently changes. Record the chosen key path in `og.env`.
- Even with `stable@`, treat the URL as *verified on reconnect*, not assumed:
  re-scrape it and warn loudly if it differs rather than carrying on.

### URL capture from a pipe — CONFIRMED by probe

tunnl.gg prints the URL only to an allocated terminal — the docs call `-t`
required, and `ssh -t` refuses when local stdin is not a tty. `ssh -tt` (force)
with stdout piped **works**; no `script -q` PTY shim is needed. Measured output,
ANSI stripped:

```
  ✓ Tunnel is live on tunnl.gg

  URL      https://silver-fox-c0b8cdee.tunnl.gg
           random: connect as stable@ to keep the same URL
  Expires  in 24h, or after 2h without traffic
           Pro: your own name and no time limits, from $5/mo: …

  [~20 lines of block-drawing QR]

  Requests appear below. Press Ctrl+C to stop.

  TIME UTC  METHOD   PATH …
```

Four things this settles for the implementation:

1. **The scrape is simple but needs two filters.** Lines are CRLF-terminated
   (`\r`) and heavily SGR-coloured — the URL itself is wrapped in
   `\e[38;5;141m`. Strip `\r` and ANSI, then
   `grep -oE 'https://[a-zA-Z0-9.-]+\.tunnl\.gg'`. Do **not** anchor on the
   `URL` label or on a line number: ~20 lines of QR follow, and the banner
   wording is not a stable contract.
2. **`StrictHostKeyChecking=accept-new` is required, as predicted.** The probe
   emitted `Warning: Permanently added 'proxy.tunnl.gg' (ED25519) to the list of
   known hosts.` on a first connect — without the option that is an interactive
   prompt, which under `nohup` hangs `og start` indefinitely.
3. **The expiry is machine-readable from the same banner** (`Expires in 24h, or
   after 2h without traffic`), and random mode self-advertises `stable@`. `og`
   can parse the expiry for the `og status` clock in Phase 3 rather than
   hardcoding 24 h.
4. **The request log streams for the life of the tunnel** — tunnl prints a table
   row per request after the banner. So the provider's logfile grows unbounded
   with traffic, unlike ngrok's startup-only logfile. Once the URL is captured,
   redirect the stream to a size-capped log or `/dev/null`; do not keep
   appending to `$OMNI_HOME/logs/`. This is a new requirement the ngrok path
   never had.

### The stdin trap — diagnosed, and the sharpest finding here

The first probe captured the URL but then 404'd on the way back in. The second
probe identified why, and it is a trap any implementation would fall into.

Evidence: the 404 carried `server: cloudflare`, `content-type: text/plain` and a
10-byte body — tunnl's own edge response, **not** Python's HTML 404 page. The
local server answered 200 directly at the same moment. tunnl's live request
table logged **zero** requests. And the session ended with
`Connection to proxy.tunnl.gg closed by remote host.`

Diagnosis: **the tunnel dies seconds after printing its URL, because `ssh -tt`
reaches EOF on stdin.** `-tt` forces a PTY and ssh then forwards stdin to the
remote, which is an interactive session by design ("Press Ctrl+C to stop").
EOF on stdin ends it, and the tunnel goes with it. So the sequence is: tunnel
opens, banner prints, URL scrapes perfectly, tunnel is already gone by the time
anything connects.

This is exactly the shape of bug that would ship looking like "tunnl.gg is
unreliable": `og start tunneled --use tunnl` would report `tunnel up: https://…`
truthfully and hand the user a dead URL. Under `nohup` with stdin from
`/dev/null` it is guaranteed, not intermittent.

**The fix: hold stdin open without writing to it.** A read-write fd on a FIFO
never sees EOF, sends no data, and keeps ssh a *single* child process, which
matters because `og`'s teardown is pidfile-based (`:1207-1216`):

```bash
mkfifo "$OMNI_HOME/og-tunnl.fifo"
exec 3<>"$OMNI_HOME/og-tunnl.fifo"
nohup ssh -tt … -R "80:localhost:$PORT" proxy.tunnl.gg <&3 >"$LOG" 2>&1 &
echo $! > "$TUNNEL_PIDFILE"
```

Rejected alternatives: `< /dev/null` and `-n` give immediate EOF — the bug
itself. `sleep infinity | ssh …` works but adds a second process the pidfile
does not track, so `og stop` would orphan it.

Two requirements this adds to the provider contract:

- **Liveness must be checked *after* the URL is captured, not before.** The
  current ngrok wait loop checks liveness only while the URL is still missing
  (`:734`). For tunnl the dangerous state is URL-present-but-tunnel-dead, so the
  provider must confirm the child is alive after a successful scrape and treat
  "URL but no process" as a failure rather than success.
- **Clean up the FIFO** in teardown alongside the pidfile (`:1225`).

The FIFO fix is **confirmed working**: ssh stayed alive after the URL appeared
and 20 s later, fetches returned 200 with `content-length: 17` and a
`last-modified` matching the probe file, and tunnl's request table logged all
three hits at 200. Forwarding is real and holds.

### Measured characteristics worth designing around

- **Latency ~520–540 ms per request**, measured by tunnl's own `TOOK` column
  (edge: Cloudflare HKG). Correctness is unaffected, but the `og` dashboard is
  request-chatty and will feel slower than over ngrok. Compare the two before
  making tunnl the default.
- **Everything is proxied through Cloudflare**, so `server: cloudflare` appears
  on success too — it is *not* usable as a provenance signal. To tell "reached
  my local port" from "answered by the edge", check `content-length` /
  `last-modified`, or tunnl's own request table.
- **tunnl injects `x-frame-options: DENY`** and a `referrer-policy`. Harmless
  for `og` today; it would break any future embedding of the UI in an iframe.

### The interstitial is narrower than feared

curl got 200 with the real file body **without** the skip header, so the warning
page did not fire for a non-browser client. Taken with the docs' wording
("the first time someone opens your URL in a browser"), the interstitial is
evidently User-Agent-gated. Two consequences:

- API and CLI clients need no special handling at all — the
  `tunnl-skip-browser-warning` header is belt-and-braces, not a requirement.
- The **WebSocket risk largely dissolves.** A browser reaching the `og` UI loads
  HTML first, clears the interstitial, and receives its cookie; only then does
  the page open its WebSocket, which carries that cookie. The dangerous ordering
  — a WS as the very first request of the day — is not how the UI loads.

Still worth a real-browser confirmation rather than treating it as closed, but it
is no longer a plausible disqualifier. Verify it by pointing `og` at a tunnl
tunnel once Phase 1 exists; there is no way to simulate it with curl.

### The full ssh invocation

Every option below exists for a failure that is otherwise silent or hanging:

- `-tt` — force the PTY tunnl requires, even with stdin not a tty.
- `<&3` on a read-write FIFO fd — the stdin-EOF fix above. Non-negotiable.
- `-o ExitOnForwardFailure=yes` — without it ssh stays up with no forward, and
  the tunnel looks healthy while returning nothing.
- `-o StrictHostKeyChecking=accept-new` — a first-ever connect otherwise prompts
  for host-key confirmation, which under `nohup` hangs `og start` forever.
- `-o ServerAliveInterval=30 -o ServerAliveCountMax=3` — survive NAT idle, and
  fail fast rather than wedge when the link really drops.
- `-i <key> -o IdentitiesOnly=yes` — pin the identity so a `stable@` URL cannot
  drift when `ssh-agent` offers a different key.

### Residual risks

- **Browser WebSocket through the interstitial** — the one open item, reasoned
  down to low (see above) but not yet observed. Check it on the first real
  Phase 1 run.
- **Latency.** ~500 ms per request. The reason to think twice before making
  tunnl the default rather than the fallback.
- **Rate limits.** 25 req/s per visitor, 50 req/s per tunnel. Fine for a dashboard;
  worth knowing before anyone points a load generator at it.
- **Body size.** 128 MB per request and response; 1 GB per WS direction.
- **Banner-format coupling.** URL and expiry are scraped from human-readable
  output with no versioned contract. A tunnl.gg redesign breaks the scrape. Keep
  the regex loose, and fail with "could not find a URL in tunnl's output" plus
  the captured log rather than a bare timeout.

## Proposal

### Phase 0 — prove the assumptions — DONE

URL capture, the stdin-EOF fix, and end-to-end forwarding are all verified by
probe. The only item left is a real-browser WebSocket check, which cannot be
done before Phase 1 exists and is no longer considered a likely blocker.
**Phase 1 is cleared to start.**

### Phase 1 — the provider seam

Introduce a provider dimension in `bin/og` as four dispatching functions,
chosen by `$OG_TUNNEL_PROVIDER`:

```
tunnel_preflight_<p>   binary + auth checks, die with actionable instructions
tunnel_spawn_<p>       launch, write pidfile, return immediately
tunnel_scrape_<p>      print the https URL or fail (polled by the wait loop)
tunnel_stop_<p>        terminate
```

Two deliberate design choices:

1. **Per-provider pidfiles** (`og-tunnel-ngrok.pid`, `og-tunnel-tunnl.pid`),
   replacing the single `og-ngrok.pid` (`:37`). Lets the two coexist and lets
   `og stop` clean up whichever is actually running rather than guessing.
2. **A URL cache file**, `$OMNI_HOME/og-tunnel.url`, written once the URL is
   known, for **both** providers. `tunnel_url()` then becomes provider-agnostic
   — it reads the file — so `og url` and `og status` need no per-provider
   branch. It is also the publication channel a Phase 3 supervisor needs to
   announce a changed URL, and it removes ngrok's cross-project 4040 misread
   described above as a side benefit.

The ngrok path keeps its current behaviour byte for byte; it just moves behind
the seam.

### Phase 2 — the knob and the flag

**Installer.** Add `tunnel_provider` beside the existing runtime knobs. The
`default_mode` entry (`og_install.py:2695-2698`) is the exact template — a
`type: "choice"` with `choices`. Five touch points, no new machinery:

- interactive ask in `build_plan_interactive`, near `:922`
- the plan dict at `:942-956` → key `tunnel_provider`
- the `--questions` schema at `:2690-2706`
- `write_og_env` (`:2283-2289`) → `OG_TUNNEL_PROVIDER=`
- `show()` (`:2712-2769`) for visibility

No `validate()` change: there is no validation for `port`, `default_mode` or
`ngrok_domain` either, and inventing it for one knob alone would be
inconsistent. Note `tests/test_og_install.py:174-177` and `:1884` assert the
simple-knob set, so they need updating.

Rename-adjacent decision: `ngrok_domain` is now provider-specific. Keep the key
and the `OG_NGROK_DOMAIN` env var for back-compat, and read it only when the
provider is ngrok; add `OG_TUNNL_SSH_KEY` and `OG_TUNNL_NAME` (Pro) for tunnl.
Do **not** merge them into one `tunnel_domain` — they mean different things
(a reserved domain vs. a key-derived subdomain vs. a Pro reserved name), and
collapsing them would make a wrong value look valid.

**The `--use` flag.** `bin/og` has **no flag parser at all** — every subcommand
is positional `case` matching (`:1450-1470`), and `cmd_start` inspects only
`$1` (`:659-667`). A value-consuming flag cannot be bolted on as one more case
arm. So add a real `while` parse loop to `cmd_start`:

```
og start [local|tunneled] [domain] [--use ngrok|tunnl|auto]
og restart tunneled --use tunnl
```

`cmd_restart` forwards `"$@"` to `cmd_start` already (`:1273-1277`), so
`restart` inherits the flag for free. Doing this once properly also unblocks
`--port` and `--domain`, which today can only be set by env var. Precedence:
`--use` > `OG_TUNNEL_PROVIDER` in `og.env` > `ngrok`.

Preserve the bare-argument back-compat at `:665-666` (`og start <domain>` means
tunneled), but make its advisory message name the provider, since a bare
argument means "reserved ngrok domain" and "tunnl Pro name" depending on
`--use` — and under plain tunnl it is not supported at all, which should be a
clear refusal rather than a silently ignored argument.

### Phase 3 — what the seam makes possible

These are the reasons to build the abstraction rather than just a second
branch. Each is small once Phase 1 exists.

- **`--use auto` / automatic fallback.** The user's actual problem is not
  "I want tunnl.gg", it is "ngrok sometimes cannot serve me". Run ngrok's
  preflight; on authtoken-missing, bandwidth-exceeded, or an existing agent
  holding 4040, say so and fall through to tunnl. This is the feature that
  retires the original complaint. Keep it opt-in (`--use auto`, or
  `OG_TUNNEL_FALLBACK=1`) — an implicit provider switch would change the public
  URL without the user asking.
- **Preflight in `og setup`.** `cmd_setup` already exists (`:1455`). Report per
  provider: ngrok installed? authtoken set? agent already running? `ssh`
  present, stable key present, reachable? Turns a launch-time failure into a
  setup-time answer.
- **`og status` reporting the provider and the expiry clock.** With tunnl's 24 h
  cap, "tunnel up" is not enough; show the provider and time remaining.
- **A reconnect supervisor.** Given `stable@`, a small loop that re-execs `ssh`
  on exit makes the 24 h cap invisible. Phase 1's URL cache file is how it
  publishes a changed URL, and the invariant to assert is that the URL did
  **not** change — if it did, the running server's baked env is wrong and the
  honest move is to warn and tell the user to `og restart`. Deliberately *not*
  Phase 1: shipping the cap as a documented limitation is more honest than
  shipping a supervisor that papers over a URL change the server cannot absorb.
- **`og url --qr`.** `print_qr` is already there (`:80-85`, `:1429`); tunnl's
  own QR is redundant output we discard.

### Recommended sequencing

Phase 0 → Phase 1 → Phase 2 → `--use auto` and `og setup` preflight → supervisor.
Phases 1 and 2 are each a single focused change set; they are independent of
each other only in review, not in order.

## Decisions

Settled by the owner 2026-10-06:

1. **Default provider stays `ngrok`.** tunnl.gg is the alternative, not the
   replacement. The ~500 ms latency makes this the right call.
2. **Precedence: `--use` overrides the setup-configured default.** `og setup`
   records a provider; a bare `og start tunneled` uses it; `og start tunneled
   --use tunnl.gg` overrides for that invocation only. Nothing is written back
   to config by an override.
3. **The 24 h / 2 h caps ship as a documented limitation.** No supervisor in the
   first cut; `og status` surfaces the expiry so the limit is visible, not
   surprising.
4. **A dedicated SSH key**, generated by `og setup` at
   `~/.omnigent/tunnl_ed25519` (mode 600), used with `-i … -o
   IdentitiesOnly=yes`. The stable URL then cannot drift when the user's
   `ssh-agent` changes.

### Accepted provider tokens

Canonical ids are `ngrok` and `tunnl`. `--use` also accepts `tunnl.gg` as an
alias for `tunnl`, since that is how the service is named — normalise on input
rather than making the user remember which spelling the flag wants. An
unrecognised value is a hard error listing the valid ones, never a silent
fallback to the default.

### Still open: automatic fallback

The owner's answer to (2) above settles *override precedence*. It does **not**
settle whether `og` should ever switch providers **by itself** when ngrok cannot
serve — the thing that actually retires the original complaint, since otherwise
an exhausted ngrok quota still means noticing the failure and retyping the
command with `--use tunnl.gg`.

Treating these as one feature would be a mistake: precedence is deterministic
and belongs in Phase 2; auto-fallback changes the public URL without the user
asking and belongs in Phase 3 behind an explicit opt-in
(`OG_TUNNEL_FALLBACK=1`), default off. Phase 1 and 2 are unaffected either way,
so this is not a blocker — but it needs an answer before Phase 3.
