/**
 * TokenEconomizer for OpenClaw (openclaw/openclaw).
 *
 * A native OpenClaw plugin that speaks the dispatcher's protocol on OpenClaw's behalf, as the Pi
 * extension does on Pi: each event below becomes one call of `scripts/te-hook --host openclaw`
 * (JSON in, JSON out), and the answer is applied with OpenClaw's own levers. All of the
 * mechanism logic lives in the dispatcher; this file only translates.
 *
 *   before_prompt_build (first per segment)  -> SessionStart, then UserPromptSubmit; the facts it
 *                                               returns are kept for the segment
 *   before_prompt_build (every prompt)       -> UserPromptSubmit; the facts go back as
 *                                               `appendSystemContext`, the same text every turn
 *   tool-result middleware                   -> PostToolUse; a redirect is appended to the result
 *                                               the model reads
 *   agent_end                                -> the turn's messages join this plugin's own
 *                                               session log (Pi format), then Stop
 *   session_end                              -> SessionEnd, which hands the segment to a fold
 *
 * An OpenClaw session can last for days (a chat keeps its session), while the dispatcher folds a
 * session once, after it has been quiet for `fold.STALE_S`. So the dispatcher is shown segments:
 * `<sessionId>#<n>`, each with its own log, and a prompt after a quiet stretch starts the next.
 *
 * SessionStart waits for the first prompt because `session_start` carries no workspace. A
 * segment the shim meets again after a plugin reload is started with `source: "resume"`, and the
 * dispatcher hands back the text that segment was first given. The tool-result middleware runs
 * before `after_tool_call` and before the result reaches the model, so it sends PostToolUse
 * itself and `after_tool_call` is not used. No `before_tool_call`: the plugin never blocks a call.
 *
 * Every handler fails open: any error, and a dispatcher that is missing, slow or broken, means
 * "no change". It does nothing under TE_SIDECALL=1 (the plugin's own side calls) or TE_DISABLE=1,
 * in incognito sessions, on cron and heartbeat turns, or on a runtime other than OpenClaw's own.
 * `/te <command>` runs the `te` command line for the session's repository.
 */
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, statSync, utimesSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { disabled, hookOutput, runLauncher } from "./te-spawn.ts";

const HOST = "openclaw";
// Below OpenClaw's own budgets (before_prompt_build 15 s, agent_end 30 s), which do not cancel a
// handler that overruns them. The first prompt makes two calls.
const PROMPT_MS = 6_000;
const TOOL_MS = 10_000;
const END_MS = 20_000;
const COMMAND_MS = 60_000;
// A segment quiet this long gets no more turns: below `fold.STALE_S` (600 s), after which the
// dispatcher may fold it, once. The next segment starts with the facts as they are then, so its
// `appendSystemContext` may differ; a gap this long has outlived a provider's prompt cache anyway.
export const QUIET_MS = 540_000;

// The names the dispatcher's mechanisms read (`loopguard/state.canon_tool`, `microskills/observe`).
export const TOOL_NAMES: Record<string, string> = {
  exec: "bash",
  bash: "bash",
  read: "view",
  edit: "edit",
  apply_patch: "edit",
  write: "create",
};

type Session = { seg: string; cwd: string; facts: string; turn: string; runId: string; prompts: number; channel: string };

export function stateRoot(): string {
  return process.env.TOKENECONOMIZER_HOME || join(homedir(), ".tokeneconomizer");
}

const sessionsDir = () => join(stateRoot(), "hosts", HOST, "sessions");
const safe = (sid: string) => sid.replace(/[^\w.-]/g, "_");

/** The dispatcher's session id for segment `n` of an OpenClaw session. */
export const segment = (sid: string, n: number) => `${safe(sid)}#${n}`;

export function logPath(seg: string): string {
  return join(sessionsDir(), `${seg.replace(/[^\w.#-]/g, "_")}.jsonl`);
}

/** The highest segment number logged for an OpenClaw session (0: none). */
export function lastSegment(sid: string): number {
  const prefix = `${safe(sid)}#`;
  let names: string[] = [];
  try {
    names = readdirSync(sessionsDir());
  } catch {}
  let n = 0;
  for (const name of names) {
    const m = name.startsWith(prefix) ? /^(\d+)\.jsonl$/.exec(name.slice(prefix.length)) : null;
    if (m) n = Math.max(n, Number(m[1]));
  }
  return n;
}

