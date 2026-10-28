#!/usr/bin/env python3
"""The model-backed live test of the Hermes and OpenClaw ports, run by
`.github/workflows/live-ports.yml` (docs/PLUGIN.md §6).

  guard  Before anything is installed: the model is listed on OpenRouter and takes tools, and
         this key's spend this week plus a worst-case estimate of one run stays under the
         budget. Writes `run=true|false` to $GITHUB_OUTPUT; exits 1 only when the model check
         fails (a pinned model that vanished or lost tool support is breakage, not a skip).
  run    The five-session trap of docs/PLUGIN.md §3, in `te probe`'s repository (bare `pytest`
         missing, `python -m unittest` works), through the agent's own CLI on OpenRouter; then
         the assertions. What is under test is the plugin (fact learned, then injected; every
         session folded and priced), not the model's skill: a model that never ran a command,
         or never met the trap often enough to teach it, makes the run inconclusive, not failed.

The key is read from OPENROUTER_API_KEY, sent only to openrouter.ai, and never printed.
Standard library only; imports the plugin's own package for the trap repository and the hosts'
transcript readers.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tokeneconomizer.plugin import hosts, probe  # noqa: E402

API = "https://openrouter.ai/api/v1"
SESSIONS = 5
MAX_TURNS = 12              # Hermes `--max-turns`; OpenClaw has no such switch, its timeout caps it
SESSION_TIMEOUT_S = 300
FOLD_WAIT_S = 90
# Worst case for one session, for the budget estimate: 12 requests of up to ~25k context each,
# and the plugin's own side calls (loop-guard judge) on top.
IN_CAP, OUT_CAP, SIDE_CALLS = 300_000, 15_000, 1.25
RATE_LIMIT = re.compile(r"\b429\b|rate[ -]?limit", re.I)


def _get(path, key=None):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _out(**kv):
    """Step outputs for the workflow (stdout outside Actions)."""
    path = os.environ.get("GITHUB_OUTPUT")
    with open(path, "a", encoding="utf-8") if path else open(os.devnull, "w") as fh:
        for k, v in kv.items():
            fh.write(f"{k}={v}\n")


def _summary(text):
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def estimate(prompt_price, completion_price, sessions=SESSIONS):
    """Worst-case dollars for one run at the given per-token prices."""
    return sessions * (IN_CAP * prompt_price + OUT_CAP * completion_price) * SIDE_CALLS


def model_prices(model):
    """(prompt, completion) $/token for `model`: the most expensive endpoint OpenRouter may route
    to, since it picks among providers per request. Exits 1 when the model is not listed or
    does not take tools."""
    listed = {m["id"]: m for m in _get("/models")["data"]}
    m = listed.get(model)
    if not m:
        print(f"::error::model {model!r} is not listed on OpenRouter; set vars.LIVE_MODEL to a "
              "listed model")
        sys.exit(1)
    if "tools" not in (m.get("supported_parameters") or []):
        print(f"::error::model {model!r} does not list `tools` in its supported parameters on "
              "OpenRouter; the trap needs a model that calls tools")
        sys.exit(1)
    prices = [(float(m["pricing"]["prompt"]), float(m["pricing"]["completion"]))]
    try:
        eps = _get(f"/models/{model}/endpoints")["data"]["endpoints"]
        prices += [(float(e["pricing"]["prompt"]), float(e["pricing"]["completion"])) for e in eps]
    except (urllib.error.URLError, KeyError, TypeError, ValueError):
        print(f"::warning::no endpoint prices for {model}; using the model's listed price")
    return max(p for p, _ in prices), max(c for _, c in prices)


def weekly_usage(key):
    """This key's spend in the current UTC week (Monday start), or None if not reported."""
    w = (_get("/key", key).get("data") or {}).get("usage_weekly")
    return float(w) if isinstance(w, (int, float)) else None


def cmd_guard(args):
    if not os.path.isdir(os.path.join(ROOT, "plugin", args.platform)):
        _summary(f"**{args.platform}**: skipped, `plugin/{args.platform}` is not in this tree "
                 "(the port is not merged here yet).")
        _out(run="false")
        return 0
    p, c = model_prices(args.model)
    est = estimate(p, c)
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("::error::OPENROUTER_API_KEY is not set")
        _out(run="false")
        return 1
    week = weekly_usage(key)
    if week is None:
        # No weekly figure: only the per-run cap and the trigger rules bound the week.
        print("::warning::OpenRouter reported no usage_weekly for this key; falling back to the "
              "per-run cap alone. Set a weekly credit limit on the key itself.")
        go = est <= args.budget
    else:
        go = week + est <= args.budget
    week_s = "unknown" if week is None else f"${week:.4f}"
    _summary(f"**{args.platform}** on `{args.model}`: worst-case run ${est:.4f} "
             f"(at ${p * 1e6:.4f}/M in, ${c * 1e6:.4f}/M out), spent this week {week_s}, "
             f"budget ${args.budget:.2f} -> {'run' if go else 'SKIPPED: over the weekly budget'}")
    if not go:
        print(f"::notice::{args.platform} live test skipped: weekly budget ${args.budget:.2f} "
              f"would be exceeded")
    _out(run="true" if go else "false", estimate=f"{est:.4f}")
    return 0


