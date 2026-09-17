#!/usr/bin/env python3
"""og-install — wire an Omnigent multi-agent coding setup onto this machine.

Rerunnable by design. Every choice lands in ~/.omnigent/og-install.json, which
is the source of truth; the YAML bundles under ~/.omnigent/agents/ are
generated artifacts. Re-running reads that state, shows it back, and lets you
change one thing without retyping the rest.

Three ways in:
  og-install                     interactive picker (default)
  og-install --show              print current config, change nothing
  og-install --plan FILE.json    apply a plan non-interactively (for an AI)
  og-install --questions         emit the decision schema as JSON (for an AI)

See AGENTS.md for the AI-driven flow.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - install.sh resolves this first
    sys.exit("og_install needs PyYAML. Run ./install.sh, which finds an interpreter that has it.")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OMNI = Path(os.environ.get("OMNIGENT_HOME", Path.home() / ".omnigent"))
STATE = OMNI / "og-install.json"
REGISTRY = json.loads((HERE / "registry.json").read_text())

# The orchestrator prompt is passed to the harness CLI inline via
# --append-system-prompt, and Omnigent shell-quotes the whole argv into ONE
# tmux command string. tmux rejects a command string past ~16,320 bytes with
# "command too long", which surfaces as "Native <X> terminal failed to start".
# The non-prompt args cost roughly 3 KB, so this is the usable prompt budget.
# Measured, not guessed — see docs/TROUBLESHOOTING.md.
PROMPT_CEILING = 13300

C = {"dim": "\033[2m", "b": "\033[1m", "g": "\033[0;32m", "y": "\033[0;33m",
     "r": "\033[0;31m", "c": "\033[0;36m", "x": "\033[0m"}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = dict.fromkeys(C, "")


def say(msg: str = "") -> None:
    print(msg)


def ok(msg: str) -> None:
    print(f"{C['g']}✓{C['x']} {msg}")


def warn(msg: str) -> None:
    print(f"{C['y']}!{C['x']} {msg}")


def err(msg: str) -> None:
    print(f"{C['r']}✗{C['x']} {msg}", file=sys.stderr)


def die(msg: str) -> "None":
    err(msg)
    raise SystemExit(1)


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def agents_by_id() -> dict:
    return {a["id"]: a for a in REGISTRY["agents"]}


def scan() -> dict:
    """Which registry agents have their CLI on PATH."""
    found = {}
    for a in REGISTRY["agents"]:
        path = shutil.which(a["binary"])
        if path:
            found[a["id"]] = path
    return found


def prereqs() -> list:
    """(name, found, why-it-matters, how-to-get-it)."""
    rows = [
        ("omnigent", shutil.which("omnigent"), "the runtime everything here configures",
         "uv tool install omnigent"),
        ("python3", shutil.which("python3"), "installer + og internals", "preinstalled on macOS"),
        ("tmux", shutil.which("tmux"), "native agent terminals run inside it", "brew install tmux"),
        ("git", shutil.which("git"), "worktrees for parallel workers", "xcode-select --install"),
        ("ngrok", shutil.which("ngrok"), "public URL so you can drive it from a phone",
         "brew install ngrok"),
        ("gh", shutil.which("gh"), "the orchestrator opens PRs with it", "brew install gh"),
        ("qrencode", shutil.which("qrencode"), "optional: QR for the tunnel URL",
         "brew install qrencode"),
    ]
    return [(n, bool(p), why, how) for n, p, why, how in rows]


def port_available(port: int) -> bool:
    """True if nothing is already listening on this port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def find_free_port(start: int, tries: int = 20) -> int | None:
    for p in range(start, start + tries):
        if port_available(p):
            return p
    return None


def list_models(agent: dict) -> list:
    """Ask the vendor CLI what it can run. Empty list on any failure."""
    cmd = (agent.get("model") or {}).get("list_cmd")
    if not cmd:
        return []
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    models = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Most vendor `--list-models` output is one bare id per line, but some
        # (e.g. kiro-cli) print a formatted table: a "*" marker for the active
        # model, then whitespace-padded columns (credits, description). Taking
        # the whole line as the id let a row like
        # "* auto      1.00x credits   Models chosen by task..." get stored
        # verbatim — and its leading "*" then broke YAML parsing (alias
        # syntax) once templated unquoted into config.yaml.
        line = line.lstrip("*").strip()
        model_id = re.split(r"\s{2,}", line)[0].strip()
        if model_id:
            models.append(model_id)
    return models


def free_models(agent: dict) -> list:
    pat = (agent.get("model") or {}).get("free_pattern")
    models = list_models(agent)
    if not pat:
        return models
    rx = re.compile(pat)
    return [m for m in models if rx.search(m)]


# --------------------------------------------------------------------------
# interactive helpers
# --------------------------------------------------------------------------
def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" {C['dim']}[{default}]{C['x']}" if default else ""
    try:
        got = input(f"{C['c']}?{C['x']} {prompt}{suffix}: ").strip()
    except EOFError:
        raise SystemExit("\naborted")
    return got or (default or "")


