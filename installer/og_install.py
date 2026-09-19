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


def host_os() -> str:
    """'macos', 'wsl', 'linux', or 'windows' -- for install hints only.

    WSL is singled out because it is the supported way to run og on Windows
    and its hints differ (apt, but no keyring; LAN needs mirrored networking).
    Native Windows is reported so --check can say plainly that it is not
    supported rather than listing seven missing tools.
    """
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win") or sys.platform == "cygwin" or os.name == "nt":
        return "windows"
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return "wsl"
    try:
        if "microsoft" in Path("/proc/version").read_text().lower():
            return "wsl"
    except OSError:
        pass
    return "linux"


def prereqs() -> list:
    """(name, found, why-it-matters, how-to-get-it, required).

    Required means og or Omnigent cannot start without it. The rest are
    per-workflow: ngrok only for `og start tunneled`, gh only when the
    project's delivery ends in a GitHub PR (the orchestrator opens it; the
    merge gate reads the review marker from its body -- without gh the gate
    just denies protected-branch merges, which is the safe side).
    """
    mac = host_os() == "macos"

    def hint(brew: str, apt: str) -> str:
        return brew if mac else apt

    rows = [
        ("omnigent", "the runtime everything here configures",
         "uv tool install omnigent", True),
        ("python3", "installer + og internals",
         hint("preinstalled on macOS", "sudo apt install python3"), True),
        ("tmux", "native agent terminals run inside it",
         hint("brew install tmux", "sudo apt install tmux"), True),
        ("git", "worktrees for parallel workers",
         hint("xcode-select --install", "sudo apt install git"), True),
        ("ngrok", "optional: `og start tunneled` (drive a run from outside your network)",
         hint("brew install ngrok", "https://ngrok.com/download"), False),
        ("gh", "optional: GitHub-hosted projects only -- the orchestrator opens the PR, "
               "the merge gate reads its body",
         hint("brew install gh", "https://cli.github.com (apt: gh)"), False),
        ("qrencode", "optional: QR for the tunnel URL",
         hint("brew install qrencode", "sudo apt install qrencode"), False),
    ]
    return [(n, bool(shutil.which(n)), why, how, req) for n, why, how, req in rows]


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


# Why the last list_models() came back empty, for the warning pick_model
# prints. A silent [] reads as "this CLI has no models"; the real causes --
# a plugin install that timed out on first run, a missing binary, a crash --
# each have a different fix and the CLI's own stderr names it.
LAST_LIST_ERROR = ""


def list_models(agent: dict) -> list:
    """Ask the vendor CLI what it can run. Empty list on any failure."""
    global LAST_LIST_ERROR
    LAST_LIST_ERROR = ""
    cmd = (agent.get("model") or {}).get("list_cmd")
    if not cmd:
        return []
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        LAST_LIST_ERROR = (f"timed out after 120s -- a first run installs plugins and fetches "
                           f"the model catalog; run `{' '.join(cmd)}` once by hand, then retry")
        return []
    except (OSError, subprocess.SubprocessError) as e:
        LAST_LIST_ERROR = str(e)
        return []
    if out.returncode != 0:
        tail = [l for l in out.stderr.splitlines() if l.strip()][-3:]
        LAST_LIST_ERROR = f"exit {out.returncode}" + (": " + " | ".join(tail) if tail else "")
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
        # cursor-agent prints "id - Display Name (tags)" with single spaces, and
        # a heading line. Take the id before " - "; drop lines with no id shape.
        model_id = model_id.split(" - ", 1)[0].strip()
        if model_id and " " not in model_id:
            models.append(model_id)
    return models


