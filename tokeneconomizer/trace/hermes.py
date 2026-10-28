"""Hermes Agent (NousResearch/hermes-agent): sessions in one SQLite database, `state.db`.

Hermes keeps every session of a profile in `<HERMES_HOME>/state.db` (WAL mode): a `sessions` row
per session (cwd, model, token totals, `ended_at`) and a `messages` row per message, in OpenAI
chat format -- an assistant row's `tool_calls` is a JSON list of `{id, function: {name,
arguments}}`, and each result is a `role='tool'` row with its `tool_call_id`. A compaction done
in place marks the rows it replaced `active=0, compacted=1`; an Undo/Rewind leaves `active=0,
compacted=0`, which is never read.

A session's "transcript path" here is the string `"<db path>#<session id>"`, so the core's "a
transcript is a path" contract holds; every reader below splits it. The database is opened
read-only (`mode=ro`): the gateway is its only writer.

`hermes_session_to_claude` writes a session as the Claude Code envelope the other readers already
handle, with Hermes' tool names mapped (`hermes_tool`) onto the ones the mechanisms know, so no
mechanism needs a Hermes branch. Stdlib only.
"""
import json
import os
import re
import sqlite3

_CONTENT_JSON_PREFIX = "\x00json:"         # Hermes' marker for structured (list/dict) content
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.M)


def hermes_split(transcript_path):
    """(db path, session id) of a `"<db>#<id>"` transcript path; ("", "") if it is not one."""
    db, sep, sid = str(transcript_path or "").rpartition("#")
    return (db, sid) if sep and db and sid else ("", "")


def _connect(db):
    # Read-only on a WAL database needs the -wal/-shm files the writer keeps (or a writable dir).
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)