def ask_yes(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    got = ask(f"{prompt} ({d})", "").lower()
    if not got:
        return default
    return got.startswith("y")


def pick_one(prompt: str, options: list, current: str | None = None) -> str:
    """options: list of (value, label, note)."""
    say()
    say(f"{C['b']}{prompt}{C['x']}")
    for i, (val, label, note) in enumerate(options, 1):
        mark = f" {C['g']}(current){C['x']}" if val == current else ""
        say(f"  {i}. {label}{mark}")
        if note:
            say(f"     {C['dim']}{note}{C['x']}")
    default_idx = next((str(i) for i, (v, _, _) in enumerate(options, 1) if v == current), "1")
    while True:
        got = ask("choose", default_idx)
        if got.isdigit() and 1 <= int(got) <= len(options):
            return options[int(got) - 1][0]
        err("pick a number from the list")


def pick_many_ordered(prompt: str, options: list, current: list | None = None) -> list:
    """Select several, in priority order. Returns values in the order given."""
    say()
    say(f"{C['b']}{prompt}{C['x']}")
    say(f"{C['dim']}  Enter numbers in PREFERENCE ORDER, comma separated (e.g. 2,1,3).{C['x']}")
    say(f"{C['dim']}  The first is tried first; later ones absorb overflow.{C['x']}")
    for i, (val, label, note) in enumerate(options, 1):
        say(f"  {i}. {label}")
        if note:
            say(f"     {C['dim']}{note}{C['x']}")
    cur = current or []
    default = ",".join(str(i) for i, (v, _, _) in enumerate(options, 1) if v in cur) or "1"
    # Preserve the saved order rather than the menu order.
    if cur:
        idx = {v: i for i, (v, _, _) in enumerate(options, 1)}
        default = ",".join(str(idx[v]) for v in cur if v in idx) or default
    while True:
        got = ask("choose (ordered)", default)
        parts = [p.strip() for p in got.split(",") if p.strip()]
        if parts and all(p.isdigit() and 1 <= int(p) <= len(options) for p in parts):
            seen, out = set(), []
            for p in parts:
                v = options[int(p) - 1][0]
                if v not in seen:
                    seen.add(v)
                    out.append(v)
            return out
        err("enter one or more numbers from the list, comma separated")


def pick_model(agent: dict, current: str | None) -> str | None:
    """Resolve the model pin for one agent."""
    spec = agent.get("model") or {}
    required = spec.get("required", False)
    if not required and not spec.get("list_cmd"):
        note = spec.get("note")
        if note:
            say(f"  {C['dim']}{agent['label']}: {note}{C['x']}")
        return current

    say()
    say(f"{C['b']}Model for {agent['label']}{C['x']}")
    if spec.get("note"):
        say(f"  {C['dim']}{spec['note']}{C['x']}")

    options = free_models(agent) or list_models(agent)
    if options:
        shown = options[:25]
        for i, m in enumerate(shown, 1):
            mark = f" {C['g']}(current){C['x']}" if m == current else ""
            say(f"  {i}. {m}{mark}")
        if len(options) > len(shown):
            say(f"  {C['dim']}… {len(options)-len(shown)} more; type a full id instead{C['x']}")
        default = current or spec.get("prefer") or shown[0]
        got = ask("model id (number or full id)", default)
        if got.isdigit() and 1 <= int(got) <= len(shown):
            return shown[int(got) - 1]
        return got

    if spec.get("list_cmd"):
        warn(f"could not list models ({' '.join(spec['list_cmd'])} failed) — type one manually")
    default = current or spec.get("prefer") or ""
    got = ask("model id" + (" (REQUIRED)" if required else " (blank = harness default)"), default)
    if required and not got:
        die(f"{agent['label']} requires a pinned model; an unpinned worker inherits the "
            "orchestrator's model id and the dispatch fails.")
    return got or None


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except ValueError:
            warn(f"{STATE} is not valid JSON; starting fresh")
    return {}


def build_plan_interactive(state: dict) -> dict:
    reg = agents_by_id()
    found = scan()

    say()
    say(f"{C['b']}Detected coding CLIs{C['x']}")
    if not found:
        die("no supported coding CLI found on PATH. Install at least one "
            "(see README.md) and re-run.")
    for aid, path in sorted(found.items()):
        flag = f" {C['y']}(unverified in this project){C['x']}" if reg[aid].get("unverified") else ""
        say(f"  {C['g']}●{C['x']} {reg[aid]['label']:22} {C['dim']}{path}{C['x']}{flag}")
    missing = [a for a in REGISTRY["agents"] if a["id"] not in found]
    if missing:
        say(f"  {C['dim']}not found: {', '.join(a['label'] for a in missing)}{C['x']}")

    def opts(role):
        return [(a["id"], reg[a["id"]]["label"],
                 reg[a["id"]].get("reviewer_warning") if role == "reviewer" else None)
                for a in REGISTRY["agents"]
                if a["id"] in found and role in a["roles"]]

    # --- orchestrator ---
    orch_opts = opts("orchestrator")
    if not orch_opts:
        die("none of the detected CLIs can act as an orchestrator "
            "(needs Omnigent's sys_* tool relay).")
    orchestrator = pick_one(
        "Which agent runs the ORCHESTRATOR? (plans, delegates, never writes product code)",
        orch_opts, state.get("orchestrator"))

    agent_name = ask("Name for the orchestrator bundle", state.get("agent_name", "dev-lead"))

    # --- coders ---
    coder_opts = opts("coder")
    if not coder_opts:
        die("no detected CLI can act as a coder.")
    coder_ids = pick_many_ordered(
        "Which agents IMPLEMENT code? (preference order — first is tried first)",
        coder_opts, [c["id"] for c in state.get("coders", [])])

    prev_models = {c["id"]: c.get("model") for c in state.get("coders", [])}
    coders = []
    for i, cid in enumerate(coder_ids, 1):
        model = pick_model(reg[cid], prev_models.get(cid))
        coders.append({"id": cid, "priority": i, "model": model})

    # --- reviewer ---
    rev_opts = opts("reviewer")
    if not rev_opts:
        die("no detected CLI can act as a reviewer.")
    reviewer_id = pick_one(
        "Which agent REVIEWS the batched diff? (reads only, never edits)",
        rev_opts, (state.get("reviewer") or {}).get("id"))
    reviewer = {"id": reviewer_id,
                "model": pick_model(reg[reviewer_id], (state.get("reviewer") or {}).get("model"))}

    # --- multi-account, for any selected agent that supports it ---
    accounts = dict(state.get("accounts") or {})
    involved = {orchestrator, reviewer_id} | {c["id"] for c in coders}
    for aid in sorted(involved):
        ma = reg[aid].get("multi_account") or {}
        if not ma.get("supported"):
            continue
        say()
        say(f"{C['b']}{reg[aid]['label']} — separate account for the reviewer?{C['x']}")
        say(f"  {C['dim']}{ma['note']}{C['x']}")
        cur = accounts.get(aid)
        if ask_yes(f"Use a second {reg[aid]['label']} account?", default=bool(cur)):
            accounts[aid] = ask(f"{ma['env']} path",
                                cur or os.path.expandvars(ma["default_dir"]))
        else:
            accounts.pop(aid, None)

    # --- runtime knobs ---
    say()
    say(f"{C['b']}Runtime{C['x']}")
    default_port = int(state.get("port", 6767))
    if not port_available(default_port):
        suggestion = find_free_port(default_port + 1)
        if suggestion:
            warn(f"port {default_port} is already in use — suggesting {suggestion} instead")
            default_port = suggestion
        else:
            warn(f"port {default_port} is already in use and no free port was found nearby")
    while True:
        port = ask("Omnigent server port", str(default_port))
        if not port.isdigit():
            err("enter a port number")
            continue
        if port_available(int(port)):
            break
        if ask_yes(f"port {port} looks like it's already in use — use it anyway?", default=False):
            break
    domain = ask("Reserved ngrok domain (blank = ephemeral URL each start)",
                 state.get("ngrok_domain", ""))
    max_dispatch = ask("Max worker dispatches per orchestrator turn",
                       str(state.get("max_dispatches", 4)))
    bin_dir = ask("Install the `og` command where?",
                  state.get("bin_dir") or str(default_bin_dir()))
    say()
    say(f"{C['dim']}  local    = LAN only; the QR points at this machine's network IP{C['x']}")
    say(f"{C['dim']}  tunneled = ngrok public URL, reachable from anywhere{C['x']}")
    default_mode = pick_one(
        "What should a bare `og start` do?",
        [("local", "local — LAN only (recommended)", None),
         ("tunneled", "tunneled — start ngrok and expose a public URL", None)],
        state.get("default_mode", "local"))
    say()
    say(f"{C['dim']}  `og` is copied out of this checkout; a `git pull` here changes nothing{C['x']}")
    say(f"{C['dim']}  until the install is re-applied. Auto-update does that on every `og start`;{C['x']}")
    say(f"{C['dim']}  off, `og start` only warns and you run `og update` yourself.{C['x']}")
    auto_update = ask_yes("Auto-update og on `og start`?", state.get("auto_update", True))

    return {
        "version": 1,
        "agent_name": agent_name,
        "orchestrator": orchestrator,
        "coders": coders,
        "reviewer": reviewer,
        "accounts": accounts,
        "port": int(port),
        "ngrok_domain": domain,
        "max_dispatches": int(max_dispatch),
        "bin_dir": bin_dir,
        "default_mode": default_mode,
        "auto_update": auto_update,
    }


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------
def validate(plan: dict, rendered_prompt: str | None = None) -> list:
    """Return a list of (level, message). level in {'error','warn'}."""
    reg = agents_by_id()
    issues = []

    for c in plan["coders"]:
        spec = reg[c["id"]].get("model") or {}
        if spec.get("required") and not c.get("model"):
            issues.append(("error",
                           f"{reg[c['id']]['label']} requires a pinned model but none is set. "
                           "An unpinned worker inherits the orchestrator's model id and the "
                           "dispatch dies (loudly on OpenCode, SILENTLY on ACP agents)."))
        if reg[c["id"]].get("silent_model_failure") and c.get("model"):
            issues.append(("warn",
                           f"{reg[c['id']]['label']} accepts a model it cannot serve without "
                           "erroring. Verify the first dispatch produced a real commit — an "
                           "empty transcript means the pin is wrong, not that it refused."))

    rev_vendor = reg[plan["reviewer"]["id"]]["vendor"]
    same = [reg[c["id"]]["label"] for c in plan["coders"]
            if reg[c["id"]]["vendor"] == rev_vendor]
    if same:
        issues.append(("warn",
                       f"reviewer ({reg[plan['reviewer']['id']]['label']}) shares a vendor with "
                       f"{', '.join(same)}. Same-vendor review shares blind spots — the "
                       "orchestrator will mark those PRs `degraded-review`."))

    if reg[plan["orchestrator"]].get("relay") is False:
        issues.append(("error",
                       f"{reg[plan['orchestrator']]['label']} runs without Omnigent's sys_* tool "
                       "relay, so it cannot dispatch sub-agents. Pick a different orchestrator."))

    # A harness that never receives the spec prompt cannot orchestrate: the
    # whole orchestration contract lives in that prompt.
    od = reg[plan["orchestrator"]].get("prompt_delivery")
    if od == "none":
        issues.append(("error",
                       f"{reg[plan['orchestrator']]['label']} never receives a spec prompt "
                       "(Omnigent reports instruction delivery NOT_DELIVERED), so the "
                       "orchestration contract would be silently discarded."))

    # Workers on such a harness only ever see the dispatch text.
    mute = [reg[c["id"]]["label"] for c in plan["coders"]
            if reg[c["id"]].get("prompt_delivery") == "none"]
    if mute:
        issues.append(("warn",
                       f"{', '.join(mute)} never receive their sub-agent prompt — that harness "
                       "does not deliver spec instructions. Their operating rules must be "
                       "inlined into args.input on every dispatch; the generated `roster` "
                       "skill tells the orchestrator to do exactly that."))

    # The tmux command-string ceiling only binds when the prompt rides on argv.
    if rendered_prompt is not None and od == "argv":
        quoted = len(shlex.quote(rendered_prompt))
        if quoted > PROMPT_CEILING:
            issues.append(("error",
                           f"orchestrator prompt is {quoted} bytes shell-quoted, over the "
                           f"{PROMPT_CEILING} ceiling for an argv-delivered harness "
                           f"({reg[plan['orchestrator']]['label']}). tmux refuses the launch "
                           "with 'command too long'. Options: drop a coder, move guidance into "
                           "a skill file, or pick an orchestrator whose harness composes the "
                           "prompt per turn (OpenCode) and has no ceiling."))
        elif quoted > PROMPT_CEILING - 800:
            issues.append(("warn",
                           f"orchestrator prompt is {quoted} bytes shell-quoted — within "
                           f"{PROMPT_CEILING - quoted} of the tmux ceiling. Put new guidance in "
                           "skills/, not the prompt."))
    return issues


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def tmpl(name: str) -> str:
    return (HERE / "templates" / name).read_text()


def worker_name(aid: str) -> str:
    """The sub-agent's name in the bundle.

    Defaults to ``coder_<id>``, but a registry row may pin `worker` explicitly
    so an established name survives a change of agent id — renaming a worker
    orphans its session history and every doc that refers to it.
    """
    return (agents_by_id().get(aid) or {}).get("worker") or f"coder_{aid}"


def render_roster(plan: dict) -> str:
    """One compact line per worker.

    Deliberately terse: this text is inlined into the harness command line,
    which tmux caps at ~16 KB (see PROMPT_CEILING). Detail that would push the
    prompt toward that ceiling goes into the generated `roster` skill instead,
    which loads from disk and costs the command line nothing. Only the
    safety-critical facts stay inline — a caveat the orchestrator must know
    BEFORE it decides to re-send a task cannot live behind an on-demand read.
    """
    reg = agents_by_id()
    ordinals = ["FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH"]
    names = [f"`{worker_name(c['id'])}`" for c in plan["coders"]] + ["`reviewer`"]
    width = max(len(n) for n in names) + 1
    lines = []
    for i, c in enumerate(plan["coders"]):
        a = reg[c["id"]]
        pin = f", pinned `{c['model']}`" if c.get("model") else ""
        tags = []
        if a.get("relay") is False:
            tags.append("leaf worker, cannot dispatch")
        if a.get("silent_model_failure"):
            tags.append("FAILS SILENTLY on a bad model — empty transcript means "
                        "misconfig, not refusal; do not re-send")
        if a["id"] == "opencode":
            tags.append("day-capped; when dry move down, do not retry")
        tag = f" {'; '.join(tags)}." if tags else ""
        lines.append(f"  - {names[i].ljust(width)}{ordinals[min(i, 5)]}: {a['label']} "
                     f"(`{a['harness']}`){pin}.{tag}")
    rv = reg[plan["reviewer"]["id"]]
    lines.append(f"  - {names[-1].ljust(width)}{rv['label']} (`{rv['harness']}`). "
                 "Reviews only; never edits.")
    lines.append("")
    lines.append("  Per-worker detail (quotas, failure shapes, auth) is in the `roster` skill.")
    return "\n".join(lines)


def render_roster_skill(plan: dict) -> str:
    """The long-form roster notes, as a skill file rather than prompt bytes."""
    reg = agents_by_id()
    out = [
        "---", "name: roster",
        "description: What each worker in this orchestrator's roster actually is — "
        "vendor, model pin, quota shape, and how it fails. Read before dispatching "
        "to a worker you have not used this run, and whenever a worker returns "
        "something you did not expect.",
        "---", "",
        "# Roster", "",
        "Workers are listed in preference order. Take the earliest one with capacity;",
        "go down only when the one above is unavailable, out of quota, or has already",
        "failed this run. **Every worker pins its own model in its spec — never pass",
        "`args.model`.** A pin lives at `executor.model`; a model placed under",
        "`executor.config` is silently ignored by the spec parser.", "",
    ]
    for i, c in enumerate(plan["coders"], 1):
        a = reg[c["id"]]
        out += [f"## {i}. `{worker_name(c['id'])}` — {a['label']}", "",
                f"- harness `{a['harness']}`, vendor `{a['vendor']}`",
                f"- model: {'pinned `' + c['model'] + '`' if c.get('model') else 'chosen by the harness'}"]
        if a.get("relay") is False:
            out.append("- **Leaf worker.** Runs without Omnigent's `sys_*` tool relay, so it "
                       "cannot orchestrate or dispatch. Implementation and exploration only.")
        if a.get("silent_model_failure"):
            out.append("- **Fails silently on a bad model.** It accepts a model switch it cannot "
                       "serve instead of rejecting it, so a wrong pin returns `completed` with a "
                       "transcript containing only your prompt and an untouched worktree. That is "
                       "a misconfiguration, not a refusal — report it and move down the roster "
                       "rather than re-sending the same task.")
        note = (a.get("model") or {}).get("note")
        if note:
            out.append(f"- {note}")
        if a.get("prompt_delivery") == "none":
            out.append("- **Does not receive its sub-agent prompt.** This harness never "
                       "delivers spec instructions, so the worker sees ONLY the text you send "
                       "in `args.input`. Every operating rule it must follow — scope limits, "
                       "which gates to run, commit locally and never push — has to be written "
                       "into the dispatch itself. Do not assume it knows the standard contract.")
        if a.get("unverified"):
            out.append("- Not yet exercised in this project — verify its first dispatch produced "
                       "a real commit before trusting a completion report.")
        out.append("")
    rv = reg[plan["reviewer"]["id"]]
    out += [f"## `reviewer` — {rv['label']}", "",
            f"- harness `{rv['harness']}`, vendor `{rv['vendor']}`",
            "- Reviews only; never edits, never gets a worktree.",
            "- Cross-vendor review is the point: never route a diff to a reviewer whose",
            "  vendor matches the implementer's. If that is unavoidable, say so and label",
            "  the PR `degraded-review`.", ""]
    return "\n".join(out)


def _count_word(n: int) -> str:
    words = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}
    return words.get(n, str(n))


