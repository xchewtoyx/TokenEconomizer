# Spec: the OpenClaw host

Target: `openclaw/openclaw` version `2026.10.8`, researched at `953eba4`. Host name: `openclaw`.
The §9 items were settled from source at `4a0ab09` (still `2026.10.8`) and against the npm
release `2026.10.8` (`dc2523e`); file:line citations are at `4a0ab09`.

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
- If OpenClaw's loader cannot import across directories, copy the helper instead, and add a
  test that the two copies are identical. **Done (copy):** OpenClaw loads a plugin from a
  captured copy of its sources, with containment checks [V `docs/plugins/architecture.md:181-183`],
  so an import from outside a linked `plugin/openclaw/` is not safe to assume [?]. Pi imports
  `plugin/shared/te-spawn.ts`; `plugin/openclaw/te-spawn.ts` is a byte-identical copy. The
  launchers are found from `api.rootDir`, the real install directory [V
  `src/plugins/registry.ts:266,280`], not from `import.meta.url`, which points into the copy.
- **Entry.** `definePluginEntry` only returns `{ id, name, description, configSchema, register }`
  [V `src/plugin-sdk/plugin-entry.ts:246-271`], and the loader accepts any default export with a
  `register` function [V `src/plugins/module-export.ts:33-37`]. The plugin exports that object
  itself, so it imports nothing from `openclaw/plugin-sdk` (verified: the release loads it).

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
  sessions and `trigger` values `cron` / `heartbeat` [V triggers are `cron | heartbeat | user`,
  `src/plugins/hook-types.ts:193`; incognito keys match `:incognito-`,
  `packages/session-url-contract/src/session-key.ts:6-13`].
- **Mode.** The child's environment carries `TE_OPENCLAW_CHANNEL=<ctx.channel>`, which is used
  for `headless`.

## 2. Event mapping

| OpenClaw hook | Context / payload used | → TE event | Answer applied |
|---|---|---|---|
| `session_start` | `sessionId, resumedFrom` | (none: no workspace in ctx [V]) | records `resumedFrom` |
| `before_prompt_build` (first time per session) | ctx `workspaceDir`, `sessionId`; `messages` | `SessionStart`, `source = resumedFrom \|\| messages.length ? "resume" : "startup"` | keeps `additionalContext` as the session's facts |
| `before_prompt_build` (every time) | as above | `UserPromptSubmit`, `prompt_id = "<sid>:<n>"` | returns `{ appendSystemContext: facts }` with the **same text every turn**, so the cache stays stable [V `hooks/prompt-and-session.md`] |
| `after_tool_call` | (not registered: the middleware runs first, §9.1) | (none) | (none) |
| agent tool-result middleware | `toolCallId, toolName, args, cwd, isError, result{content, details}` | `PostToolUse`, or `PostToolUseFailure` when `isError` | appends the redirect to the result text |
| `agent_end` | `messages` | `Stop` | appends the turn's messages to the session log (§4); notices to `api.logger` (§9.4) |
| `session_end` | `sessionId, reason, sessionFile` | `SessionEnd` | none (the fold is detached) |
| `before_tool_call` | (not registered) | (none) | (none) |

Notes:

- **Why `SessionStart` is deferred.** `session_start` carries no workspace, and the core pins a
  session to a repository by its `cwd`. `before_prompt_build` is the first event that has
  `workspaceDir`, so the shim sends `SessionStart` there, followed by `UserPromptSubmit`.
- **This is Pi's `prompt_context = True` pattern:** the facts are re-sent on every prompt.
- **Resume and reload: do not reuse Pi's `carriedSection`.**
  - That function reads Pi's named system sections. OpenClaw's `appendSystemContext` text is
    never stored as one, so on OpenClaw it would find nothing and drop the facts.
  - Rely on the core instead. `OpenClaw` leaves `keeps_start_context = False`, so a
    `SessionStart` with `source: "resume"` returns the text that session was first given. The
    core stores it as `injected_text` in `sessions/<id>.json` (controller.py:116).
  - The shim sends that `SessionStart` on the first `before_prompt_build` it sees for a
    session. This covers a resumed session and the first prompt after a plugin reload, which
    wipes the in-memory facts.
  - Tests: resume and reload with a non-empty fact block must return the same text.
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
    order. If the middleware runs first, it must itself spawn the `PostToolUse` dispatch;
    then `after_tool_call` is not registered. **Settled: the middleware runs first** (§9.1),
    so it does.
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
  **Settled:** no hook context names its runtime; only the middleware's does (`ctx.runtime`,
  `src/plugins/agent-tool-result-middleware-types.ts`), and `llm_output.harnessId` arrives after
  the model call. OpenClaw's own runtime builds the prompt-hook context with `activeProjectKeys`
  (`src/agents/embedded-agent-runner/run/attempt-prompt-build.ts:139-150`); the Codex runtime's
  has none (`extensions/codex/src/app-server/run-attempt-context.ts:100-130`). So
  `before_prompt_build` acts only when that field is an array, and `agent_end`, the middleware,
  `session_end` and `/te` act only on sessions it started. That field is not documented as a
  runtime marker [?]: if a later OpenClaw drops it, the plugin goes quiet, never wrong. `te
  doctor` does not report the runtime.
