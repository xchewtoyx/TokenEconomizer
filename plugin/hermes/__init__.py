"""TokenEconomizer for Hermes Agent (NousResearch/hermes-agent).

Hermes loads this package into its own process and calls `register(ctx)`. Every event below
becomes one call of the shared dispatcher (`../scripts/te-hook --host hermes`, JSON in, JSON
out), as on Pi; all of the mechanism logic lives there, and this file only translates:

    system-prompt section   -> SessionStart       the learned facts, as the section's text;
                                                 Hermes renders it once per session and
                                                 freezes it
    pre_llm_call            -> UserPromptSubmit   opens a prompt-turn (Hermes' `turn_id`)
    transform_tool_result   -> PostToolUse        the guard; a redirect is appended to the
                               (PostToolUseFailure  result the model reads next
                               when status != ok)
    on_session_end          -> Stop               fires after every turn in Hermes; sent only
                                                 for a turn that completed on its own
    on_session_finalize     -> SessionEnd         hands the session to a detached fold

The dispatcher runs as a child process, never imported here: a crash or a hang in it cannot take
the gateway down. Every callback fails open: any error, a missing or slow dispatcher, answers
"no change" (None, or "" for the section) and leaves a line in ~/.tokeneconomizer/errors.log.
Subagent sessions (`delegate_task` children) are left alone. It does nothing under TE_SIDECALL=1
(the plugin's own side calls) or TE_DISABLE=1. `/te <command>` runs the `te` command line for
this repository. Standard library only.
"""
import contextvars
import json
import os
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.realpath(__file__))
SECTION = "tokeneconomizer"
MAX_SECTION_CHARS = 4000          # Hermes skips a longer section
# Below Hermes' `plugins.hook_callback_timeout` (30 s by default): the calls the turn waits on
# (and the section, which Hermes does not time at all) get the longer budget, the rest the
# short one. On timeout the child is killed.
SLOW_S = 20
FAST_S = 5


def _scripts():
    """The plugin's launchers: beside this directory in a checkout, or inside it."""
    for d in (os.path.join(HERE, "..", "scripts"), os.path.join(HERE, "scripts")):
        if os.path.isfile(os.path.join(d, "te-hook")):
            return os.path.normpath(d)
    return None


def _truthy(v):
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _log_error(where, detail=None):
    """One entry in the plugin's errors.log: `detail`, else the exception being handled."""
    try:
        home = os.environ.get("TOKENECONOMIZER_HOME") or os.path.join(
            os.path.expanduser("~"), ".tokeneconomizer")
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} hermes {where}\n"
                     f"{detail or traceback.format_exc()}\n")
    except Exception:
        pass


def _guarded(default=None):
    """Run the callback; on any exception, log it and answer `default`."""
    def wrap(fn):
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                _log_error(fn.__name__)
                return default
        inner.__name__ = fn.__name__
        return inner
    return wrap


def _run(script, args, stdin_text, timeout, extra_env=None):
    """One launcher run; its stdout, or "" if it is missing, fails to start or times out."""
    d = _scripts()
    if not d:
        _log_error(script, f"no launchers found beside {HERE} (expected ../scripts or scripts)")
        return ""
    try:
        proc = subprocess.run([os.path.join(d, script)] + list(args), input=stdin_text,
                              capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, **(extra_env or {})))
    except subprocess.TimeoutExpired:
        _log_error(script, f"{script} {' '.join(args[:3])}: no answer within {timeout} s; "
                           f"killed")
        return ""
    except (OSError, subprocess.SubprocessError):
        _log_error(script)
        return ""
    return proc.stdout or ""


def _hook(payload, timeout, surface=""):
    """One dispatcher call; {} (no change) on any failure."""
    if _truthy(os.environ.get("TE_SIDECALL")) or _truthy(os.environ.get("TE_DISABLE")):
        return {}
    out = _run("te-hook", ["--host", "hermes"], json.dumps(payload), timeout,
               {"TE_HERMES_SURFACE": surface or ""})
    try:
        answer = json.loads((out.strip().splitlines() or ["{}"])[-1])
    except ValueError:
        return {}
    return answer if isinstance(answer, dict) else {}


def _hermes_home():
    # Hermes resolves the profile's home itself; read it from the module it has loaded.
    get = getattr(sys.modules.get("hermes_constants"), "get_hermes_home", None)
    try:
        if get:
            return str(get())
    except Exception:
        pass
    return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


# Hermes treats these as "no directory" (agent/relay_cwd.py).
_CWD_SKIP = {"", ".", "./", "auto", "cwd"}


