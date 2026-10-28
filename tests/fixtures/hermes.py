"""A synthetic Hermes `state.db`: the `sessions` and `messages` tables, with only the columns the
readers in `tokeneconomizer.trace.hermes` use (names and meaning as in Hermes' schema,
`hermes_state_common.py` SCHEMA_SQL), in WAL mode as Hermes keeps it."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, parent_session_id TEXT,
    started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    cwd TEXT, estimated_cost_usd REAL, actual_cost_usd REAL, api_call_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
    timestamp REAL NOT NULL, _compressed_summary INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0);
"""


def hermes_state_db(path, session):
    """Add one session to the database at `path` (created if missing); returns its transcript
    path, `"<path>#<id>"`. `session`: id, cwd, model, started_at, ended_at, the usage columns,
    and `messages`, each {role, content, tool_calls?, tool_call_id?, timestamp?, active?,
    compacted?, _compressed_summary?}."""
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        now = time.time()
        cols = ("id", "source", "model", "parent_session_id", "started_at", "ended_at",
                "end_reason", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "cwd", "estimated_cost_usd", "actual_cost_usd",
                "api_call_count")
        row = dict({"source": "cli", "started_at": now}, **{k: v for k, v in session.items()
                                                             if k in cols})
        con.execute(f"INSERT INTO sessions ({', '.join(row)}) VALUES "
                    f"({', '.join('?' * len(row))})", tuple(row.values()))
        for k, m in enumerate(session.get("messages") or []):
            calls = m.get("tool_calls")
            con.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, "
                "tool_name, timestamp, _compressed_summary, active, compacted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session["id"], m["role"], m.get("content"), m.get("tool_call_id"),
                 json.dumps(calls) if calls else None, m.get("tool_name"),
                 m.get("timestamp", row["started_at"] + k), m.get("_compressed_summary", 0),
                 m.get("active", 1), m.get("compacted", 0)))
        con.commit()
    finally:
        con.close()
    return f"{path}#{session['id']}"


def tool_call(cid, name, args):
    """One entry of an assistant row's `tool_calls`, in the OpenAI shape Hermes stores."""
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def terminal_result(output, exit_code, error=None):
    """A `terminal` tool's result string (tools/terminal_tool.py)."""
    return json.dumps({"output": output, "exit_code": exit_code, "error": error})


def trap_messages(repo, i):
    """The trap every session falls into: bare `pytest` is missing; `python -m pytest` works."""
    calls = [("patch", {"path": f"{repo}/pkg/mod{i}.py", "old_string": "a", "new_string": "b"},
              json.dumps({"success": True, "diff": "..."})),
             ("terminal", {"command": "pytest -q tests/test_a.py"},
              terminal_result("bash: pytest: command not found", 127)),
             ("terminal", {"command": "python -m pytest -q tests/test_a.py"},
              terminal_result("1 passed in 0.1s", 0))]
    msgs = [{"role": "user", "content": f"fix bug {i}"}]
    for k, (name, args, result) in enumerate(calls):
        cid = f"call_{i}_{k}"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [tool_call(cid, name, args)]})
        msgs.append({"role": "tool", "content": result, "tool_call_id": cid, "tool_name": name})
    msgs.append({"role": "assistant", "content": "Fixed."})
    return msgs
