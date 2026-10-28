#!/usr/bin/env python3
"""The model-backed live test of the Hermes and OpenClaw ports, run by
`.github/workflows/live-ports.yml` (docs/PLUGIN.md §6).

  guard  Before anything is installed: the model is listed on OpenRouter and takes tools, and
         this key's spend this week plus a reserve for one run stays under the budget. Writes
         `run=true|false` and `reserve` to $GITHUB_OUTPUT; exits 1 only when the model check
         fails (a pinned model that vanished or lost tool support is breakage, not a skip).
         Without every endpoint's price it skips: the reserve is priced at the dearest one.
  run    The five-session trap of docs/PLUGIN.md §3, in `te probe`'s repository (bare `pytest`
         missing, `python -m unittest` works), through the agent's own CLI on OpenRouter; then
         the assertions. What is under test is the plugin (fact learned, then injected; every
         session folded and priced), not the model's skill: a model that never ran a command,
         or never met the trap often enough to teach it, makes the run inconclusive, not failed.
         A watchdog polls the key's spend while each session runs and kills the agent once the
         run has spent half its reserve.

The budget is best effort: the reserve is an estimate, not a bound (an agent can make more or
larger requests than it assumes), and OpenRouter reports spend with a lag. The only hard cap is
the weekly credit limit set on the key itself in OpenRouter.

The key is read from OPENROUTER_API_KEY, sent only to openrouter.ai, and never printed.
Standard library only; imports the plugin's own package for the trap repository and the hosts'
transcript readers.
"""
import argparse
import glob
import json
import math
import os
import re
import signal
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
# The reserve for one session: 12 requests of up to ~25k context each, and the plugin's own side
# calls (loop-guard judge) on top. An estimate of a heavy session, not a bound.
IN_CAP, OUT_CAP, SIDE_CALLS = 300_000, 15_000, 1.25
WATCH_S = 15                # how often the watchdog reads the key's spend
# The watchdog trips at this share of the reserve: OpenRouter's usage figure trails the requests
# by up to a minute or so, and the rest of the reserve covers what is spent but not yet reported.
TRIP_SHARE = 0.5
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
    """The dollars reserved for one run at the given per-token prices."""
    return sessions * (IN_CAP * prompt_price + OUT_CAP * completion_price) * SIDE_CALLS


def model_prices(model):
    """(prompt, completion) $/token for `model`: the most expensive endpoint OpenRouter may route
    to, since it picks among providers per request; None when those prices cannot be read or
    are not valid. Exits 1 when the model is not listed or does not take tools."""
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
    try:
        eps = _get(f"/models/{model}/endpoints")["data"]["endpoints"]
        prices = [(float(e["pricing"]["prompt"]), float(e["pricing"]["completion"])) for e in eps]
    except (urllib.error.URLError, OSError, KeyError, TypeError, ValueError):
        return None
    if not prices or not all(math.isfinite(x) and x >= 0 for pc in prices for x in pc):
        return None
    return max(p for p, _ in prices), max(c for _, c in prices)


def key_usage(key):
    """(spend this UTC week from Monday or None if not reported, all-time spend) for this key."""
    d = _get("/key", key).get("data") or {}
    w = d.get("usage_weekly")
    return (float(w) if isinstance(w, (int, float)) else None), float(d.get("usage") or 0)


