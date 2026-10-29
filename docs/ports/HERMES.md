# Spec: the Hermes Agent host

Target: `NousResearch/hermes-agent` release `v2026.9.24`, researched at `780d5a3`; the
implementation re-checked every claim at `main` `6696c7b0` (2026-10-28, `__release_date__`
`2026.9.24`, schema version 32). Hermes tags two schemes: date tags (`v2026.9.24`, the release
date `hermes_cli/__init__.py` carries) and the semver tags published images use (`v0.22.5` is
the latest stable, a different commit). Name the semver release in user-facing docs.
Host name: `hermes`.

**Implementation status (v1).** Built as described below, with the changes noted inline as
"Implemented:". Line numbers marked `@6696c7b0` are from that commit.

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

Implemented: Hermes loads a user plugin with `spec_from_file_location(...,
submodule_search_locations=[plugin_dir])` [SRC `hermes_cli/plugins_loader.py:811`@6696c7b0],
so the shim resolves `os.path.realpath(__file__)` (a symlinked plugin directory then finds the
checkout) and looks in both places. The section function gets 20 s, not 5: Hermes does not time
it at all (`plugins_dispatch.py:697`@6696c7b0), and SessionStart is the slowest event (the other
hosts give it 15 s). Each session's calls are serialized in the shim, so concurrent tool results
fold in order. Notices go to `ctx.post_message` in a thread of their own: it can wait up to 30 s
on the gateway loop (`hermes_cli/plugin_messages.py:99-140`@6696c7b0).

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
- **Payloads carry no `cwd`.** The section's session info may carry one
  (`resolve_context_cwd()`, often empty on the CLI). Hook kwargs do not. v0.21.5's
  `PluginContext` (tag `v2026.9.24`, `f97608f`) has no `session_cwd`; that method is
  later (`plugins.py` on main). The shim uses `session_cwd()` when the method exists,
  otherwise the directory v0.21.5 pins on `agent.runtime_cwd` (cron and the gateway set
  it and do not chdir), otherwise `sessions.cwd` in `state.db`, otherwise the process
  cwd (`resolve_agent_cwd`: `TERMINAL_CWD`, then `os.getcwd()`). An empty result sends
  nothing.
- **Subagents.** The shim sets `agent_id` when the session has a `parent_session_id` or is
  running under `delegate_task` [?], so `Host.is_subagent` skips its calls.
  Implemented differently: `parent_session_id` is **not** a subagent marker, because a
  compaction that rotates the session also starts a child with a `parent_session_id`
  (`agent/conversation_compression.py:1572`@6696c7b0). The markers used are the `subagent_start`
  hook's `child_session_id` (`tools/delegate_tool.py:355`) and the child agent's
  `platform="subagent"` (`tools/delegate_tool.py:296`), seen in the section's session info and in
  `on_session_start`. The shim then sends nothing at all for that session, which is what the
  other hosts amount to (no SessionStart for a subagent there either).
- **Stop.** Implemented: `on_session_end` becomes `Stop` only when `completed` is true and
  neither `interrupted` nor `failed` is; Hermes also fires it on CLI exit with
  `interrupted=True` (`hermes_cli/cli_shutdown.py:200`@6696c7b0). A turn that did not stop on its
  own is closed, unseen, by the next prompt or the session's end.

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

Implemented, with these differences:

- `relays_notices = True`: `post_message` needs a live messaging gateway in the process and a
  session bound in the calling context, and answers `gateway_unavailable` / `no_conversation`
  otherwise (`hermes_cli/plugin_messages.py:99-108, 177-182`@6696c7b0). Whether the CLI, TUI and
  Desktop show it is unverified, so the still-stuck notice also goes through the model.
- `interrupt = "Send /stop to stop it (in the terminal, Ctrl+C also interrupts it)."` [DOC
  `website/docs/user-guide/cli.md:274, 484`]; `end_run` keeps the default Ctrl+C line.
- `headless` is `HERMES_SINGLE_QUERY_SESSION=1` (set by `hermes -z` and `chat -q`,
  `hermes_cli/oneshot.py:280`@6696c7b0) or a surface with no person reading (`cron`,
  `api_server`, `kanban`, `tool`, `webhook`). A messaging chat (Telegram, Discord, ...) is not
  headless: `/stop` works there, Ctrl+C does not.
- `tool_call` also maps `search_files` → `grep`, so the guard reads its output as file content,
  and takes a V4A `patch`'s path from its `*** Update File:` line.
- `detect` recognises a `state.db#` transcript path; `default_host` is unchanged (no Hermes env
  marker for subprocesses was found).
- `AGENT_DEFAULT_MODEL` gets no `hermes` entry: Hermes has no fixed default model (the person
  configures a provider), and every session row and side call names its model.
- `Host.last_activity` is also used by `fold.last_activity`, the deny list's idle check.

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