def _wrap(text: str, width: int) -> list:
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def render_preflight_map(plan: dict) -> str:
    reg = agents_by_id()
    rows = [f"    `{worker_name(c['id'])}` -> `{reg[c['id']]['harness']}`" for c in plan["coders"]]
    rows.append(f"    `reviewer` -> `{reg[plan['reviewer']['id']]['harness']}`")
    return "\n".join(rows)


OPENCODE_PREFLIGHT = """  ### Zen model preflight (once per run, only if dispatching {name})
  A CHECK, not a choice. `args.model` overrides the spec pin, replacing a
  verified free model with a per-run guess — that is how a paid model gets
  picked and hits OpenCode's "No payment method" wall. Call `sys_list_models`
  once and confirm the pinned id is still listed:
  - Listed, or query failed -> dispatch with no `args.model`. Say nothing.
  - Gone (Zen rotates its lineup) -> pick the strongest replacement ending in
    `-free`, pass it as `args.model` this run only, and tell the human the spec
    pin needs updating. A non-`-free` id is a failed dispatch, not a slower one.

"""


def render_orchestrator(plan: dict) -> str:
    reg = agents_by_id()
    s = tmpl("orchestrator.yaml.tmpl")
    oc = next((c for c in plan["coders"] if c["id"] == "opencode"), None)
    agent_list = "\n".join(f"    - {worker_name(c['id'])}" for c in plan["coders"])
    agent_list += "\n    - reviewer"
    subs = {
        "{{AGENT_NAME}}": plan["agent_name"],
        "{{ORCHESTRATOR_HARNESS}}": reg[plan["orchestrator"]]["harness"],
        "{{ROSTER_BULLETS}}": render_roster(plan),
        "{{PREFLIGHT_MAP}}": render_preflight_map(plan),
        "{{OPENCODE_PREFLIGHT}}": OPENCODE_PREFLIGHT.format(name=worker_name("opencode")) if oc else "",
        "{{AGENT_LIST}}": agent_list,
        "{{MAX_DISPATCHES}}": str(plan["max_dispatches"]),
        "{{AGENT_COUNT_WORD}}": _count_word(len(plan["coders"]) + 1),
    }
    for k, v in subs.items():
        s = s.replace(k, v)
    left = re.findall(r"\{\{[A-Z_]+\}\}", s)
    if left:
        die(f"orchestrator template still has placeholders: {sorted(set(left))}")
    return s


