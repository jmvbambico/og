"""Unit tests for installer/og_audit.py's quota-failure detection.

The live chat.db is 140 MB and machine-local, so every test builds its own
sqlite database in tmp_path and points the module's OMNI at it. The schema
matches what `og audit` reads: conversations + conversation_items with the
type codes T_MESSAGE=1, T_CALL=2, T_RESULT=3, T_RESOURCE=8.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import og_audit as m

T_MESSAGE, T_CALL, T_RESULT, T_RESOURCE = 1, 2, 3, 8


def _blob(b: bytes) -> bytes:
    return b


def _mk_db(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        create table conversations (
            id blob primary key, created_at integer, updated_at integer,
            title text, parent_conversation_id blob, root_conversation_id blob
        );
        create table conversation_items (
            id blob primary key, conversation_id blob, response_id text,
            created_at integer, position integer, data text, search_text text,
            created_by text, workspace_id bigint, type smallint, status smallint
        );
        """
    )
    return con


def _root(con: sqlite3.Connection, title: str, ts: int) -> bytes:
    rid = _blob(bytes(range(1, 17)))
    con.execute(
        "insert into conversations (id, created_at, updated_at, title, root_conversation_id) "
        "values (?, ?, ?, ?, ?)", (rid, ts, ts, title, rid))
    return rid


def _child(con: sqlite3.Connection, root: bytes, title: str, ts: int) -> bytes:
    cid = _blob(bytes(range(17, 33)))
    con.execute(
        "insert into conversations (id, created_at, updated_at, title, "
        "parent_conversation_id, root_conversation_id) values (?, ?, ?, ?, ?, ?)",
        (cid, ts, ts, title, root, root))
    return cid


def _item(con: sqlite3.Connection, cid: bytes, itype: int, data: str, pos: int,
          ts: int) -> None:
    con.execute(
        "insert into conversation_items (conversation_id, type, data, position, created_at) "
        "values (?, ?, ?, ?, ?)", (cid, itype, data, pos, ts))


def _analyse(con, cid):
    return m.analyse(m.items(con, cid))


def test_signature_hit_in_a_result_item(tmp_path, monkeypatch):
    """A Kilo worker's credit failure reaches chat.db as a T_RESULT output, and
    the audit surfaces the Kilo signature with its remedy."""
    monkeypatch.setattr(m, "OMNI", tmp_path)
    (tmp_path / "opencode-native").mkdir()
    db = tmp_path / "chat.db"
    con = _mk_db(db)
    root = _root(con, "root", 1_000)
    cid = _child(con, root, "coder_kilo:task", 1_000)
    _item(con, cid, T_RESULT,
          '{"call_id":"c1","output":"inner executor error: Internal error: Add credits '
          'to continue, or switch to a free model"}', 1, 1_000)
    con.commit()

    a = _analyse(con, cid)
    hints = m.signature_hints(a["items_text"])
    assert any("Add credits to continue" in h for h in hints)
    assert any("og stats --mark" in h for h in hints)
    con.close()


def test_rate_limit_signature_is_log_only(tmp_path, monkeypatch):
    """`Rate limit exceeded` also appears inside source code a worker is merely
    reading, so scanning chat.db for it must NOT fire — only the opencode log
    scan matches it."""
    monkeypatch.setattr(m, "OMNI", tmp_path)
    (tmp_path / "opencode-native").mkdir()
    db = tmp_path / "chat.db"
    con = _mk_db(db)
    root = _root(con, "root", 1_000)
    cid = _child(con, root, "coder_zen:task", 1_000)
    _item(con, cid, T_RESULT,
          '{"call_id":"c1","output":"default: errorMsg = \\"Rate limit exceeded. Please '
          'try again later.\\""}', 1, 1_000)
    con.commit()

    a = _analyse(con, cid)
    assert m.signature_hints(a["items_text"]) == []
    assert m.signature_hints("Rate limit exceeded", log_only=True)
    con.close()


def test_no_false_opencode_attribution_for_a_non_opencode_child(tmp_path, monkeypatch):
    """The OLD bug: rate_limit_hint fired for ANY child with no tool calls when
    an opencode log in the window had a rate-limit line. A Kilo worker that
    never acted must get the Kilo signature from its own text, NOT an
    OpenCode rate-limit attribution."""
    monkeypatch.setattr(m, "OMNI", tmp_path)
    ocdir = tmp_path / "opencode-native" / "deadbeef" / "xdg-data" / "opencode" / "log"
    ocdir.mkdir(parents=True)
    (ocdir / "opencode.log").write_text(
        "stream error ... Rate limit exceeded\n", encoding="utf-8")
    # mtime now so the window check passes
    import os, time
    now = time.time()
    os.utime(ocdir / "opencode.log", (now, now))

    db = tmp_path / "chat.db"
    con = _mk_db(db)
    root = _root(con, "root", int(now))
    cid = _child(con, root, "coder_kilo:task", int(now))
    # Kilo's failure text, no tool calls, no report -> NO TOOL CALLS + Kilo hint
    _item(con, cid, T_RESULT,
          '{"call_id":"c1","output":"inner executor error: Internal error: Add credits '
          'to continue, or switch to a free model"}', 1, int(now))
    con.commit()

    a = _analyse(con, cid)
    label = a["agent"] or "acp"
    assert not label.lower().startswith("opencode")
    # The opencode log exists in the window, but this child is not opencode.
    hint = m.rate_limit_hint(a["first"], label)
    assert hint is None, f"false attribution: {hint}"
    # ...and it still gets the Kilo signature from its own text.
    assert any("Add credits to continue" in h for h in m.signature_hints(a["items_text"]))
    con.close()


def test_no_tool_calls_flag_still_fires(tmp_path, monkeypatch):
    """The pre-existing NO TOOL CALLS flag is preserved: a child with no tool
    calls, no report, and no quota text is flagged as never-acted."""
    monkeypatch.setattr(m, "OMNI", tmp_path)
    (tmp_path / "opencode-native").mkdir()
    db = tmp_path / "chat.db"
    con = _mk_db(db)
    root = _root(con, "root", 1_000)
    cid = _child(con, root, "coder_cline:task", 1_000)
    _item(con, cid, T_MESSAGE, '{"role":"user","content":[]}', 1, 1_000)
    con.commit()

    a = _analyse(con, cid)
    assert not a["calls"]
    assert not a["report"]
    assert a["agent"] is None
    assert m.rate_limit_hint(a["first"], "cline") is None
    assert m.signature_hints(a["items_text"]) == []
    con.close()