"""Trace seam: load_trajectory / tool_usage / parse_catalog over synthetic transcripts."""
import os

from tokeneconomizer import trace
from fixtures import write_claude_transcript, write_codex_transcript


def test_load_trajectory_claude(tmp_path):
    p = str(tmp_path / "t.jsonl")
    write_claude_transcript(p, [
        ("bash", {"command": "python -m pytest tests/test_x.py"}, "2 passed"),
        ("edit", {"file_path": "pkg/mod.py"}, "ok")])
    traj = trace.load_trajectory(p)
    names = [name for name, _a, _r, _ok in traj]
    assert "bash" in names and "edit" in names


def test_a_claude_request_blob_repeating_the_conversation_adds_no_call(tmp_path):
    """Claude Code 2.1.300 writes `api-request-blob` entries that repeat a request's messages,
    earlier tool calls and results included. Read as calls, each one counted twice."""
    import json
    use = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest -q"}}
    res = {"type": "tool_result", "tool_use_id": "toolu_1",
           "content": "/bin/bash: line 1: pytest: command not found", "is_error": True}
    lines = [{"type": "assistant", "cwd": "/w/repo", "message": {"role": "assistant",
                                                                "content": [use]}},
             {"type": "user", "message": {"role": "user", "content": [res]}},
             {"type": "api-request-blob", "message": {"role": "assistant", "content": [use]}},
             {"type": "api-request-blob", "message": {"role": "user", "content": [res]}},
             {"type": "assistant", "message": {"role": "assistant", "content": [use]}}]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines))
    calls = trace.load_trajectory(str(p))
    assert [(c[0], c[1]["command"], c[3]) for c in calls] == [("Bash", "pytest -q", False)]
    assert trace.call_cwds(str(p)) == ["/w/repo"]
    assert trace.tool_usage(str(p)) == {"Bash": 1}
    from tokeneconomizer.microskills import distiller
    assert len(distiller.load_calls(str(p))) == 1


def test_tool_usage_counts(tmp_path):
    p = str(tmp_path / "t.jsonl")
    write_claude_transcript(p, [
        ("bash", {"command": "ls"}, ""),
        ("bash", {"command": "pwd"}, ""),
        ("edit", {"file_path": "a.py"}, "")])
    usage = trace.tool_usage(p)
    assert usage.get("bash", 0) >= 2


def test_parse_catalog_codex_default():
    cat = trace.parse_catalog("/nonexistent", "codex")
    assert "shell" in cat and "apply_patch" in cat


def test_parse_catalog_missing_returns_empty(tmp_path):
    # Claude path with no transcript file -> empty catalog, no crash.
    assert trace.parse_catalog(str(tmp_path), "claude") == {}


# ---- Copilot tool-catalog sources: one pipeline across CLI builds -------------------
#
# The CLI has moved the loaded tool catalog twice, and each move made the then-current
# reader return {} -- which untooling cannot tell apart from "no tools loaded", so it
# degraded to a silent no-op for a whole sweep (measured: CLI 1.0.82 dropped 8 of 17 tools
# on 30/31 tasks; 1.0.83, same harness and repo, dropped nothing on 31/31). These tests
# pin all three source formats and the auto-selection between them.
import json as _json

from tokeneconomizer import trace as _trace
from tokeneconomizer.trace import catalog as _catalog

_SCHEMA_LOG = (
    '{"body":"{\\"tools\\":[{\\"name\\":\\"bash\\",\\"title\\":null,'
    '\\"description\\":\\"Run a shell command\\"},'
    '{\\"name\\":\\"view\\",\\"description\\":\\"Read a file\\"}]}"}'
)
_FLAT_LOG = '{\n  "tools": [\n    "bash:66921ccc",\n    "view:0963ceac",\n    "edit:9e640c64"\n  ]\n}'


