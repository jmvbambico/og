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
from typing import NamedTuple

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
        # cmd's `--list-models` groups ids under Capitalized vendor section
        # headings (Stealth, Anthropic, OpenAI, ...) plus a trailing `Docs:`
        # line, each of which otherwise parses as a bare id. Real ids from
        # every vendor CLI seen so far are lowercase, so drop any candidate
        # with an uppercase letter instead of allowlisting vendor names that
        # will drift the next time cmd adds a provider.
        if model_id and " " not in model_id and model_id == model_id.lower():
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
    # Unpinnable is DECLARED (model.pinnable: false), not inferred from missing
    # metadata. Inferring it meant a required=false row with no live listing and
    # no static `choices` -- codex, agy -- silently skipped the question, so a
    # user who wanted a reviewer model pin (the reported bug) was never asked.
    # Only a row that says so is skipped; every other row reaches the prompt,
    # and with no listing or choices it falls to manual entry (blank = harness
    # default). `pinnable` defaults to true, so an absent key is pinnable.
    if not spec.get("pinnable", True):
        note = spec.get("note")
        if note:
            say(f"  {C['dim']}{agent['label']}: {note}{C['x']}")
        return current

    say()
    say(f"{C['b']}Model for {agent['label']}{C['x']}")
    if spec.get("note"):
        say(f"  {C['dim']}{spec['note']}{C['x']}")

    options = list_models(agent)
    # No live listing? Use the registry's static `choices` so the same picker
    # menu serves both live (`list_cmd`) and fixed lineups.
    if not options and spec.get("choices"):
        options = spec["choices"]
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


# A role's plan value is an ORDERED list of entries, first tried first. `coders`
# has always been that; `orchestrator` and `reviewer` were singletons, which is
# why a quota-dry Codex reviewer had nowhere to fail over to. The list order IS
# the failover order, so `priority` is assigned from array position exactly as
# `main()` has always done for coders.
def chain_entries(value) -> list:
    """A role's plan value as an ordered entry list.

    Accepts the old singleton shapes (`"orchestrator": "claude"`, `"reviewer":
    {"id": ...}`) because a live og-install.json holds them and MUST keep
    loading and installing unchanged — a bare string or a single object
    normalizes to a one-element chain on read rather than being rejected.
    """
    if isinstance(value, str):
        items = [{"id": value}]
    elif isinstance(value, dict):
        items = [value]
    elif isinstance(value, list):
        items = [{"id": raw} if isinstance(raw, str) else raw for raw in value]
    else:
        return []
    out = []
    for i, raw in enumerate(items, 1):
        entry = dict(raw)
        entry.setdefault("priority", i)
        # Canonical key order — id, priority, model, then anything else. The
        # state file is written back on every apply and compared across
        # machines, so an old singleton entry and the chain form it normalizes
        # to must serialize byte-identically, not merely decode equal.
        canonical = {k: entry[k] for k in ("id", "priority", "model") if k in entry}
        canonical.update({k: v for k, v in entry.items()
                          if k not in ("id", "priority", "model")})
        out.append(canonical)
    return out


def chain(plan: dict, role: str) -> list:
    """The ordered failover chain for a role, whatever shape it was saved in."""
    return chain_entries(plan.get(role))


def primary(plan: dict, role: str) -> dict:
    """The entry tried first in a role's chain — the head."""
    return chain(plan, role)[0]


# --------------------------------------------------------------------------
# the role table
# --------------------------------------------------------------------------
# ONE table declares every role this installer wires. Every per-role loop
# below iterates it rather than naming the roles again: normalize_plan(),
# account_entries(), validate(), write_shims(), patch_global_config(),
# render_orchestrator(), apply(), show(), emit_questions() and og_stats's
# lineup(). Adding a role used to mean editing seven of those in lockstep, and
# two roles added that way drift apart the moment one of them is touched: a
# role copy-pasted through ten call sites looks correct until a branch is
# missed. A row here is the whole change.
class Role(NamedTuple):
    key: str        # plan key, and the spec-name stem of a singleton chain
    role: str       # the name a registry row lists in its `roles` array
    template: str   # the template that renders it
    multi: bool     # True: many parallel workers; False: one-at-a-time chain
    spec: bool      # True: renders a sub-agent dir; False: the root config
    optional: bool  # True: the plan may omit it and the bundle stays correct
    ask: str        # the --questions prompt that offers this role


ROLES = [
    Role("orchestrator", "orchestrator", "orchestrator.yaml.tmpl",
         multi=False, spec=False, optional=False,
         ask="Which agent plans and delegates (never writes product code), "
             "in preference order (first is tried first)?"),
    Role("coders", "coder", "coder.yaml.tmpl",
         multi=True, spec=True, optional=False,
         ask="Which agents implement code, in preference order (first is tried first)?"),
    Role("reviewer", "reviewer", "reviewer.yaml.tmpl",
         multi=False, spec=True, optional=False,
         ask="Which agents review the batched diff, in preference order (first "
             "is tried first; the next takes over when one is out of quota)? "
             "Prefer a vendor that differs from every coder."),
    # A read-only worker for the orchestrator's own biggest sink: reading and
    # searching a repo with its own hands (measured at 52% of its result bytes
    # across 30 sessions). Optional because it spends prompt bytes; a user who
    # omits it gets a bundle with no scout spec and no dangling reference.
    Role("scout", "scout", "scout.yaml.tmpl",
         multi=False, spec=True, optional=True,
         ask="Which agents answer repo reading, search and git-state questions "
             "as a read-only scout, in preference order (first is tried first)?"),
    # A worker for the orchestrator's own git and gate plumbing — measured at
    # 727 git calls / 725 kB of result bytes (diff, log, status, worktree,
    # merge) plus 185 gate calls / 84 kB across 30 audited sessions, all of it
    # mechanical and all of it otherwise ingested whole. Optional because it
    # spends prompt bytes; a user who omits it gets a bundle with no integrator
    # spec and no dangling reference.
    Role("integrator", "integrator", "integrator.yaml.tmpl",
         multi=False, spec=True, optional=True,
         ask="Which agents run git, worktree and gate plumbing for the "
             "orchestrator (they never decide a merge), in preference order "
             "(first is tried first)?"),
]

# Roles that render a sub-agent spec under <bundle>/agents/<name>. The
# orchestrator is the one excluded: its spec IS the bundle's root config.
SPEC_ROLES = [r for r in ROLES if r.spec]


def role_of(key: str) -> Role:
    return next(r for r in ROLES if r.key == key)


def role_names(plan: dict, key: str) -> list:
    """Spec names for a role, in chain order.

    A singleton chain keeps its own name for the head (`reviewer`, `scout`) and
    appends the position for each backup — the rule `reviewer_names` has always
    used, and the same rule the coder specs follow (a stable name for the head,
    a distinct one per addition, so renaming nothing that already exists in a
    session history). A multi role names each worker by its registry `worker`
    override or `coder_<id>` instead, so two roles can wire the same agent
    without their spec directories colliding.
    """
    r = role_of(key)
    entries = chain(plan, key)
    if r.multi:
        return [worker_name(e["id"]) for e in entries]
    return [key if i == 1 else f"{key}_{i}" for i in range(1, len(entries) + 1)]


def normalize_plan(plan: dict) -> dict:
    """Rewrite every role to the ordered-list shape, in place.

    Applied once before a plan is persisted, so og-install.json always holds
    the new shape going forward. Readers go through chain()/primary() and stay
    correct for either, which is what lets an old state file skip a migration.
    The set of roles comes from ROLES, so a role the table does not declare (a
    hand-added key) is left exactly as it was rather than guessed at.
    """
    for r in ROLES:
        if plan.get(r.key) is not None:
            plan[r.key] = chain_entries(plan[r.key])
    return plan


def reviewer_names(plan: dict) -> list:
    """Spec names for the reviewer chain, in order.

    The primary keeps the established `reviewer` name — the orchestrator
    prompt's agent list, the cross-review skill and every existing session
    history all refer to it — and each backup appends its chain position. That
    is the same rule the coder specs follow (`coder_<id>`, with some registry
    rows pinning `worker` explicitly to keep a name stable across an id
    change): the head keeps its name, additions get a distinct one.
    """
    return role_names(plan, "reviewer")