# ---- the scenario -------------------------------------------------------------------------
def agent_cmd(platform, model, repo, work):
    if platform == "hermes":
        return ["hermes", "chat", "-q", probe.PROMPT, "-Q", "--provider", "openrouter",
                "-m", model, "--max-turns", str(MAX_TURNS), "--yolo"]
    state = os.path.join(work, "openclaw-state")
    os.makedirs(state, exist_ok=True)       # `--state-dir` must exist; it keeps the transcripts
    return ["openclaw", "agent", "exec", "--json", "--cwd", repo, "--state-dir", state,
            "--model", "openrouter/" + model, "--timeout", str(SESSION_TIMEOUT_S - 30),
            probe.PROMPT]


def state_dirs(home, platform):
    return glob.glob(os.path.join(home, "repos", "*", platform))


def te_sessions(home, platform):
    """{session id: its record} for every session the plugin recorded on this host."""
    out = {}
    for d in state_dirs(home, platform):
        for f in glob.glob(os.path.join(d, "sessions", "*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    out[os.path.basename(f)[:-5]] = dict(json.load(fh), _dir=d)
            except (OSError, ValueError):
                pass
    return out


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _lines(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except (OSError, ValueError):
        return []


def folded(home, platform):
    out = {}
    for d in state_dirs(home, platform):
        out.update(_read_json(os.path.join(d, "folded.json"), {}))
    return out


def shell_commands(platform, transcript, work):
    """The shell commands a finished session ran, read through the host's own trace reader."""
    if not transcript:
        return []
    tmp = tempfile.mkdtemp(dir=work)
    try:
        path = hosts.HOSTS[platform]().mechanism_trace(transcript, tmp)
    except Exception:  # noqa: BLE001 -- an unreadable log is reported, not fatal here
        return []
    cmds = []
    for ev in _lines(path) if path else []:
        for b in ((ev.get("message") or {}).get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use" \
                    and str(b.get("name")).lower() == "bash":
                cmds.append(str((b.get("input") or {}).get("command") or ""))
    return cmds


def check(home, platform, work, allow_errors=(), folded_at_end=None):
    """[(status, what)] with status PASS / FAIL / INCONCLUSIVE, from the plugin's state alone."""
    res = []
    sess = te_sessions(home, platform)
    order = sorted(sess, key=lambda s: sess[s].get("started") or 0)
    res.append(("PASS" if len(order) >= SESSIONS else "FAIL",
                f"the plugin recorded {len(order)} of {SESSIONS} sessions"))
    done = folded(home, platform)
    ledger = [r for d in state_dirs(home, platform) for r in _lines(os.path.join(d, "ledger.jsonl"))
              if r.get("kind") == "session"]
    rows = {r.get("session_id"): r for r in ledger}
    for sid in order:
        r = rows.get(sid) or {}
        priced = (r.get("model") and r.get("input_tokens") is not None
                  and r.get("output_tokens") is not None
                  and isinstance(r.get("effective_tokens"), (int, float))
                  and r.get("priced_by") in ("model", "fallback"))
        res.append(("PASS" if priced else "FAIL",
                    f"{sid}: ledger row model={r.get('model')} in={r.get('input_tokens')} "
                    f"out={r.get('output_tokens')} effective={r.get('effective_tokens')} "
                    f"priced_by={r.get('priced_by')}" if r else f"{sid}: no ledger row"))
        if platform == "openclaw":
            tp = sess[sid].get("transcript") or ""
            res.append(("PASS" if tp and os.path.isfile(tp) and sid in done else "FAIL",
                        f"{sid}: session log {'exists' if tp and os.path.isfile(tp) else 'missing'}"
                        f", {'folded' if sid in done else 'not folded'}"))
    for sid, ok in (folded_at_end or {}).items():
        if not ok:
            res.append(("FAIL", f"{sid}: not folded within {FOLD_WAIT_S}s of its end "
                                "(no SessionEnd from the agent?); folded afterwards by `te fold`"))
    # The fact: learned from the early sessions, injected at a later session's start.
    events = [e for d in state_dirs(home, platform) for e in _lines(os.path.join(d, "events.jsonl"))]
    later = set(order[1:])
    injected = [e for e in events if e.get("kind") == "inject" and e.get("session_id") in later
                and (e.get("chars") or 0) > 0]
    cmds = {sid: shell_commands(platform, sess[sid].get("transcript"), work) for sid in order}
    trapped = sum(1 for c in cmds.values() if probe.test_commands(c)[1])
    if injected:
        res.append(("PASS", f"fact injected at the start of {len(injected)} later session(s)"))
    elif not any(cmds.values()):
        res.append(("INCONCLUSIVE", "model did not use tools: no shell call in any session"))
    elif trapped < SESSIONS - 1:
        res.append(("INCONCLUSIVE", f"model met the trap in only {trapped} of {len(order)} "
                                    "sessions, too few to teach the fact"))
    else:
        res.append(("FAIL", f"no `inject` event, though {trapped} sessions met the trap"))
    for sid in order:
        first = (probe.test_commands(cmds[sid])[0] or ["-"])[0][:70]
        print(f"  {sid}: {len(cmds[sid])} shell calls, first test command: {first}")
    errs = []
    try:
        with open(os.path.join(home, "errors.log"), encoding="utf-8", errors="replace") as fh:
            errs = [l.rstrip() for l in fh if l.strip()]
    except OSError:
        pass
    bad = [l for l in errs if not any(re.search(p, l) for p in allow_errors)]
    res.append(("PASS" if not bad else "FAIL",
                f"errors.log: {len(errs)} line(s), {len(bad)} unexpected"
                + "".join(f"\n      {l[:200]}" for l in bad[:10])))
    return res


def wait_folded(home, platform, before, timeout=FOLD_WAIT_S):
    """{new session id: folded within timeout} for the sessions that appeared since `before`."""
    deadline = time.time() + timeout
    while True:
        new = set(te_sessions(home, platform)) - before
        done = folded(home, platform)
        if (new and new <= set(done)) or time.time() > deadline:
            return {s: s in done for s in new}
        time.sleep(2)


def cmd_run(args):
    home = os.environ.get("TOKENECONOMIZER_HOME")
    if not home:
        raise SystemExit("TOKENECONOMIZER_HOME must be set (the workflow puts it in $RUNNER_TEMP)")
    work = os.path.abspath(args.work)
    logs = os.path.join(work, "transcripts")
    os.makedirs(logs, exist_ok=True)
    repo, trap = probe.make_repo(work)
    # Micro-skills' free layer learns the trap's fact without a model call (as `te probe` runs).
    env = dict(os.environ, PATH=trap + os.pathsep + os.environ.get("PATH", ""),
               TE_MICROSKILLS="envfacts")
    key = os.environ.get("OPENROUTER_API_KEY")
    at_end = {}
    for i in range(1, SESSIONS + 1):
        if key and not args.no_budget_check:
            week = weekly_usage(key)
            if week is not None and week >= args.budget:
                _summary(f"**{args.platform}**: STOPPED before session {i}: this week's spend "
                         f"${week:.4f} reached the ${args.budget:.2f} budget.")
                return 0
        before = set(te_sessions(home, args.platform))
        cmd = agent_cmd(args.platform, args.model, repo, work)
        try:
            out = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,
                                 timeout=SESSION_TIMEOUT_S, stdin=subprocess.DEVNULL)
            stdout, stderr, rc = out.stdout or "", out.stderr or "", out.returncode
        except subprocess.TimeoutExpired as e:
            raw = e.stdout or ""
            stdout = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
            stderr, rc = f"timed out after {SESSION_TIMEOUT_S}s", "timeout"
        for ext, text in (("out", stdout), ("err", stderr)):
            with open(os.path.join(logs, f"session-{i}.{ext}"), "w", encoding="utf-8") as fh:
                fh.write(text)
        print(f"session {i}: exit {rc}")
        if rc != 0 and RATE_LIMIT.search(stdout + stderr):
            _summary(f"**{args.platform}**: skipped: OpenRouter rate limit at session {i} "
                     "(not a failure; re-run later).")
            print(f"::notice::{args.platform}: skipped: rate limit")
            return 0
        got = wait_folded(home, args.platform, before)
        if not got:
            print(f"::warning::session {i}: the plugin recorded no session "
                  f"(stderr tail: {stderr.strip().splitlines()[-1:] or ['-']})")
        for sid, ok in got.items():
            at_end[sid] = ok
            if not ok:
                subprocess.run([os.path.join(ROOT, "plugin", "scripts", "te"), "--host",
                                args.platform, "--cwd", repo, "fold", "--session", sid],
                               env=env, cwd=repo, timeout=120)
    res = check(home, args.platform, work, args.allow_error, at_end)
    _summary(f"\n### {args.platform} on `{args.model}`\n")
    for status, what in res:
        _summary(f"- **{status}** {what}")
    if any(s == "FAIL" for s, _ in res):
        return 1
    if any(s == "INCONCLUSIVE" for s, _ in res):
        print(f"::warning::{args.platform}: inconclusive (see the summary)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=("guard", "run"))
    ap.add_argument("--platform", required=True, choices=("hermes", "openclaw"))
    ap.add_argument("--model", required=True, help="an OpenRouter model id")
    ap.add_argument("--budget", type=float, default=2.00, help="dollars per UTC week, this key")
    ap.add_argument("--work", default=None, help="run: the scratch directory")
    ap.add_argument("--allow-error", action="append", default=[],
                    help="run: a regex for an errors.log line that is expected")
    ap.add_argument("--no-budget-check", action="store_true",
                    help="run: skip the between-session spend check (local stubbed runs only)")
    args = ap.parse_args(argv)
    if args.action == "guard":
        return cmd_guard(args)
    if not args.work:
        ap.error("run needs --work")
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
