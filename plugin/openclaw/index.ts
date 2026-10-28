/**
 * TokenEconomizer for OpenClaw (openclaw/openclaw).
 *
 * A native OpenClaw plugin that speaks the dispatcher's protocol on OpenClaw's behalf, as the Pi
 * extension does on Pi: each event below becomes one call of `scripts/te-hook --host openclaw`
 * (JSON in, JSON out), and the answer is applied with OpenClaw's own levers. All of the
 * mechanism logic lives in the dispatcher; this file only translates.
 *
 *   before_prompt_build (first per session)  -> SessionStart, then UserPromptSubmit; the facts it
 *                                               returns are kept for the session
 *   before_prompt_build (every prompt)       -> UserPromptSubmit; the facts go back as
 *                                               `appendSystemContext`, the same text every turn
 *   tool-result middleware                   -> PostToolUse; a redirect is appended to the result
 *                                               the model reads
 *   agent_end                                -> the turn's messages join this plugin's own
 *                                               session log (Pi format), then Stop
 *   session_end                              -> SessionEnd, which hands the session to a fold
 *
 * SessionStart waits for the first prompt because `session_start` carries no workspace. A
 * session the shim meets with history already in it (a resumed one, or the first prompt after a
 * plugin reload) is started with `source: "resume"`, and the dispatcher hands back the text that
 * session was first given. The tool-result middleware runs before `after_tool_call` and before
 * the result reaches the model, so it sends PostToolUse itself and `after_tool_call` is not used.
 * No `before_tool_call`: the plugin never blocks a call.
 *
 * Every handler fails open: any error, and a dispatcher that is missing, slow or broken, means
 * "no change". It does nothing under TE_SIDECALL=1 (the plugin's own side calls) or TE_DISABLE=1,
 * in incognito sessions, or on cron and heartbeat turns. `/te <command>` runs the `te` command
 * line for the session's repository.
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { disabled, hookOutput, runLauncher } from "./te-spawn.ts";

const HOST = "openclaw";
// Below OpenClaw's own budgets (before_prompt_build 15 s, agent_end 30 s), which do not cancel a
// handler that overruns them. The first prompt makes two calls.
const PROMPT_MS = 6_000;
const TOOL_MS = 10_000;
const END_MS = 20_000;
const COMMAND_MS = 60_000;

// The names the dispatcher's mechanisms read (`loopguard/state.canon_tool`, `microskills/observe`).
export const TOOL_NAMES: Record<string, string> = {
  exec: "bash",
  bash: "bash",
  read: "view",
  edit: "edit",
  apply_patch: "edit",
  write: "create",
};

type Session = { cwd: string; facts: string; turn: string; runId: string; prompts: number; channel: string };

export function stateRoot(): string {
  return process.env.TOKENECONOMIZER_HOME || join(homedir(), ".tokeneconomizer");
}

export function logPath(sid: string): string {
  return join(stateRoot(), "hosts", HOST, "sessions", `${sid.replace(/[^\w.-]/g, "_")}.jsonl`);
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

/** The repository directory a session log's header names ("" when there is no log yet). */
export function loggedCwd(sid: string): string {
  const head = lines(logPath(sid))[0];
  return head?.type === "session" && typeof head.cwd === "string" ? head.cwd : "";
}

/** A directory inside a git checkout (a `.git` directory or file at it or above). */
function inGit(dir: string | undefined): boolean {
  for (let d = dir || ""; d; d = dirname(d)) {
    if (existsSync(join(d, ".git"))) return true;
    if (dirname(d) === d) break;
  }
  return false;
}

