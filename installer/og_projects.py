#!/usr/bin/env python3
"""File og's root sessions into this install's Omnigent project folder.

Omnigent's sidebar groups sessions into "Projects" — project folders — and every
session grown under a fresh install was born unfiled: the user who has a
`hivemind` project sees three sessions in it and forty in the flat list. This
module closes that gap for the one project og owns, the orchestrator bundle
named in the install plan.

API facts, read from a live server's own /openapi.json:

* Membership in a LEGACY LABEL-PROJECT is the session label `omni_project`, and
  the label's VALUE is the project's name. `GET /v1/sessions?project=<name>`
  selects it, and `GET /v1/sessions/projects` lists it beside first-class
  projects. A label-project is a name and nothing else — no id, no icon — which
  is exactly what an install plan has to offer, so this routes through the LABEL
  and never through `/v1/projects`. Claiming a first-class project would mean
  owning an id and an icon in a table somebody else curates; out of scope.
* The label is written through `PATCH /v1/sessions/{id}` with an
  `UpdateSessionRequest` whose `labels` field is a `{str: str}` map. It is a
  WHOLE map: a PATCH replaces it, so a blind write would erase every other label
  on the session. Hence the read-merge-write in `ProjectFiler.file`.
* `GET /v1/sessions/{id}/labels` is the authoritative read (a
  `SessionLabelsResponse`, `{"id": …, "labels": {…}}`). The LISTING row's
  `labels` is a different, array-shaped field and is not what the PATCH takes, so
  it is not read here.

stdlib only; Python 3.10+.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Optional

# The session label that IS membership in a legacy label-project, whose value is
# the project's name. Not a knob: it is the server's own key, and the whole
# sidebar query is built on it.
PROJECT_LABEL = "omni_project"


class ProjectFilerError(Exception):
    """One filing call failed — a transport error or an unreadable reply.

    Raised only from the private helpers; `ProjectFiler.file` folds it into its
    return value, because filing is a convenience and its caller must not have
    to catch for it (see the module docstring of og_herdr's `_file_under_project`).
    """


class ProjectFiler:
    """Files a session into a project folder by setting its `omni_project` label.

    `opener` is the transport seam, spelled exactly like `SessionWatcher`'s: it
    defaults to `urllib.request.urlopen` and is called as `opener(request)`,
    returning a context manager whose `read()` yields the body. Tests inject a
    fake and never touch a server.
    """

    def __init__(self, base_url: str, token: Optional[str] = None,
                 opener: Optional[Any] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def file(self, session_id: str, project: str) -> tuple:
        """Put `session_id` in the `project` folder.

        Returns `(status, detail)`:

        * `("filed", project)`  — the label was written.
        * `("kept", note)`      — the session is ALREADY filed under some
          project, including a different one the user chose; it is left exactly
          as it was. Re-filing is not this module's call to make.
        * `("failed", note)`    — the labels could not be read, or the PATCH
          failed. The note says why.

        It never raises: a session's pane is worth more than this bookkeeping, so
        a dead server becomes a reported outcome, not an exception the caller has
        to catch. The read is what makes the write safe — a PATCH built from the
        server's own map is the only version that cannot drop another label — so
        an unreadable read is a failure and does NOT fall back to a blind write.
        """
        try:
            labels = self._read_labels(session_id)
        except ProjectFilerError as exc:
            return ("failed", str(exc))
        existing = labels.get(PROJECT_LABEL)
        if isinstance(existing, str) and existing:
            return ("kept", f"already in the '{existing}' project")
        labels[PROJECT_LABEL] = project
        try:
            self._patch_labels(session_id, labels)
        except ProjectFilerError as exc:
            return ("failed", str(exc))
        return ("filed", project)

    def _read_labels(self, session_id: str) -> dict:
        """The session's current labels as a `{str: str}` map, or raise.

        Every entry is kept only when both key and value are strings: the PATCH
        takes nothing else, and carrying a non-string through would turn the
        merge into a request the server refuses — for every session, not just
        the malformed one.
        """
        url = (f"{self.base_url}/v1/sessions/"
               f"{urllib.parse.quote(session_id)}/labels")
        body = self._call(url, "GET")
        labels = body.get("labels") if isinstance(body, dict) else None
        if not isinstance(labels, dict):
            raise ProjectFilerError(
                f"could not read the labels of session {session_id}")
        return {k: v for k, v in labels.items()
                if isinstance(k, str) and isinstance(v, str)}

    def _patch_labels(self, session_id: str, labels: dict) -> None:
        url = f"{self.base_url}/v1/sessions/{urllib.parse.quote(session_id)}"
        self._call(url, "PATCH", {"labels": labels})

    def _call(self, url: str, method: str, payload: Optional[dict] = None) -> Any:
        """One request; JSON body in when given, JSON document out (or None).

        `(OSError, ValueError)` is the whole reachable failure surface of one
        request: OSError covers URLError / HTTPError / socket.timeout, ValueError
        covers a body that is not JSON and a non-UTF-8 decode. A bug in this
        module raises something else and is deliberately not absorbed here.
        """
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with self.opener(request) as resp:
                raw = resp.read()
        except (OSError, ValueError) as exc:
            raise ProjectFilerError(f"{method} {url} failed: {exc}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8")
                              if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError):
            return None