def _query(transcript_path, sql, args=()):
    """Rows of `sql` (with the session id as its first parameter), or None when the database or
    the session cannot be read."""
    db, sid = hermes_split(transcript_path)
    if not db or not os.path.exists(db):
        return None
    try:
        con = _connect(db)
        try:
            con.row_factory = sqlite3.Row
            return con.execute(sql, (sid,) + tuple(args)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _session(transcript_path):
    rows = _query(transcript_path, "SELECT * FROM sessions WHERE id = ?")
    return dict(rows[0]) if rows else None


def hermes_tool(name, args):
    """(name, args) of a Hermes tool call, in the names the mechanisms key on: the shell is
    `bash {"command"}`, and the file tools are `view` / `edit` / `create` / `grep`."""
    args = args if isinstance(args, dict) else {}
    if name == "terminal":
        return "bash", {"command": str(args.get("command") or "")}
    if name == "read_file":
        return "view", {"path": str(args.get("path") or "")}
    if name == "patch":
        # V4A mode carries the paths inside the patch text instead of `path`.
        paths = _PATCH_FILE_RE.findall(args.get("patch") or "") if isinstance(
            args.get("patch"), str) else []
        return "edit", {"path": str(args.get("path") or (paths[0] if paths else ""))}
    if name == "write_file":
        return "create", {"path": str(args.get("path") or "")}
    if name == "search_files":
        return "grep", {"pattern": str(args.get("pattern") or ""),
                        "path": str(args.get("path") or "")}
    return name, args


def hermes_result(text):
    """(result_text, exit_code, failed) of a tool result string. A terminal result is JSON
    `{output, exit_code, error}`: its text is the output and the error, and it failed on a
    non-zero exit code. Any result with a non-empty `error` failed; one with no exit code is
    never failed for that reason alone."""
    text = _decode(text)
    try:
        data = json.loads(text) if text.lstrip().startswith("{") else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return text, None, False
    code = data.get("exit_code")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    err = data.get("error")
    failed = bool(err) or (code is not None and code != 0)
    if "exit_code" in data and "output" in data:
        parts = [x for x in (data.get("output"), err) if isinstance(x, str) and x]
        return "\n".join(parts), code, failed
    return text, code, failed


def _decode(content):
    """Hermes' stored content as text: structured content is `"\\x00json:" + json`."""
    if content is None:
        return ""
    if not isinstance(content, str):
        return str(content)
    if not content.startswith(_CONTENT_JSON_PREFIX):
        return content
    try:
        value = json.loads(content[len(_CONTENT_JSON_PREFIX):])
    except ValueError:
        return content
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(p.get("text", "") for p in value if isinstance(p, dict)
                         and isinstance(p.get("text"), str))
    return json.dumps(value)


def hermes_prompt_count(transcript_path):
    """How many prompts the session ran: its live `user` rows (a compaction's summary is not
    one). None when the session cannot be read."""
    rows = _query(transcript_path, "SELECT COUNT(*) FROM messages WHERE session_id = ? AND "
                                   "role = 'user' AND active = 1 AND _compressed_summary = 0")
    return int(rows[0][0]) if rows else None


def hermes_session_to_claude(transcript_path, out_path):
    """Write a Hermes session as Claude Code transcript lines, every call with the session's
    `cwd`. Rows a compaction replaced are read too (learning reads the whole session); rows an
    Undo removed are not. Returns the number of tool calls written, or None when the session's
    row is missing."""
    sess = _session(transcript_path)
    if sess is None:
        return None
    rows = _query(transcript_path, "SELECT role, content, tool_calls, tool_call_id FROM messages "
                                   "WHERE session_id = ? AND (active = 1 OR compacted = 1) "
                                   "ORDER BY id") or []
    cwd = sess.get("cwd") if isinstance(sess.get("cwd"), str) and sess.get("cwd") else None
    n = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for k, r in enumerate(rows):
            role = r["role"]
            if role == "user":
                out.write(json.dumps({"type": "user", "message": {
                    "role": "user", "content": _decode(r["content"])}}) + "\n")
            elif role == "assistant":
                blocks = []
                text = _decode(r["content"])
                if text:
                    blocks.append({"type": "text", "text": text})
                try:
                    calls = json.loads(r["tool_calls"]) if r["tool_calls"] else []
                except ValueError:
                    calls = []
                for c in calls if isinstance(calls, list) else []:
                    fn = (c.get("function") or {}) if isinstance(c, dict) else {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}") if isinstance(
                            fn.get("arguments"), str) else (fn.get("arguments") or {})
                    except ValueError:
                        args = {}
                    name, args = hermes_tool(str(fn.get("name") or ""), args)
                    blocks.append({"type": "tool_use", "id": c.get("id"), "name": name,
                                   "input": args})
                    n += 1
                msg = {"id": f"hermes-{k}", "role": "assistant", "content": blocks,
                       "model": sess.get("model")}
                out.write(json.dumps(dict({"type": "assistant", "message": msg},
                                          **({"cwd": cwd} if cwd else {}))) + "\n")
            elif role == "tool":
                text, _code, failed = hermes_result(r["content"])
                out.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": r["tool_call_id"], "content": text,
                    "is_error": failed}]}}) + "\n")
    return n


def hermes_session_usage(transcript_path):
    """The session's token totals, from its `sessions` row. Hermes' `input_tokens` excludes the
    cache reads and writes (`agent/usage_pricing.py` `CanonicalUsage.prompt_tokens`)."""
    s = _session(transcript_path) or {}
    cost = s.get("actual_cost_usd")
    if cost is None:
        cost = s.get("estimated_cost_usd")
    return {"input_tokens": int(s.get("input_tokens") or 0),
            "cache_read_input_tokens": int(s.get("cache_read_tokens") or 0),
            "cache_creation_input_tokens": int(s.get("cache_write_tokens") or 0),
            "output_tokens": int(s.get("output_tokens") or 0),
            "requests": int(s.get("api_call_count") or 0),
            "cost_usd_vendor": round(float(cost or 0), 6),
            "model": s.get("model") or None}


def hermes_last_activity(transcript_path):
    """When the session last did anything: its `ended_at`, else its newest message; None when
    its row is missing or unreadable."""
    s = _session(transcript_path)
    if s is None:
        return None
    if s.get("ended_at"):
        return float(s["ended_at"])
    rows = _query(transcript_path, "SELECT MAX(timestamp) FROM messages WHERE session_id = ?")
    latest = rows[0][0] if rows else None
    return float(latest) if latest is not None else float(s.get("started_at") or 0) or None


def hermes_ended(transcript_path):
    """True once the session's row has its `ended_at`."""
    s = _session(transcript_path)
    return bool(s and s.get("ended_at"))