def _checkpoint_transcript(*names):
    return _json.dumps({
        "type": "session.usage_checkpoint",
        "data": {"promptCacheBreakState": [
            {"models": {"m": {"tools": [{"name": n} for n in names]}}}]},
    })


def test_flat_catalog_parses_name_fingerprint_form():
    """CLI >= 1.0.82 publishes `"tools": ["bash:66921ccc", ...]` and no schemas."""
    cat = _catalog.parse_tool_catalog_flat(_FLAT_LOG)
    assert set(cat) == {"bash", "view", "edit"}
    assert all(v == "" for v in cat.values())      # names only, no descriptions
    assert _catalog.parse_tool_catalog(_FLAT_LOG) == {}   # schema reader stays schema-only


def test_schema_catalog_still_wins_and_keeps_descriptions(tmp_path):
    (tmp_path / "copilot_debug.log").write_text(_SCHEMA_LOG + "\n" + _FLAT_LOG)
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert src == "schema_debug_log"               # richest: it has descriptions
    assert cat["bash"].startswith("Run a shell command")


def test_flat_used_when_schemas_and_checkpoint_are_gone(tmp_path):
    """The 1.0.83 shape: no schemas, no usage_checkpoint -- must NOT parse empty."""
    (tmp_path / "copilot_debug.log").write_text(_FLAT_LOG)
    (tmp_path / "transcript.jsonl").write_text('{"type":"assistant.message","data":{}}')
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert src == "flat_debug_log"
    assert set(cat) == {"bash", "view", "edit"}


def test_usage_checkpoint_source_is_still_read(tmp_path):
    """The 1.0.82 shape must keep working -- supporting the new build is not a swap."""
    (tmp_path / "transcript.jsonl").write_text(_checkpoint_transcript("bash", "view"))
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert src == "usage_checkpoint"
    assert set(cat) == {"bash", "view"}


def test_no_source_reports_none_not_empty_catalog(tmp_path):
    """No source must be distinguishable from an empty catalog: {} is not 'no tools'."""
    (tmp_path / "copilot_debug.log").write_text("nothing useful here")
    cat, src = _trace.parse_catalog_with_source(str(tmp_path), "copilot")
    assert cat == {}
    assert src is None                              # defect signal, not an empty catalog


# ---- Web-Bench pass@1 vs pass@N ------------------------------------------------------
#
# Web-Bench grades each task with up to `retry` attempts, so a task has TWO defensible
# resolve metrics, and they differ materially (measured on prisma: 1-4 tasks per arm).
# Web-Bench's own headline is pass@1. The runner breaks out of the attempt loop on the
# first pass, which makes pass@1 recoverable from older rows that predate the explicit
# fields -- these pin that derivation, since getting it wrong reports pass@N under the
# pass@1 column.
def _p1p2(row):
    """The aggregator's per-row pass@1/pass@N resolution, mirrored for test."""
    p1, p2 = row.get("resolved_p1", ""), row.get("resolved_p2", "")
    att = row.get("webbench_attempts", "")
    if p1 == "" and p2 == "":
        if att != "":
            att = int(att or 0)
            if att == 0:
                return None, None
            p2 = row["resolved"] is True
            p1 = p2 and att == 1
        else:
            p1 = p2 = row["resolved"]
    return p1, p2


def test_explicit_attempt_fields_win():
    assert _p1p2({"resolved": True, "resolved_p1": False,
                  "resolved_p2": True, "webbench_attempts": 2}) == (False, True)


def test_legacy_row_second_attempt_pass_is_not_pass_at_1():
    """resolved=True after 2 attempts means attempt 1 FAILED."""
    assert _p1p2({"resolved": True, "webbench_attempts": 2}) == (False, True)


def test_legacy_row_first_attempt_pass():
    assert _p1p2({"resolved": True, "webbench_attempts": 1}) == (True, True)


def test_legacy_row_failed_every_attempt():
    assert _p1p2({"resolved": False, "webbench_attempts": 2}) == (False, False)