def _expand_xdg(path: str) -> Path:
    """`$XDG_DATA_HOME` with its spec default, then `~`."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(os.path.expanduser(path.replace("$XDG_DATA_HOME", data_home)))


def auth_providers(agent: dict) -> dict:
    """{provider_id: 'api' | 'oauth'} from the CLI's own credential store
    (registry `model.auth_file`), or {} when there is none to read."""
    spec = agent.get("model") or {}
    if not spec.get("auth_file"):
        return {}
    try:
        data = json.loads(_expand_xdg(spec["auth_file"]).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v.get("type", "")) for k, v in data.items() if isinstance(v, dict)}


def unlisted_logins(agent: dict, models: list) -> list:
    """Providers the user has logged into that contribute nothing to the
    model listing, each with the reason and the fix.

    A login that yields no models is the report "I logged in but og can't
    see it". The CLI is not lying -- it lists exactly what it can route to
    -- but it does not say WHY a credential is idle, and the two causes need
    different fixes: an API key is a provider by itself (so its absence
    means it is disabled, or the catalog is stale), while an OAuth session
    only becomes a provider through an auth plugin for that vendor, some
    bundled with the CLI and the rest installed by name.
    """
    spec = agent.get("model") or {}
    listed = {m.split("/", 1)[0] for m in models if "/" in m}
    builtin = set(spec.get("oauth_builtin") or [])
    plugins = spec.get("oauth_plugins") or {}
    cli = spec.get("list_cmd", ["?"])[0]
    out = []
    for pid, kind in sorted(auth_providers(agent).items()):
        if pid in listed:
            continue
        if kind == "oauth":
            if pid in builtin:
                why = (f"OAuth session, plugin is built in -- the session may have expired: "
                       f"`{cli} auth login` again")
            elif pid in plugins:
                why = (f"OAuth session -- it surfaces only through an auth plugin. Add "
                       f"\"{plugins[pid]}\" to the `plugin` list in ~/.config/{cli}/{cli}.json")
            else:
                why = (f"OAuth session -- it surfaces only through an auth plugin for `{pid}`, "
                       f"and none is built in or known here")
        else:
            why = (f"API key present but no {pid}/ models -- check `disabled_providers` in "
                   f"~/.config/{cli}/{cli}.json, or refresh the catalog: `{cli} models --refresh`")
        out.append((pid, kind, why))
    return out


# Multi-provider harnesses (OpenCode, Kilo) list every provider the user has
# added with `<cli> auth login` under its own prefix: `deepseek/deepseek-chat`
# next to `opencode/glm-5`. Group by that prefix so a second provider is
# visible instead of buried under Zen's ~70 ids. Every id the tool offers is
# shown and any may be pinned -- a subscriber picks a paid id exactly like a
# free one. Placement only: ids that are a router (`auto`) or a free tier
# (`free`) go to the top of their group, and groups holding one come first,
# because those are the ones people look for by name in a 300-line list.
MODELS_PER_PROVIDER = 12
FEATURED = re.compile(r"auto|free", re.IGNORECASE)


def grouped_models(agent: dict, models: list) -> list:
    """[(provider, [ids])] one group per `provider/` prefix; featured ids
    (auto / free) first within a group and groups with one first overall,
    the tool's own order otherwise."""
    groups: dict = {}
    for m in models:
        provider = m.split("/", 1)[0] if "/" in m else ""
        groups.setdefault(provider, []).append(m)
    def rank(m: str) -> int:
        # routers (`auto`) first, then free tiers, then the rest; stable
        # within each band so the tool's own order still shows through.
        return 0 if re.search(r"auto", m, re.IGNORECASE) else 1 if re.search(r"free", m, re.IGNORECASE) else 2
    out = [(provider, sorted(ids, key=rank)) for provider, ids in groups.items()]
    out.sort(key=lambda g: 0 if FEATURED.search(" ".join(g[1])) else 1)
    return out


def search_models(models: list, needle: str) -> list:
    """Case-insensitive substring match; the picker's answer to a long list."""
    n = needle.lower()
    return [m for m in models if n in m.lower()]


