# Spec: the Hermes Agent host

Target: `NousResearch/hermes-agent` release `v2026.9.24`, researched at `780d5a3`.
Host name: `hermes`.

Read [`README.md`](README.md) first. It covers what the core asks of a host, the registration
checklist, and the decisions shared with OpenClaw.

Hermes paths below are relative to that checkout. [SRC] means verified in source. [DOC] means
read in the repository docs only. [?] means unverified: it must be settled before release (§9).

---

## 1. Shape

```
plugin/hermes/
  plugin.yaml          manifest (name, version, description, provides_hooks, requires_env: [])
  __init__.py          register(ctx): the shim. Standard library only.
```

The shim is Python, but it runs **inside the Hermes gateway process**
(`hermes_cli/plugins.py`). Every surface runs there: CLI, TUI, Desktop, messaging, cron and ACP.
So the shim:

- never imports `tokeneconomizer`. It spawns `plugin/scripts/te-hook --host hermes` with the
  payload on stdin and reads the last stdout line, exactly as `plugin/pi/tokeneconomizer.ts`
  does;
- wraps every callback in `try/except Exception` and returns `None`, with the error appended to
  `~/.tokeneconomizer/errors.log`;
- gives each spawn a timeout below Hermes' `plugins.hook_callback_timeout` (default 30 s)
  [SRC `plugins_dispatch.py:60`]. Use 20 s for `transform_tool_result` and `pre_llm_call`, and
  5 s for the others. On timeout it kills the child and returns `None`;
- passes `TE_HERMES_SURFACE=<platform>` in the child's environment. It is used for `headless`.

The plugin root is found from `__file__`. `te-hook` is at `<root>/../scripts/te-hook`, or at
`<root>/scripts/te-hook` in a pip build (§7).

## 2. Event mapping

| Hermes hook | Payload used | → TE event | Answer applied |
|---|---|---|---|
| `register_system_prompt_section("tokeneconomizer", fn, position="after_memory")` | `fn` receives the cwd (and the session id, see §9.1) | `SessionStart`, `source: "startup"` | `fn` returns the rendered text (≤ 4000 chars, otherwise `""`). The section is frozen for the session [DOC hooks.md §Cache-safe system prompt sections] |
| `on_session_start` | `session_id, platform` | (none) | records `session_id → platform` for the later events |
| `pre_llm_call` | `session_id, turn_id, is_first_turn` | `UserPromptSubmit`, `prompt_id = turn_id` | ignored in v1 (the facts are already in the frozen section). Returns `None` |
| `transform_tool_result` | `tool_name, args, result, session_id, turn_id, status, error_message` | `PostToolUse`, or `PostToolUseFailure` when `status != "ok"` [?] | if the answer carries context, return `result + "\n\n" + context`; otherwise `None` (result unchanged) |
| `on_session_end` | `session_id, turn_id, completed, interrupted` | `Stop` | notices go out through `ctx.post_message` [?] |
| `on_session_finalize` | `session_id, platform, reason` | `SessionEnd`, `reason` passed through | none; the fold is detached by the core |
| `pre_tool_call` | (not registered) | (none) | (none) |

Notes:

- **`on_session_end` fires after every turn** [DOC hooks.md catalog], so it is TE's `Stop`, not
  `SessionEnd`.
- **Resume or compaction.** The section function runs once per *new* session. A resumed Hermes
  session keeps its frozen prompt, which is the behaviour `keeps_start_context = True` describes.
  After a compaction, Hermes rebuilds the system prompt [DOC prompt-assembly.md]. The section
  then reruns and should send `source: "compact"` when Hermes says so [?]; until verified, send
  `"startup"`.
- **`transform_tool_result` instead of `post_tool_call`.** It receives the same payload, and it
  is the only hook that can add a redirect to what the model reads next without blocking the
  call. One hook does both jobs.
- **Payloads carry no `cwd`.** Every payload the shim builds sets `cwd = ctx.session_cwd()`
  [SRC `plugins.py:597`]. If that returns `None`, the shim sends nothing; the session is not
  pinned to a repository.
- **Subagents.** The shim sets `agent_id` when the session has a `parent_session_id` or is
  running under `delegate_task` [?], so `Host.is_subagent` skips its calls.

The payload the shim sends is Claude-shaped:
`{hook_event_name, session_id, cwd, transcript_path, source?, prompt_id?, tool_name?,
tool_input?, tool_response?, reason?, agent_id?}`. Its `transcript_path` is
`"<HERMES_HOME>/state.db#<session_id>"` (§4).

## 3. `class Hermes(Host)` in `tokeneconomizer/plugin/hosts.py`