function writeHeader(sid: string, cwd: string) {
  const path = logPath(sid);
  if (existsSync(path) && statSync(path).size > 0) return;
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  appendFileSync(path, JSON.stringify({ type: "session", version: 4, id: sid, timestamp: new Date().toISOString(), cwd }) + "\n");
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

/** Append the messages the log does not hold yet, as one linear `parentId` chain. What is
 * already written is read from the file, never from memory, so a plugin reload followed by
 * another `agent_end` appends nothing twice -- whether it passes the whole session or one turn.
 * Returns how many entries were appended. */
export function appendMessages(sid: string, messages: unknown[]): number {
  const path = logPath(sid);
  const entries = lines(path);
  if (entries[0]?.type !== "session") return 0;
  const written = entries.filter((e) => e?.type === "message");
  const seen = new Set(written.map((e) => keyOf(e.message)));
  // The chain goes on from the last entry written, the leaf `pi_active_branch` starts from.
  let parent = written.length ? written[written.length - 1].id ?? null : null;
  // Past the highest id written, so an unreadable line skipped above cannot cause a repeat.
  const start = 1 + Math.max(0, ...written.map((e) => Number(String(e.id ?? "").slice(1)) || 0));
  let n = start;
  let text = "";
  for (const m of messages ?? []) {
    if (!m || typeof m !== "object" || seen.has(keyOf(m))) continue;
    seen.add(keyOf(m));
    const id = `m${n++}`;
    const ts = typeof (m as any).timestamp === "number" ? new Date((m as any).timestamp).toISOString() : new Date().toISOString();
    text += JSON.stringify({ type: "message", id, parentId: parent, timestamp: ts, message: normalized(m) }) + "\n";
    parent = id;
  }
  if (text) appendFileSync(path, text);
  return n - start;
}

/** The plugin's `scripts/` directory: under the plugin root when OpenClaw installed the whole
 * repository (`git:`), beside it when this directory itself is the plugin root. */
function scriptsDir(root: string): string {
  const options = [join(root, "plugin", "scripts"), join(root, "..", "scripts")];
  return options.find((d) => existsSync(join(d, "te-hook"))) ?? options[0];
}

const ignored = (ctx: any) =>
  disabled() || !ctx?.sessionId || /:incognito-/i.test(String(ctx?.sessionKey ?? "")) || ["cron", "heartbeat"].includes(ctx?.trigger);

export default {
  id: "tokeneconomizer",
  name: "TokenEconomizer",
  description: "Repository facts learned from earlier sessions, and a guard for turns stuck in a failing loop.",
  register(api: any) {
    const scripts = scriptsDir(api.rootDir ?? api.resolvePath?.(".") ?? ".");
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

    async function hook(s: Session, sid: string, payload: Record<string, unknown>, ms: number): Promise<any> {
      if (disabled()) return {};
      const body = { session_id: sid, transcript_path: logPath(sid), cwd: s.cwd, ...payload };
      const out = await runLauncher(join(scripts, "te-hook"), ["--host", HOST], JSON.stringify(body), { TE_OPENCLAW_CHANNEL: s.channel }, ms);
      return hookOutput(out);
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
          if (ignored(ctx)) return undefined;
          const sid: string = ctx.sessionId;
          if (ctx.sandboxed) sandboxed.add(sid);
          let s = sessions.get(sid);
          if (!s) {
            // A log on disk means this session ran before (a resumed session, or a plugin reload).
            const logged = loggedCwd(sid);
            const cwd = logged || found.get(sid) || (inGit(ctx.workspaceDir) ? ctx.workspaceDir : "");
            if (!cwd) return undefined; // no repository yet: an exec call may name one
            s = { cwd, facts: "", turn: "", runId: "", prompts: 0, channel: String(ctx.channel ?? "") };
            sessions.set(sid, s);
            found.delete(sid);
            writeHeader(sid, cwd);
            // Not "history in `messages`": a session whose first prompt ran outside a repository
            // has history too, and was never given anything to be given back.
            const carried = Boolean(logged) || resumed.has(sid);
            const out = await hook(s, sid, { hook_event_name: "SessionStart", source: carried ? "resume" : "startup" }, PROMPT_MS);
            s.facts = out?.hookSpecificOutput?.additionalContext ?? "";
            log(out?.systemMessage);
          }
          s.channel = String(ctx.channel ?? s.channel);
          // One prompt can be built more than once (a retry, a fallback model): one turn per run.
          const runId = String(ctx.runId ?? "");
          if (!runId || runId !== s.runId) {
            s.prompts += 1;
            s.runId = runId;
            s.turn = `${sid}:${runId || s.prompts}`;
            const out = await hook(s, sid, { hook_event_name: "UserPromptSubmit", prompt_id: s.turn, prompt: event?.prompt ?? "" }, PROMPT_MS);
            const extra = out?.hookSpecificOutput?.additionalContext;
            if (extra) s.facts = s.facts ? `${s.facts}\n\n${extra}` : extra;
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
          const content = (event?.result?.content ?? [])
            .filter((c: any) => c?.type === "text")
            .map((c: any) => c.text)
            .join("\n");
          const isError = Boolean(event?.isError);
          const out = await hook(
            s,
            ctx.sessionId,
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
          appendMessages(ctx.sessionId, event?.messages ?? []);
          await hook(s, ctx.sessionId, { hook_event_name: "Stop", prompt_id: s.turn, stop_hook_active: false }, END_MS);
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
          const cwd = sessions.get(sid)?.cwd || loggedCwd(sid);
          sessions.delete(sid);
          resumed.delete(sid);
          sandboxed.delete(sid);
          found.delete(sid);
          if (!cwd) return undefined;
          const s: Session = { cwd, facts: "", turn: "", runId: "", prompts: 0, channel: "" };
          await hook(s, sid, { hook_event_name: "SessionEnd", reason: event?.reason ?? "unknown" }, 1_500);
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
          const sid = String(ctx?.sessionId ?? "");
          const cwd = (sid && (sessions.get(sid)?.cwd || loggedCwd(sid))) || "";
          if (!cwd) return { text: "te: this session has no repository yet (it starts on a prompt in one)." };
          const argv = (ctx?.args || "status").trim().split(/\s+/).filter(Boolean);
          const out = await runLauncher(join(scripts, "te"), ["--host", HOST, "--cwd", cwd, ...argv], "", { TE_SESSION_ID: sid }, COMMAND_MS);
          return { text: out.trim() || "te: no output (is Python 3.10+ on PATH?)" };
        } catch {
          return { text: "te: failed" };
        }
      },
    });
  },
};