# The prefix of a `provider/model` id says who BILLS; for a reseller it says
# nothing about who trained the model, which is what the cross-vendor review
# rule is about. `opencode/claude-sonnet-5` reviewed by Claude Code is
# same-vendor review however the invoice reads.
AGGREGATOR_PROVIDERS = {"opencode", "openrouter", "kilo"}
MODEL_FAMILIES = [
    (r"claude", "anthropic"),
    (r"gpt|codex|^o[1-9]\b", "openai"),
    (r"gemini|gemma", "google"),
    (r"deepseek", "deepseek"),
    (r"glm", "zhipu"),
    (r"kimi|moonshot", "moonshot"),
    (r"qwen", "alibaba"),
    (r"grok", "xai"),
    (r"mistral|devstral|codestral|magistral", "mistral"),
    (r"llama", "meta"),
    (r"minimax", "minimax"),
    (r"mimo", "xiaomi"),
]


def model_vendor(model_id: str | None) -> str | None:
    """Vendor implied by a pinned model id, or None when it says nothing
    (no provider prefix, or an aggregator's own router like kilo-auto)."""
    if not model_id or "/" not in model_id:
        return None
    provider, name = model_id.split("/", 1)
    if provider not in AGGREGATOR_PROVIDERS:
        return provider
    for pat, vendor in MODEL_FAMILIES:
        if re.search(pat, name, re.IGNORECASE):
            return vendor
    return None


def vendor_of(agent: dict, entry: dict) -> str:
    """The registry vendor, refined by the plan entry's model pin when that
    pin names a vendor. Falls back to the registry when it does not."""
    return model_vendor(entry.get("model")) or agent["vendor"]


def is_zen(model_id: str | None) -> bool:
    """OpenCode Zen ids carry the `opencode/` prefix; anything else routed
    through OpenCode is the user's own provider and its own bill."""
    return bool(model_id) and model_id.startswith("opencode/")


def is_zen_free(model_id: str | None) -> bool:
    """A Zen free-tier id (`opencode/*-free`). Only these rotate out of the
    lineup and only these are day-capped; a paid Zen pin is a normal model."""
    return is_zen(model_id) and model_id.endswith("-free")


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

    options = list_models(agent)
    for pid, kind, why in unlisted_logins(agent, options):
        warn(f"{pid}: logged in ({kind}) but not listed. {why}")
    if options:
        # A stale `prefer` (Zen rotates its lineup) must not become the default
        # just because it is written in the registry.
        prefer = spec.get("prefer")
        default = current or (prefer if prefer in options else None) or options[0]
        pool = options
        while True:
            shown = []
            for provider, ids in grouped_models(agent, pool):
                if provider:
                    say(f"  {C['dim']}{provider}/{C['x']}")
                for m in ids[:MODELS_PER_PROVIDER]:
                    shown.append(m)
                    mark = f" {C['g']}(current){C['x']}" if m == current else ""
                    say(f"  {len(shown):2}. {m}{mark}")
                if len(ids) > MODELS_PER_PROVIDER:
                    say(f"      {C['dim']}… {len(ids)-MODELS_PER_PROVIDER} more {provider}/ ids; "
                        f"type part of a name to search, or a full id{C['x']}")
            got = ask("model id (number, full id, or text to search)", default)
            if got in options:
                return got
            if got.isdigit():
                if 1 <= int(got) <= len(shown):
                    return shown[int(got) - 1]
                # A bare number outside the menu is a typo, not a model id --
                # storing "30" as the pin would fail at the first dispatch.
                err(f"enter 1-{len(shown)}, or a full model id")
                continue
            # Anything else narrows the menu. No match: accept it verbatim only
            # if the user insists, since the tool did not list it.
            hits = search_models(options, got)
            if hits:
                pool = hits
                say(f"  {C['dim']}{len(hits)} match(es) for {got!r}{C['x']}")
                continue
            if ask_yes(f"{got!r} is not in the list; pin it anyway?", default=False):
                return got
            pool = options

    if spec.get("list_cmd"):
        warn(f"could not list models: `{' '.join(spec['list_cmd'])}` "
             f"{LAST_LIST_ERROR or 'printed nothing'} — type one manually")
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

    rev_vendor = vendor_of(reg[plan["reviewer"]["id"]], plan["reviewer"])
    same = [reg[c["id"]]["label"] for c in plan["coders"]
            if vendor_of(reg[c["id"]], c) == rev_vendor]
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
        if a["id"] == "opencode" and is_zen_free(c.get("model")):
            tags.append("day-capped; when dry move down, do not retry")
        tag = f" {'; '.join(tags)}." if tags else ""
        lines.append(f"  - {names[i].ljust(width)}{ordinals[min(i, 5)]}: {a['label']} "
                     f"(`{a['harness']}`){pin}.{tag}")
    rv = reg[plan["reviewer"]["id"]]
    lines.append(f"  - {names[-1].ljust(width)}{rv['label']} (`{rv['harness']}`). "
                 "Reviews only; never edits.")
    return "\n".join(lines)


