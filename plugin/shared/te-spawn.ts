/**
 * The one way the plugin's TypeScript extensions (Pi, OpenClaw) reach the Python core: spawn a
 * launcher (`scripts/te-hook`, `scripts/te`) with JSON on stdin and read its stdout. It fails
 * open: a launcher that is missing, slow or broken yields "", and `hookOutput` turns that into
 * `{}` (no change). The OpenClaw extension carries a byte-identical copy, pinned by a test.
 */
import { spawn } from "node:child_process";

export const truthy = (v: string | undefined) => ["1", "true", "yes", "on"].includes((v ?? "").trim().toLowerCase());

/** The plugin's own side calls (`TE_SIDECALL=1`) and an explicit `TE_DISABLE=1` do nothing. */
export const disabled = () => truthy(process.env.TE_SIDECALL) || truthy(process.env.TE_DISABLE);

/** Run `path` with `args`, `input` on stdin and `env` added to the environment; resolve to its
 * stdout, or "" when it cannot start or outlives `timeoutMs` (it is then killed). */
export function runLauncher(
  path: string,
  args: string[],
  input: string,
  env: Record<string, string>,
  timeoutMs: number,
): Promise<string> {
  return new Promise((resolve) => {
    let out = "";
    let child;
    try {
      child = spawn(path, args, { env: { ...process.env, ...env }, stdio: ["pipe", "pipe", "ignore"] });
    } catch {
      resolve("");
      return;
    }
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve("");
    }, timeoutMs);
    child.stdout.on("data", (d: Buffer) => (out += d.toString()));
    child.on("error", () => {
      clearTimeout(timer);
      resolve("");
    });
    child.on("close", () => {
      clearTimeout(timer);
      resolve(out);
    });
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

/** The dispatcher's answer: the last line of its stdout as JSON; `{}` on anything else. */
export function hookOutput(out: string): any {
  try {
    return JSON.parse(out.trim().split("\n").pop() || "{}") ?? {};
  } catch {
    return {};
  }
}