def cmd_guard(args):
    if not os.path.isdir(os.path.join(ROOT, "plugin", args.platform)):
        _summary(f"**{args.platform}**: skipped, `plugin/{args.platform}` is not in this tree "
                 "(the port is not merged here yet).")
        _out(run="false")
        return 0
    prices = model_prices(args.model)
    if prices is None:
        _summary(f"**{args.platform}**: SKIPPED: OpenRouter's endpoint prices for `{args.model}` "
                 "could not be read, so no reserve can be priced.")
        print(f"::notice::{args.platform} live test skipped: no endpoint prices for the model")
        _out(run="false")
        return 0
    p, c = prices
    est = estimate(p, c)
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("::error::OPENROUTER_API_KEY is not set")
        _out(run="false")
        return 1
    week = key_usage(key)[0]
    if week is None:
        # No weekly figure: only the run's reserve, its watchdog and the triggers bound the week.
        print("::warning::OpenRouter reported no usage_weekly for this key; falling back to the "
              "run's reserve alone. Set a weekly credit limit on the key itself.")
        go = est <= args.budget
    else:
        go = week + est <= args.budget
    week_s = "unknown" if week is None else f"${week:.4f}"
    _summary(f"**{args.platform}** on `{args.model}`: run reserve ${est:.4f} "
             f"(at ${p * 1e6:.4f}/M in, ${c * 1e6:.4f}/M out), spent this week {week_s}, "
             f"budget ${args.budget:.2f} -> {'run' if go else 'SKIPPED: over the weekly budget'}")
    if not go:
        print(f"::notice::{args.platform} live test skipped: weekly budget ${args.budget:.2f} "
              f"would be exceeded")
    _out(run="true" if go else "false", reserve=f"{est:.4f}")
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


def run_watched(cmd, cwd, env, out_path, err_path, spent=None, trip=None,
                timeout=SESSION_TIMEOUT_S):
    """Run one agent session, its output to files. Every WATCH_S, `spent()` (this run's spend so
    far) is read; at `trip` dollars, or after three reads in a row fail, or at `timeout`, the
    agent's whole process group is killed. Returns (exit code or None, why it was stopped)."""
    with open(out_path, "w") as out, open(err_path, "w") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=err,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    deadline, fails, why = time.monotonic() + timeout, 0, None
    while why is None:
        try:
            return proc.wait(timeout=WATCH_S), None
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() > deadline:
            why = f"timed out after {timeout}s"
        elif spent is not None:
            try:
                fails, now = 0, spent()
            except (urllib.error.URLError, OSError, ValueError):
                fails, now = fails + 1, None
            if fails >= 3:
                why = "spend unknown: the key's usage could not be read"
            elif now is not None and now >= trip:
                why = f"run reserve reached (${now:.4f} spent, trips at ${trip:.4f})"
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
    return None, why


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
    watch = bool(key) and not args.no_budget_check
    if watch and not args.reserve:
        raise SystemExit("run needs --reserve (the guard's `reserve` output)")
    start = key_usage(key)[1] if watch else 0.0
    spent = (lambda: key_usage(key)[1] - start) if watch else None
    trip = (args.reserve or 0) * TRIP_SHARE
    at_end = {}
    for i in range(1, SESSIONS + 1):
        if watch:
            week, _ = key_usage(key)
            if week is not None and week >= args.budget:
                _summary(f"**{args.platform}**: STOPPED before session {i}: this week's spend "
                         f"${week:.4f} reached the ${args.budget:.2f} budget.")
                return 0
        before = set(te_sessions(home, args.platform))
        cmd = agent_cmd(args.platform, args.model, repo, work)
        out_p, err_p = (os.path.join(logs, f"session-{i}.{x}") for x in ("out", "err"))
        rc, why = run_watched(cmd, repo, env, out_p, err_p, spent, trip)
        stdout, stderr = (open(p, encoding="utf-8", errors="replace").read() for p in (out_p, err_p))
        print(f"session {i}: exit {rc}" + (f" (stopped: {why})" if why else ""))
        if why and why.startswith(("run reserve", "spend unknown")):
            _summary(f"**{args.platform}**: STOPPED in session {i}: {why}. The agent was killed; "
                     "OpenRouter's usage lags, so a little more may still be billed.")
            print(f"::error::{args.platform}: stopped: {why}")
            return 1
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
    ap.add_argument("--reserve", type=float, default=None,
                    help="run: the guard's reserve in dollars; the watchdog trips at half of it")
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