def render_vendor_map(plan: dict) -> str:
    """One line per worker naming its vendor, for the Review rules section.

    Generated from the actual plan rather than hand-written, so it can never
    go stale the way a hardcoded example (naming specific workers and
    vendors that drift the moment the roster is reconfigured) would.
    """
    reg = agents_by_id()
    rv = vendor_of(reg[plan["reviewer"]["id"]], plan["reviewer"])
    lines = []
    for c in plan["coders"]:
        cv = vendor_of(reg[c["id"]], c)
        same = (f" — same vendor as `reviewer` ({rv}); that pairing is "
                "degraded-review" if cv == rv else "")
        lines.append(f"  - `{worker_name(c['id'])}` is {cv}{same}.")
    lines.append(f"  - `reviewer` is {rv}.")
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
        "failed this run. Every worker pins its own model in its spec — never pass",
        "`args.model`.", "",
    ]
    for i, c in enumerate(plan["coders"], 1):
        a = reg[c["id"]]
        out += [f"## {i}. `{worker_name(c['id'])}` — {a['label']}", "",
                f"- harness `{a['harness']}`, vendor `{vendor_of(a, c)}`",
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
            f"- harness `{rv['harness']}`, vendor `{vendor_of(rv, plan['reviewer'])}`",
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
  A CHECK, not a choice: `args.model` replaces a verified free pin with a
  guess, which is how a paid model hits OpenCode's "No payment method" wall.
  Call `sys_list_models` once. Pinned id listed, or query failed -> dispatch
  with no `args.model`, say nothing. Gone (Zen rotates its lineup) -> pick the
  strongest replacement ending in `-free`, pass it as `args.model` this run
  only, and tell the human the pin needs updating. A non-`-free` id is a
  failed dispatch, not a slower one.

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
        "{{VENDOR_MAP}}": render_vendor_map(plan),
        "{{PREFLIGHT_MAP}}": render_preflight_map(plan),
        # The Zen preflight guards against a rotated FREE-tier id; a paid Zen
        # pin or a provider the user added (deepseek/..., anthropic/...) has no
        # such rotation, so the check would only invite an `args.model` override.
        "{{OPENCODE_PREFLIGHT}}": (OPENCODE_PREFLIGHT.format(name=worker_name("opencode"))
                                   if oc and is_zen_free(oc.get("model")) else ""),
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
def acp_command(agent: dict, model: str | None) -> str:
    """The ACP launch command, with the model pin baked in where the CLI needs it.

    Omnigent's generic ACP executor never delivers a model pin to the agent:
    `executor.model` lands in HARNESS_ACP_MODEL, which the executor documents
    as inert unless `send_model` is set -- and that only adds a non-standard
    `model` field to `session/new`, which Cline ignores. `session/set_config_option`
    is used for interactive `/model` picks only. Traced on the wire
    (initialize, session/new, session/prompt -- nothing else), so a Cline
    worker always started on Cline's hard-coded default, `anthropic/claude-sonnet-5`
    on usage-billing, and with no credits behind it Cline answers `end_turn`
    with no content: the "completed with no output" signature.

    Cline's ACP `newSession` reads the model from `CLINE_MODEL` (and the
    provider from CLINE_PROVIDER); a registry row names that variable as
    `model_env`. The executor exec's the argv directly (no shell, no $VAR), but
    `env` is a real binary, so `env CLINE_MODEL=<pin> cline --acp ...` sets it
    for exactly that process. Agents without such a variable (Kilo) take their
    default from their own config file instead -- see the registry note.
    """
    cmd = agent["acp_command"]
    var = agent.get("model_env")
    if var and model:
        return f"env {var}={shlex.quote(model)} {cmd}"
    return cmd


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
            row = {"command": acp_command(a, c.get("model")), "name": a["label"],
                   "omnigent_mcp": a.get("omnigent_mcp", False)}
            if c.get("model"):
                row["model"] = c["model"]
            if by_name.get(a["label"]) != row:
                rows = [r for r in rows if r.get("name") != a["label"]] + [row]
                changed.append(f"acp.agents[{a['label']}] = {row['command']}")
        acp["agents"] = rows
        cfg["acp"] = acp

    cfg["default_agent"] = str(OMNI / "agents" / plan["agent_name"])
    oc = next((c for c in plan["coders"] if c["id"] == "opencode"), None)
    if oc and oc.get("model"):
        cfg["opencode_model"] = oc["model"]

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=True, default_flow_style=False))
    return changed