def _usable_cwd(value):
    """A directory path, or "" when it is empty or a Hermes sentinel.

    The sentinel check uses a trimmed copy. The path is returned unchanged: a
    checkout's name may end in a space."""
    if not isinstance(value, str):
        return ""
    return "" if value.strip().lower() in _CWD_SKIP else value


def _context_cwd(ctx):
    """PluginContext.session_cwd() when this Hermes has it.

    v0.21.5 (f97608f, the v2026.9.24 image) has no such method. A method that
    exists and raises still propagates: the callback guard logs it."""
    fn = getattr(ctx, "session_cwd", None)
    if not callable(fn):
        return ""
    got = fn()
    return _usable_cwd(got)


def _hermes_call(module, name):
    """Call `name` in a Hermes module, or None when this process is not Hermes.

    Loaded by name so the shim stays importable without Hermes installed."""
    try:
        import importlib
        fn = getattr(importlib.import_module(module), name)
        return fn() if callable(fn) else None
    except Exception:
        return None


def _runtime_session_cwd():
    """The directory Hermes v0.21.5 pinned for this task, or "".

    Cron and the gateway set `agent.runtime_cwd`'s session contextvar
    (`gateway.session_context.set_session_vars(cwd=...)`) and do not chdir."""
    return _usable_cwd(_hermes_call("agent.runtime_cwd", "scoped_session_cwd"))


def _recorded_cwd(sid):
    """`sessions.cwd` for this session in the profile's state.db, or ""."""
    if not sid:
        return ""
    db = os.path.join(_hermes_home(), "state.db")
    if not os.path.isfile(db):
        return ""
    try:
        import sqlite3
        from urllib.parse import quote
        # `#` and `?` in the profile path are URI syntax unless percent-encoded.
        con = sqlite3.connect("file:" + quote(db, safe="/") + "?mode=ro", uri=True, timeout=1.0)
        try:
            row = con.execute("SELECT cwd FROM sessions WHERE id = ?", (sid,)).fetchone()
        finally:
            con.close()
    except Exception:
        return ""
    return _usable_cwd(row[0] if row else "")


def _process_cwd():
    """TERMINAL_CWD, else the process cwd: what `resolve_agent_cwd` returns when no
    session directory is pinned. The process cwd alone, outside a Hermes process."""
    got = _hermes_call("agent.runtime_cwd", "resolve_agent_cwd")
    if got:
        return _usable_cwd(str(got))
    try:
        return os.getcwd()
    except OSError:
        return ""


def _working_dir(ctx, sid, explicit=None):
    """Where this session's repository is.

    A directory the event already carried wins. Otherwise a later Hermes'
    `session_cwd()`, then the directory v0.21.5 actually pins, then the session
    row, then the process cwd. Each step runs only when the earlier ones are empty."""
    got = _usable_cwd(explicit)
    if got:
        return got
    got = _context_cwd(ctx)
    if got:
        return got
    got = _runtime_session_cwd()
    if got:
        return got
    got = _recorded_cwd(sid)
    if got:
        return got
    return _process_cwd()