def model_block(model: str | None) -> str:
    """Render the pin at executor.model — NOT executor.config.model.

    The parser populates spec.executor.model from the `executor.model` key
    only. executor.config is a free-form dict, so a model placed there is
    accepted without complaint and silently ignored.
    """
    if not model:
        return ""
    return (
        "  # Pinned at executor.model, NOT executor.config.model: the parser reads\n"
        "  # `executor.model` only, and a model under `config` is silently ignored.\n"
        # Quoted via json.dumps (a valid YAML flow scalar) rather than
        # interpolated bare: an unquoted value starting with a YAML indicator
        # character (*, &, !, #, ...) or containing a colon breaks the parser,
        # and a vendor CLI's model id/free-text is not guaranteed to avoid those.
        f"  model: {json.dumps(model)}\n"
    )


def render_coder(plan: dict, c: dict) -> str:
    reg = agents_by_id()
    a = reg[c["id"]]
    notes = []
    if a["kind"] == "acp-user":
        notes.append(f"# Reaches Omnigent over ACP: `{a['acp_command']}` speaks the Agent Client")
        notes.append(f"# Protocol on stdio, registered in ~/.omnigent/config.yaml under")
        notes.append(f"# `acp.agents` as \"{a['label']}\", which Omnigent slugs to `{a['harness']}`.")
    if a.get("relay") is False:
        notes.append("# No sys_* tool relay: leaf worker only, cannot orchestrate or dispatch.")
    if a.get("silent_model_failure"):
        notes.append("# Accepts a model id it cannot serve instead of rejecting it, so a wrong")
        notes.append("# pin yields an empty transcript and no error. Verify the first dispatch.")
    permission_mode_block = ""
    if a["kind"] == "acp-user":
        notes.append("# permission_mode: bypassPermissions -- an acp-user CLI relays every tool call")
        notes.append("# to Omnigent as session/request_permission; Omnigent's own default")
        notes.append("# (HARNESS_ACP_PERMISSION_MODE=auto) parks a human approval card for anything")
        notes.append("# no policy has an opinion on. bypassPermissions grants those instead, so a")
        notes.append("# headless worker isn't stuck waiting on someone to click approve. Policies")
        notes.append("# (below) still gate whatever they DO have an opinion on either way.")
        permission_mode_block = "    permission_mode: bypassPermissions"
    s = tmpl("coder.yaml.tmpl")
    for k, v in {
        "{{NAME}}": worker_name(c["id"]),
        "{{LABEL}}": a["label"],
        "{{PRIORITY}}": str(c["priority"]),
        "{{HARNESS}}": a["harness"],
        "{{ORCHESTRATOR}}": plan["agent_name"],
        "{{KIND_NOTE}}": "\n".join(notes) + ("\n" if notes else ""),
        "{{MODEL_BLOCK}}": model_block(c.get("model")),
        "{{PERMISSION_MODE_BLOCK}}": permission_mode_block,
        "{{BLAST_RADIUS_HANDLER}}": "omnigent_local_policies.blast_radius_with_branch_cleanup",
    }.items():
        s = s.replace(k, v)
    return s