# OpenCode's `question` tool blocks the turn until a human answers. Omnigent
# mirrors it to a web approval card and waits up to a day (the hook timeout is
# hard-coded server-side, not a spec knob), so a worker that "just checks"
# which of two designs you prefer parks the whole run until someone happens to
# look -- observed twice in one session, ~10 minutes each, both on a free-tier
# model that likes to ask. The coder prompt says never to ask; this makes the
# tool unavailable so the prompt is not the only guard.
#
# Delivery: OpenCode merges `$OPENCODE_CONFIG_DIR/opencode.json` AFTER the
# per-session config Omnigent synthesizes, and Omnigent passes `OPENCODE_*`
# env through to `opencode serve` (only OPENCODE_CONFIG / _CONFIG_CONTENT are
# denylisted). `og start` exports the var, and forwards it host->runner via
# OMNIGENT_RUNNER_ENV_PASSTHROUGH.
#
# Why `permission` and not `tools: {question: false}`: the `tools` form is
# rewritten into a `question: deny` rule that lands BEFORE Omnigent's `*: ask`
# rule, and OpenCode's last-match-wins evaluation lets `*: ask` win -- the tool
# stays visible (verified against 1.18.30 over `opencode serve`). Spelling out
# `*: ask` first and `question: deny` after it keeps every other tool on the
# policy-engine route Omnigent depends on and drops only `question`.
OPENCODE_WORKER_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "permission": {"*": "ask", "question": "deny"},
}

# Code-intelligence MCP servers a worker may tap, keyed by the CLI that must be
# on PATH. Each is a plain stdio process (no Docker). Wired only when the CLI
# is present at install time: the user's global opencode.json is invisible to
# workers, so this is the only way the tool reaches them, and a tool the model
# can SEE in its list gets used where a prompt hint about "a CLI on PATH" does
# not (free-tier models especially). The worker prompt stays tool-agnostic;
# whatever is wired here is what it finds.
CODE_INTEL_MCP = {
    "codegraph": {"type": "local", "command": ["codegraph", "serve", "--mcp"], "enabled": True},
}


def opencode_worker_config() -> dict:
    cfg = json.loads(json.dumps(OPENCODE_WORKER_CONFIG))
    mcp = {name: srv for name, srv in CODE_INTEL_MCP.items() if shutil.which(name)}
    if mcp:
        cfg["mcp"] = mcp
    return cfg