def register(ctx):
    subagents = set()           # session ids of `delegate_task` children
    platforms = {}              # session id -> platform, from on_session_start
    opened = {}                 # session id -> the turn_id its last UserPromptSubmit opened
    locks = {}                  # session id -> lock: one session's calls fold in order
    locks_guard = threading.Lock()

    def lock_for(sid):
        with locks_guard:
            return locks.setdefault(sid, threading.Lock())

    def surface(sid):
        try:
            return ctx.session_surface() or platforms.get(sid, "")
        except Exception:
            return platforms.get(sid, "")

    def base(sid, cwd=None):
        """The payload every event shares, or None when there is no session or repository to
        pin it to."""
        if not sid or sid in subagents:
            return None
        cwd = _working_dir(ctx, sid, cwd)
        if not cwd:
            return None
        return {"session_id": sid, "cwd": cwd,
                "transcript_path": os.path.join(_hermes_home(), "state.db") + "#" + sid}

    def say(answer):
        """A notice for the person, posted into the session; never waited on."""
        text = answer.get("systemMessage")
        if not isinstance(text, str) or not text:
            return
        post = getattr(ctx, "post_message", None)
        if callable(post):
            # The bound session is read from this call's context; the post itself can wait on
            # the gateway, so it runs in a thread of its own.
            run = contextvars.copy_context().run
            threading.Thread(target=run, args=(_guarded()(post), text), daemon=True).start()

    @_guarded(default="")
    def section(info):
        sid = str(info.get("session_id") or "")
        if info.get("platform") == "subagent":
            subagents.add(sid)
            return ""
        p = base(sid, info.get("cwd"))
        if p is None:
            return ""
        out = _hook(dict(p, hook_event_name="SessionStart", source="startup"), SLOW_S,
                    info.get("platform") or surface(sid))
        say(out)
        text = out.get("additionalContext")
        return text if isinstance(text, str) and len(text.strip()) <= MAX_SECTION_CHARS else ""

    @_guarded()
    def on_session_start(session_id=None, platform=None, **_):
        platforms[str(session_id or "")] = platform or ""
        if platform == "subagent":
            subagents.add(str(session_id or ""))

    @_guarded()
    def subagent_start(child_session_id=None, **_):
        if child_session_id:
            subagents.add(str(child_session_id))

    @_guarded()
    def pre_llm_call(session_id=None, turn_id=None, **_):
        # Hermes fires this once per turn, before the turn's request loop
        # (agent/turn_context.py:1185, from build_turn_context, conversation_loop.py:1660). A
        # second call for the same turn must still not reopen it: that would reset the guard.
        sid, turn = str(session_id or ""), str(turn_id or "")
        p = base(sid)
        if p is not None:
            with lock_for(sid):
                if turn and opened.get(sid) == turn:
                    return None
                opened[sid] = turn
                _hook(dict(p, hook_event_name="UserPromptSubmit", prompt_id=turn),
                      SLOW_S, surface(sid))
        return None

    @_guarded()
    def transform_tool_result(tool_name=None, args=None, result=None, session_id=None,
                              turn_id=None, status=None, error_message=None, **_):
        sid = str(session_id or "")
        p = base(sid)
        if p is None:
            return None
        failed = bool(status) and status != "ok"
        payload = dict(p, hook_event_name="PostToolUseFailure" if failed else "PostToolUse",
                       prompt_id=str(turn_id or ""), tool_name=str(tool_name or ""),
                       tool_input=args if isinstance(args, dict) else {},
                       tool_response=result if isinstance(result, str) else json.dumps(result))
        if failed and error_message:
            payload["error"] = str(error_message)
        with lock_for(sid):
            out = _hook(payload, SLOW_S, surface(sid))
        say(out)
        extra = out.get("additionalContext")
        if isinstance(extra, str) and extra and isinstance(result, str):
            return result + "\n\n" + extra
        return None

    @_guarded()
    def on_session_end(session_id=None, turn_id=None, completed=None, interrupted=None,
                       failed=None, **_):
        # Fires after every turn. A turn that was interrupted, failed or ran out of budget did
        # not stop on its own: it is closed, unseen, by the next prompt or the session's end.
        sid = str(session_id or "")
        p = base(sid)
        if p is None or not completed or interrupted or failed:
            return None
        with lock_for(sid):
            _hook(dict(p, hook_event_name="Stop", prompt_id=str(turn_id or ""),
                       stop_hook_active=False), FAST_S, surface(sid))
        return None

    @_guarded()
    def on_session_finalize(session_id=None, platform=None, reason=None, **_):
        sid = str(session_id or "")
        p = base(sid)
        if p is not None:
            with lock_for(sid):          # after any call of this session still in flight
                _hook(dict(p, hook_event_name="SessionEnd", reason=str(reason or "")), FAST_S,
                      platform or surface(sid))
        with locks_guard:
            locks.pop(sid, None)
        platforms.pop(sid, None)
        opened.pop(sid, None)
        subagents.discard(sid)
        return None

    @_guarded(default="te: the command failed; see ~/.tokeneconomizer/errors.log")
    def te(raw_args="", session_id=None, **_):
        argv = (raw_args or "status").split()
        cwd = _working_dir(ctx, str(session_id or ""))
        out = _run("te", ["--host", "hermes", "--cwd", cwd] + argv, "", SLOW_S,
                   {"TE_SESSION_ID": str(session_id or "")})
        return out.strip() or "te: no output (is Python 3.10+ on PATH?)"

    ctx.register_system_prompt_section(SECTION, section, position="after_memory")
    for name, fn in (("on_session_start", on_session_start), ("subagent_start", subagent_start),
                     ("pre_llm_call", pre_llm_call),
                     ("transform_tool_result", transform_tool_result),
                     ("on_session_end", on_session_end),
                     ("on_session_finalize", on_session_finalize)):
        ctx.register_hook(name, fn)
    ctx.register_command("te", te, description="TokenEconomizer status and settings",
                         args_hint="<command>")