```python
class Hermes(Host):
    name = "hermes"
    prompt_context = False       # facts live in the frozen system-prompt section
    keeps_start_context = True   # resume keeps the frozen prompt
    relays_notices = False       # [?] flip to True if post_message is not shown on a surface
    interrupt = "..."            # Hermes' own key for aborting a turn [?]
    end_run = "..."

    def turn_key(self, p): return str(p.get("prompt_id") or "")
    def headless(self, env, transcript=None):
        return env.get("TE_HERMES_SURFACE", "cli") not in ("cli", "tui", "desktop")
    def tool_call(self, p): ...          # below
    def context(self, event, text): return {"additionalContext": text}
    def notice(self, text): return {"systemMessage": text}
    def mechanism_trace(self, tp, workdir): ...   # trace.hermes_session_to_claude
    def prompt_count(self, tp): ...               # trace.hermes_prompt_count
    def session_usage(self, tp): ...              # trace.hermes_session_usage
```

`tool_call` maps tool names as follows:

| Hermes tool | → name | args |
|---|---|---|
| `terminal` | `bash` | `{"command": args["command"]}` |
| `read_file` | `view` | `{"path": ...}` |
| `patch` | `edit` | `{"path": ...}` |
| `write_file` | `create` | `{"path": ...}` |
| anything else | unchanged | unchanged |

The `result` argument is a JSON string. For `terminal` it is `{"output", "exit_code", "error"}`
[SRC `tools/terminal_tool.py:1036`]. `result_text` is `output` plus `error`, and `exit_code` is
`exit_code`. For other tools, `exit_code` is `None` and `result_text` is the raw string.

The answer shape (`context`/`notice`) is internal to the shim and the host class. Pick the
smallest dict that carries both fields. The shim reads `additionalContext` and
`systemMessage`.

Register the host in:

- `HOSTS`;
- `default_host`: an env marker set by Hermes for subprocesses, if any [?]; otherwise rely on
  `--host hermes`;
- `controller._cmd`: `"/te {name}"`;
- `pricing.AGENT_DEFAULT_MODEL["hermes"]`.

## 4. Finished-session log: `tokeneconomizer/trace/hermes.py`

Hermes keeps sessions in SQLite at `get_hermes_home()/state.db`, in WAL mode with schema
version 31 [SRC `hermes_state_common.py:461+`]. The location is `~/.hermes/state.db`,
`~/.hermes/profiles/<name>/state.db`, or `$HERMES_HOME/state.db`. The stdlib `sqlite3` module
reads it. Open it read-only (`file:…?mode=ro`, `uri=True`), because the gateway is the only
writer.

`transcript_path` is the string `"<db path>#<session_id>"`. That keeps the core's
"a transcript is a path" contract. Every reader in `trace/hermes.py` splits it.

| Function | Query | Output |
|---|---|---|
| `hermes_session_to_claude(tp, out)` | `messages WHERE session_id=? AND active=1 ORDER BY id` | Claude transcript lines. Each assistant `tool_calls` entry becomes a `tool_use` block, with the tool name mapped as in §3. The matching `role='tool'` row becomes a `tool_result`, with `is_error` set when the result JSON has a non-null `exit_code` other than 0, or a non-empty `error`. A tool whose result has no `exit_code` (anything but `terminal`) is never marked as failed for that reason alone. Every call gets `cwd` from `sessions.cwd`. Returns the number of calls, or `None` if the row is missing. |
| `hermes_prompt_count(tp)` | `COUNT(*) … role='user' AND active=1` | an int |
| `hermes_session_usage(tp)` | the `sessions` row | `model`; `requests = api_call_count`; `input_tokens`; `cache_read_input_tokens = cache_read_tokens`; `cache_creation_input_tokens = cache_write_tokens`; `output_tokens`; `cost_usd_vendor = actual_cost_usd or estimated_cost_usd` |

- **Compacted turns.** Hermes compacts in place and marks old rows `active=0, compacted=1`
  [SRC]. Learning reads the whole session: `active=1 OR compacted=1`. The prompt count reads
  `active=1` only, like Pi's active branch. Settle this with a recorded compacted session.
- **Pending folds need a liveness check that understands this path.**
  `fold.pending_sessions` (fold.py:427) calls `os.path.getmtime` on the stored transcript. A
  `state.db#<id>` string never stats, so a session whose finalize hook was missed would never
  fold. The database file's mtime would not help either: other sessions keep writing to it.
  - Add `Host.last_activity(transcript_path)`. Its default is `os.path.getmtime`, and
    `pending_sessions` calls it instead of `getmtime`.
  - `Hermes` overrides it: `sessions.ended_at` if set, else the newest `messages.timestamp` for
    that session id. A missing row counts as never active.
  - Tests: a Hermes session with no end event and an old last message is picked up by
    `pending_sessions`; one with a recent message is not.
- **`settle()`.** On `on_session_finalize`, wait (bounded at 2 s) until `sessions.ended_at` is
  set, like Copilot's wait for `session.shutdown`.

## 5. Side calls (loop-guard judge, micro-skill proposer)

`sidecall.hermes_call(prompt, model)` runs Hermes' own one-shot CLI with tools disabled. Per
CLAUDE.md, it is never a direct provider call. The exact invocation (for example
`hermes chat -q … --toolsets none --json`) is unverified [?]; settle it before wiring
`runner_for`. Add a stub in `tests/conftest.py`.