def opencode_worker_config_dir(plan: dict) -> Path | None:
    """Where og's OpenCode worker overrides live, or None when they must not.

    Only when OpenCode is a coder and NOT the orchestrator: the config applies
    to every OpenCode session og launches, and an OpenCode orchestrator needs
    `question` for its plan gate.
    """
    is_coder = any(c["id"] == "opencode" for c in plan["coders"])
    if not is_coder or plan["orchestrator"] == "opencode":
        return None
    return OMNI / "opencode"


def write_opencode_worker_config(plan: dict) -> Path | None:
    """Write (or remove) the OpenCode worker config dir; returns it if written."""
    d = opencode_worker_config_dir(plan)
    stale = OMNI / "opencode" / "opencode.json"
    if d is None:
        # Rerunnable: a roster that no longer qualifies must not leave a config
        # behind that og.env stops pointing at but a hand-set env could reach.
        if stale.exists():
            stale.unlink()
        return None
    d.mkdir(parents=True, exist_ok=True)
    (d / "opencode.json").write_text(json.dumps(opencode_worker_config(), indent=2) + "\n")
    return d


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
    ocd = opencode_worker_config_dir(plan)
    if ocd:
        lines += ["", "# OpenCode worker overrides (drops the blocking `question` tool). og start",
                  "# exports this as OPENCODE_CONFIG_DIR and forwards it to the workers.",
                  f"OG_OPENCODE_CONFIG_DIR={ocd}"]
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
    ocd = write_opencode_worker_config(plan)
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
    if ocd:
        wired = ", ".join(sorted(opencode_worker_config().get("mcp", {}))) or "none found"
        ok(f"opencode      {ocd/'opencode.json'} (question tool off; code-intel MCP: {wired})")
    elif any(c["id"] == "opencode" for c in plan["coders"]):
        warn("OpenCode is the orchestrator AND a coder: its `question` tool stays on for "
             "both, so a coder that asks will park the run until you answer.")
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


POLICY_MODULE = "omnigent_local_policies"
POLICY_HANDLERS = [f"{POLICY_MODULE}.merge_gate",
                   f"{POLICY_MODULE}.blast_radius_with_branch_cleanup"]


def _shebang(path: Path) -> Path | None:
    try:
        with path.open("rb") as fh:
            first = fh.readline(512).decode(errors="replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    parts = first[2:].split()
    if not parts:
        return None
    if Path(parts[0]).name == "env" and len(parts) > 1:
        found = shutil.which(parts[1])
        return Path(found) if found else None
    return Path(parts[0])


def omnigent_python() -> Path | None:
    """The interpreter omnigent itself runs under -- NOT the one running this
    installer. `uv tool`, pipx, `pip install --user` and Homebrew all give it
    its own venv; the entry point's shebang names that venv's python, which
    is the one whose site-packages a .pth must land in."""
    exe = shutil.which("omnigent")
    if exe:
        real = Path(os.path.realpath(exe))
        py = _shebang(real)
        if py and py.is_file():
            return py
        for name in ("python3", "python"):
            if (real.parent / name).is_file():
                return real.parent / name
    # No entry point on PATH (or a launcher without a shebang): the venvs the
    # supported installers create, by convention.
    uv = shutil.which("uv")
    if uv:
        try:
            out = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True,
                                 timeout=15, check=False)
            if out.returncode == 0 and out.stdout.strip():
                for name in ("python3", "python"):
                    cand = Path(out.stdout.strip()) / "omnigent" / "bin" / name
                    if cand.is_file():
                        return cand
        except (OSError, subprocess.SubprocessError):
            pass
    for cand in (Path.home() / ".local" / "pipx" / "venvs" / "omnigent" / "bin" / "python",
                 Path.home() / ".local" / "share" / "uv" / "tools" / "omnigent" / "bin" / "python"):
        if cand.is_file():
            return cand
    # Last resort: this interpreter, but only if omnigent actually imports here.
    probe = subprocess.run([sys.executable, "-c", "import omnigent"], capture_output=True,
                           text=True, timeout=30, check=False)
    return Path(sys.executable) if probe.returncode == 0 else None


