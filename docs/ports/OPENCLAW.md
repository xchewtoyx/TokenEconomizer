# Spec: the OpenClaw host

Target: `openclaw/openclaw` version `2026.10.8`, researched at `953eba4`. Host name: `openclaw`.

Read [`README.md`](README.md) first. It covers what the core asks of a host, the registration
checklist, and the decisions shared with Hermes.

OpenClaw docs paths are publicly available at `docs.openclaw.ai/<path>`. [V] means verified in
source or docs. [?] means unverified: it must be settled before release (§9).

OpenClaw marks every plugin API as experimental [V `docs/plugins/sdk-overview.md`]. The
manifest therefore pins `openclaw.compat.pluginApi`, and `te doctor` reports a version
mismatch.

---

## 1. Shape

```
plugin/openclaw/
  openclaw.plugin.json   { id: "tokeneconomizer", configSchema: {}, contracts: { agentToolResultMiddleware: ["openclaw"] } }
  package.json           { "type": "module", "openclaw": { "extensions": ["./index.ts"], "compat": { "pluginApi": "…" } } }
  index.ts               export default definePluginEntry({ id, name, description, register(api) { … } })
```

OpenClaw vendored Pi's agent core but **does not load Pi extensions**. The route that did so,
`registerEmbeddedExtensionFactory`, was removed [V `docs/plugins/sdk-migration.md`]. So
`plugin/pi/tokeneconomizer.ts` cannot be reused as is. It is still the template:

- Lift its spawn helper (spawn `te-hook --host <name>`, JSON on stdin, last stdout line, 60 s
  SIGKILL, `{}` on any failure) into `plugin/shared/te-spawn.ts`. Import it from both
  extensions, and have `test_plugin_package.py` pin it in one place.
- If OpenClaw's loader cannot import across directories [?], copy the helper instead, and add a
  test that the two copies are identical.

Rules:

- **Fail open.** Every handler is wrapped in `try/catch` and returns `undefined`. OpenClaw
  blocks a tool call when `before_tool_call` throws or times out [V `hooks/reference.md`]. The
  plugin registers no `before_tool_call`, but the rule applies to every handler.
- **Timeouts.** Each `api.on` call sets `timeoutMs`. Spawns time out below it, because a
  timeout does not cancel the handler's work [V].
- **State on disk only.** Plugin reload swaps the instance [V]. In-memory maps
  (`sessionKey → cwd`, the prompt counter) are rebuilt from the first event after a reload,
  which is acceptable.
- **Global registrations.** Every handler filters on `ctx.sessionId`, and ignores incognito
  sessions and `trigger` values `cron` / `heartbeat` [?].
- **Mode.** The child's environment carries `TE_OPENCLAW_CHANNEL=<ctx.channel>`, which is used
  for `headless`.

## 2. Event mapping

| OpenClaw hook | Context / payload used | → TE event | Answer applied |
|---|---|---|---|
| `session_start` | `sessionId, resumedFrom` | (none: no workspace in ctx [V]) | records `resumedFrom` |
| `before_prompt_build` (first time per session) | ctx `workspaceDir`, `sessionId`; `messages` | `SessionStart`, `source = resumedFrom \|\| messages.length ? "resume" : "startup"` | keeps `additionalContext` as the session's facts |
| `before_prompt_build` (every time) | as above | `UserPromptSubmit`, `prompt_id = "<sid>:<n>"` | returns `{ appendSystemContext: facts }` with the **same text every turn**, so the cache stays stable [V `hooks/prompt-and-session.md`] |
| `after_tool_call` | `toolName, params, result, error, toolCallId` | `PostToolUse`, or `PostToolUseFailure` when `error` is set | none (observe only [V]); a redirect is queued for the middleware |
| agent tool-result middleware | the tool result for `toolCallId` | (none) | appends any queued redirect for that call to the result text |
| `agent_end` | `messages` | `Stop` | appends the turn's messages to the session log (§4); notices via `api.runtime` UI / log [?] |
| `session_end` | `sessionId, reason, sessionFile` | `SessionEnd` | none (the fold is detached) |
| `before_tool_call` | (not registered) | (none) | (none) |

Notes:

- **Why `SessionStart` is deferred.** `session_start` carries no workspace, and the core pins a
  session to a repository by its `cwd`. `before_prompt_build` is the first event that has
  `workspaceDir`, so the shim sends `SessionStart` there, followed by `UserPromptSubmit`.
- **This is Pi's `prompt_context = True` pattern:** the facts are re-sent on every prompt. A
  resumed session is given back the section it already carries, using Pi's
  `carriedSection` logic. Reuse that function.
- **Which directory counts.** By default `workspaceDir` is the persona home
  (`~/.openclaw/workspace`), not a repository [V]. Sessions can run in a registered project
  checkout or a worktree [V]. The `cwd` sent to the core is chosen in this order:
  1. the `cwd` of the first exec call, from `details.cwd`;
  2. a `workspaceDir` that is inside a git checkout;
  3. nothing.

  With nothing, the shim stays silent: no events are sent, which is what happens on every host
  outside a repository. The consequence is that the facts can be injected only from the second
  prompt of a session whose first prompt ran no command. That is documented as a known
  limitation, and §9.2 may remove it.