A side call starts a Hermes session of its own, so it must carry `TE_SIDECALL=1`, which turns
the hooks into no-ops. Its rows in `state.db` should be recognisable, for example by
`source`, so that a later pending-fold sweep skips them.

## 6. User command

The shim registers this command:

```python
ctx.register_command("te", handler, description="TokenEconomizer status and settings", args_hint="<command>")
```

The handler:

1. takes the `session_id` keyword argument;
2. spawns `scripts/te --host hermes --cwd <ctx.session_cwd()> <args>` with
   `TE_SESSION_ID=<session_id>`;
3. returns its stdout as text, because Hermes drops anything printed from a handler.

The command works on every surface.

`register_cli_command` (`hermes te …`) is not needed: `~/.tokeneconomizer/bin/te` already
exists.

## 7. Packaging and install

- **Primary:** `hermes plugins install xchewtoyx/TokenEconomizer`, then
  `hermes plugins enable tokeneconomizer`. Whether `install` accepts a subdirectory
  (`plugin/hermes`) or needs the manifest at the repository root is unverified [?]. If it needs
  the root, add a root `plugin.yaml` whose `register` re-exports `plugin/hermes`. That mirrors
  how the root `package.json` serves Pi, and `test_plugin_package.py` should pin the two to
  agree in the same way.
- **pip (optional, later):** an entry point `hermes_agent.plugins` → `tokeneconomizer_hermes:register`.
  This needs a `pyproject.toml` the repository does not have yet; skip it in v1.
- **Profiles.** Every profile has its own `config.yaml` and `state.db`, and plugins are enabled
  per profile. TE state is keyed by repository, not by profile, so two profiles working in one
  repository share what has been learned. That is intended.
- **Project-local plugins** (`.hermes/plugins/`) are not loaded by the gateway [DOC]. Document
  that the plugin must be installed per user.

## 8. Tool pruning (phase 2)

The host is not added to `denylist._SETTINGS` in v1. Candidate levers, in order of preference:

1. **An `llm_request` middleware** [SRC `hermes_cli/middleware.py`] that drops the tools named
   in this repository's `untool.json` from `api_kwargs["tools"]`. It is per session and needs
   no config edit. The `tools` shape differs by API mode (chat completions, Anthropic,
   codex_responses) [?]. Filter at the first request of a session only, and keep that list for
   the rest of the session, so the cached prefix stays stable.
2. **`agent.disabled_toolsets`** in the profile's `config.yaml`. It works at toolset level
   (`terminal`, `file`, `web`, …), applies to every repository in the profile, and needs a YAML
   edit without a YAML library. Do not use it.

The protected tools are `terminal`, `read_file`, `patch`, `write_file`, `search_files`,
`skill_view`, `skills_list` and `clarify`. MCP tools are named `mcp__<server>__<tool>`
[SRC `tools/mcp_tool_schema.py`]. The rules that already handle Claude's `mcp__` names apply
unchanged.

## 9. Verify before release (to become a `docs/PLUGIN.md` §3 block)

1. The section function's signature: does it receive `session_id`? If it does not, correlate
   through the `on_session_start` record keyed by cwd plus time, or move the injection to the
   first `pre_llm_call` (user message, not cached). The first option is preferred.
2. `transform_tool_result`: confirm its `status` values, and that a returned string is what
   the model sees.
3. `ctx.post_message`: is it shown on CLI, TUI and Desktop? If not, set `relays_notices`.
4. How `on_session_finalize` behaves on gateway restart and on `/new`, and whether a session
   whose end was never seen is picked up by `fold.pending_sessions`.
5. A subagent marker for `delegate_task` children.
6. The one-shot CLI invocation for side calls.
7. Whether `hermes plugins install` takes a subdirectory.
8. Whether Hermes rebuilds the system prompt after compaction, and which `source` the section
   sees.
9. That the WAL read-only open works while the gateway holds the database.

## 10. Tests

- **`tests/fixtures`:** a `hermes_state_db(path, session)` writer that builds a minimal
  `state.db` (the `sessions` and `messages` tables, with only the columns the readers use).
- **`test_live_core.py`:**
  - `test_host_detection` gains `hermes`;
  - `test_the_hermes_host_reads_what_the_shim_sends`, covering `tool_call` for `terminal` with
    a non-zero `exit_code`.
- **`test_trace.py`:** the prompt count, usage mapping and compacted-row handling of the
  readers.
- **`test_live_hooks.py`:**
  - `_guard_turn` / `_stuck` gain the Hermes payload shape;
  - the parametrized guard test covers `hermes`;
  - five trap sessions inject the fact at the fifth start.
- **`test_plugin_package.py`:**
  - `plugin/hermes/plugin.yaml` names the hooks `__init__.py` registers;
  - `__init__.py` imports only the standard library;
  - the spawn line includes `"--host", "hermes"`;
  - every callback body is guarded. Check this by calling each registered callback with a
    payload that makes the child fail, and asserting `None`.
- **Recorded sessions** (CLAUDE.md): before release, record trap sessions on real Hermes through
  OpenRouter, and check the fold and injection against them.