function lines(path: string): any[] {
  let text = "";
  try {
    text = readFileSync(path, "utf8");
  } catch {
    return [];
  }
  const out = [];
  for (const line of text.split("\n")) {
    try {
      if (line.trim()) out.push(JSON.parse(line));
    } catch {}
  }
  return out;
}

/** The repository directory a segment log's header names ("" when there is no log yet). */
export function loggedCwd(seg: string): string {
  const head = lines(logPath(seg))[0];
  return head?.type === "session" && typeof head.cwd === "string" ? head.cwd : "";
}

/** Whether a segment's log was touched within `QUIET_MS`. */
function fresh(seg: string): boolean {
  try {
    return Date.now() - statSync(logPath(seg)).mtimeMs < QUIET_MS;
  } catch {
    return false;
  }
}

function touch(seg: string) {
  try {
    const now = new Date();
    utimesSync(logPath(seg), now, now);
  } catch {}
}

/** `paths.is_git_entry`: a `.git` directory holding `HEAD`, or a `.git` file whose `gitdir:`
 * names a directory that exists. */
function isGitEntry(dot: string): boolean {
  try {
    if (statSync(dot).isDirectory()) return existsSync(join(dot, "HEAD"));
    const line = readFileSync(dot, "utf8").slice(0, 4096).trim();
    if (!line.startsWith("gitdir:")) return false;
    const gitdir = line.slice(7).trim();
    return statSync(isAbsolute(gitdir) ? gitdir : resolve(dirname(dot), gitdir)).isDirectory();
  } catch {
    return false;
  }
}

/** `paths.find_repo_root`: is `start` at or below a git checkout? The walk never climbs into a
 * `GIT_CEILING_DIRECTORIES` entry, nor from below the home directory into it. */
export function inRepo(start: string | undefined): boolean {
  if (!start) return false;
  const ceilings = new Set((process.env.GIT_CEILING_DIRECTORIES ?? "").split(":").filter(Boolean).map((c) => resolve(c)));
  let home = "";
  try {
    home = realpathSync(process.env.HOME || homedir());
  } catch {}
  for (let cur = resolve(start); ; ) {
    if (isGitEntry(join(cur, ".git"))) return true;
    const parent = dirname(cur);
    let real = parent;
    try {
      real = realpathSync(parent);
    } catch {}
    if (parent === cur || ceilings.has(parent) || (home && real === home)) return false;
    cur = parent;
  }
}

function writeHeader(seg: string, cwd: string) {
  const path = logPath(seg);
  if (existsSync(path) && statSync(path).size > 0) return;
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  appendFileSync(path, JSON.stringify({ type: "session", version: 4, id: seg, timestamp: new Date().toISOString(), cwd }) + "\n");
}

const keyOf = (m: any) => JSON.stringify([m?.role ?? null, m?.timestamp ?? null, m?.toolCallId ?? null]);

/** A message as the log keeps it: tool names renamed to the ones the dispatcher reads. */
function normalized(m: any): any {
  const out = JSON.parse(JSON.stringify(m));
  if (out?.role === "assistant" && Array.isArray(out.content)) {
    for (const b of out.content) {
      if (b?.type === "toolCall" && typeof b.name === "string") b.name = TOOL_NAMES[b.name] ?? b.name;
    }
  }
  if (out?.role === "toolResult" && typeof out.toolName === "string") out.toolName = TOOL_NAMES[out.toolName] ?? out.toolName;
  return out;
}

/** Append to segment `n` of session `sid` the messages no segment of it holds yet, as one linear
 * `parentId` chain. What is already written is read from the files, never from memory, so a
 * plugin reload followed by another `agent_end` appends nothing twice -- whether it passes the
 * whole session or one turn -- and a new segment does not take its predecessors' messages.
 * Returns how many entries were appended. */