def render_reviewer(plan: dict) -> str:
    reg = agents_by_id()
    a = reg[plan["reviewer"]["id"]]
    acct = plan.get("accounts", {}).get(a["id"])
    note = ""
    if acct:
        env = (a.get("multi_account") or {}).get("env", "CONFIG_DIR")
        note = (f"# Runs on a SEPARATE account: the server is launched with\n"
                f"# {env}={acct}, so this reviewer is independent of the\n"
                f"# account your interactive sessions use.\n")
    s = tmpl("reviewer.yaml.tmpl")
    for k, v in {
        "{{LABEL}}": a["label"],
        "{{HARNESS}}": a["harness"],
        "{{ORCHESTRATOR}}": plan["agent_name"],
        "{{ACCOUNT_NOTE}}": note,
        "{{MODEL_BLOCK}}": model_block(plan["reviewer"].get("model")),
    }.items():
        s = s.replace(k, v)
    return s


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------
def patch_global_config(plan: dict) -> list:
    """Merge og's required keys into ~/.omnigent/config.yaml, preserving the rest."""
    reg = agents_by_id()
    cfg_path = OMNI / "config.yaml"
    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    changed = []

    # policy_modules: without this, omnigent_local_policies handlers are NOT in
    # the registry. Being importable via the .pth is not the same as registered.
    mods = list(cfg.get("policy_modules") or [])
    if "omnigent_local_policies" not in mods:
        mods.append("omnigent_local_policies")
        cfg["policy_modules"] = mods
        changed.append("policy_modules += omnigent_local_policies")

    # acp.agents entries for every acp-user agent we selected -- the reviewer
    # included. A missing row does not fail loudly: Omnigent resolves an
    # unknown acp:<slug> to the FIRST configured row, so an ACP reviewer with
    # no row of its own would silently run as whichever coder is listed first.
    want = [c for c in plan["coders"] if reg[c["id"]]["kind"] == "acp-user"]
    if reg[plan["reviewer"]["id"]]["kind"] == "acp-user":
        want.append(plan["reviewer"])
    if want:
        acp = cfg.get("acp") or {}
        rows = list(acp.get("agents") or [])
        by_name = {r.get("name"): r for r in rows if isinstance(r, dict)}
        for c in want:
            a = reg[c["id"]]
            row = {"command": a["acp_command"], "name": a["label"],
                   "omnigent_mcp": a.get("omnigent_mcp", False)}
            if c.get("model"):
                row["model"] = c["model"]
            if by_name.get(a["label"]) != row:
                rows = [r for r in rows if r.get("name") != a["label"]] + [row]
                changed.append(f"acp.agents[{a['label']}] = {a['acp_command']}")
        acp["agents"] = rows
        cfg["acp"] = acp

    cfg["default_agent"] = str(OMNI / "agents" / plan["agent_name"])
    oc = next((c for c in plan["coders"] if c["id"] == "opencode"), None)
    if oc and oc.get("model"):
        cfg["opencode_model"] = oc["model"]

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=True, default_flow_style=False))
    return changed


