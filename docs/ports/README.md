# Porting TokenEconomizer to Hermes Agent and OpenClaw

**Branching:** all port work lands on the long-running `claw-port` branch, and every port PR
targets `claw-port`, never `main`. `main` stays clean so that syncing from upstream is never
blocked.

This directory holds the research and the implementation specs for two new hosts:

- [`HERMES.md`](HERMES.md): the spec for Hermes Agent (Nous Research). Researched at
  `NousResearch/hermes-agent@780d5a3`, release `v2026.9.24`.
- [`OPENCLAW.md`](OPENCLAW.md): the spec for OpenClaw. Researched at
  `openclaw/openclaw@953eba4`, version `2026.10.8`.

The claims about each host were read from that host's source and the docs in its repository.
They have **not** been checked against a running agent yet. Each spec ends with a list of what
must be verified before a first release, in the spirit of `docs/PLUGIN.md` §3. Once an item is
verified, it moves into that section.

---

## 1. What the core asks of a host

Every host speaks one protocol: a Claude-shaped JSON payload goes into
`plugin/scripts/te-hook --host <name>`, and one JSON answer comes back. Dispatch happens in
`tokeneconomizer/plugin/controller.py` on `hook_event_name`. The events and what each one is
for:

| Event | Used by | Fields read |
|---|---|---|
| `SessionStart` | micro-skills inject; pending folds; session-start notices | `session_id`, `cwd`, `transcript_path`, `source` |
| `UserPromptSubmit` | loop guard opens a turn; prompt-context hosts re-send the facts | `session_id`, the turn key (`Host.turn_key`) |
| `PostToolUse` / `PostToolUseFailure` | loop guard folds the call | resolved by `Host.tool_call(p)` into `(name, args, result_text, exit_code)` |
| `Stop` | loop guard closes the turn; green turns feed `envelope.json` | `session_id`, turn key |
| `SessionEnd` | a detached `te fold` learns from the finished log | `session_id`, `transcript_path`, `reason` |

The plugin **never blocks a call**: no host uses a pre-tool hook (`docs/PLUGIN.md` §2). Both new
hosts *can* block. Both specs below still choose not to, because both hosts fail **closed** when
a pre-tool hook throws or times out. A fail-open plugin therefore must not sit on that path.

Fold-time needs, per host:

- a log `mechanism_trace` can turn into lines `trace.load_trajectory` reads;
- shell calls named `bash`/`shell` with `args["command"]`, the result text, and a success flag;
- the cwd of each call;
- `session_usage` with `model`, `requests`, `input_tokens`, `cache_read_input_tokens`,
  `cache_creation_input_tokens`, `output_tokens`.

## 2. Where a new host is registered (both specs)