- **Code Mode** (`tools.codeMode`) hides tool calls behind `exec` with
  `toolKind: "code_mode_exec"` [V]. The shim maps such calls to `bash` like any other exec. The
  loop guard sees one opaque command per batch. Document this.

## 3. `class OpenClaw(Host)` in `tokeneconomizer/plugin/hosts.py`

```python
class OpenClaw(Host):
    name = "openclaw"
    prompt_context = True        # the shim re-appends the facts every prompt
    keeps_start_context = False  # resume and reload get the stored injected_text back (§2)
    relays_notices = True        # no plugin API shows the person a notice (§9.4)
    interrupt = "Send /stop to stop it."   # [V docs/tools/slash-commands.md:238,651]
    end_run = "Press Ctrl+C to end the run."   # [V docs/cli/agent.md: SIGINT cancels]

    def turn_key(self, p): return str(p.get("prompt_id") or "")
    def headless(self, env, transcript=None):
        return not env.get("TE_OPENCLAW_CHANNEL", "").strip()   # TUI and Control UI are `webchat` (§9.8)
    def tool_call(self, p): ...
    def mechanism_trace(self, tp, workdir):   # the shim writes a Pi-format log, so reuse Pi's reader
        out = os.path.join(workdir, "openclaw_trace.jsonl")
        n = trace.pi_session_to_claude(tp, out)
        return out if n is not None else None
    def prompt_count(self, tp): return trace.pi_prompt_count(tp)
    def session_usage(self, tp): return trace.pi_session_usage(tp)
```

The shim sends the tool result in Pi's shape: `tool_response = {content, isError, exit_code}`,
with `exit_code` taken from `result.details.exitCode` when present [V in the middleware: §9.3]. That lets `tool_call`
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
- `default_host` (`OPENCLAW_SHELL=exec`, set on the exec tool's shell [V
  `src/gateway/operator-cli-message-input.ts:6`, `docs/cli/agent.md:14`]);
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
  (`tokeneconomizer/trace/pi.py:31`). Messages already written are skipped.
  - That state comes from the log file on disk, never from memory, because a plugin reload
    wipes memory. Before appending, the shim reads the log's message entries.
  - If `agent_end` passes the whole session's messages, the first N are skipped, where N is
    the number of entries already written.
  - If it passes only the turn's messages [?: it passes the attempt's `messagesSnapshot`,
    `src/agents/embedded-agent-runner/run/attempt-finalize.ts:367-380`], an entry is skipped when its
    `(role, timestamp, toolCallId)` is already in the log. **Changed:** two replies can share a
    millisecond, so a message is known by a hash of its whole logged content, counted: a batch
    skips as many copies of a message as the log holds.
  - Either way, a reload followed by a repeated `agent_end` appends nothing twice.
- **Tool names are normalized as they are written.** Each `toolCall` block's `name` gets the
  §3 mapping (`exec`/`bash` → `bash` with `arguments.command`, and so on) before it lands in
  the log. `pi_session_to_claude` copies names unchanged, and the micro-skill observer reads
  only `bash`/`shell` (`microskills/observe.py`). An `exec` left as `exec` would teach the fold
  nothing, even though the live loop guard saw it as `bash`.
  - The live payload's `tool_name` is left as OpenClaw sent it; only the log is renamed.
  - `toolResult.toolName` is renamed to match.
- **Usage.** It is read from the assistant messages' `usage{input, output, cacheRead,
  cacheWrite, cost}`, the fields `pi_session_usage` already reads. Check that against a
  recorded session [?; the type requires it: `usage: Usage` with those fields,
  `packages/llm-core/src/types.ts:316-345,525-551`]. If assistant messages lack usage, record `llm_output` usage as extra
  entries instead.
- **Privacy.** The log holds conversation text, so it falls under `docs/PLUGIN.md` §4. It is
  deleted after a successful fold, keeping only what the other hosts keep.

The SQLite store is a fallback for sessions whose `agent_end` was never seen. It is read only
when `compression.zstd` is importable. Phase 2.

## 5. Side calls

`sidecall.openclaw_call(prompt, model)` runs OpenClaw's own one-shot agent invocation with tools
denied. The exact command (for example `openclaw agent --local --message … --json` with a
`tools.deny: ["*"]` override) had to be settled before wiring `runner_for`.
**Settled (§9.6):** `openclaw agent exec --json --message-file - --cwd <tmp>` with
`--config-override '{"tools":{"deny":["*"]}}'`; the release has no `--config-override`, so then a
temporary config beside the person's that `$include`s it and adds the deny.

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
openclaw plugins install git:github.com/xchewtoyx/TokenEconomizer@<ref>   # installs the repository root (§9.7)
openclaw plugins enable tokeneconomizer
openclaw config set plugins.entries.tokeneconomizer.hooks.allowConversationAccess true
```