- **Delivering the nudge.**
  - Preferred: agent tool-result middleware
    (`api.registerAgentToolResultMiddleware`, declared under `contracts`) [V
    `sdk-overview/host-hooks.md`]. It is the analogue of Pi's steering message and of Hermes'
    `transform_tool_result`.
  - Ordering: `after_tool_call` and the middleware for the same `toolCallId` may run in either
    order [?]. If the middleware runs first, it must itself spawn the `PostToolUse` dispatch;
    then `after_tool_call` is not registered.
  - Fallbacks:
    - if middleware is unavailable, return the redirect as `prependContext` on the next
      `before_prompt_build`;
    - `enqueueNextTurnInjection` is too late for a mid-turn redirect.
- **Permissions.** `before_prompt_build` and `agent_end` need
  `plugins.entries.tokeneconomizer.hooks.allowConversationAccess: true` [V]. Without it the
  plugin can neither inject facts nor log a turn. The install section says so, and `te doctor`
  checks it.
- **Runtimes.** Support the embedded `openclaw` runtime only in v1. On the `codex` runtime the
  transcripts are Codex-native, and `toolsAllow` and parameter rewrites are rejected [V]. There,
  every handler returns `undefined` and `te doctor` says the runtime is unsupported.
- **Code Mode** (`tools.codeMode`) hides tool calls behind `exec` with
  `toolKind: "code_mode_exec"` [V]. The shim maps such calls to `bash` like any other exec. The
  loop guard sees one opaque command per batch. Document this.

## 3. `class OpenClaw(Host)` in `tokeneconomizer/plugin/hosts.py`

```python
class OpenClaw(Host):
    name = "openclaw"
    prompt_context = True        # the shim re-appends the facts every prompt
    relays_notices = False       # [?] depends on §9.4
    interrupt = "..."            # [?]
    end_run = "..."

    def turn_key(self, p): return str(p.get("prompt_id") or "")
    def headless(self, env, transcript=None):
        return env.get("TE_OPENCLAW_CHANNEL", "") not in ("tui", "web", "cli")   # [?] channel names
    def tool_call(self, p): ...
    def mechanism_trace(self, tp, workdir):   # the shim writes a Pi-format log, so reuse Pi's reader
        out = os.path.join(workdir, "openclaw_trace.jsonl")
        n = trace.pi_session_to_claude(tp, out)
        return out if n is not None else None
    def prompt_count(self, tp): return trace.pi_prompt_count(tp)
    def session_usage(self, tp): return trace.pi_session_usage(tp)
```

The shim sends the tool result in Pi's shape: `tool_response = {content, isError, exit_code}`,
with `exit_code` taken from `result.details.exitCode` when present [?]. That lets `tool_call`
share Pi's result handling. It maps tool names as follows:

| OpenClaw tool | → name | args |
|---|---|---|
| `exec` / `bash` | `bash` | `{"command": params.command}` |
| `read` | `view` | (from `params`) |
| `edit` / `apply_patch` | `edit` | (from `params`) |
| `write` | `create` | (from `params`) |
| anything else | unchanged | unchanged |

Register the host in:

- `HOSTS`;
- `default_host` (`OPENCLAW_*` env marker [?]);
- `controller._cmd` → `"/te {name}"`;
- `pricing.AGENT_DEFAULT_MODEL`.

## 4. Finished-session log: the plugin writes it

OpenClaw's own store does not suit a stdlib reader:

- runtime transcripts live in SQLite
  (`~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite`, or PostgreSQL) [V];
- every event of 1 KiB or more is **zstd-compressed** [V `src/config/sessions/transcript-payload.ts`];
- Python's standard library decodes zstd only from 3.14, and the core supports 3.10.

So the shim keeps its own log, in **Pi session format** (which is what OpenClaw's events
already are: session version 4 [V]):

- **Path:** `<TOKENECONOMIZER_HOME>/hosts/openclaw/sessions/<sessionId>.jsonl`. It is sent as
  `transcript_path` on every event.
- **Header**, on the first `before_prompt_build`:
  `{"type":"session","version":4,"id":sid,"timestamp":…,"cwd":<chosen cwd>}`.
- **On `agent_end`:** append the turn's `messages` the host passes (already decompressed). Each
  entry is `{"type":"message","id":…,"parentId":<previous id>,"timestamp":…,"message":m}`. The
  `parentId` chain is required: `trace.pi_active_branch` walks it from the last entry
  (`tokeneconomizer/trace/pi.py:31`). Messages already written (tracked by count) are skipped.