def account_entries(plan: dict, reg: dict | None = None) -> list:
    """(agent_id, env_var, config_dir) for every installed id that has an account.

    `accounts` is keyed by agent id, and interactive collects it for every id in
    every chain — orchestrator, each reviewer, each coder — not just the head.
    Only a row whose registry `multi_account.env` names the variable can be
    wired; a stray accounts key is ignored rather than guessed at. Chain order,
    first occurrence of a repeated id wins.

    `reg` is passed in by validate(), which already holds the catalog; reloading
    it here was a second agents_by_id() per call for no reason.
    """
    reg = reg or agents_by_id()
    out, seen = [], set()
    for r in ROLES:
        for e in chain(plan, r.key):
            aid = e["id"]
            if aid in seen:
                continue
            seen.add(aid)
            acct = (plan.get("accounts") or {}).get(aid)
            env = (reg.get(aid, {}).get("multi_account") or {}).get("env")
            if acct and env:
                out.append((aid, f"OG_{env}", acct))
    return out


def role_caveat(agent: dict, role: str) -> str | None:
    """The UNVERIFIED-in-this-role caveat for `agent`, or None.

    A row records the roles it has never been driven in as a structured list
    rather than free text: grok and devin are verified coders but unverified
    ORCHESTRATORS, so the whole-row `unverified` flag (which condemns a row in
    the detected-CLI list) would be wrong here. This is the field CODE reads,
    so the caveat reaches the picker, validate() and --questions instead of
    sitting in the registry unread the way a `roles_note` did.
    """
    if role not in (agent.get("unverified_roles") or []):
        return None
    return (f"{agent['label']} is UNVERIFIED as {role}: it clears the bars "
            "validate() enforces, but no og run has been driven with it in that "
            "role, so the first real dispatch is the only proof. Demote it to a "
            "verified role if that dispatch cannot perform the role.")


def role_options(role: str, found: dict | None = None) -> list:
    """(id, label, note) for every detected agent that may fill `role`.

    Extracted from build_plan_interactive so the per-role caveat can be asserted
    without a terminal: `pick_one` renders the third field under the option, so
    a caveat that only lived in the registry was invisible at exactly the moment
    the user chose.
    """
    if found is None:
        found = scan()
    out = []
    for a in REGISTRY["agents"]:
        if a["id"] not in found or role not in a["roles"]:
            continue
        notes = []
        if role == "reviewer" and a.get("reviewer_warning"):
            notes.append(a["reviewer_warning"])
        caveat = role_caveat(a, role)
        if caveat:
            notes.append(caveat)
        out.append((a["id"], a["label"], " ".join(notes) or None))
    return out


