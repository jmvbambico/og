#!/usr/bin/env python3
"""Per-instance identity for agents that appear more than once in a roster.

WHY THIS EXISTS (2026-10-20): the same harness can legitimately be installed
twice with different model pins — an OpenCode coder on Zen's free tier (day
capped) and a second OpenCode coder on Command Code's paid DeepSeek endpoint.
State used to be keyed by the registry agent id, so the two shared one quota
record and one dry mark: marking the free instance dry also discarded the paid
one. The first instance keeps the id and worker name it has always had, so
every existing install and saved plan reads exactly as before; only the
ADDITIONAL instances need a derived identity.

Stdlib only, deliberately: `og stats` and `og_start_probe.py` must import this
without PyYAML, the same reason `og_stats` cannot import `og_install`.

Instance id (the key in og-quota.json, `--mark`, `--agent`, and probe output):
    opencode                      first OpenCode
    opencode@cmdcode              second, from a commandcode/... pin
    opencode@deepseek             second, from a deepseek/... pin
    opencode-2                    second with no provider in the pin
    opencode@cmdcode-2            a third whose provider suffix collided

Worker name (the generated spec name, sys_session_send agent enum, and title
prefix):
    coder_zen                     first OpenCode (registry `worker` override)
    coder_zen_cmdcode             second, provider-derived
    coder_zen_2                   second with no provider in the pin

The provider suffix is the pin's provider prefix with a small alias map
(commandcode -> cmdcode) and everything outside [a-z0-9-] collapsed to '-',
so a name stays safe as a spec directory and an agent enum value. When the pin
names no provider, the ordinal is the fallback. Collisions are resolved by
appending -2, -3, ... to the suffix, never by renumbering the first instance.
"""
from __future__ import annotations

import re

# Pin providers that are bridges/aggregators with a short, familiar name. Two
# OpenCode instances on Zen would both otherwise read `opencode`; a Command
# Code pin reads `cmdcode`, the id the rest of the repo already uses for it.
PROVIDER_ALIASES = {"commandcode": "cmdcode"}


def provider_slug(model: str | None) -> str:
    """The provider portion of a model pin, safe for a name. '' when absent."""
    if not model or "/" not in str(model):
        return ""
    raw = str(model).split("/", 1)[0].strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return PROVIDER_ALIASES.get(slug, slug)


def _occurrence_ids(entries: list[dict], *, sep: str, first,
                    numeric_sep: str | None = None, key=None) -> list[str]:
    """Build one identity per entry, first occurrence unchanged.

    `first(entry)` names the first instance; later occurrences append `sep` +
    the provider slug, or `numeric_sep` + the ordinal when the pin names none.
    A candidate that is already taken (two pins from the same provider) gets
    `-2`, `-3`, ... `key` is what "the same agent" means; it defaults to the
    registry id.
    """
    key = key or (lambda e: e["id"])
    numeric_sep = sep if numeric_sep is None else numeric_sep
    counts: dict[str, int] = {}
    used: set[str] = set()
    out: list[str] = []
    for entry in entries:
        aid = key(entry)
        counts[aid] = counts.get(aid, 0) + 1
        ordinal = counts[aid]
        if ordinal == 1:
            candidate = first(entry)
        else:
            slug = provider_slug(entry.get("model"))
            joiner = sep if slug else numeric_sep
            base = f"{first(entry)}{joiner}{slug or ordinal}"
            candidate = base
            n = 2
            while candidate in used:
                candidate = f"{base}-{n}"
                n += 1
        used.add(candidate)
        out.append(candidate)
    return out


def instance_ids(entries: list[dict]) -> list[str]:
    """Per-entry state key. First instance keeps the bare registry id."""
    return _occurrence_ids(entries, sep="@", numeric_sep="-",
                           first=lambda e: e["id"])


def worker_names(base_names: list[str], entries: list[dict]) -> list[str]:
    """Per-entry worker name. First instance keeps its established name.

    `base_names` is the registry's `worker` override (or `coder_<id>`) for each
    entry, in the same order, because only the registry knows whether a name is
    already load-bearing for an existing session history.
    """
    pairs = [{"id": e["id"], "model": e.get("model"), "base": base}
             for base, e in zip(base_names, entries)]
    return _occurrence_ids(pairs, sep="_", first=lambda e: e["base"])