export function appendMessages(sid: string, n: number, messages: unknown[]): number {
  const path = logPath(segment(sid, n));
  const entries = lines(path);
  if (entries[0]?.type !== "session") return 0;
  const seen = new Set<string>();
  // shortcut: reads every earlier segment on each agent_end; keep a key index if sessions grow huge.
  for (let k = 1; k < n; k++) {
    for (const e of lines(logPath(segment(sid, k)))) if (e?.type === "message") seen.add(keyOf(e.message));
  }
  const written = entries.filter((e) => e?.type === "message");
  for (const e of written) seen.add(keyOf(e.message));
  // The chain goes on from the last entry written, the leaf `pi_active_branch` starts from.
  let parent = written.length ? written[written.length - 1].id ?? null : null;
  // Past the highest id written, so an unreadable line skipped above cannot cause a repeat.
  const start = 1 + Math.max(0, ...written.map((e) => Number(String(e.id ?? "").slice(1)) || 0));
  let next = start;
  let text = "";
  for (const m of messages ?? []) {
    if (!m || typeof m !== "object" || seen.has(keyOf(m))) continue;
    seen.add(keyOf(m));
    const id = `m${next++}`;
    const ts = typeof (m as any).timestamp === "number" ? new Date((m as any).timestamp).toISOString() : new Date().toISOString();
    text += JSON.stringify({ type: "message", id, parentId: parent, timestamp: ts, message: normalized(m) }) + "\n";
    parent = id;
  }
  if (text) appendFileSync(path, text);
  else touch(segment(sid, n));
  return next - start;
}

/** The plugin's `scripts/` directory: under the plugin root when OpenClaw installed the whole
 * repository (`git:`), beside it when this directory itself is the plugin root. */
function scriptsDir(root: string): string {
  const options = [join(root, "plugin", "scripts"), join(root, "..", "scripts")];
  return options.find((d) => existsSync(join(d, "te-hook"))) ?? options[0];
}

const ignored = (ctx: any) =>
  disabled() || !ctx?.sessionId || /:incognito-/i.test(String(ctx?.sessionKey ?? "")) || ["cron", "heartbeat"].includes(ctx?.trigger);

// No hook context names its runtime; only the middleware's does (`ctx.runtime`). OpenClaw's own
// runtime builds the prompt-hook context with `activeProjectKeys` (an array, empty outside a
// repository); the Codex runtime's has none. A context without it is left alone.
const embedded = (ctx: any) => Array.isArray(ctx?.activeProjectKeys);