def _run_py(py: Path, code: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                          timeout=60, check=False)


def install_pth(pol_dir: Path) -> None:
    """Put the policies dir on the OMNIGENT interpreter's sys.path, and prove it.

    config.yaml names `omnigent_local_policies` in `policy_modules`, so a
    server that cannot import it does not degrade -- every policy evaluation
    raises and the failure mode is deny-everything, with nothing in the chat
    error pointing back here. That makes a missing .pth a broken install, not
    a warning: this dies with the exact remediation.

    `site.getsitepackages()` was the previous approach; it answers for the
    interpreter running THIS script (install.sh's system python3 on Linux,
    whose site-packages are root-owned), never for omnigent's venv.
    """
    py = omnigent_python()
    if py is None:
        die("could not find the interpreter omnigent runs under (is `omnigent` on PATH?).\n"
            f"    The local policies must be importable by it. Install omnigent first:\n"
            "      uv tool install omnigent")
    out = _run_py(py, "import sysconfig; print(sysconfig.get_paths()['purelib'])")
    site_dir = Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    pth_line = str(pol_dir) + "\n"
    manual = (f"    Write it yourself:\n"
              f"      echo '{pol_dir}' > <omnigent site-packages>/{POLICY_MODULE.replace('_', '-')}.pth\n"
              f"    (site-packages of {py})")
    if site_dir is None or not site_dir.is_dir():
        die(f"could not resolve site-packages for {py}:\n    {out.stderr.strip()}\n{manual}")
    pth = site_dir / "omnigent-local-policies.pth"
    try:
        pth.write_text(pth_line)
    except OSError as e:
        die(f"could not write {pth}: {e}\n{manual}")

    # Prove it, in the interpreter that matters: a fresh process reads the
    # .pth at startup, so this is exactly what the server will see.
    check = _run_py(py, f"import {POLICY_MODULE} as m; print(m.__file__)")
    if check.returncode != 0 or str(pol_dir) not in check.stdout:
        die(f"wrote {pth} but `{POLICY_MODULE}` still does not import under {py}:\n"
            f"    {check.stderr.strip() or check.stdout.strip()}\n"
            "    The server would deny every action with 'policy evaluation error'.")
    # Handler registration through omnigent's own registry, when this build
    # exposes it. Best-effort: a registry API change must not fail installs.
    reg = _run_py(py, (
        "from omnigent.policies.registry import load_registry, is_registered_handler\n"
        f"load_registry(extra_modules=['{POLICY_MODULE}'])\n"
        f"missing = [h for h in {POLICY_HANDLERS!r} if not is_registered_handler(h)]\n"
        "print(' '.join(missing))"))
    if reg.returncode == 0 and reg.stdout.strip():
        die(f"policies import but these handlers are not registered: {reg.stdout.strip()}\n"
            f"    ({pol_dir / (POLICY_MODULE + '.py')} may be stale or edited)")
    ok(f"policies        {pth} -> {pol_dir}  (verified under {py})")


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
        host = host_os()
        if host == "windows":
            die("native Windows is not supported: og is a bash script and Omnigent's "
                "native agent terminals need tmux. Install WSL2 (wsl --install), "
                "then clone and run this inside the WSL shell.")
        say(f"{C['b']}Prerequisites{C['x']}  {C['dim']}({host}){C['x']}")
        if host == "wsl":
            say(f"{C['dim']}WSL: credentials go to ~/.omnigent/og-credentials (0600) -- "
                f"no keyring here; LAN access needs mirrored networking or "
                f"`og start tunneled`.{C['x']}")
        missing_required = False
        for name, present, why, how, required in prereqs():
            if present:
                ok(f"{name:10} {C['dim']}{why}{C['x']}")
            elif not required:
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