def installed_version() -> str:
    """The version stamp for this apply: ``git describe --tags`` of the checkout.

    ``v0.2.0`` on a release, ``v0.2.0-3-gabc123`` past one, ``-dirty`` with
    local edits, a bare sha on a checkout with no tags, ``unknown`` when not
    a git checkout at all. `og start` compares the leading ``vX.Y.Z`` against
    the newest release tag on origin, so this is what "current version" means.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if out.returncode != 0:
        return "unknown"
    return out.stdout.strip() or "unknown"


def write_og_env(plan: dict) -> None:
    reg = agents_by_id()
    lines = [
        "# Generated by og install.sh. Sourced by `og` at startup.",
        "",
        f"OG_AGENT={plan['agent_name']}",
        f"OG_PORT={plan['port']}",
        "",
        "# Where this og checkout lives. `og init` needs it to find the",
        "# scaffolder, since `og` itself is copied out of the repo.",
        f"OG_REPO={REPO}",
        "",
        "# What was installed, from `git describe --tags` at apply time. `og start`",
        "# compares this against the latest release tag on origin.",
        f"OG_VERSION={installed_version()}",
        "",
        "# Default for a bare `og start`: local (LAN only) or tunneled (ngrok).",
        f"OG_DEFAULT_MODE={plan.get('default_mode', 'local')}",
        "",
        "# 1: when a newer release exists, `og start` pulls the checkout, re-applies",
        "# this install and re-runs itself on the new script. 0: it only prints the",
        "# notice (`og update` applies it). OG_SKIP_UPDATE=1 bypasses the check once.",
        f"OG_AUTO_UPDATE={1 if plan.get('auto_update', True) else 0}",
    ]
    if plan.get("ngrok_domain"):
        lines.append("")
        lines.append("# Used by `og start tunneled` only.")
        lines.append(f"OG_NGROK_DOMAIN={plan['ngrok_domain']}")
    else:
        lines += ["# OG_NGROK_DOMAIN=your-name.ngrok.app   # a reserved domain keeps",
                  "#   invite links and session cookies working across restarts."]
    rev = plan["reviewer"]["id"]
    acct = plan.get("accounts", {}).get(rev)
    if acct:
        env = (reg[rev].get("multi_account") or {}).get("env")
        if env == "CLAUDE_CONFIG_DIR":
            lines += ["", "# The reviewer runs on this account, separate from your interactive one.",
                      f"OG_CLAUDE_CONFIG_DIR={acct}"]
    (OMNI / "og.env").write_text("\n".join(lines) + "\n")


def install_og_script(dest: Path) -> None:
    """Copy bin/og to *dest* by writing a sibling and renaming over it.

    `og update` runs this installer FROM the installed og, and bash reads a
    script incrementally as it executes. Copying over the same inode
    (shutil.copy2 truncates and rewrites in place) hands the still-running
    shell a file whose bytes moved under it -- "syntax error near unexpected
    token `esac'" on the next line it reads. A rename swaps the directory
    entry and leaves the old inode intact for the process still reading it.
    """
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copy2(REPO / "bin" / "og", tmp)
    tmp.chmod(0o755)
    os.replace(tmp, dest)


def apply(plan: dict, dry_run: bool = False) -> None:
    reg = agents_by_id()
    bundle = OMNI / "agents" / plan["agent_name"]
    prompt = yaml.safe_load(render_orchestrator(plan)).get("prompt", "")

    issues = validate(plan, prompt)
    for level, msg in issues:
        (err if level == "error" else warn)(msg)
    if any(l == "error" for l, _ in issues):
        die("refusing to write a configuration that cannot work. Fix the above and re-run.")

    if dry_run:
        say()
        ok(f"dry run: {plan['agent_name']} would be written to {bundle}")
        say(json.dumps(plan, indent=2))
        return

    # 1. agent bundle
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "config.yaml").write_text(render_orchestrator(plan))

    # Prune workers that are no longer in the roster. Without this, dropping or
    # renaming a coder leaves an orphaned directory: unreachable (it is not in
    # tools.agents) but indistinguishable on disk from a live worker, which is
    # exactly the kind of stale state that makes a rerunnable installer
    # untrustworthy.
    keep = {worker_name(c["id"]) for c in plan["coders"]} | {"reviewer"}
    for d in sorted((bundle / "agents").iterdir()):
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d)
            say(f"{C['dim']}  pruned stale worker: {d.name}{C['x']}")
    for c in plan["coders"]:
        d = bundle / "agents" / worker_name(c["id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(render_coder(plan, c))
    rd = bundle / "agents" / "reviewer"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "config.yaml").write_text(render_reviewer(plan))

    # 2. skills (verbatim from the repo)
    skills_src = REPO / "agents" / "dev-lead" / "skills"
    dst = bundle / "skills"
    if dst.exists():
        shutil.rmtree(dst)
    if skills_src.is_dir():
        shutil.copytree(skills_src, dst)
    # The roster skill is GENERATED from the chosen lineup, so the long-form
    # per-worker detail lives on disk instead of costing prompt bytes.
    (dst / "roster").mkdir(parents=True, exist_ok=True)
    (dst / "roster" / "SKILL.md").write_text(render_roster_skill(plan))

    # 3. policies + the .pth that puts them on sys.path
    pol_dir = OMNI / "policies"
    pol_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "policies" / "omnigent_local_policies.py", pol_dir)
    install_pth(pol_dir)

    # 4. global config + og.env + the og script
    changed = patch_global_config(plan)
    write_og_env(plan)
    bindir = resolve_bin_dir(plan)
    bindir.mkdir(parents=True, exist_ok=True)
    install_og_script(bindir / "og")
    if str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
        warn(f"{bindir} is not on your PATH — `og` will not be found. Add it:\n"
             f"    export PATH=\"{bindir}:$PATH\"")

    # 5. state
    STATE.write_text(json.dumps(plan, indent=2) + "\n")

    say()
    ok(f"orchestrator  {plan['agent_name']} ({reg[plan['orchestrator']]['label']})")
    for c in plan["coders"]:
        pin = f" → {c['model']}" if c.get("model") else ""
        ok(f"coder #{c['priority']}      {worker_name(c['id'])} ({reg[c['id']]['label']}){pin}")
    ok(f"reviewer      {reg[plan['reviewer']['id']]['label']}")
    for line in changed:
        ok(f"config.yaml   {line}")
    ok(f"og            {bindir/'og'}")
    ok(f"state         {STATE}")
    say()
    say(f"{C['dim']}prompt: {len(shlex.quote(prompt))}/{PROMPT_CEILING} bytes shell-quoted{C['x']}")

    # A coder is selectable here whether or not it's actually logged in —
    # nothing above checks. Left unauthenticated, the first sign a worker was
    # ever picked is a dispatch failing deep in a runner log ("You need to
    # sign in to use this model"), not anything surfaced during install. This
    # is a reminder, not a check: verifying login state is vendor-specific
    # (a credential file, a `whoami`, a token expiry...) and not worth
    # guessing at generically, so just list the command for each active
    # worker and let the user confirm it themselves.
    login_agents = [reg[plan["orchestrator"]]] + [reg[c["id"]] for c in plan["coders"]] \
        + [reg[plan["reviewer"]["id"]]]
    seen_ids = set()
    login_lines = []
    for a in login_agents:
        if a["id"] in seen_ids or not a.get("login"):
            continue
        seen_ids.add(a["id"])
        login_lines.append(f"  {a['login']:<20} {C['dim']}# {a['label']}{C['x']}")
    if login_lines:
        say()
        say(f"{C['b']}Before your first `og start`, make sure each is logged in{C['x']} "
            f"{C['dim']}(skip any already done):{C['x']}")
        for line in login_lines:
            say(line)

    say()
    say(f"Next: {C['b']}og start{C['x']}   (from the repo you want as the default workspace)")


def default_bin_dir() -> Path:
    """Where `og` goes when the user expresses no preference.

    Prefers a directory already on PATH so the install works without the user
    editing their shell profile; falls back to ~/.local/bin, which is
    conventional even when absent.
    """
    path = os.environ.get("PATH", "").split(os.pathsep)
    for candidate in (Path.home() / ".local" / "bin", Path.home() / "bin"):
        if str(candidate) in path:
            return candidate
    return Path.home() / ".local" / "bin"


def resolve_bin_dir(plan: dict) -> Path:
    """Precedence: plan value > OG_BIN_DIR env > default."""
    raw = plan.get("bin_dir") or os.environ.get("OG_BIN_DIR") or ""
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw))).resolve()
    return default_bin_dir()


def install_pth(pol_dir: Path) -> None:
    """Put the policies dir on the omnigent interpreter's sys.path."""
    try:
        import site
        targets = [Path(p) for p in site.getsitepackages()]
    except Exception:
        targets = []
    for t in targets:
        if t.is_dir() and os.access(t, os.W_OK):
            (t / "omnigent-local-policies.pth").write_text(str(pol_dir) + "\n")
            return
    warn(f"could not write a .pth into site-packages; add {pol_dir} to PYTHONPATH "
         "or the local policies will not import.")


# --------------------------------------------------------------------------
# AI-facing surfaces
# --------------------------------------------------------------------------
def emit_questions() -> None:
    reg = agents_by_id()
    found = scan()
    say(json.dumps({
        "detected": {k: reg[k]["label"] for k in found},
        "state_file": str(STATE),
        "current": load_state() or None,
        "prompt_ceiling_bytes": PROMPT_CEILING,
        "questions": [
            {"key": "agent_name", "type": "string", "default": "dev-lead",
             "ask": "What should the orchestrator bundle be called?"},
            {"key": "orchestrator", "type": "choice",
             "choices": [a["id"] for a in REGISTRY["agents"]
                         if a["id"] in found and "orchestrator" in a["roles"]],
             "ask": "Which agent plans and delegates (never writes product code)?"},
            {"key": "coders", "type": "ordered_multi",
             "choices": [a["id"] for a in REGISTRY["agents"]
                         if a["id"] in found and "coder" in a["roles"]],
             "ask": "Which agents implement code, in preference order (first is tried first)?",
             "per_item": {"model": "Model id to pin. REQUIRED for agents where "
                                   "registry.model.required is true."}},
            {"key": "reviewer", "type": "choice",
             "choices": [a["id"] for a in REGISTRY["agents"]
                         if a["id"] in found and "reviewer" in a["roles"]],
             "ask": "Which agent reviews the batched diff? Prefer a vendor that "
                    "differs from every coder."},
            {"key": "accounts", "type": "map",
             "ask": "For any agent with registry.multi_account.supported, should the "
                    "reviewer run on a second account? Value is the config dir path.",
             "applies_to": [a["id"] for a in REGISTRY["agents"]
                            if (a.get("multi_account") or {}).get("supported")]},
            {"key": "port", "type": "int", "default": 6767, "ask": "Omnigent server port?"},
            {"key": "ngrok_domain", "type": "string", "default": "",
             "ask": "Reserved ngrok domain? Blank means a new URL each start."},
            {"key": "max_dispatches", "type": "int", "default": 4,
             "ask": "Max worker dispatches per orchestrator turn?"},
            {"key": "default_mode", "type": "choice", "choices": ["local", "tunneled"],
             "default": "local",
             "ask": "Should a bare `og start` serve on the LAN only (local) or open "
                    "an ngrok tunnel (tunneled)?"},
            {"key": "bin_dir", "type": "path", "default": str(default_bin_dir()),
             "ask": "Which directory should the `og` command be installed into? "
                    "It must be on the user's PATH."},
            {"key": "auto_update", "type": "bool", "default": True,
             "ask": "Should `og start` auto-update (git pull the checkout and re-apply "
                    "the install) before starting? If not, it only warns when a newer "
                    "og exists and `og update` applies it."},
        ],
        "apply_with": "og-install --plan plan.json",
        "registry": REGISTRY["agents"],
    }, indent=2))


def show(state: dict) -> None:
    if not state:
        say("no og install found. Run: og-install")
        return
    reg = agents_by_id()
    found = scan()
    say(f"{C['b']}orchestrator{C['x']}  {state['agent_name']} "
        f"({reg[state['orchestrator']]['label']})")
    for c in state["coders"]:
        live = "" if c["id"] in found else f" {C['r']}(CLI missing){C['x']}"
        pin = f" → {c['model']}" if c.get("model") else ""
        say(f"{C['b']}coder #{c['priority']}{C['x']}      "
            f"{worker_name(c['id'])} ({reg[c['id']]['label']}){pin}{live}")
    say(f"{C['b']}reviewer{C['x']}      {reg[state['reviewer']['id']]['label']}")
    for aid, path in (state.get("accounts") or {}).items():
        say(f"{C['b']}account{C['x']}       {reg[aid]['label']} → {path}")
    say(f"{C['b']}port{C['x']}          {state['port']}")
    say(f"{C['b']}ngrok{C['x']}         {state.get('ngrok_domain') or '(ephemeral)'}")
    say(f"{C['b']}og command{C['x']}    {resolve_bin_dir(state) / 'og'}")
    say(f"{C['b']}og start{C['x']}      {state.get('default_mode', 'local')}")
    say(f"{C['b']}auto-update{C['x']}   {'on' if state.get('auto_update', True) else 'off (og start warns; og update applies)'}")
    say(f"{C['b']}version{C['x']}       {installed_version()} (checkout; og.env holds what was last applied)")
    issues = validate(state)
    for level, msg in issues:
        (err if level == "error" else warn)(msg)


def main() -> None:
    p = argparse.ArgumentParser(prog="install.sh", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--show", action="store_true", help="print current config and exit")
    p.add_argument("--questions", action="store_true",
                   help="emit the decision schema as JSON (for an AI installer)")
    p.add_argument("--plan", metavar="FILE", help="apply a plan JSON non-interactively")
    p.add_argument("--dry-run", action="store_true", help="validate and print, write nothing")
    p.add_argument("--check", action="store_true", help="report prerequisites and exit")
    args = p.parse_args()

    if args.questions:
        return emit_questions()

    if args.check:
        say(f"{C['b']}Prerequisites{C['x']}")
        missing_required = False
        for name, present, why, how in prereqs():
            optional = name == "qrencode"
            if present:
                ok(f"{name:10} {C['dim']}{why}{C['x']}")
            elif optional:
                warn(f"{name:10} {why} — {how}")
            else:
                err(f"{name:10} {why} — {how}")
                missing_required = True
        say()
        say(f"{C['b']}Coding CLIs{C['x']}")
        reg, found = agents_by_id(), scan()
        for aid, path in sorted(found.items()):
            ok(f"{reg[aid]['label']:22} {C['dim']}{path}{C['x']}")
        if not found:
            err("none found — install at least one (see README.md)")
        raise SystemExit(1 if missing_required or not found else 0)

    if args.show:
        return show(load_state())

    if args.plan:
        plan = json.loads(Path(args.plan).read_text())
        plan.setdefault("version", 1)
        plan.setdefault("auto_update", True)
        for i, c in enumerate(plan.get("coders", []), 1):
            c.setdefault("priority", i)
        return apply(plan, dry_run=args.dry_run)

    state = load_state()
    if state:
        say(f"{C['b']}Existing install{C['x']} — current configuration:")
        say()
        show(state)
        say()
        if not ask_yes("Reconfigure?", default=True):
            return
    plan = build_plan_interactive(state)
    apply(plan, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