export default {
  id: "tokeneconomizer",
  name: "TokenEconomizer",
  description: "Repository facts learned from earlier sessions, and a guard for turns stuck in a failing loop.",
  register(api: any) {
    const scripts = scriptsDir(api.rootDir ?? api.resolvePath?.(".") ?? ".");
    // OpenClaw session id -> its current segment. Only sessions started on OpenClaw's own runtime
    // get here, so `agent_end`, the middleware and `/te` follow the prompt hook's runtime gate.
    const sessions = new Map<string, Session>();
    const resumed = new Set<string>();
    // Sessions whose tools run in a sandbox, where an exec's directory is a container path the
    // gateway (and the dispatcher) cannot resolve; `workspaceDir` is the gateway-side one.
    const sandboxed = new Set<string>();
    // The directory of a session's first exec call, for a session not started yet.
    const found = new Map<string, string>();
    const log = (text: unknown) => {
      if (typeof text === "string" && text) api.logger?.info?.(text);
    };

    async function hook(s: { seg: string; cwd: string; channel: string }, payload: Record<string, unknown>, ms: number): Promise<any> {
      if (disabled()) return {};
      const body = { session_id: s.seg, transcript_path: logPath(s.seg), cwd: s.cwd, ...payload };
      const out = await runLauncher(join(scripts, "te-hook"), ["--host", HOST], JSON.stringify(body), { TE_OPENCLAW_CHANNEL: s.channel }, ms);
      return hookOutput(out);
    }

    const starting = new Map<string, Promise<Session | undefined>>();
    // Per session, the work that must not overlap a segment change: `agent_end` and the change.
    const queues = new Map<string, Promise<unknown>>();
    function serial<T>(sid: string, fn: () => Promise<T>): Promise<T> {
      const run = (queues.get(sid) ?? Promise.resolve()).catch(() => {}).then(fn);
      queues.set(sid, run);
      run.finally(() => queues.get(sid) === run && queues.delete(sid)).catch(() => {});
      return run;
    }

    /** Start the session's next segment (or, after a plugin reload, its still-fresh last one) and
     * publish it once SessionStart has answered; undefined when it has no repository yet. */
    async function startSegment(sid: string, ctx: any): Promise<Session | undefined> {
      const n = lastSegment(sid);
      const last = n ? segment(sid, n) : "";
      const logged = last ? loggedCwd(last) : "";
      // A segment still fresh on disk is this one again, after a plugin reload.
      const again = Boolean(logged) && fresh(last);
      const cwd = logged || found.get(sid) || (inRepo(ctx.workspaceDir) ? ctx.workspaceDir : "");
      if (!cwd) return undefined;
      const seg = again ? last : segment(sid, n + 1);
      const s: Session = { seg, cwd, facts: "", turn: "", runId: "", prompts: 0, channel: String(ctx.channel ?? "") };
      if (logged && !again) {
        // The quiet segment ends here, so the dispatcher folds it now rather than when, and
        // if, another session starts after it has been quiet for `fold.STALE_S`.
        await hook({ seg: last, cwd: logged, channel: s.channel }, { hook_event_name: "SessionEnd", reason: "segment" }, PROMPT_MS);
      }
      writeHeader(seg, cwd);
      // Not "history in `messages`": a session whose first prompt ran outside a repository has
      // history too, and was never given anything to be given back.
      const carried = again || (n === 0 && resumed.has(sid));
      const out = await hook(s, { hook_event_name: "SessionStart", source: carried ? "resume" : "startup" }, PROMPT_MS);
      s.facts = out?.hookSpecificOutput?.additionalContext ?? "";
      log(out?.systemMessage);
      found.delete(sid);
      sessions.set(sid, s);
      return s;
    }

    /** The segment a session-wide command or end acts on: in memory, else the last one logged. */
    function current(sid: string): { seg: string; cwd: string } | null {
      const s = sessions.get(sid);
      if (s) return s;
      const n = lastSegment(sid);
      const cwd = n ? loggedCwd(segment(sid, n)) : "";
      return cwd ? { seg: segment(sid, n), cwd } : null;
    }

    api.on("session_start", (event: any) => {
      try {
        if (event?.resumedFrom && event?.sessionId) resumed.add(event.sessionId);
      } catch {}
      return undefined;
    });

    api.on(
      "before_prompt_build",
      async (event: any, ctx: any) => {
        try {
          if (ignored(ctx) || !embedded(ctx)) return undefined;
          const sid: string = ctx.sessionId;
          if (ctx.sandboxed) sandboxed.add(sid);
          let s = sessions.get(sid);
          if (s && !fresh(s.seg)) s = undefined; // quiet for a fold's while: the next segment
          if (!s) {
            // One SessionStart per segment: a concurrent build waits for the one in flight.
            let p = starting.get(sid);
            if (!p) {
              p = serial(sid, () => startSegment(sid, ctx)).finally(() => starting.delete(sid));
              starting.set(sid, p);
            }
            s = await p;
            if (!s) return undefined; // no repository yet: an exec call may name one
          }
          touch(s.seg);
          s.channel = String(ctx.channel ?? s.channel);
          // One prompt can be built more than once (a retry, a fallback model): one turn per run.
          const runId = String(ctx.runId ?? "");
          if (!runId || runId !== s.runId) {
            s.prompts += 1;
            s.runId = runId;
            s.turn = `${s.seg}:${runId || s.prompts}`;
            const out = await hook(s, { hook_event_name: "UserPromptSubmit", prompt_id: s.turn, prompt: event?.prompt ?? "" }, PROMPT_MS);
            // The repository map arrives on a segment's first prompt, before any text was sent,
            // and joins the facts; later prompts' context never does, so the text stays the same.
            const extra = out?.hookSpecificOutput?.additionalContext;
            if (extra && s.prompts === 1) s.facts = s.facts ? `${s.facts}\n\n${extra}` : extra;
            log(out?.systemMessage);
          }
          return s.facts ? { appendSystemContext: s.facts } : undefined;
        } catch {
          return undefined;
        }
      },
      { timeoutMs: 15_000 },
    );

    api.registerAgentToolResultMiddleware(
      async (event: any, ctx: any) => {
        try {
          if (disabled() || !ctx?.sessionId || ctx?.runtime !== HOST) return undefined;
          const s = sessions.get(ctx.sessionId);
          const details = event?.result?.details ?? {};
          if (!s) {
            // The first exec call's directory is the session's repository from the next prompt on.
            const cwd = TOOL_NAMES[event?.toolName] === "bash" ? details.cwd || event?.cwd : "";
            if (cwd && !sandboxed.has(ctx.sessionId)) {
              found.set(ctx.sessionId, cwd);
            }
            return undefined;
          }
          touch(s.seg); // a turn at work is not a quiet segment
          const content = (event?.result?.content ?? [])
            .filter((c: any) => c?.type === "text")
            .map((c: any) => c.text)
            .join("\n");
          const isError = Boolean(event?.isError);
          const out = await hook(
            s,
            {
              hook_event_name: isError ? "PostToolUseFailure" : "PostToolUse",
              prompt_id: s.turn,
              tool_name: event?.toolName,
              tool_use_id: event?.toolCallId,
              tool_input: event?.args ?? {},
              tool_response: { content, isError, exit_code: typeof details.exitCode === "number" ? details.exitCode : undefined },
            },
            TOOL_MS,
          );
          log(out?.systemMessage);
          const nudge = out?.hookSpecificOutput?.additionalContext;
          if (!nudge) return undefined;
          const result = event.result;
          return { result: { ...result, content: [...(result?.content ?? []), { type: "text", text: nudge }] } };
        } catch {
          return undefined;
        }
      },
      { runtimes: [HOST] },
    );

    api.on(
      "agent_end",
      async (event: any, ctx: any) => {
        try {
          if (ignored(ctx)) return undefined;
          const s = sessions.get(ctx.sessionId);
          if (!s) return undefined;
          // shortcut: a single tool call longer than QUIET_MS can let its segment be folded under
          // it; the turn's messages then go unlearned. Rare; a keep-alive timer would close it.
          await serial(ctx.sessionId, async () => {
            appendMessages(ctx.sessionId, Number(s.seg.slice(s.seg.lastIndexOf("#") + 1)), event?.messages ?? []);
            await hook(s, { hook_event_name: "Stop", prompt_id: s.turn, stop_hook_active: false }, END_MS);
          });
        } catch {}
        return undefined;
      },
      { timeoutMs: 25_000 },
    );

    api.on(
      "session_end",
      async (event: any, ctx: any) => {
        try {
          const sid = event?.sessionId ?? ctx?.sessionId;
          if (disabled() || !sid) return undefined;
          const cur = current(sid);
          sessions.delete(sid);
          resumed.delete(sid);
          sandboxed.delete(sid);
          found.delete(sid);
          if (!cur) return undefined;
          await hook({ ...cur, channel: "" }, { hook_event_name: "SessionEnd", reason: event?.reason ?? "unknown" }, 1_500);
        } catch {}
        return undefined;
      },
      { timeoutMs: 2_000 },
    );

    api.registerCommand({
      name: "te",
      description: "TokenEconomizer: status | microskills | config | repos (what it learned here, what it did)",
      acceptsArgs: true,
      handler: async (ctx: any) => {
        try {
          const cur = current(String(ctx?.sessionId ?? ""));
          if (!cur) return { text: "te: this session has no repository yet (it starts on a prompt in one)." };
          const argv = (ctx?.args || "status").trim().split(/\s+/).filter(Boolean);
          const out = await runLauncher(join(scripts, "te"), ["--host", HOST, "--cwd", cur.cwd, ...argv], "", { TE_SESSION_ID: cur.seg }, COMMAND_MS);
          return { text: out.trim() || "te: no output (is Python 3.10+ on PATH?)" };
        } catch {
          return { text: "te: failed" };
        }
      },
    });
  },
};