Implemented: `hermes -z <prompt> --ignore-rules -t context_engine --usage-file <tmp> [-m
<paid_model>]`, run in a temporary directory. `-z` prints only the reply; `--usage-file` writes
a JSON report with `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`,
`model` (`hermes_cli/_parser.py:148-158`, `hermes_cli/oneshot.py:31-35`@6696c7b0). There is no
`--toolsets none`; `context_engine` is a valid toolset whose tools come only from the active
context engine, empty for the built-in one (`toolsets.py:127`, `agent/agent_init.py:2029`)
[?]. `--source` exists only on `chat`, not with `-z`; it is not needed, because a side call's
hooks are no-ops and its session is never recorded in TE's own `sessions/`, which is all a
pending-fold sweep reads.

## 6. User command

The shim registers this command:

```python
ctx.register_command("te", handler, description="TokenEconomizer status and settings", args_hint="<command>")
```

The handler:

1. takes the `session_id` keyword argument;
2. spawns `scripts/te --host hermes --cwd <the directory resolved above> <args>` with
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

Implemented: `hermes plugins install owner/repo/sub/dir` (or `#sub/dir`) **does** take a
subdirectory (`hermes_cli/plugins_cmd.py:294-330`@6696c7b0), but it sparse-checks-out and
installs only that folder (`plugins_cmd_install.py:415`, `plugins_cmd_git.py:219-246`), so
`plugin/hermes` alone would have no launchers and no core. v1 therefore installs one of:

- a symlink from a checkout: `ln -s <clone>/plugin/hermes $HERMES_HOME/plugins/tokeneconomizer`
  (discovery follows it, `hermes_cli/plugins_discovery.py:162`; the shim resolves the real path);
- the self-contained copy `scripts/build_plugin.py` now also writes,
  `<out>/hermes/tokeneconomizer/` (manifest, shim, `scripts/`, `lib/`), copied to
  `$HERMES_HOME/plugins/tokeneconomizer`.

No root `plugin.yaml`. Each profile has its own `HERMES_HOME` (`plugins/`, `state.db`), so the
plugin is installed and enabled per profile. In the official container image the gateway's HOME
(`/opt/data`) differs from the terminal tool's (`/opt/data/home`, `hermes_constants.py:765`
`get_subprocess_home`), so the docs ask for an explicit `TOKENECONOMIZER_HOME`; the launchers use
the `python3` on `PATH`, never a path into Hermes' venv.

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

Settled from source (`main` `6696c7b0`); no agent turn has been run yet. A smoke test with
Hermes' own plugin manager (see `docs/PLUGIN.md` §3) loaded the linked plugin, registered every
hook, the section and `/te`, and reached the dispatcher from the section and from
`transform_tool_result`.

1. **Settled [SRC].** The section function receives one read-only mapping with `session_id`,
   `model`, `provider`, `platform`, `profile_name` and `cwd`
   (`agent/system_prompt.py:86-94`). No correlation is needed.
2. **Settled [SRC].** `status` is `"ok"` or `"error"` (`model_tools.py:649-664`: an `error` key
   in the result JSON, or Hermes' own failure detector, which for `terminal` is a non-zero
   `exit_code`). The first string any callback returns replaces the result before it enters the
   context (`model_tools.py:856`). That the model then acts on the appended redirect is [?].
3. **Partly settled [SRC].** `post_message` works only with a live messaging gateway in the
   process and a session bound to the call, and returns `ok=False` otherwise
   (`hermes_cli/plugin_messages.py`). Whether the CLI, TUI and Desktop (which attach to the
   unified gateway, `website/docs/user-guide/cli.md:176`) show it is [?]; `relays_notices` is
   on until it is checked.
4. **Partly settled [SRC].** `/new` finalizes the old session in the CLI
   (`hermes_cli/cli_session_mixin.py:578`); a gateway shutdown may finalize without a reset
   [DOC hooks.md]. A session whose end was never seen is picked up by `fold.pending_sessions`
   through `Host.last_activity` (tested on a synthetic database). Gateway restart behaviour [?].
5. **Settled [SRC].** `subagent_start` names `child_session_id`, and a child agent runs with
   `platform="subagent"` (`tools/delegate_tool.py:296, 355`). See §2.
6. **Settled [SRC], unrun.** See §5. That `-t context_engine` leaves the model no tools [?].
7. **Settled [SRC].** A subdirectory is accepted but installed alone. See §7.
8. **Settled [SRC].** Compaction invalidates the frozen sections and re-renders them at the
   commit boundary (`agent/system_prompt.py:816-832`,
   `agent/conversation_compression.py:3096`). The section is not told why it is rendered, so it
   sends `source: "startup"`, which the controller treats like `"compact"` for injection. A
   compaction can also rotate the session to a child id: the section then renders for the new
   id, and the parent (ended with `end_reason='compression'`) is folded by the pending sweep.
9. **Settled in a test, not on Hermes.** A `mode=ro` connection reads a WAL database while
   another connection holds a write transaction (`tests/test_trace.py`). On Hermes' own file
   the reader needs the `-wal`/`-shm` files present or a writable directory [?].

Still [?]: the section reaching the model and being kept across resume; `turn_id` in
`transform_tool_result` matching `pre_llm_call`'s; a messaging surface's notices; the side-call
toolset; installing and enabling on a real profile. Recorded trap sessions through OpenRouter are
still owed (CLAUDE.md).

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