| # | Place | What to add |
|---|---|---|
| 1 | `tokeneconomizer/plugin/hosts.py` | a `Host` subclass; an entry in `HOSTS` (line 440); `default_host` env markers; `detect` hints |
| 1a | `tokeneconomizer/plugin/fold.py` | only if the host's `transcript_path` is not a plain file (Hermes): `pending_sessions` asks `Host.last_activity` instead of calling `os.path.getmtime` |
| 2 | `tokeneconomizer/trace/<host>.py` + `trace/__init__.py` | the log reader: `*_session_to_claude` (or reuse Pi's), `*_prompt_count`, `*_session_usage` |
| 3 | `tokeneconomizer/plugin/sidecall.py` | `<host>_call` and a branch in `runner_for` (line 270); a stub in `tests/conftest.py` |
| 4 | `tokeneconomizer/plugin/controller.py` | a `_cmd` branch (line 64), so notices name the host's own `/te` command |
| 5 | `tokeneconomizer/pricing.py` | `AGENT_DEFAULT_MODEL` |
| 6 | `tokeneconomizer/plugin/cli.py` | `cmd_doctor` checks for the host (the `--host` choices come from `HOSTS`) |
| 7 | `tokeneconomizer/trace/catalog.py` | `catalog_sources` (only when tool pruning is wired) |
| 8 | `tokeneconomizer/plugin/denylist.py` | `_SETTINGS` / `_UNITS` and the `apply`/`restore` dispatch (only when pruning is wired) |
| 9 | `plugin/<host>/` | the host's package and manifest |
| 10 | tests | `test_live_core.py::test_host_detection`; `test_live_hooks.py` `_guard_turn`/`_stuck` payload shapes and the parametrized guard test; `test_plugin_package.py` manifest pins; a five-trap-session end-to-end test like Pi's (`test_live_hooks.py:915`) |
| 11 | docs | `docs/PLUGIN.md` §3 (a verified-behaviour block), `plugin/README.md` (an install section), the host list in `README.md`, `CLAUDE.md` and `docs/ARCHITECTURE.md` |

`scripts/build_plugin.py` copies all of `plugin/`, so it needs no change.

## 3. The two hosts side by side

| Need | Hermes Agent | OpenClaw |
|---|---|---|
| Plugin language / process | Python, in the gateway process (`plugin.yaml` + `register(ctx)`) | TS/ESM, in the gateway process (`openclaw.plugin.json` + `definePluginEntry`) |
| Template in this repository | none (first in-process Python host) | `plugin/pi/tokeneconomizer.ts` (OpenClaw vendored Pi but does not load Pi extensions) |
| Inject facts, cache-safe | `ctx.register_system_prompt_section` (frozen per session, ≤4000 chars, receives cwd) | `before_prompt_build` → `appendSystemContext` (kept stable every turn) |
| Turn start | `pre_llm_call` (`turn_id`) | `before_prompt_build` (no turn id: numbered by the shim, as on Pi) |
| Tool result plus a nudge | `transform_tool_result` (returning a string replaces the result) | `after_tool_call` (observe only); nudge through agent tool-result middleware |
| Turn end | `on_session_end` (fires **after every turn**) | `agent_end` |
| Session end | `on_session_finalize` | `session_end` (2 s shared drain on shutdown) |
| Repository cwd | `ctx.session_cwd()`; `sessions.cwd` / `git_repo_root` in `state.db` | `before_prompt_build` ctx `workspaceDir`; session header `cwd`; exec `details.cwd` |
| Finished log | SQLite `~/.hermes[/profiles/<p>]/state.db` (stdlib `sqlite3` reads it) | SQLite with zstd for events ≥1 KiB (**unreadable** without zstd before Python 3.14) |
| Exit code | terminal result JSON `{"output","exit_code","error"}` | exec `details.exitCode` (whether the hook sees it is unverified) |
| Tool pruning lever | `agent.disabled_toolsets` (per profile, YAML) or `llm_request` middleware | `before_prompt_build` → `toolsAllow` (per turn, no config edit) |
| User command | `ctx.register_command("te")` | `api.registerCommand({name:"te"})` |
| Install | `hermes plugins install` / pip entry point `hermes_agent.plugins`, then `enable` | `openclaw plugins install git:…`, then `enable`, plus `allowConversationAccess` |

## 4. Decisions shared by both specs

1. **Keep the subprocess boundary.** Both hosts can call plugin code in-process. Each shim still
   spawns `te-hook --host <name>`, as Pi's does, for four reasons:
   - the core stays one implementation;
   - one protocol stays covered by the tests;
   - a crash or hang in the core can never take down the long-running gateway;
   - the core's per-process assumptions hold: stdout is redirected, and state is re-read per
     call.

   Spawn cost is about one Python start per tool call. That already holds on Pi.
2. **No pre-tool hook.** It matches the four existing hosts and avoids the fail-closed path on
   both new ones.
3. **Fail open in the shim.** Every callback catches everything and returns the host's
   "no-op" value (`None` / `undefined`). Each per-callback budget is shorter than the host's
   own timeout.
4. **Tool pruning is phase 2.** Pi ships without pruning. A host is not in
   `denylist._SETTINGS` until its lever is verified. Both specs name the lever to use.
5. **Shell tool → `bash {"command"}` in `Host.tool_call`,** as Codex does. This keeps
   `loopguard/state.canon_tool` and `microskills/observe.py` unchanged. The other tools map onto
   the names `canon_tool` already knows (`view`, `edit`, `create`).
6. **The plugin's own text makes no claims of effect** (CLAUDE.md). This covers manifests,
   descriptions and command output.

## 5. Known issue found on the way (out of scope)

`tests/test_plugin_package.py:65-68` puts the Codex `SessionEnd` ≤ 3 s assertion under
`if host == "copilot":`, so that assertion never runs. Fix it separately.