def role_notes(role: str) -> dict:
    """agent id -> per-role caveat, for the `--questions` surface.

    AGENTS.md Part 1 has an AI installer drive its conversation from that
    output, and it must never offer an agent without the warning that applies to
    it. The caveat is per role -- grok/devin are verified coders and unverified
    orchestrators -- so it is keyed by id under the question that offers them.
    """
    out = {}
    for a in REGISTRY["agents"]:
        caveat = role_caveat(a, role)
        if caveat:
            out[a["id"]] = caveat
    return out


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
        # role_options carries the reviewer warning AND the per-role UNVERIFIED
        # caveat into the note pick_one renders under each option, so a caveat
        # is visible while choosing rather than after the install.
        return role_options(role, found)

    # --- orchestrator ---
    orch_opts = opts("orchestrator")
    if not orch_opts:
        die("none of the detected CLIs can act as an orchestrator "
            "(needs Omnigent's sys_* tool relay).")
    # A chain, like the coders: the later entries are the failover a launch
    # takes when the brain above them is out of quota.
    orchestrator_ids = pick_many_ordered(
        "Which agent runs the ORCHESTRATOR? (plans, delegates, never writes "
        "product code — preference order, first is tried first)",
        orch_opts, [e["id"] for e in chain(state, "orchestrator")])
    orchestrators = [{"id": aid, "priority": i}
                     for i, aid in enumerate(orchestrator_ids, 1)]

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
    rev_prev = {e["id"]: e.get("model") for e in chain(state, "reviewer")}
    reviewer_ids = pick_many_ordered(
        "Which agents REVIEW the batched diff? (reads only, never edits — "
        "preference order, the next takes over when one is out of quota)",
        rev_opts, [e["id"] for e in chain(state, "reviewer")])
    reviewers = [{"id": rid, "priority": i, "model": pick_model(reg[rid], rev_prev.get(rid))}
                 for i, rid in enumerate(reviewer_ids, 1)]

    # --- optional roles, one row each (scout) ---
    # The answer defaults to what a previous install chose, and "no" writes an
    # EMPTY chain rather than omitting the key: the generated bundle then carries
    # no spec for the role, the orchestrator prompt spends no bytes on it, and
    # re-running the installer offers the same choice again. A role the table
    # marks optional is never forced.
    optional = {}
    for r in ROLES:
        if not (r.optional and r.spec):
            continue
        o_opts = opts(r.role)
        cur = [e["id"] for e in chain(state, r.key)]
        prev_models = {e["id"]: e.get("model") for e in chain(state, r.key)}
        optional[r.key] = []
        if not o_opts:
            continue
        say()
        say(f"{C['dim']}{r.ask}{C['x']}")
        if not ask_yes(f"Install an optional {r.key} worker?", default=bool(cur)):
            continue
        optional[r.key] = [
            {"id": oid, "priority": i, "model": pick_model(reg[oid], prev_models.get(oid))}
            for i, oid in enumerate(pick_many_ordered(r.ask, o_opts, cur), 1)]

    # --- multi-account, for any selected agent that supports it ---
    accounts = dict(state.get("accounts") or {})
    involved = set(orchestrator_ids) | set(reviewer_ids) | {c["id"] for c in coders} \
        | {e["id"] for chain_ in optional.values() for e in chain_}
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
        "orchestrator": orchestrators,
        "coders": coders,
        "reviewer": reviewers,
        **optional,
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
    # A role may now be a chain; chain() normalizes the old singleton shape so a
    # saved og-install.json validates exactly as it always did.
    orchestrators = chain(plan, "orchestrator")
    reviewers = chain(plan, "reviewer")

    # A row declaring both `model.required` (a dispatch MUST pin a model) and
    # `model.pinnable: false` (the installer must never offer one) is
    # self-contradictory: `pick_model` skips the question, so the worker runs
    # unpinned and inherits the ORCHESTRATOR's model id -- the exact failure
    # `required` exists to prevent, now silent. Refuse it. No row does this
    # today; the guard is here so the next registry edit cannot slip it past.
    for aid in sorted({e["id"] for r in ROLES for e in chain(plan, r.key)}):
        spec = reg[aid].get("model") or {}
        if spec.get("required") and not spec.get("pinnable", True):
            issues.append(("error",
                           f"{reg[aid]['label']} declares model.required true and "
                           "model.pinnable false. The installer would never ask "
                           "for the pin, so the worker runs unpinned and inherits "
                           "the orchestrator's model id. Drop one of the two in "
                           "the registry row."))

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

    # Cross-vendor review is the point of the reviewer role: an agent must never
    # review code its own vendor wrote, and vendor follows the model pin, not
    # the registry row. This WARNs rather than refuses -- refusing would block a
    # user whose only available backup is same-vendor, which is worse than
    # telling them plainly what they are getting. Checked for EVERY entry in the
    # chain: the one that gets used is the one the orchestrator reaches for when
    # the primary is dry.
    for i, (name, r) in enumerate(zip(reviewer_names(plan), reviewers)):
        rv = vendor_of(reg[r["id"]], r)
        colliding = [f"`{worker_name(c['id'])}` ({reg[c['id']]['label']})"
                     for c in plan["coders"] if vendor_of(reg[c["id"]], c) == rv]
        if colliding:
            # The PRIMARY collides immediately -- you never fail over TO the
            # primary -- so the failover wording is right only for a backup.
            tail = ("cross-vendor, so this is a same-vendor review — the PR must be "
                    "labelled `degraded-review`." if i == 0 else
                    f"cross-vendor, so failing over to `{name}` would produce a "
                    "same-vendor review — the PR must then be labelled "
                    "`degraded-review`.")
            issues.append(("warn",
                           f"reviewer `{name}` ({reg[r['id']]['label']}) shares a vendor with "
                           f"{', '.join(colliding)}: all are `{rv}`. Review is meant to be "
                           + tail))

    for o in orchestrators:
        a = reg[o["id"]]
        if a.get("relay") is False:
            issues.append(("error",
                           f"{a['label']} runs without Omnigent's sys_* tool "
                           "relay, so it cannot dispatch sub-agents. Pick a different "
                           "orchestrator."))

        # A harness that never receives the spec prompt cannot orchestrate: the
        # whole orchestration contract lives in that prompt. Checked for EVERY
        # entry in the chain -- a `none` BACKUP is exactly as unusable as a
        # `none` primary, and it is the one reached for when the primary is dry.
        if a.get("prompt_delivery") == "none":
            issues.append(("error",
                           f"{a['label']} never receives a spec prompt "
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

    # A `none` reviewer never sees the whole review contract that lives in
    # reviewer.yaml.tmpl, and no warning covered it until this one. Kept separate
    # from the coder `mute` warning above because the remedy differs: a coder
    # needs its scope and gate rules inlined, a reviewer needs the review
    # contract and the exact three-section report format. Folding the two would
    # lose which set of rules the orchestrator must supply. Every entry in the
    # chain is checked: a BACKUP that never receives the contract is exactly as
    # dangerous as a primary that does not, and it is the one the orchestrator
    # reaches for when the primary is dry.
    for r in reviewers:
        if reg[r["id"]].get("prompt_delivery") == "none":
            issues.append(("warn",
                           f"{reg[r['id']]['label']} never receives the review "
                           "contract in reviewer.yaml.tmpl — that harness does not deliver spec "
                           "instructions. The whole contract (judge only against the acceptance "
                           "contract, never edit code, report in exactly three sections BLOCKING / "
                           "NON-BLOCKING / SUGGESTIONS with file:line evidence) must be inlined "
                           "into args.input on every review dispatch; the generated `roster` skill "
                           "tells the orchestrator to do exactly that."))

    # The scout's version of the same hazard, with its own remedy. A `none`
    # scout never sees scout.yaml.tmpl, so both halves of its contract must be
    # inlined on every dispatch: missing the read-only rule it can edit the
    # repo, and missing the bounded-answer rule it pastes whole files back into
    # the context the role exists to keep clear.
    for s in chain(plan, "scout"):
        if reg[s["id"]].get("prompt_delivery") == "none":
            issues.append(("warn",
                           f"{reg[s['id']]['label']} never receives the scout "
                           "contract in scout.yaml.tmpl — that harness does not deliver spec "
                           "instructions. Every scout dispatch must therefore inline the whole "
                           "contract itself: READ-ONLY (never edit, create or delete a file, never "
                           "commit, never run a command that mutates the repo or working tree) and "
                           "a BOUNDED summary (paths with line ranges and short excerpts, never a "
                           "file dump), and it must say plainly when something was not found. The "
                           "generated `roster` skill tells the orchestrator to do exactly that."))

    # The integrator's version of the same hazard, with its own remedy. A `none`
    # integrator never sees integrator.yaml.tmpl, so its whole contract must be
    # inlined on every dispatch: missing the BOUNDED-result rule it pastes a
    # full diff or log back into the orchestrator's context (the bytes the role
    # exists to keep out), and missing the never-decide-a-merge rule it can
    # decide one. Every entry in the chain is checked: a BACKUP that never
    # receives the contract is exactly as dangerous as a primary that does not.
    for ig in chain(plan, "integrator"):
        if reg[ig["id"]].get("prompt_delivery") == "none":
            issues.append(("warn",
                           f"{reg[ig['id']]['label']} never receives the integrator "
                           "contract in integrator.yaml.tmpl — that harness does not deliver "
                           "spec instructions. Every integrator dispatch must therefore inline "
                           "the whole contract itself: git and gate plumbing on the "
                           "orchestrator's behalf (worktrees, the gate commands, the combined "
                           "diff for review, commit SHAs and branch state) returning a BOUNDED "
                           "result (the verdict, the SHAs, and only the FAILING gate output — "
                           "never a full diff, log or passing log); NEVER merging into a protected "
                           "branch, never pushing, never opening or merging a PR, and never "
                           "deciding whether a merge is allowed; and never running the "
                           "integration suite while another integrator may be running it. The "
                           "generated `roster` skill tells the orchestrator to do exactly that."))

    # A role a row marks `unverified_roles` clears validate()'s gates but has
    # never been driven here -- grok/devin as orchestrator. The registry used to
    # record that only in a free-text `roles_note` nothing read, so a user
    # selecting one as the brain was never shown the caveat. The config is legal
    # (a weak orchestrator at runtime, not a broken install), so this WARNs
    # rather than refuses; the first real dispatch is the proof.
    for r in ROLES:
        for e in chain(plan, r.key):
            caveat = role_caveat(reg[e["id"]], r.role)
            if caveat:
                issues.append(("warn", caveat))

    # A `{shim:<name>}` token with no matching shim block renders a path to
    # a file nothing ever writes. The launch then fails with an exec error
    # pointing nowhere near the installer, so refuse it here instead.
    for r in SPEC_ROLES:
        for c in chain(plan, r.key):
            a = reg.get(c["id"])
            if not a:
                continue
            declared = (a.get("shim") or {}).get("name")
            for tok in sorted(set(SHIM_TOKEN.findall(a.get("acp_command") or ""))):
                if tok != declared:
                    issues.append(("error",
                                   f"{a['label']} ({c['id']}) references {{shim:{tok}}} in "
                                   f"acp_command but declares no shim named '{tok}'. The "
                                   "expanded path points at a file nothing writes and the "
                                   "launch fails — declare shim.name == "
                                   f"'{tok}' or fix the token."))

    # og launches ONE server with ONE environment, so a single env var cannot
    # name two config dirs. Two installed ids can share a `multi_account.env`
    # (nothing in the registry forbids it) and each be given a different
    # account; whichever og.env line won would silently run the other agent on
    # the wrong account. Refuse rather than write a file where one account is
    # quietly dropped.
    by_env = {}
    for aid, var, acct in account_entries(plan, reg):
        prev = by_env.get(var)
        if prev and prev[1] != acct:
            issues.append(("error",
                           f"{reg[aid]['label']} and {reg[prev[0]]['label']} both need "
                           f"{var}, but og launches one server with one value. Two "
                           f"accounts ({prev[1]} and {acct}) cannot both be set. Give "
                           "them different multi_account.env values in the registry, or "
                           "drop one of the accounts."))
        else:
            by_env[var] = (aid, acct)

    # The tmux command-string ceiling binds whenever ANY orchestrator in the
    # chain rides on argv -- NOT just the head. Reading only orchestrators[0]
    # meant a per_turn primary (OpenCode) hid an argv BACKUP (claude, codex):
    # the config validated clean, then the backup -- the entry that actually
    # launches once the primary is dry -- died at launch with 'command too long'
    # (surfacing as a native terminal that failed to start). Name every argv
    # entry so a two-orchestrator chain says WHICH one is over the ceiling.
    argv_orchestrators = [o for o in orchestrators
                          if reg[o["id"]].get("prompt_delivery") == "argv"]
    if rendered_prompt is not None and argv_orchestrators:
        quoted = len(shlex.quote(rendered_prompt))
        names = ", ".join(reg[o["id"]]["label"] for o in argv_orchestrators)
        # A two-entry argv chain names two harnesses; "an argv-delivered harness
        # (A, B)" reads as if only one were over the ceiling. Pluralize both the
        # noun and the article so WHICH entries are bound is never misread.
        phrase = ("an argv-delivered harness" if len(argv_orchestrators) == 1
                  else "argv-delivered harnesses")
        if quoted > PROMPT_CEILING:
            issues.append(("error",
                           f"orchestrator prompt is {quoted} bytes shell-quoted, over the "
                           f"{PROMPT_CEILING} ceiling for {phrase} "
                           f"({names}). tmux refuses the launch "
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
    rev_names = role_names(plan, "reviewer")
    sc_names = role_names(plan, "scout")
    ig_names = role_names(plan, "integrator")
    names = [f"`{worker_name(c['id'])}`" for c in plan["coders"]] \
        + [f"`{n}`" for n in rev_names + sc_names + ig_names]
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
    for name, e in zip(rev_names, chain(plan, "reviewer")):
        rv = reg[e["id"]]
        pin = f", pinned `{e['model']}`" if e.get("model") else ""
        lines.append(f"  - {f'`{name}`'.ljust(width)}{rv['label']} "
                     f"(`{rv['harness']}`){pin}. Reviews only; never edits.")
    # The scout's read-only contract is a fact the orchestrator must know BEFORE
    # it dispatches one, so it stays inline rather than behind the roster skill.
    for name, e in zip(sc_names, chain(plan, "scout")):
        sc = reg[e["id"]]
        pin = f", pinned `{e['model']}`" if e.get("model") else ""
        lines.append(f"  - {f'`{name}`'.ljust(width)}{sc['label']} "
                     f"(`{sc['harness']}`){pin}. Read-only; never edits, commits "
                     "or mutates. Bounded summary only.")
    # The integrator's two hard rules change a decision made BEFORE anything is
    # read: that its answer is bounded (so delegating is worth the round trip),
    # and that the merge decision is still the orchestrator's. Both stay inline
    # rather than behind the roster skill, for the same reason as the scout's.
    for name, e in zip(ig_names, chain(plan, "integrator")):
        ig = reg[e["id"]]
        pin = f", pinned `{e['model']}`" if e.get("model") else ""
        lines.append(f"  - {f'`{name}`'.ljust(width)}{ig['label']} "
                     f"(`{ig['harness']}`){pin}. Git/gate plumbing; bounded "
                     "result; never decides a merge.")
    return "\n".join(lines)


def render_vendor_map(plan: dict) -> str:
    """One line per worker naming its vendor, for the Review rules section.

    Generated from the actual plan rather than hand-written, so it can never
    go stale the way a hardcoded example (naming specific workers and
    vendors that drift the moment the roster is reconfigured) would.
    """
    reg = agents_by_id()
    lines = []
    for c in plan["coders"]:
        lines.append(f"  - `{worker_name(c['id'])}` is {vendor_of(reg[c['id']], c)}.")
    # The collision note sits on the REVIEWER line now: with a chain, the same
    # coder can be same-vendor with one reviewer and cross-vendor with the next,
    # so the pairing belongs to the reviewer being named (and matches the
    # `degraded-review` warning validate() raises for that entry).
    for name, e in zip(reviewer_names(plan), chain(plan, "reviewer")):
        rv = vendor_of(reg[e["id"]], e)
        same = [f"`{worker_name(c['id'])}`" for c in plan["coders"]
                if vendor_of(reg[c["id"]], c) == rv]
        note = (f" — same vendor as {', '.join(same)}; that pairing is "
                "degraded-review" if same else "")
        lines.append(f"  - `{name}` is {rv}{note}.")
    return "\n".join(lines)


def quota_line(a: dict) -> str:
    """One line naming the worker's capacity probe, or `not measurable`.

    The registry's `quota` block is optional and its `probe` may be null when
    the limit is known but exposes no queryable API — `og stats` reports that
    as `unknown` rather than guessing, so the line says so instead of naming a
    probe that does not exist.
    """
    q = a.get("quota") or {}
    probe = q.get("probe")
    return (f"- quota: `{probe}`" if probe else
            f"- quota: not measurable{'' if q else ' (no quota block in the registry)'}")


def quota_failure_lines(a: dict) -> list:
    """The vendor-specific failure strings for a row, from its `quota.note`.

    Generated rather than frozen into the prompt: the exact message a worker
    prints when it is dry is what the orchestrator matches to mark it dry, and a
    hand-copied string drifts the moment a registry row is reworded. A row with
    no quota block, or a null note, has no known failure shape to name.
    """
    note = (a.get("quota") or {}).get("note")
    return [f"- quota failure shape: {note}"] if note else []


def render_roster_skill(plan: dict) -> str:
    """The long-form roster notes, as a skill file rather than prompt bytes.

    The preflight procedure and the per-worker failure strings live here rather
    than in the prompt: both are consulted while a dispatch is already being
    prepared or has just failed, which is when an on-demand read is affordable.
    What stays in the prompt is the part that changes a decision made BEFORE
    anything is read (that a preflight is mandatory, and the three-way BOOT /
    TASK / QUOTA classification). The mapping and the failure shapes are
    generated from the plan and the registry so they cannot drift.
    """
    reg = agents_by_id()
    oc = next((c for c in plan["coders"] if c["id"] == "opencode"), None)
    zen = ([zen_preflight_note(worker_name("opencode")), ""]
           if oc and is_zen_free(oc.get("model")) else [])
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
        "## Preflight (FIRST turn, before any dispatch)", "",
        "Run ONE `sys_session_get_info({})` and read `configured_harnesses`. Each",
        "worker maps to exactly one harness id:", "",
        render_preflight_map(plan), "",
        "A worker is available ONLY when its value is exactly `true`. Route only to",
        "the available set. Do not announce a clean result; a MISSING worker is the",
        "only fact worth words. Do this in the same turn you start planning.", "",
        *zen,
        "## Capacity", "",
        "Run `og stats --json` (og is on PATH) before the FIRST dispatch of this run,",
        "and `og stats --agent <id> --json` before every later one. If `og` is missing",
        "or errors, proceed as today and say so once — do not stall the run on it.",
        "Map the stats output's agent ids back to workers by id: `coder_<id>` maps",
        "to `<id>`, and `reviewer`/`scout` map to their chain's agent ids. A worker",
        "whose state is `dry` while `reset_at` is in the future is out of capacity —",
        "skip it and take the next worker. Preference order still wins: only when two",
        "candidates are otherwise equal does `ok` outrank `unknown`. `og stats` reports",
        "a measured `ok` only when the probe actually answered; an inferred or unknown",
        "state is not a clean bill of health.",
        "",
        "On a QUOTA failure — rate limit, usage cap, out of credits, Kilo's",
        "`Add credits to continue, or switch to a free model`, freebuff's",
        "`not enough Freebucks`, an OpenCode worker gone silent with `Rate limit",
        "exceeded` in its log, or a Cline worker returning an empty turn — mark the",
        "worker dry and move on, do not re-send it:",
        "",
        "    og stats --mark <id> dry --until <reset_at from stats if known, else +1h>",
        "        --reason \"<the error line>\"",
        "",
        "Then re-dispatch from a CLEAN worktree to the next worker in preference",
        "order — the replacement must not inherit half-finished state. `dry` is not",
        "`dropped for the run`: before dispatching to that worker again, re-run",
        "`og stats --agent <id> --json`. An expired mark or a measured `ok` puts it",
        "back in the roster. Never paste the JSON into chat — one line per worker:",
        "",
        "    coder_cline     deepseek-balance  ok       23:41 reset",
        "    coder_kilo      kilo-profile     dry      reset 20:00 tomorrow",
        "",
    ]
    for i, c in enumerate(plan["coders"], 1):
        a = reg[c["id"]]
        out += [f"## {i}. `{worker_name(c['id'])}` — {a['label']}", "",
                f"- harness `{a['harness']}`, vendor `{vendor_of(a, c)}`",
                f"- model: {'pinned `' + c['model'] + '`' if c.get('model') else 'chosen by the harness'}",
                quota_line(a)]
        out += quota_failure_lines(a)
        if a.get("relay") is False:
            out.append("- **Leaf worker.** Runs without Omnigent's `sys_*` tool relay, so it "
                       "cannot orchestrate or dispatch. Implementation and exploration only.")
        if a.get("silent_model_failure"):
            out.append("- **Fails silently on a bad model.** It accepts a model switch it cannot "
                       "serve instead of rejecting it, so a wrong pin returns `completed` with a "
                       "transcript containing only your prompt and an untouched worktree. That is "
                       "a misconfiguration, not a refusal — report it and move down the roster "
                       "rather than re-sending the same task.")
        if a["id"] == "cline":
            note = (a.get("model") or {}).get("note")
            if not note or "one session at a time" not in note.lower():
                out.append("- **One session at a time.** Concurrent Cline sessions on one login "
                           "get cut mid-turn — never dispatch two tasks to `coder_cline` in the "
                           "same turn.")
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
    # The reviewer chain, in the same voice as the coder roster above: the
    # orchestrator has to know who backs whom BEFORE it reads anything, because
    # the moment it is needed is the moment the primary comes back dry.
    rev_chain = chain(plan, "reviewer")
    rev_names = reviewer_names(plan)
    listed = [f"`{n}` ({reg[e['id']]['label']})" for n, e in zip(rev_names, rev_chain)]
    if len(listed) == 1:
        order = f"{listed[0]} is the only reviewer in this roster."
    else:
        verb = "backs it up" if len(listed) == 2 else "back it up"
        order = (f"{listed[0]} is the primary; {', '.join(listed[1:])} "
                 f"{verb}, in that order.")
    out += ["## Reviewers — failover chain", "",
            order, "",
            "Check `og stats` before the first review dispatch, and",
            "`og stats --agent <id> --json` before every later one: take the",
            "earliest entry with capacity and move down only when one is dry, dropped",
            "for the run, or already failed this run. Preference order still wins —",
            "`ok` outranks `unknown` only when two candidates are otherwise equal — and",
            "a reviewer marked dry comes back after the `reset_at` `og stats` reports,",
            "so re-check it before using it again. This is the same chain rule as the",
            "coder roster above; the failsafe is too: never re-send a diff to a",
            "reviewer that already failed this run.", ""]
    # One section per reviewer in the chain, in failover order. A backup is
    # reached exactly when the primary is dry — the moment nobody is watching
    # for a surprise — so it gets the same contract, quota shape and caveats
    # rather than a one-line mention.
    for name, e in zip(rev_names, rev_chain):
        rv = reg[e["id"]]
        pin = f", pinned `{e['model']}`" if e.get("model") else ""
        out += [f"## `{name}` — {rv['label']}{pin}", "",
                f"- harness `{rv['harness']}`, vendor `{vendor_of(rv, e)}`",
                quota_line(rv), *quota_failure_lines(rv)]
        # The same collision validate() warns about at install time, carried
        # here so the orchestrator knows it at DISPATCH time too -- the moment
        # it is deciding which entry to use, long after the install scrolled by.
        colliding = [f"`{worker_name(c['id'])}`" for c in plan["coders"]
                     if vendor_of(reg[c["id"]], c) == vendor_of(rv, e)]
        if colliding:
            out.append(f"- **Same-vendor review (`{vendor_of(rv, e)}`).** This reviewer shares "
                       f"a vendor with {', '.join(colliding)}, so failing over to `{name}` "
                       "produces a same-vendor review — it shares the blind spots that produced "
                       "the diff. Use it only after saying so in chat, and label the PR "
                       "`degraded-review`.")
        out += ["- Reviews only; never edits, never gets a worktree.",
                "- Cross-vendor review is the point: never route a diff to a reviewer whose",
                "  vendor matches the implementer's. If that is unavoidable, say so and label",
                "  the PR `degraded-review`."]
        if rv.get("prompt_delivery") == "none":
            # The same hazard as the coder bullet above, but the reviewer's whole
            # contract lives in reviewer.yaml.tmpl and is lost here — so this
            # bullet restates that contract in the dispatch, keeping the two from
            # drifting. Rendered per entry: a BACKUP that never receives the
            # contract is exactly as dangerous as a primary that does not.
            out.append("- **Does not receive its sub-agent prompt.** This harness never "
                       "delivers spec instructions, so the reviewer sees ONLY the text you send "
                       "in `args.input`. Every review dispatch must therefore carry the whole "
                       "contract itself: the acceptance contract and the diff as TEXT (never a "
                       "worktree), judge the diff ONLY against the contract, never edit code and "
                       "never go looking for a worktree, and report in exactly three sections — "
                       "BLOCKING / NON-BLOCKING / SUGGESTIONS — each finding with file:line "
                       "evidence. Do not assume it knows the review format.")
        out.append("")
    # The scout chain, only when one is installed. Same chain rule and same
    # per-entry voice as the reviewers above, plus the contract that makes the
    # role worth its prompt bytes: read-only, and bounded.
    sc_chain = chain(plan, "scout")
    if sc_chain:
        sc_names = role_names(plan, "scout")
        listed = [f"`{n}` ({reg[e['id']]['label']})" for n, e in zip(sc_names, sc_chain)]
        if len(listed) == 1:
            order = f"{listed[0]} is the only scout in this roster."
        else:
            verb = "backs it up" if len(listed) == 2 else "back it up"
            order = (f"{listed[0]} is the primary; {', '.join(listed[1:])} "
                     f"{verb}, in that order.")
        out += ["## Scouts — read-only repo reading", "",
                order, "",
                "Send `scout` the repo reading you would otherwise do yourself:",
                "locating code, `grep`-style searches, git state (`status`, `log`,",
                "`diff`, `rev-parse`, branch and worktree listings), and worker capacity",
                "(`og stats`). It answers with a BOUNDED summary — paths with line ranges,",
                "short excerpts, the shape of the answer — never a file dump; a scout that",
                "pastes whole files has failed its purpose. Take its answer instead of",
                "re-reading the files. It is READ-ONLY: it never edits, creates or deletes",
                "a file, never commits, and never runs a command that mutates the repo or",
                "the working tree, so when the answer needs a change it reports that",
                "instead of making it.", "",
                "Same chain rule as the reviewers: check `og stats` before the first",
                "scout dispatch and `og stats --agent <id> --json` before every later one,",
                "take the earliest entry with capacity, and move down only when one is",
                "dry, dropped for the run, or already failed this run — never re-send a",
                "question to a scout that already failed.", ""]
        for name, e in zip(sc_names, sc_chain):
            sc = reg[e["id"]]
            pin = f", pinned `{e['model']}`" if e.get("model") else ""
            out += [f"## `{name}` — {sc['label']}{pin}", "",
                    f"- harness `{sc['harness']}`, vendor `{vendor_of(sc, e)}`",
                    quota_line(sc), *quota_failure_lines(sc),
                    "- **Read-only.** Never edits, creates or deletes a file, never",
                    "  commits, never runs a command that mutates the repo or the working",
                    "  tree. If the answer needs a change, it reports that instead of",
                    "  making it.",
                    "- **Bounded answer.** Paths with line ranges, short excerpts and the",
                    "  shape of the answer — never a file dump.",
                    "- **Says what it did not find.** It says so plainly rather than",
                    "  guessing; a confident wrong answer is worse than \"not found\",",
                    "  because you cannot tell the two apart."]
            if sc.get("silent_model_failure"):
                out.append("- **Fails silently on a bad model.** A wrong pin returns an empty "
                           "transcript with no error, which reads as \"found nothing\" rather than "
                           "\"misconfigured\" — check the pin before trusting an empty scout "
                           "report, and do not re-send the same question.")
            if sc.get("prompt_delivery") == "none":
                # The same hazard as the reviewer bullet above, restated for the
                # scout's contract. Rendered per entry: a BACKUP that never
                # receives the contract is exactly as dangerous as a primary.
                out.append("- **Does not receive its sub-agent prompt.** This harness never "
                           "delivers spec instructions, so the scout sees ONLY the text you send "
                           "in `args.input`. Every scout dispatch must therefore carry the whole "
                           "contract itself: READ-ONLY — never edit, create or delete a file, "
                           "never commit, never run a command that mutates the repo or working "
                           "tree, and report a needed change rather than making it — and a "
                           "BOUNDED summary: paths with line ranges and short excerpts, never a "
                           "file dump, and an explicit \"not found\" instead of a guess. Do not "
                           "assume it knows the read-only rule.")
            out.append("")
    # The integrator chain, only when one is installed. Same chain rule and
    # same per-entry voice as the scouts above, plus the two contracts that
    # make the role safe to hand the orchestrator's git plumbing to: a BOUNDED
    # result, and no merge decision. The concurrency rule is restated because
    # getting it wrong produces flaky failures that look like real bugs.
    ig_chain = chain(plan, "integrator")
    if ig_chain:
        ig_names = role_names(plan, "integrator")
        listed = [f"`{n}` ({reg[e['id']]['label']})" for n, e in zip(ig_names, ig_chain)]
        if len(listed) == 1:
            order = f"{listed[0]} is the only integrator in this roster."
        else:
            verb = "backs it up" if len(listed) == 2 else "back it up"
            order = (f"{listed[0]} is the primary; {', '.join(listed[1:])} "
                     f"{verb}, in that order.")
        out += ["## Integrators — git and gate plumbing", "",
                order, "",
                "Send `integrator` the git and gate plumbing you would otherwise do",
                "yourself: creating and removing worktrees, running the gate commands,",
                "collecting diffs and producing the combined diff text for a review,",
                "and reporting commit SHAs and branch state. It answers with a BOUNDED",
                "result — the verdict, the exact SHAs and branch names, and only the",
                "FAILING gate output, never a full diff, log or passing log; an",
                "integrator that pastes a full diff back has failed its purpose.",
                "Take its result instead of re-running the plumbing.",
                "",
                "**The merge decision is yours, never the integrator's.** It never",
                "merges into a protected branch, never pushes, and never opens or",
                "merges a PR. It may merge task branches into an integration branch",
                "only when you explicitly tell it to, and it reports what it did.",
                "",
                "**One integrator at a time.** Never dispatch two integrators against",
                "one repo concurrently: the integration suite must run in exactly ONE",
                "place at a time, and parallel runs against one database corrupt each",
                "other's state and surface as flaky failures that look like real bugs.",
                "An integrator must not run the integration suite while it believes",
                "another is running it.",
                "",
                "Same chain rule as the reviewers: check `og stats` before the first",
                "integrator dispatch and `og stats --agent <id> --json` before every",
                "later one, take the earliest entry with capacity, and move down only",
                "when one is dry, dropped for the run, or already failed this run —",
                "never re-send a dispatch to an integrator that already failed.", ""]
        for name, e in zip(ig_names, ig_chain):
            ig = reg[e["id"]]
            pin = f", pinned `{e['model']}`" if e.get("model") else ""
            out += [f"## `{name}` — {ig['label']}{pin}", "",
                    f"- harness `{ig['harness']}`, vendor `{vendor_of(ig, e)}`",
                    quota_line(ig), *quota_failure_lines(ig),
                    "- **Bounded result.** The verdict, the SHAs and branch names, and",
                    "  only the FAILING gate output — never a full diff, log or passing",
                    "  log.",
                    "- **Never decides a merge.** It never merges into a protected",
                    "  branch, never pushes, never opens or merges a PR. It merges task",
                    "  branches into an integration branch only when explicitly told to,",
                    "  and reports what it did.",
                    "- **One at a time.** Never run the integration suite while another",
                    "  integrator may be running it; it runs in exactly one place."]
            if ig.get("silent_model_failure"):
                out.append("- **Fails silently on a bad model.** A wrong pin returns an empty "
                           "transcript with no error, which reads as \"nothing to report\" rather "
                           "than \"misconfigured\" — check the pin before trusting an empty "
                           "integrator report, and do not re-send the same dispatch.")
            if ig.get("prompt_delivery") == "none":
                # The same hazard as the scout bullet above, restated for the
                # integrator's contract. Rendered per entry: a BACKUP that never
                # receives the contract is exactly as dangerous as a primary.
                out.append("- **Does not receive its sub-agent prompt.** This harness never "
                           "delivers spec instructions, so the integrator sees ONLY the text "
                           "you send in `args.input`. Every integrator dispatch must therefore "
                           "carry the whole contract itself: git and gate plumbing on your "
                           "behalf — worktrees, the gate commands, the combined diff for review, "
                           "commit SHAs and branch state — returning a BOUNDED result (the "
                           "verdict, the SHAs, and only the FAILING gate output, never a full "
                           "diff, log or passing log); NEVER merging into a protected branch, "
                           "never pushing, never opening or merging a PR, never deciding whether "
                           "a merge is allowed; and never running the integration suite while "
                           "another integrator may be running it. Do not assume it knows any of "
                           "this.")
            out.append("")
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
    """The worker -> harness-id table the roster skill's preflight uses.

    Generated from the plan rather than frozen into the prompt: the mapping
    must match the roster the user actually chose, and a hardcoded table
    silently drifts the moment a coder is added, dropped or renamed.
    """
    reg = agents_by_id()
    rows = [f"    `{name}` -> `{reg[e['id']]['harness']}`"
            for r in SPEC_ROLES
            for name, e in zip(role_names(plan, r.key), chain(plan, r.key))]
    return "\n".join(rows)


def zen_preflight_note(name: str) -> str:
    """The Zen free-tier preflight, as roster-skill prose.

    Moved out of the prompt, where it cost ~570 shell-quoted bytes inline: it
    is a procedure consulted while already dispatching, not a fact that changes
    a decision made before reading anything. Generated rather than frozen so it
    names the worker this plan actually wires the harness to.
    """
    return (
        f"### Zen model preflight (once per run, only if dispatching `{name}`)\n\n"
        "A CHECK, not a choice: `args.model` replaces a verified free pin with a\n"
        "guess, which is how a paid model hits OpenCode's \"No payment method\" wall.\n"
        "Call `sys_list_models` once. Pinned id listed, or query failed -> dispatch\n"
        "with no `args.model`, say nothing. Gone (Zen rotates its lineup) -> pick the\n"
        "strongest replacement ending in `-free`, pass it as `args.model` this run\n"
        "only, and tell the human the pin needs updating. A non-`-free` id is a\n"
        "failed dispatch, not a slower one.")


def scout_note(plan: dict) -> str:
    """The scout pointer for the prompt, or "" when no scout is installed.

    Only the part the orchestrator must know BEFORE it decides to read
    anything: the role exists, it is read-only, and sending it the reading is
    cheaper than doing it here. The chain, the failover rule and the full
    read-only contract live in the generated `roster` skill, which loads from
    disk and costs the command line nothing. The existing tension is kept — a
    quick look at a file or two to scope a dispatch is still fine, because a
    scout round trip is not free — rather than telling the brain to delegate
    every read, which would cost more round trips than it saves.

    Trailing newline when present so the paragraph keeps its blank line before
    the next section; empty otherwise, which leaves the template byte-identical
    for a plan with no scout.
    """
    if not chain(plan, "scout"):
        return ""
    return ("  Reading beyond that quick look — locating code, `grep`-style searches,\n"
            "  git state — goes to `scout`: read-only, never edits or commits, answers\n"
            "  with a bounded summary. Take its answer rather than reading the files\n"
            "  yourself.\n")


def integrator_note(plan: dict) -> str:
    """The integrator pointer for the prompt, or "" when none is installed.

    Only what the orchestrator must know BEFORE it decides anything: that the
    role exists, that it does the git/worktree/gate plumbing, and — the clause
    that must not live behind an on-demand read — that the merge decision stays
    with the orchestrator. An orchestrator that had to read a skill to learn it
    could sail past a gate it is not allowed to delegate. Everything else (the
    chain, the failover rule, the bounded-output and one-integrator-at-a-time
    contracts) is in the generated `roster` skill, which costs no prompt bytes.

    Trailing newline when present so the paragraph keeps its blank line before
    the next section; empty otherwise, which leaves the template byte-identical
    for a plan with no integrator.
    """
    if not chain(plan, "integrator"):
        return ""
    return ("  Git and gate plumbing — worktrees, the gate commands, the combined\n"
            "  diff for review — goes to `integrator`, which returns a BOUNDED\n"
            "  result, never a full diff or log. It NEVER decides a merge: that\n"
            "  decision stays with you.\n")


def render_orchestrator(plan: dict) -> str:
    reg = agents_by_id()
    s = tmpl("orchestrator.yaml.tmpl")
    # Every reviewer in the chain, not just the primary: tools.agents is the
    # only dispatch surface, so a backup omitted here is unreachable and the
    # failover the roster skill promises cannot happen. EACH backup grows the
    # rendered prompt by an agent-list line here plus its roster lines in
    # {{ROSTER_BULLETS}}, so headroom against PROMPT_CEILING shrinks per entry.
    # Run --dry-run for the current shell-quoted size and the bytes left; a
    # hardcoded figure here went stale the moment the prompt changed (it read
    # 1,401 while the measured roster sat at 1,004). validate() refuses an argv
    # chain over the ceiling, so the hazard stays guarded -- but a new chain
    # entry spends bytes the prompt has to have.
    agent_list = "\n".join(f"    - {name}" for r in SPEC_ROLES
                           for name in role_names(plan, r.key))
    subs = {
        "{{AGENT_NAME}}": plan["agent_name"],
        "{{ORCHESTRATOR_HARNESS}}": reg[primary(plan, "orchestrator")["id"]]["harness"],
        "{{ROSTER_BULLETS}}": render_roster(plan),
        "{{VENDOR_MAP}}": render_vendor_map(plan),
        "{{SCOUT_NOTE}}": scout_note(plan),
        "{{INTEGRATOR_NOTE}}": integrator_note(plan),
        "{{AGENT_LIST}}": agent_list,
        "{{MAX_DISPATCHES}}": str(plan["max_dispatches"]),
        "{{AGENT_COUNT_WORD}}": _count_word(sum(len(chain(plan, r.key))
                                                 for r in SPEC_ROLES)),
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


def render_coder(plan: dict, c: dict, name: str | None = None) -> str:
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
        "{{NAME}}": name or worker_name(c["id"]),
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


def render_reviewer(plan: dict, entry: dict | None = None, name: str = "reviewer") -> str:
    """One reviewer spec.

    Defaults to the chain's primary as `reviewer` (the name the orchestrator
    prompt, the cross-review skill and existing session history use); each
    backup is rendered under its own name with its own model pin, so the
    failover target carries the same review contract and harness as the head.
    """
    reg = agents_by_id()
    entry = primary(plan, "reviewer") if entry is None else entry
    a = reg[entry["id"]]
    s = tmpl("reviewer.yaml.tmpl")
    for k, v in {
        "{{NAME}}": name,
        "{{LABEL}}": a["label"],
        "{{HARNESS}}": a["harness"],
        "{{ORCHESTRATOR}}": plan["agent_name"],
        "{{ACCOUNT_NOTE}}": account_note(plan, a, "reviewer"),
        "{{MODEL_BLOCK}}": model_block(entry.get("model")),
    }.items():
        s = s.replace(k, v)
    return s


def account_note(plan: dict, a: dict, what: str) -> str:
    """The `# Runs on a SEPARATE account` comment for an agent that has one.

    Shared by every worker spec that can be given its own account, so the
    reviewer's and the scout's notes cannot drift apart. `what` names the role
    in the prose, so the generated sentence still reads as the role it lands in.
    """
    acct = (plan.get("accounts") or {}).get(a["id"])
    if not acct:
        return ""
    env = (a.get("multi_account") or {}).get("env", "CONFIG_DIR")
    return (f"# Runs on a SEPARATE account: the server is launched with\n"
            f"# {env}={acct}, so this {what} is independent of the\n"
            f"# account your interactive sessions use.\n")


def render_scout(plan: dict, entry: dict | None = None, name: str = "scout") -> str:
    """One scout spec.

    Same chain shape as the reviewer: the primary keeps the stable `scout` name
    and each backup appends its position. The one thing it does differently is
    `permission_mode`: an acp-user CLI relays every tool call as an approval
    request, and `auto` parks a human card for anything no policy opines on —
    a read-only scout runs `cat`/`grep`/`git`, so it would stall on its first
    read. Same reasoning as coder.yaml.tmpl's bypass; a native harness keeps
    the reviewer's `auto`.
    """
    reg = agents_by_id()
    entry = primary(plan, "scout") if entry is None else entry
    a = reg[entry["id"]]
    perm = ("    permission_mode: bypassPermissions" if a["kind"] == "acp-user"
            else "    permission_mode: auto")
    s = tmpl("scout.yaml.tmpl")
    for k, v in {
        "{{NAME}}": name,
        "{{LABEL}}": a["label"],
        "{{HARNESS}}": a["harness"],
        "{{ORCHESTRATOR}}": plan["agent_name"],
        "{{ACCOUNT_NOTE}}": account_note(plan, a, "scout"),
        "{{MODEL_BLOCK}}": model_block(entry.get("model")),
        "{{PERMISSION_MODE_BLOCK}}": perm,
    }.items():
        s = s.replace(k, v)
    return s


def render_integrator(plan: dict, entry: dict | None = None, name: str = "integrator") -> str:
    """One integrator spec.

    Same chain shape as the reviewer and the scout: the primary keeps the stable
    `integrator` name and each backup appends its position. It writes, so it
    keeps the coder's permission rule rather than the scout's read-only one:
    an acp-user CLI relays every tool call as an approval request and `auto`
    parks a human card for anything no policy opines on, which would stall a
    worker whose first act is `git worktree add`. A native harness keeps the
    reviewer's `auto`.
    """
    reg = agents_by_id()
    entry = primary(plan, "integrator") if entry is None else entry
    a = reg[entry["id"]]
    perm = ("    permission_mode: bypassPermissions" if a["kind"] == "acp-user"
            else "    permission_mode: auto")
    s = tmpl("integrator.yaml.tmpl")
    for k, v in {
        "{{NAME}}": name,
        "{{LABEL}}": a["label"],
        "{{HARNESS}}": a["harness"],
        "{{ORCHESTRATOR}}": plan["agent_name"],
        "{{ACCOUNT_NOTE}}": account_note(plan, a, "integrator"),
        "{{MODEL_BLOCK}}": model_block(entry.get("model")),
        "{{PERMISSION_MODE_BLOCK}}": perm,
    }.items():
        s = s.replace(k, v)
    return s


# role.template -> the renderer for it. Keyed by the template string the ROLES
# row names, so a role added to the table without a renderer fails loudly on
# the first apply instead of writing an empty spec directory.
SPEC_RENDERERS = {
    "coder.yaml.tmpl": render_coder,
    "reviewer.yaml.tmpl": render_reviewer,
    "scout.yaml.tmpl": render_scout,
    "integrator.yaml.tmpl": render_integrator,
}


def render_spec(plan: dict, role: Role, entry: dict, name: str) -> str:
    """Render one sub-agent spec through the role's own renderer."""
    return SPEC_RENDERERS[role.template](plan, entry, name)


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------
# A `{shim:<name>}` token inside a registry `acp_command` expands to the
# absolute path of the helper script write_shims() materializes. The name is
# everything up to the closing brace; names are installer-controlled slugs.
SHIM_TOKEN = re.compile(r"\{shim:([^}]+)\}")


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

    Newer rows name the variable inline as `model.env_var` (e.g. a freebuff/blink
    pin must reach `BLINK_MODEL`). Read it from there first, then the legacy
    top-level `model_env` -- never hardcode an agent name.

    A row may also carry `{shim:<name>}` inside `acp_command`. The token
    expands to the absolute path of a helper script materialized by
    write_shims() before this is ever rendered (OMNI/"shims"/<name>). This
    exists for bridges like cmd-acp that only accept their model and write
    permission via `session/set_config_option`, which Omnigent never sends:
    the bridge honours CMD_BIN to pick which binary it spawns, so the shim
    appends the flags the bridge will not otherwise receive. Expansion is a
    shlex.quoted substitution (a no-op when the path has no special
    characters), so it composes with the env-var prefix below, and the
    executor exec's the argv with no shell -- no $VAR may survive unexpanded.
    """
    cmd = SHIM_TOKEN.sub(lambda mt: shlex.quote(str(OMNI / "shims" / mt.group(1))), agent["acp_command"])
    var = (agent.get("model") or {}).get("env_var") or agent.get("model_env")
    if var and model:
        return f"env {var}={shlex.quote(model)} {cmd}"
    return cmd


def write_shims(plan: dict) -> list:
    """Write helper scripts for selected agents whose registry row has a `shim`
    block ({"name": ..., "script": ...}) to OMNI/"shims"/<name>, mode 0o755.

    Covers the same selected set patch_global_config renders rows for --
    plan["coders"] plus every reviewer in the chain -- so a `{shim:<name>}`
    token in any rendered command line already exists on disk. Rerunnable: a
    script whose on-disk content AND mode already match is left alone and
    reported as no change. Converges both: a script with right content but
    wrong mode gets its mode repaired (and reported), because the bridge exec's
    this path directly and a non-executable shim fails at launch with nothing
    pointing back at the installer. Returns human-readable entries in
    patch_global_config's style.
    """
    reg = agents_by_id()
    changed = []
    for r in SPEC_ROLES:
        for c in chain(plan, r.key):
            shim = reg[c["id"]].get("shim")
            if not shim:
                continue
            dest = OMNI / "shims" / shim["name"]
            if dest.is_file() and dest.read_text() == shim["script"]:
                if dest.stat().st_mode & 0o777 == 0o755:
                    continue
                dest.chmod(0o755)
                changed.append(f"shims[{shim['name']}] mode -> 0o755 ({dest})")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(shim["script"])
            dest.chmod(0o755)
            changed.append(f"shims[{shim['name']}] = {dest}")
    return changed


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

    # acp.agents entries for every acp-user agent we selected -- every reviewer
    # in the chain included. A missing row does not fail loudly: Omnigent
    # resolves an unknown acp:<slug> to the FIRST configured row, so an ACP
    # reviewer with no row of its own would silently run as whichever coder is
    # listed first.
    want = [c for r in SPEC_ROLES for c in chain(plan, r.key)
            if reg[c["id"]]["kind"] == "acp-user"]
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
    # ANY orchestrator in the chain, not just the primary: the override applies
    # to every OpenCode session og launches, and a failover to an OpenCode brain
    # needs `question` for its plan gate too.
    if not is_coder or any(o["id"] == "opencode" for o in chain(plan, "orchestrator")):
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
    # Every id with its own account gets an env line, not just the primary
    # reviewer. Interactive now collects `accounts` for every id in every chain,
    # but this only ever emitted the head's -- so a BACKUP reviewer configured
    # with its own account silently ran on the primary's (or the user's
    # interactive) login. Same failure shape as the ceiling gate: the config
    # looked right and the backup ran wrong.
    accts = account_entries(plan)
    if accts:
        lines += ["", "# Each agent below runs on this account, separate from your",
                  "# interactive login for that agent."]
        lines += [f"{var}={acct}" for _aid, var, acct in accts]
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
    # Canonicalize on the way in: an old-shape state file (a bare
    # "orchestrator"/"reviewer" singleton) installs exactly as it always did,
    # and what gets written back to og-install.json is the new chain shape.
    normalize_plan(plan)
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
    keep = {name for r in SPEC_ROLES for name in role_names(plan, r.key)}
    for d in sorted((bundle / "agents").iterdir()):
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d)
            say(f"{C['dim']}  pruned stale worker: {d.name}{C['x']}")
    for r in SPEC_ROLES:
        for name, e in zip(role_names(plan, r.key), chain(plan, r.key)):
            d = bundle / "agents" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "config.yaml").write_text(render_spec(plan, r, e, name))

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
    # Shims first: patch_global_config renders command lines that may
    # reference OMNI/"shims"/<name>, so the scripts must exist before that.
    shim_changed = write_shims(plan)
    changed = patch_global_config(plan)
    changed = shim_changed + changed
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
    ok(f"orchestrator  {plan['agent_name']} ({reg[primary(plan, 'orchestrator')['id']]['label']})")
    for c in plan["coders"]:
        pin = f" → {c['model']}" if c.get("model") else ""
        ok(f"coder #{c['priority']}      {worker_name(c['id'])} ({reg[c['id']]['label']}){pin}")
    for i, (name, e) in enumerate(zip(reviewer_names(plan), chain(plan, "reviewer"))):
        pin = f" → {e['model']}" if e.get("model") else ""
        if i == 0:
            ok(f"reviewer      {reg[e['id']]['label']}{pin}")
        else:
            ok(f"reviewer #{e['priority']}   {name} ({reg[e['id']]['label']}){pin}")
    for i, (name, e) in enumerate(zip(role_names(plan, "scout"), chain(plan, "scout"))):
        pin = f" → {e['model']}" if e.get("model") else ""
        if i == 0:
            ok(f"scout         {reg[e['id']]['label']}{pin}")
        else:
            ok(f"scout #{e['priority']}      {name} ({reg[e['id']]['label']}){pin}")
    for i, (name, e) in enumerate(zip(role_names(plan, "integrator"),
                                      chain(plan, "integrator"))):
        pin = f" → {e['model']}" if e.get("model") else ""
        if i == 0:
            ok(f"integrator    {reg[e['id']]['label']}{pin}")
        else:
            ok(f"integrator #{e['priority']}   {name} ({reg[e['id']]['label']}){pin}")
    for line in changed:
        ok(f"config.yaml   {line}")
    if ocd:
        wired = ", ".join(sorted(opencode_worker_config().get("mcp", {}))) or "none found"
        ok(f"opencode      {ocd/'opencode.json'} (question tool off; code-intel MCP: {wired})")
    elif any(o["id"] == "opencode" for o in chain(plan, "orchestrator")):
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
    # Every agent in the roster, chains included: a backup that is not logged in
    # is only discovered when the primary goes dry and the failover dies too.
    login_agents = [reg[e["id"]] for r in ROLES for e in chain(plan, r.key)]
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


def _sandbox_real_home() -> str | None:
    """The invoking user's real home when $HOME is sandboxed, else None.

    The AGENTS.md sandbox recipe runs the installer with HOME pointed at a
    temp dir; the .pth target (omnigent's site-packages) is OUTSIDE that
    sandbox, so keying the skip on OMNIGENT_HOME alone is wrong — a user may
    legitimately set OMNIGENT_HOME permanently and still need the .pth. Only
    a $HOME that differs from the invoking user's real home counts.
    """
    home = os.environ.get("HOME")
    if not home:
        return None
    try:
        import pwd
        real = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return None  # cannot tell (e.g. non-posix): assume not sandboxed
    if os.path.realpath(home) != os.path.realpath(real):
        return real
    return None


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
    real_home = _sandbox_real_home()
    if real_home is not None:
        # Sandboxed run (AGENTS.md recipe): the .pth lives outside
        # $OMNIGENT_HOME, so writing it would repoint the LIVE policy stack
        # at a temp policies dir that is deleted afterwards — silently
        # unloading the user's merge-gate policy. Skip the write and say so,
        # naming the live path left untouched.
        warn(f"sandboxed HOME ({os.environ.get('HOME')} != {real_home}): "
             f"skipping .pth write — live policy path {pth} left untouched")
        return
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
    # The static per-agent model list, offered under every role that can pin one.
    model_choices = {a["id"]: (a.get("model") or {}).get("choices")
                     for a in REGISTRY["agents"]
                     if (a.get("model") or {}).get("choices")}
    # One question per role, in table order. Orchestrator and reviewer are
    # ordered chains, exactly like coders: the entries after the first are the
    # failover used when the one above is out of quota. A single id is still
    # accepted (it normalizes to a one-element chain), so a consumer that
    # answers with one value does not have to change. The per-role UNVERIFIED
    # caveat rides the question: AGENTS.md Part 1 has the AI installer drive its
    # conversation from this output, and it must never offer a role without the
    # warning that applies to it.
    role_questions = []
    for r in ROLES:
        q = {"key": r.key, "type": "ordered_multi",
             "choices": [a["id"] for a in REGISTRY["agents"]
                         if a["id"] in found and r.role in a["roles"]],
             "notes": role_notes(r.role),
             "ask": r.ask}
        if r.spec:
            # The same static model list under every role that pins one, so an
            # AI installer can answer for each entry of the chain.
            q["per_item"] = {
                "model": "Model id to pin. REQUIRED for agents where "
                         "registry.model.required is true.",
                "choices": model_choices}
        role_questions.append(q)
    say(json.dumps({
        "detected": {k: reg[k]["label"] for k in found},
        "state_file": str(STATE),
        "current": load_state() or None,
        "prompt_ceiling_bytes": PROMPT_CEILING,
        "questions": [
            {"key": "agent_name", "type": "string", "default": "dev-lead",
             "ask": "What should the orchestrator bundle be called?"},
            *role_questions,
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
    # Both roles render as a chain in order, primary first. The `(primary)`
    # tag only appears when there IS a chain, so a single-entry install reads
    # exactly as it always has.
    orch = chain(state, "orchestrator")
    revs = chain(state, "reviewer")
    scs = chain(state, "scout")
    say(f"{C['b']}orchestrator{C['x']}  {state['agent_name']} "
        f"({reg[orch[0]['id']]['label']}){' (primary)' if len(orch) > 1 else ''}")
    for e in orch[1:]:
        say(f"{C['b']}  backup #{e['priority']}{C['x']}  {reg[e['id']]['label']} "
            f"{C['dim']}— failover for the orchestrator{C['x']}")
    for c in state["coders"]:
        live = "" if c["id"] in found else f" {C['r']}(CLI missing){C['x']}"
        pin = f" → {c['model']}" if c.get("model") else ""
        say(f"{C['b']}coder #{c['priority']}{C['x']}      "
            f"{worker_name(c['id'])} ({reg[c['id']]['label']}){pin}{live}")
    for i, e in enumerate(revs):
        live = "" if e["id"] in found else f" {C['r']}(CLI missing){C['x']}"
        pin = f" → {e['model']}" if e.get("model") else ""
        if i == 0:
            tail = " (primary)" if len(revs) > 1 else ""
            say(f"{C['b']}reviewer{C['x']}      {reg[e['id']]['label']}{pin}{tail}{live}")
        else:
            say(f"{C['b']}  backup #{e['priority']}{C['x']}  {reg[e['id']]['label']}"
                f"{pin}{live}")
    for i, e in enumerate(scs):
        live = "" if e["id"] in found else f" {C['r']}(CLI missing){C['x']}"
        pin = f" → {e['model']}" if e.get("model") else ""
        if i == 0:
            tail = " (primary)" if len(scs) > 1 else ""
            say(f"{C['b']}scout{C['x']}         {reg[e['id']]['label']}{pin}{tail}{live}")
        else:
            say(f"{C['b']}scout #{e['priority']}{C['x']}     {reg[e['id']]['label']}"
                f"{pin}{live}")
    igs = chain(state, "integrator")
    for i, e in enumerate(igs):
        live = "" if e["id"] in found else f" {C['r']}(CLI missing){C['x']}"
        pin = f" → {e['model']}" if e.get("model") else ""
        if i == 0:
            tail = " (primary)" if len(igs) > 1 else ""
            say(f"{C['b']}integrator{C['x']}    {reg[e['id']]['label']}{pin}{tail}{live}")
        else:
            say(f"{C['b']}integrator #{e['priority']}{C['x']} {reg[e['id']]['label']}"
                f"{pin}{live}")
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
        # Every role to the ordered-chain shape, so an old singleton plan and a
        # new one take the identical path from here on. `priority` still comes
        # from array order, exactly as the coder loop here always set it.
        return apply(normalize_plan(plan), dry_run=args.dry_run)

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