- **Install source.** If install needs the manifest at the repository root, add a root
  `openclaw.plugin.json`, and an `openclaw` key in the root `package.json` that points at
  `plugin/openclaw/index.ts`. The root `package.json` already serves Pi.
  `test_plugin_package.py` pins root and `plugin/` to agree, as it does for Pi.
- **Development.** For local development, `openclaw plugins install --link ./plugin/openclaw`.
- **Capability consent.** It is shown at install. Non-interactive installs need
  `--accept-capabilities` [V], and a linked local path also needs `--force`, or the install is
  cancelled as outside ClawHub review [V: live run 40772314518].
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

Status after reading the source (v1 implementation, `claw-port-openclaw`). [V] items are read
from source, not yet observed on a running gateway with a model.

1. The order of `after_tool_call` and the tool-result middleware for one `toolCallId`, and that
   middleware text is what the model reads. **[V] Middleware first.** Agent-core calls
   `afterToolCall` before it emits `tool_execution_end` (`packages/agent-core/src/types.ts:341`);
   the session's `afterToolCall` emits the extension `tool_result` event
   (`src/agents/sessions/agent-session-base.ts:211-231`), whose handler runs the middleware and
   returns its `content`/`details` as the result (`src/agents/embedded-agent-runner/extensions.ts:59-110`).
   `after_tool_call` fires later, fire-and-forget, from `handleToolExecutionEnd`
   (`src/agents/embedded-agent-subscribe.handlers.tools.completion.ts:437-461`) and sees the
   middleware's result. So the middleware sends `PostToolUse` itself and `after_tool_call` is not
   registered. A middleware that throws replaces the result with a failure
   (`src/agents/harness/tool-result-middleware.ts:280-282`): it must never throw. That the model
   reads the appended text: [?] until a model run.
2. Whether `before_prompt_build`'s `workspaceDir` is the effective execution or project
   directory, or the persona home. **[V] Neither, exactly:** the hook context carries the run's
   `workspaceDir` and `sandboxed` (`src/agents/embedded-agent-runner/run/agent-hook-context.ts:45-46`),
   while tools run in `requestedCwd ?? effectiveWorkspace`, and in the sandbox's own path when
   sandboxed (`src/agents/workspace-sandbox.ts:164-168,190-192`). The §2 order stands, with one
   change: in a sandboxed session an exec's `details.cwd` is a container path the gateway cannot
   resolve, so only `workspaceDir` counts there. The first-command limitation remains.
3. Whether the exit code reaches the plugin. **[V] In the middleware:** its event carries the
   tool's raw `details` (`extensions.ts:77-80`), which for exec is `ExecToolDetails` with
   `exitCode` and `cwd` (`src/agents/bash-tools.exec-types.ts:207-226`), and its own `cwd`
   (`src/plugins/agent-tool-result-middleware-types.ts:14`).
4. How a notice reaches the person. **[V] It cannot, from a plugin:** the API has a logger
   (`src/plugins/plugin-api.types.ts:219`); `steerActiveRun` steers the model and shows only in
   the Control UI (`plugin-api.types.ts:132`); `enqueueSystemEvent` queues model context. So
   notices go to `api.logger`, and `relays_notices = True` sends the still-stuck notice through
   the model's reply.
5. That assistant messages in `agent_end` carry `usage`. [?] on a recorded session; the type
   requires `usage` with `input, output, cacheRead, cacheWrite, cost`
   (`packages/llm-core/src/types.ts:316-345,525-551`), the fields `pi_session_usage` reads.
   Whether `messages` is the whole session or the turn: it is the attempt's `messagesSnapshot`
   (`run/attempt-finalize.ts:367-380`); the log writer's dedupe handles both.
6. The one-shot CLI for side calls, and how to mark its sessions. **[V]** `openclaw agent exec`
   runs one embedded turn without the gateway, in a temporary state directory
   (`src/cli/program/register.agent-turn.ts:103-136`, `docs/cli/agent.md`). It loads installed
   plugins in its own process, so `TE_SIDECALL=1` in its environment reaches this plugin; no
   agent-id marker is needed. Tools are denied with `--config-override` in source, which the
   release (`dc2523e`) lacks; there the deny goes in a temporary config beside the person's that
   `$include`s it (`$include` reads only inside the config directory,
   `src/config/includes.ts:430-437`). Run against the release with no model login: the config
   was accepted, and the run stopped at authentication. A real answer: [?].