- **Usage.** It is read from the assistant messages' `usage{input, output, cacheRead,
  cacheWrite, cost}`, the fields `pi_session_usage` already reads. Check that against a
  recorded session [?]. If assistant messages lack usage, record `llm_output` usage as extra
  entries instead.
- **Privacy.** The log holds conversation text, so it falls under `docs/PLUGIN.md` §4. It is
  deleted after a successful fold, keeping only what the other hosts keep.

The SQLite store is a fallback for sessions whose `agent_end` was never seen. It is read only
when `compression.zstd` is importable. Phase 2.

## 5. Side calls

`sidecall.openclaw_call(prompt, model)` runs OpenClaw's own one-shot agent invocation with tools
denied. The exact command (for example `openclaw agent --local --message … --json` with a
`tools.deny: ["*"]` override) is unverified [?]. Settle it before wiring `runner_for`.

A side call starts a session, so it must carry `TE_SIDECALL=1`. The shim reads that variable
from the gateway environment and cannot see it per session. The side-call session therefore
also needs a recognisable marker, such as a dedicated agent id (`te-sidecall`) that the shim
ignores.

## 6. User command

`api.registerCommand({ name: "te", description, acceptsArgs: true, handler })`:

- The command context has `sessionId` and `sessionKey` but no workspace [V
  `src/plugins/plugin-command.types.ts`].
- The handler looks up the cwd recorded for `sessionId`, spawns
  `scripts/te --host openclaw --cwd <cwd> <args>` with `TE_SESSION_ID`, and returns `{ text }`.
- With no recorded cwd it replies that the session has no repository.

## 7. Packaging and install

```
openclaw plugins install git:github.com/xchewtoyx/TokenEconomizer@<ref>   # subdirectory support [?]
openclaw plugins enable tokeneconomizer
openclaw config set plugins.entries.tokeneconomizer.hooks.allowConversationAccess true
```

- **Install source.** If install needs the manifest at the repository root, add a root
  `openclaw.plugin.json`, and an `openclaw` key in the root `package.json` that points at
  `plugin/openclaw/index.ts`. The root `package.json` already serves Pi.
  `test_plugin_package.py` pins root and `plugin/` to agree, as it does for Pi.
- **Development.** For local development, `openclaw plugins install --link ./plugin/openclaw`.
- **Capability consent.** It is shown at install. Non-interactive installs need
  `--accept-capabilities` [V].
- **Python.** The plugin needs `python3` on the gateway host. `te doctor` already checks for it.

## 8. Tool pruning (phase 2)

**The lever.** `before_prompt_build` returns `toolsAllow`, which narrows the current turn's tools
without a config edit [V]. The tools offered are the session's tools minus this repository's
`untool.json` denials and minus the protected set. The shim needs the full tool list to build
the allow-list. It reads that list from `ctx` or `api.runtime` [?], or from
`hosts/openclaw/catalog.json` refreshed by `te toolprune refresh-catalog --host openclaw`.

**Cache rule.** Compute the list once per session and return the same list every turn.

**Protected tools:** `exec`, `process`, `read`, `write`, `edit` and `apply_patch`. Denying
`write` does not deny `apply_patch` [V], so denials are checked per tool, never per group.

**Codex harness.** It rejects a restrictive `toolsAllow` [V]. On that runtime, pruning stays off.

**Registration.** When this lands, add `openclaw` to `denylist._SETTINGS` with a "turn tool
list" unit. The `apply`/`restore` dispatch then writes only TE state, because nothing in
OpenClaw config changes.

## 9. Verify before release (to become a `docs/PLUGIN.md` §3 block)

1. The order of `after_tool_call` and the tool-result middleware for one `toolCallId`, and that
   middleware text is what the model reads.
2. Whether `before_prompt_build`'s `workspaceDir` is the effective execution or project
   directory, or the persona home. If it is the execution directory, the first-command
   limitation in §2 goes away.
3. Whether `after_tool_call.result` carries exec `details.exitCode` (issue #102961).
4. How a notice reaches the person on TUI, web and channels.
5. That assistant messages in `agent_end` carry `usage`.
6. The one-shot CLI for side calls, and how to mark its sessions.
7. Whether `openclaw plugins install git:` accepts a subdirectory.
8. The channel and trigger values that mean headless, cron or heartbeat.
9. Whether a plugin can import `plugin/shared/` relative to its root.

## 10. Tests

- **`test_live_core.py`:**
  - `test_host_detection` gains `openclaw`;
  - `test_the_openclaw_host_reads_what_the_shim_sends`, which covers exec with
    `exit_code != 0` and an `error` field.
- **`test_live_hooks.py`:**
  - the `_guard_turn` / `_stuck` shapes and the parametrized guard test gain `openclaw`;
  - a five-trap-session end-to-end test reusing `_pi_session`. The log is the same format,
    written the way the shim writes it: linear `parentId` chain, no tree branches.
- **`test_plugin_package.py`:**
  - `openclaw.plugin.json` and `package.json` agree on the entry, and the contracts block is
    present;
  - the spawn line includes `"--host", "openclaw"`;
  - no handler registers `before_tool_call`;
  - the shared spawn helper is the one Pi uses.
- **A Node unit test** in the style of the existing `carriedSection` extraction: feed `agent_end`
  twice with overlapping messages, and check that the log's `parentId` chain and the skip-count
  are right.
- **Recorded sessions** (CLAUDE.md): before release, record trap sessions on a real OpenClaw
  gateway with an OpenRouter model, on the embedded runtime, and check the fold and injection
  against them.