def test_unreached_task_counts_in_neither_metric():
    """Never attempted is not a miss -- it must not enter numerator or denominator."""
    assert _p1p2({"resolved": False, "webbench_attempts": 0}) == (None, None)


def test_single_attempt_domain_collapses_to_resolved():
    """SWE-bench rows carry no attempt fields; pass@1 and pass@N are the same number."""
    assert _p1p2({"resolved": True}) == (True, True)
    assert _p1p2({"resolved": False}) == (False, False)


def _jsonl(path, records):
    import json
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in records) + "\n")
    return str(path)


def test_a_claude_session_counts_the_prompts_the_person_gave(tmp_path):
    """Typed prompts and slash commands; not tool results, a local command's output, an
    interrupt marker, a compaction summary, meta text, or a subagent's prompts."""
    def user(content, **flags):
        return dict({"type": "user", "message": {"role": "user", "content": content}}, **flags)
    p = _jsonl(tmp_path / "c.jsonl", [
        user("fix the test"),
        user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        user([{"type": "text", "text": "and the docs"}, {"type": "image", "source": {}}]),
        user("<command-name>/tokeneconomizer:status</command-name>"),
        user("<local-command-stdout>done</local-command-stdout>"),
        user("[Request interrupted by user]"),
        user("Caveat: the messages below were generated by the user", isMeta=True),
        user("This session is being continued...", isCompactSummary=True),
        user("subagent task", isSidechain=True),
        {"type": "assistant", "message": {"role": "assistant", "content": "done"}}])
    assert trace.claude_prompt_count(p) == 3