7. Whether `openclaw plugins install git:` accepts a subdirectory. **[V] No:** it installs the
   clone's root (`src/plugins/git-install.ts:355-363`, `packageDir: repoDir`). So the root
   carries `openclaw.plugin.json` and an `openclaw` key in `package.json`, pinned to agree with
   `plugin/openclaw/`. The `git:` route itself (and its `npm install` of the root, which also
   fetches the Pi peer dependency): [?]. The documented install is `--link plugin/openclaw`,
   verified to load on the release.
8. The channel and trigger values. **[V]** Triggers are `cron | heartbeat | user`
   (`src/plugins/hook-types.ts:193`). The TUI sends through `chat.send`
   (`src/tui/gateway-chat.ts:315`), whose origin channel is the internal `webchat`
   (`src/gateway/server-methods/chat-origin-routing.ts:115`, `src/utils/message-channel-constants.ts:3`),
   as for the Control UI; messaging channels name themselves. So `headless` is "no channel".
   What channel a CLI `agent` turn reports: [?].
9. Whether a plugin can import `plugin/shared/` relative to its root. [?] Sources are captured
   with containment checks (`docs/plugins/architecture.md:181-183`); designed around with a
   pinned copy (§1).

Also settled: hook budgets are 15 s for `before_prompt_build` and 30 s for `agent_end`
(`src/plugins/hook-types.ts:112-126`); `te` is not a reserved command name
(`src/plugins/command-registration.ts:46-83`); middleware registration needs the manifest
contract and an explicitly enabled plugin (`src/plugins/registry-registrars.ts:375-389`); a
plugin directory owned by another user or world-writable is blocked
(`src/plugins/discovery.ts:170`); a manifest should set `activation.onStartup`
(`docs/plugins/manifest/capabilities.md:250`).

Deviations in v1, and why:

- `SessionStart`'s `source` is `resume` only when the plugin's log for the session exists (a
  reload) or `session_start` named `resumedFrom`, not whenever `messages` is non-empty: a session
  whose first prompt ran outside a repository has history and was never given anything.
- `prompt_id` is `<sid>:<runId>` (counter when there is no run id), and a repeated
  `before_prompt_build` of the same run sends no second `UserPromptSubmit`: the counter restarts
  after a reload, and a retried attempt would otherwise close the turn as interrupted.
- The plugin's log is not deleted after a fold (§4 Privacy): an OpenClaw session outlives a fold
  of its quiet stretch, and the reload-safe dedupe reads that file.
- The plain-object entry instead of `definePluginEntry` (§1).
- **Segments.** The dispatcher folds a session once, after `fold.STALE_S` (600 s) of quiet, and
  an OpenClaw session can last for days. The plugin shows the dispatcher segments,
  `<sessionId>#<n>`, each with its own log: a prompt after 9 minutes without activity (the log's
  mtime, touched on every prompt and tool call, so reload-safe) starts the next one with
  `source: "startup"`, after a `SessionEnd` (`reason: "segment"`; the core only records the
  reason) for the quiet one, serialised behind its `agent_end`, so it folds at once. `agent_end` dedupes across all of a session's segments, so a new segment
  does not take its predecessors' messages. The core is unchanged.

## 10. Tests

- **`test_live_core.py`:**
  - `test_host_detection` gains `openclaw`;
  - `test_the_openclaw_host_reads_what_the_shim_sends`, which covers exec with
    `exit_code != 0` and an `error` field.
- **`test_live_hooks.py`:**
  - the `_guard_turn` / `_stuck` shapes and the parametrized guard test gain `openclaw`;
  - a five-trap-session end-to-end test reusing `_pi_session`. The log is the same format,
    written the way the shim writes it: linear `parentId` chain, no tree branches, tool names
    already normalized. It asserts a shell observation from a trap `exec` call that reached
    the fold, not only a loop-guard result;
  - resume and plugin reload with a non-empty fact block: the second `SessionStart`
    (`source: "resume"`) returns the first one's text.
- **`test_plugin_package.py`:**
  - `openclaw.plugin.json` and `package.json` agree on the entry, and the contracts block is
    present;
  - the spawn line includes `"--host", "openclaw"`;
  - no handler registers `before_tool_call`;
  - the shared spawn helper is the one Pi uses.
- **A Node unit test** in the style of the existing `carriedSection` extraction: feed `agent_end`
  twice with overlapping messages, and check that the log's `parentId` chain and the skip-count
  are right. Repeat with a fresh shim instance between the two calls, which simulates a reload,
  and check that nothing is appended twice.
- **Recorded sessions** (CLAUDE.md): before release, record trap sessions on a real OpenClaw
  gateway with an OpenRouter model, on the embedded runtime, and check the fold and injection
  against them.