def test_a_codex_session_counts_one_prompt_per_turn(tmp_path):
    turn = [{"type": "turn_context", "payload": {"turn_id": "t"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user"}}]
    assert trace.codex_prompt_count(_jsonl(tmp_path / "r.jsonl", turn * 3)) == 3
    # a rollout without task_started events counts its turn contexts
    older = [r for r in turn if r["type"] != "event_msg"]
    assert trace.codex_prompt_count(_jsonl(tmp_path / "o.jsonl", older * 2)) == 2


def test_a_copilot_session_counts_its_user_messages(tmp_path):
    p = _jsonl(tmp_path / "events.jsonl", [
        {"type": "session.start", "data": {}}, {"type": "user.message", "data": {}},
        {"type": "tool.execution_start", "data": {}}, {"type": "user.message", "data": {}}])
    assert trace.copilot_prompt_count(p) == 2



def test_a_pi_session_counts_the_prompts_on_its_active_branch(tmp_path):
    """Pi keeps a conversation as a tree; a prompt on a branch the person left is not counted."""
    def msg(eid, parent, role):
        return {"type": "message", "id": eid, "parentId": parent, "message": {"role": role}}
    p = _jsonl(tmp_path / "pi.jsonl", [
        {"type": "session", "id": "s"}, msg("a", None, "user"), msg("b", "a", "assistant"),
        msg("x", "b", "user"), msg("y", "x", "assistant"),        # abandoned
        msg("c", "b", "user"), msg("d", "c", "toolResult"), msg("e", "d", "assistant")])
    assert trace.pi_prompt_count(p) == 2


# ---- Hermes: sessions in a SQLite state.db ---------------------------------------------------
def _hermes_db(tmp_path, **extra):
    from fixtures.hermes import hermes_state_db, terminal_result, tool_call, trap_messages
    msgs = trap_messages("/w/repo", 0)
    # A compaction in place replaced the first prompt and its calls; an Undo removed a call.
    for m in msgs[:3]:
        m.update(active=0, compacted=1)
    msgs.insert(3, {"role": "user", "content": "[summary of earlier turns]",
                    "_compressed_summary": 1})
    msgs += [{"role": "user", "content": "now the docs"},
             {"role": "assistant", "content": "", "active": 0,
              "tool_calls": [tool_call("undone", "terminal", {"command": "rm -rf docs"})]},
             {"role": "tool", "content": terminal_result("", 0), "tool_call_id": "undone",
              "active": 0}]
    return hermes_state_db(str(tmp_path / "state.db"), dict({
        "id": "h1", "cwd": "/w/repo", "model": "anthropic/claude-haiku-4.5",
        "input_tokens": 30, "output_tokens": 150, "cache_read_tokens": 3000,
        "cache_write_tokens": 300, "api_call_count": 4, "estimated_cost_usd": 0.01,
        "messages": msgs}, **extra))


def test_a_hermes_session_reads_compacted_rows_and_skips_undone_ones(tmp_path):
    tp = _hermes_db(tmp_path)
    out = str(tmp_path / "trace.jsonl")
    assert trace.hermes_session_to_claude(tp, out) == 3
    traj = trace.load_trajectory(out)
    assert [(n, a.get("command") or a.get("path"), ok) for n, a, _, ok in traj] == [
        ("edit", "/w/repo/pkg/mod0.py", True), ("bash", "pytest -q tests/test_a.py", False),
        ("bash", "python -m pytest -q tests/test_a.py", True)]
    assert "command not found" in traj[1][2] and '"exit_code"' not in traj[1][2]
    assert "rm -rf docs" not in open(out).read()
    assert trace.call_cwds(out) == ["/w/repo"] * 3
    assert trace.hermes_session_to_claude(tp.replace("#h1", "#nope"), out) is None


def test_a_hermes_session_counts_its_live_prompts_and_maps_its_usage(tmp_path):
    tp = _hermes_db(tmp_path, actual_cost_usd=0.02)
    assert trace.hermes_prompt_count(tp) == 1          # "now the docs"; not the summary
    u = trace.hermes_session_usage(tp)
    assert (u["input_tokens"], u["cache_read_input_tokens"], u["cache_creation_input_tokens"],
            u["output_tokens"], u["requests"]) == (30, 3000, 300, 150, 4)
    assert u["cost_usd_vendor"] == 0.02 and u["model"] == "anthropic/claude-haiku-4.5"
    assert trace.hermes_prompt_count(str(tmp_path / "missing.db#h1")) is None


def test_a_hermes_result_fails_on_its_exit_code_or_its_error():
    from fixtures.hermes import terminal_result
    assert trace.hermes_result(terminal_result("boom", 2, "exit 2")) == ("boom\nexit 2", 2, True)
    assert trace.hermes_result(terminal_result("ok", 0)) == ("ok", 0, False)
    assert trace.hermes_result('{"success": true}')[2] is False       # no exit code: not failed
    assert trace.hermes_result('{"error": "no such file"}')[2] is True
    assert trace.hermes_result("plain text") == ("plain text", None, False)
    assert trace.hermes_tool("patch", {"mode": "patch", "patch": "*** Update File: a/b.py\n"}) \
        == ("edit", {"path": "a/b.py"})


def test_a_hermes_database_path_may_contain_uri_punctuation(tmp_path):
    """`#` and `?` in the profile path are URI syntax; the reader still opens the file."""
    from fixtures.hermes import hermes_state_db
    home = tmp_path / "dev #1?"
    home.mkdir()
    tp = hermes_state_db(str(home / "state.db"), {"id": "h1", "cwd": "/w/repo",
                                                  "messages": [{"role": "user", "content": "hi"}]})
    assert "#" in tp and "?" in tp
    assert trace.hermes_prompt_count(tp) == 1


def test_a_hermes_database_is_read_while_hermes_holds_it_open(tmp_path):
    """Hermes keeps state.db open in WAL mode; a read-only reader still sees committed rows."""
    import sqlite3
    tp = _hermes_db(tmp_path)
    writer = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                       "VALUES ('h1', 'user', 'uncommitted', 0)")
        assert trace.hermes_prompt_count(tp) == 1
    finally:
        writer.rollback()
        writer.close()
