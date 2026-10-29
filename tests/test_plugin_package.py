"""The installable plugin: manifests, hook files, launchers, and the self-contained bundle.

These pin the packaging facts a host relies on and a refactor could silently break: every hook
file runs the one launcher that exists and is executable, the Codex file names its host and keeps
SessionEnd under Codex's three-second cap, the marketplace points at the plugin directory, the
launcher fails open, and the bundle `scripts/build_plugin.py` writes runs with no checkout beside
it -- which is what a marketplace install, copying only the plugin directory, actually gets.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugin")
EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"}


def _json(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def _commands(hooks_doc):
    for event, groups in hooks_doc["hooks"].items():
        for group in groups:
            for h in group["hooks"]:
                yield event, group, h


def test_manifests_agree_on_identity():
    claude = _json(PLUGIN, ".claude-plugin", "plugin.json")
    codex = _json(PLUGIN, ".codex-plugin", "plugin.json")
    # Copilot reads `.github/plugin/plugin.json` in preference to `.claude-plugin/` (verified on
    # 1.0.83: only the hooks it names ran), which is how it gets a hook file of its own.
    copilot = _json(PLUGIN, ".github", "plugin", "plugin.json")
    assert claude["name"] == codex["name"] == copilot["name"] == "tokeneconomizer"
    assert claude["version"] == codex["version"] == copilot["version"]
    assert codex["hooks"] == "./hooks/codex-hooks.json"
    assert copilot["hooks"] == "hooks/copilot-hooks.json"
    for m in (codex, copilot):
        assert os.path.exists(os.path.join(PLUGIN, m["hooks"]))
    assert "hooks" not in claude              # Claude Code reads hooks/hooks.json by convention


@pytest.mark.parametrize("name,host", [("hooks.json", None), ("codex-hooks.json", "codex"),
                                       ("copilot-hooks.json", "copilot")])
def test_hook_files_run_the_launcher_on_every_event(name, host):
    doc = _json(PLUGIN, "hooks", name)
    # no PreToolUse: the plugin never blocks a call. Claude Code and Copilot report a failed
    # tool in an event of its own, which the guard must see too.
    want = EVENTS | ({"PostToolUseFailure"} if host in (None, "copilot") else set())
    assert set(doc["hooks"]) == want
    launcher = os.path.join(PLUGIN, "scripts", "te-hook")
    assert os.access(launcher, os.X_OK)
    for event, group, h in _commands(doc):
        assert h["type"] == "command" and "/scripts/te-hook" in h["command"]
        assert "matcher" not in group                     # PostToolUse watches every tool
        if host:
            assert f"--host {host}" in h["command"]
        if host == "copilot":
            assert h["command"].startswith('"${PLUGIN_ROOT}/scripts/te-hook"')
            if event == "SessionEnd" and host == "codex":
                assert h["timeout"] <= 3                  # Codex's cap for SessionEnd


def test_marketplace_lists_the_plugin_directory():
    m = _json(REPO, ".claude-plugin", "marketplace.json")
    entry = {p["name"]: p for p in m["plugins"]}["tokeneconomizer"]
    assert entry["source"] == "./plugin"
    assert os.path.isdir(os.path.join(REPO, entry["source"], ".claude-plugin"))


def test_no_bin_directory():
    """Files in a plugin's bin/ go on the Bash tool's PATH, and claude.ai will not install a
    plugin that has one; the launchers live in scripts/."""
    assert not os.path.exists(os.path.join(PLUGIN, "bin"))


def _run_hook(launcher, payload, env):
    p = subprocess.run([launcher], input=json.dumps(payload), capture_output=True, text=True,
                       env=env, timeout=60)
    return p.returncode, p.stdout, p.stderr


def _hook_env(tmp_path, **extra):
    env = {"PATH": os.environ.get("PATH", ""), "TOKENECONOMIZER_HOME": str(tmp_path / "home"),
           "GIT_CEILING_DIRECTORIES": str(tmp_path), "TE_FOLD_SYNC": "1"}
    env.update(extra)
    return env


def test_the_launcher_runs_the_dispatcher_from_the_checkout(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    code, out, _ = _run_hook(os.path.join(PLUGIN, "scripts", "te-hook"),
                             {"hook_event_name": "SessionStart", "session_id": "s",
                              "cwd": str(work), "source": "startup"}, _hook_env(tmp_path))
    assert code == 0 and json.loads(out) == {}
    assert os.path.isdir(tmp_path / "home" / "repos")      # it ran, and recorded the session


def test_the_launcher_fails_open_without_python(tmp_path):
    code, out, _ = _run_hook(os.path.join(PLUGIN, "scripts", "te-hook"),
                             {"hook_event_name": "SessionStart"},
                             {"PATH": "", "TE_PYTHON": "/nonexistent/python"})
    assert code == 0 and json.loads(out) == {}


def test_the_bootstrap_answers_no_change_on_an_old_python(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
    try:
        import te_bootstrap
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(te_bootstrap, "MIN_VERSION", (99, 0))
    monkeypatch.setattr(sys, "argv", ["te_bootstrap.py", "hook"])
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert te_bootstrap.main() == 0 and out.getvalue().strip() == "{}"


def test_the_bundle_runs_without_a_checkout(tmp_path):
    out = tmp_path / "market"
    subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_plugin.py"),
                    "--out", str(out)], check=True, capture_output=True)
    bundle = out / "tokeneconomizer"
    assert (bundle / "lib" / "tokeneconomizer" / "plugin" / "hook.py").exists()
    assert not (bundle / "lib" / "te_live").exists()
    assert (bundle / ".github" / "plugin" / "plugin.json").exists()
    assert (bundle / "hooks" / "copilot-hooks.json").exists()
    assert (bundle / "lib" / "tokeneconomizer" / "__init__.py").exists()
    assert not (bundle / "lib" / "te_harness").exists()
    assert not (bundle / "lib" / "te_bench").exists()
    assert not list(bundle.rglob("__pycache__"))
    m = json.loads((out / ".claude-plugin" / "marketplace.json").read_text())
    assert m["plugins"][0]["source"] == "./tokeneconomizer"
    work = tmp_path / "work"
    work.mkdir()
    # Run the bundled launcher with the core reachable ONLY through the bundle's lib/.
    code, stdout, stderr = _run_hook(str(bundle / "scripts" / "te-hook"),
                                     {"hook_event_name": "SessionStart", "session_id": "s",
                                      "cwd": str(work), "source": "startup"},
                                     _hook_env(tmp_path, PYTHONNOUSERSITE="1"))
    assert code == 0 and json.loads(stdout) == {}, stderr
    sys.path.insert(0, str(bundle / "scripts"))
    try:
        import importlib
        import te_bootstrap
        importlib.reload(te_bootstrap)
        assert te_bootstrap.core_path() == str(bundle / "lib")
    finally:
        sys.path.pop(0)


# ---- the layering the README promises ------------------------------------------------------
MECHANISMS = ("microskills", "toolprune", "loopguard", "trace")
CORE_MODULES = ("config.py", "domain.py", "ledger.py", "pricing.py", "stage.py")


def _imports(path):
    """(module, level) of every import statement in a file."""
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield node.module or "", node.level
            if node.level and not node.module:          # `from .. import plugin`
                for a in node.names:
                    yield a.name, node.level


def _core_files():
    pkg = os.path.join(REPO, "tokeneconomizer")
    for name in CORE_MODULES + ("__init__.py",):
        yield os.path.join(pkg, name), 0
    for sub in MECHANISMS:
        for name in sorted(os.listdir(os.path.join(pkg, sub))):
            if name.endswith(".py"):
                yield os.path.join(pkg, sub, name), 1


def test_the_mechanisms_never_import_the_plugin_runtime():
    """The plugin runs the mechanisms; the mechanisms never know it exists. Checked on the source
    (no import names `plugin`, absolutely or relatively) and on a live import."""
    for path, depth in _core_files():
        for mod, level in _imports(path):
            if level == 0:
                assert not mod.startswith("tokeneconomizer.plugin"), f"{path} imports {mod}"
            else:
                # `from ..plugin import x` inside a subpackage, `from .plugin` at the top level
                parts = mod.split(".")
                reaches_top = level == depth + 1
                assert not (reaches_top and parts[0] == "plugin"), f"{path} imports .{mod}"
    probe = ("import sys, tokeneconomizer, tokeneconomizer.domain, tokeneconomizer.stage\n"
             + "".join(f"import tokeneconomizer.{m}\n" for m in MECHANISMS)
             + "print(sorted(m for m in sys.modules if m.startswith('tokeneconomizer.plugin') "
               "or m.split('.')[0] in ('te_bench', 'te_harness', 'te_live')))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def test_the_plugin_ships_and_loads_no_benchmark_code():
    """The benchmark adapters, the facade, the workloads, the baselines and the benchmark-only
    controllers live with the experiments (`experiments/te_bench/`). The plugin's library is the
    package alone, and running every part of the plugin loads none of them."""
    shipped = os.path.join(PLUGIN, "lib", "tokeneconomizer")
    assert sorted(n for n in os.listdir(shipped)
                  if os.path.isdir(os.path.join(shipped, n)) and n != "__pycache__") == sorted(
        MECHANISMS + ("plugin",))
    for gone in ("adapters", "mechanisms", "vendor", "facade.py", "workloads.py", "runner.py",
                 "arms.py", "baselines"):
        assert not os.path.exists(os.path.join(shipped, gone)), gone
    for gone in ("codex_spiral.py", "spiral_stage.py", "nudge_guard.py"):
        assert not any(gone in files for _, _, files in os.walk(shipped)), gone
    mods = ("hook", "controller", "fold", "cli", "denylist", "toolcheck", "repomap",
            "sidecall", "judge", "probe")
    probe = ("import sys\n" + "".join(f"import tokeneconomizer.plugin.{m}\n" for m in mods)
             + "print(sorted({m.split('.')[0] for m in sys.modules} "
               "& {'te_bench', 'te_harness', 'te_live'}))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def test_the_package_is_stdlib_only_and_never_touches_the_benchmark_harness():
    pkg = os.path.join(REPO, "tokeneconomizer")
    for top, _, files in os.walk(pkg):
        for name in sorted(files):
            # the repository-map generator runs as a subprocess under a Python that has aider;
            # nothing imports it
            if not name.endswith(".py") or name == "repomap_gen.py":
                continue
            path = os.path.join(top, name)
            for mod, level in _imports(path):
                if level:
                    continue
                root = mod.split(".")[0]
                assert root not in ("te_harness", "te_bench", "te_live", "docker"), \
                    f"{path} imports {mod}"
                # tomllib: standard library from 3.11, imported behind an ImportError guard
                assert root in sys.stdlib_module_names or root in (
                    "tokeneconomizer", "tomllib"), f"{path} imports non-stdlib {mod}"


def test_a_resumed_pi_conversation_keeps_the_section_it_carries():
    """Pi rebuilds a conversation's system message from its system entries, so a resumed one is
    given back the facts section it carries, byte for byte: unwrapped from the `<name>` tags Pi
    stores it in (Pi wraps it again), gone once a patch removed it, and as a compaction restated
    it. Run through Node, which loads the extension's TypeScript directly."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    ext = os.path.join(PLUGIN, "pi", "tokeneconomizer.ts")
    script = """
const { carriedSection } = await import(process.argv[1]);
const sys = (sections) => ({ type: "message", message: { role: "system", sections } });
const compact = (sections) => ({ type: "compaction", systemMessage: { sections } });
const user = { type: "message", message: { role: "user" } };
const facts = sys({ tokeneconomizer: "<tokeneconomizer>\\nFacts:\\n- one\\n</tokeneconomizer>" });
console.log(JSON.stringify([
  carriedSection([]),
  carriedSection([facts, user]),
  carriedSection([facts, sys({ cwd: "<cwd>\\n/x\\n</cwd>" })]),
  carriedSection([facts, sys({ tokeneconomizer: null })]),
  carriedSection([facts, compact({ cwd: "c" })]),
  carriedSection([compact(facts.message.sections)]),
]));
"""
    run = subprocess.run([node, "--experimental-strip-types", "--no-warnings",
                          "--input-type=module", "-e", script, ext],
                         capture_output=True, text=True, timeout=60)
    if run.returncode and "strip-types" in run.stderr:
        pytest.skip("this node cannot load TypeScript")
    assert run.returncode == 0, run.stderr
    facts = "Facts:\n- one"
    assert json.loads(run.stdout) == ["", facts, facts, "", "", facts]


def test_the_plugin_directory_is_also_a_pi_package():
    """One directory, three agents: Pi loads the extension its `pi` manifest names, and that
    extension drives the same launchers the Claude Code and Codex hooks run."""
    pkg = _json(PLUGIN, "package.json")
    assert "pi-package" in pkg["keywords"]
    (ext,) = pkg["pi"]["extensions"]
    src = open(os.path.join(PLUGIN, ext), encoding="utf-8").read()
    assert 'join(HERE, "..", "scripts")' in src and '"te-hook", ["--host", "pi"]' in src
    # Exactly the dispatcher's five events. Not `tool_call`, the one Pi event that can block a
    # call before it runs: the plugin guards nothing, on Pi as on the hook hosts.
    assert set(re.findall(r'pi\.on\("(\w+)"', src)) == {
        "session_start", "before_agent_start", "tool_result", "agent_end", "session_shutdown"}
    # The repository root is the same package for `pi install git:github.com/...`, which clones
    # the repository and reads the manifest at its root: same package, same extension file.
    root = _json(os.path.dirname(PLUGIN), "package.json")
    (root_ext,) = root["pi"]["extensions"]
    assert os.path.realpath(os.path.join(os.path.dirname(PLUGIN), root_ext)) == \
        os.path.realpath(os.path.join(PLUGIN, ext))
    # (The root is also the OpenClaw plugin `openclaw plugins install git:` installs; see below.)
    assert {k: v for k, v in root.items() if k not in ("pi", "openclaw")} == \
        {k: v for k, v in pkg.items() if k != "pi"}
    # no lockfile: Claude Code installs a plugin's npm dependencies only when one exists,
    # and this package has none to install (the Pi API is a peer dependency)
    assert not any(os.path.exists(os.path.join(PLUGIN, f)) for f in
                   ("package-lock.json", "npm-shrinkwrap.json", "bun.lock", "bun.lockb"))


def test_the_openclaw_plugin_manifests_agree_and_it_never_blocks_a_call():
    """The OpenClaw plugin is `plugin/openclaw/` for a linked install and the repository root for
    `openclaw plugins install git:...`, which installs the clone's root: the two manifests are the
    same, and both packages name the same entry file."""
    here = os.path.join(PLUGIN, "openclaw")
    manifest = _json(here, "openclaw.plugin.json")
    assert manifest == _json(REPO, "openclaw.plugin.json")
    assert manifest["id"] == "tokeneconomizer" and manifest["activation"] == {"onStartup": True}
    assert manifest["contracts"] == {"agentToolResultMiddleware": ["openclaw"]}
    pkg, root = _json(here, "package.json"), _json(REPO, "package.json")
    assert pkg["type"] == "module"
    (ext,) = pkg["openclaw"]["extensions"]
    (root_ext,) = root["openclaw"]["extensions"]
    assert os.path.realpath(os.path.join(here, ext)) == os.path.realpath(os.path.join(REPO, root_ext))
    assert pkg["openclaw"]["compat"] == root["openclaw"]["compat"]
    from tokeneconomizer.plugin import cli
    assert pkg["openclaw"]["compat"]["pluginApi"] == ">=" + cli.OPENCLAW_PLUGIN_API
    src = open(os.path.join(here, ext), encoding="utf-8").read()
    assert 'runLauncher(join(scripts, "te-hook"), ["--host", HOST]' in src and 'HOST = "openclaw"' in src
    # Not `before_tool_call`, which fails CLOSED when a handler throws or times out.
    assert set(re.findall(r'api\.on\(\s*"(\w+)"', src)) == {
        "session_start", "before_prompt_build", "agent_end", "session_end"}
    assert "before_tool_call" not in src.replace("`before_tool_call`", "")
    assert "carriedSection" not in src                    # Pi's sections are not OpenClaw's


def test_both_extensions_spawn_through_the_one_shared_helper():
    """Pi imports `plugin/shared/te-spawn.ts`; OpenClaw carries a byte-identical copy, so a
    linked `plugin/openclaw/` imports nothing from outside its own directory."""
    shared = open(os.path.join(PLUGIN, "shared", "te-spawn.ts"), encoding="utf-8").read()
    assert open(os.path.join(PLUGIN, "openclaw", "te-spawn.ts"), encoding="utf-8").read() == shared
    pi = open(os.path.join(PLUGIN, "pi", "tokeneconomizer.ts"), encoding="utf-8").read()
    oc = open(os.path.join(PLUGIN, "openclaw", "index.ts"), encoding="utf-8").read()
    assert 'from "../shared/te-spawn.ts"' in pi and 'from "./te-spawn.ts"' in oc
    assert "spawn(" not in pi and "spawn(" not in oc


OPENCLAW_DRIVER = r"""
import { mkdirSync, writeFileSync, readFileSync, existsSync, utimesSync } from "node:fs";
import { join } from "node:path";
const [ext, home, repo, root, scenario] = process.argv.slice(1);
process.env.TOKENECONOMIZER_HOME = home;
const load = async (v) => (await import(ext + "?v=" + v));
const mod = await load(1);
const read = (seg) => readFileSync(mod.logPath(seg), "utf8").trim().split("\n").map((l) => JSON.parse(l));
const reg = (m) => {
  const api = { rootDir: root, on: (n, h, o) => (api.hooks[n] = h), hooks: {}, logger: { info: () => {} },
                registerAgentToolResultMiddleware: (h, o) => { api.mw = h; api.mwOpts = o; },
                registerCommand: (c) => (api.cmd = c) };
  m.default.register(api);
  return api;
};
const ctxOf = (sid, runId, extra = {}) => ({ sessionId: sid, sessionKey: "agent:main:main", workspaceDir: repo,
  channel: "webchat", runId, trigger: "user", activeProjectKeys: [], ...extra });
const usage = { input: 1, output: 2, cacheRead: 3, cacheWrite: 4, cost: { total: 0 } };
const asst = (ts, id) => ({ role: "assistant", timestamp: ts, model: "m", usage,
  content: [{ type: "toolCall", id, name: "exec", arguments: { command: "pytest -q" } }] });
const out = {};
const api = reg(mod);

if (scenario === "main") {
  out.hooks = Object.keys(api.hooks).sort();
  out.mwOpts = api.mwOpts;
  out.cmdName = api.cmd.name;
  const u = { role: "user", content: "fix it", timestamp: 1 };
  const a = asst(2, "c1");
  const r = { role: "toolResult", toolCallId: "c1", toolName: "exec", isError: true, timestamp: 3,
              content: [{ type: "text", text: "boom" }] };
  const u2 = { role: "user", content: "again", timestamp: 4 };
  const ctx = ctxOf("S1", "r1");
  out.first = await api.hooks.before_prompt_build({ prompt: "fix it", messages: [] }, ctx);
  out.again = await api.hooks.before_prompt_build({ prompt: "fix it", messages: [] }, ctx);
  out.mw = await api.mw({ toolCallId: "c1", toolName: "exec", args: { command: "pytest -q" }, isError: true,
                         result: { content: [{ type: "text", text: "boom" }], details: { exitCode: 1, cwd: repo } } },
                       { runtime: "openclaw", sessionId: "S1" });
  await api.hooks.agent_end({ messages: [u, a] }, ctx);
  await api.hooks.agent_end({ messages: [u, a, r] }, ctx);
  const fresh = reg(await load(2));                          // a plugin reload: nothing in memory
  await fresh.hooks.agent_end({ messages: [u, a, r] }, ctx);  // no session known: nothing written
  out.reloaded = await fresh.hooks.before_prompt_build({ prompt: "again", messages: [u, a, r] }, ctxOf("S1", "r2"));
  await fresh.hooks.agent_end({ messages: [r, u2] }, ctxOf("S1", "r2"));
  const log = read(mod.segment("S1", 1));
  out.header = log[0];
  out.segments = mod.lastSegment("S1");
  out.chain = log.slice(1).map((e) => [e.id, e.parentId]);
  out.roles = log.slice(1).map((e) => e.message.role);
  out.names = [log[2].message.content[0].name, log[3].message.toolName];
  out.cmd = await fresh.cmd.handler({ sessionId: "S1", args: "status" });
  out.nocmd = await fresh.cmd.handler({ sessionId: "nope" });
  // Five prompts in one segment: the same text every time.
  out.five = [];
  for (let k = 1; k <= 5; k++) out.five.push(await fresh.hooks.before_prompt_build({ prompt: "p" + k, messages: [] }, ctxOf("S7", "q" + k)));
  // Two builds at once for a new session: one SessionStart, the same facts for both.
  out.both = await Promise.all([fresh.hooks.before_prompt_build({ prompt: "x", messages: [] }, ctxOf("S6", "w1")),
                                fresh.hooks.before_prompt_build({ prompt: "x", messages: [] }, ctxOf("S6", "w1"))]);
  out.s6segments = mod.lastSegment("S6");
  // Left alone: cron, incognito, no repository, the Codex runtime (no `activeProjectKeys`).
  out.cron = await fresh.hooks.before_prompt_build({ prompt: "x", messages: [] }, ctxOf("S2", "x", { trigger: "cron" }));
  out.incognito = await fresh.hooks.before_prompt_build({ prompt: "x", messages: [] },
    ctxOf("S3", "x", { sessionKey: "agent:main:dashboard:incognito-1" }));
  out.noRepo = await fresh.hooks.before_prompt_build({ prompt: "x", messages: [] }, ctxOf("S4", "x", { workspaceDir: home }));
  const { activeProjectKeys, ...codexCtx } = ctxOf("S5", "x");
  out.codex = await fresh.hooks.before_prompt_build({ prompt: "x", messages: [] }, codexCtx);
  await fresh.hooks.agent_end({ messages: [u, a] }, codexCtx);
  out.codexCmd = await fresh.cmd.handler({ sessionId: "S5" });
  out.noLog = ["S2", "S3", "S4", "S5"].some((s) => mod.lastSegment(s) > 0);
} else if (scenario === "segments") {
  // One session, a turn, a quiet stretch past the fold threshold, another turn.
  const u = { role: "user", content: "one", timestamp: 1 };
  const u2 = { role: "user", content: "two", timestamp: 10 };
  await api.hooks.before_prompt_build({ prompt: "one", messages: [] }, ctxOf("L", "r1"));
  await api.hooks.agent_end({ messages: [u, asst(2, "c1")] }, ctxOf("L", "r1"));
  const old = (Date.now() - 700_000) / 1000;
  utimesSync(mod.logPath(mod.segment("L", 1)), old, old);
  await api.hooks.before_prompt_build({ prompt: "two", messages: [u, asst(2, "c1")] }, ctxOf("L", "r2"));
  await api.hooks.agent_end({ messages: [u, asst(2, "c1"), u2, asst(11, "c2")] }, ctxOf("L", "r2"));
  out.segments = mod.lastSegment("L");
  out.second = read(mod.segment("L", 2)).slice(1).map((e) => e.message.role);
} else if (scenario === "ended") {
  // A turn, 9.5 quiet minutes (past the segment limit, short of the fold's), a turn, the end.
  const u = { role: "user", content: "one", timestamp: 1 };
  const u2 = { role: "user", content: "two", timestamp: 10 };
  await api.hooks.before_prompt_build({ prompt: "one", messages: [] }, ctxOf("M", "r1"));
  await api.hooks.agent_end({ messages: [u, asst(2, "c1")] }, ctxOf("M", "r1"));
  const old = (Date.now() - 570_000) / 1000;
  utimesSync(mod.logPath(mod.segment("M", 1)), old, old);
  await api.hooks.before_prompt_build({ prompt: "two", messages: [u, asst(2, "c1")] }, ctxOf("M", "r2"));
  await api.hooks.agent_end({ messages: [u, asst(2, "c1"), u2, asst(11, "c2")] }, ctxOf("M", "r2"));
  await api.hooks.session_end({ sessionId: "M", reason: "idle" }, { sessionId: "M" });
  out.segments = mod.lastSegment("M");
} else if (scenario === "late") {
  // The next prompt arrives before the old run's agent_end: the old segment is ended (and, with
  // TE_FOLD_SYNC, folded) only after that agent_end has logged the run.
  const u = { role: "user", content: "one", timestamp: 1 };
  const u2 = { role: "user", content: "two", timestamp: 10 };
  await api.hooks.before_prompt_build({ prompt: "one", messages: [] }, ctxOf("N", "r1"));
  const old = (Date.now() - 570_000) / 1000;
  utimesSync(mod.logPath(mod.segment("N", 1)), old, old);
  const second = api.hooks.before_prompt_build({ prompt: "two", messages: [] }, ctxOf("N", "r2"));
  await new Promise((done) => setTimeout(done, 300));
  await api.hooks.agent_end({ messages: [u, asst(2, "c1")] }, ctxOf("N", "r1"));
  await second;
  await api.hooks.agent_end({ messages: [u, asst(2, "c1"), u2, asst(11, "c2")] }, ctxOf("N", "r2"));
  await api.hooks.session_end({ sessionId: "N", reason: "idle" }, { sessionId: "N" });
  out.first = read(mod.segment("N", 1)).slice(1).map((e) => e.message.role);
  out.second = read(mod.segment("N", 2)).slice(1).map((e) => e.message.role);
} else if (scenario === "sameMs") {
  // Two replies in one millisecond, logged by a whole-session batch, then again by a per-run one.
  const u = { role: "user", content: "go", timestamp: 5 };
  const batch = [u, asst(5, "c1"), asst(5, "c2")];
  await api.hooks.before_prompt_build({ prompt: "go", messages: [] }, ctxOf("Q", "r1"));
  await api.hooks.agent_end({ messages: batch }, ctxOf("Q", "r1"));
  await api.hooks.agent_end({ messages: batch.slice(1) }, ctxOf("Q", "r1"));
  out.path = mod.logPath(mod.segment("Q", 1));
} else if (scenario === "repo") {
  // A workspace below a versioned home is not a checkout, nor is one with an empty `.git`.
  const h = join(home, "user");
  mkdirSync(join(h, ".git"), { recursive: true });
  writeFileSync(join(h, ".git", "HEAD"), "ref: refs/heads/main\n");
  mkdirSync(join(h, "ws"), { recursive: true });
  mkdirSync(join(home, "empty", ".git"), { recursive: true });
  const saved = process.env.HOME;
  process.env.HOME = h;
  out.home = await api.hooks.before_prompt_build({ prompt: "x", messages: [] }, ctxOf("H", "x", { workspaceDir: join(h, "ws") }));
  process.env.HOME = saved;
  out.empty = await api.hooks.before_prompt_build({ prompt: "x", messages: [] }, ctxOf("E", "x", { workspaceDir: join(home, "empty") }));
  out.real = mod.inRepo(repo);
  out.noLog = mod.lastSegment("H") + mod.lastSegment("E");
} else if (scenario === "exec") {
  // `openclaw agent exec`: one run, no `session_end`, then the process exits.
  out.detected = [["node", "openclaw", "gateway", "run"], ["node", "openclaw", "agent", "-m", "hi"],
                  ["node", "openclaw", "agent", "--agent", "main", "exec", "fix it"],
                  ["node", "openclaw", "--profile", "work", "agent", "exec", "fix it"],
                  ["node", "openclaw", "--log-level", "debug", "gateway", "run"]].map((a) => mod.oneShotExec(a));
  process.argv = [process.argv[0], "/usr/lib/openclaw/openclaw.mjs", "agent", "exec", "--json", "fix it"];
  const ex = reg(await load(3));
  await ex.hooks.before_prompt_build({ prompt: "fix it", messages: [] }, ctxOf("X", "r1"));
  await ex.hooks.agent_end({ messages: [{ role: "user", content: "fix it", timestamp: 1 }, asst(2, "c1")] }, ctxOf("X", "r1"));
  console.log(JSON.stringify(out));
  process.exit(0);
}
console.log(JSON.stringify(out));
"""


def _openclaw_node(tmp_path, scenario, **env):
    """Run OPENCLAW_DRIVER's `scenario` against the real dispatcher; (output, repo, home)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    home = tmp_path / "home"
    ext = os.path.join(PLUGIN, "openclaw", "index.ts")
    run = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "--input-type=module",
                          "-e", OPENCLAW_DRIVER, ext, str(home), str(repo),
                          os.path.join(PLUGIN, "openclaw"), scenario],
                         capture_output=True, text=True, timeout=180,
                         env=dict(os.environ, TE_SIDECALL="", TE_DISABLE="", TE_MICROSKILLS="envfacts",
                                  GIT_CEILING_DIRECTORIES=str(tmp_path), **env))
    if run.returncode and "strip-types" in run.stderr:
        pytest.skip("this node cannot load TypeScript")
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout), repo, home


def _openclaw_store(repo, home):
    from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
    from tokeneconomizer.plugin.store import Store
    env = dict(os.environ, TOKENECONOMIZER_HOME=str(home))
    return Store(repo_state_dir(repo_identity(str(repo), env), "openclaw", env))


def test_the_openclaw_plugin_logs_each_message_once_across_reloads_and_starts_from_the_log(tmp_path):
    """The plugin, loaded by Node with a stand-in for OpenClaw's `api`, against the real
    dispatcher: a session in a git checkout starts on its first prompt and gets the same text on
    every prompt; a repeated build of one run opens no second turn, and two builds at once start
    the session once; `agent_end` batches that overlap, and a reload, append each message once to
    one linear `parentId` chain with the tool names renamed; after the reload the segment is
    started again from its log, as a resume; cron, incognito, repository-less and Codex-runtime
    sessions leave nothing behind."""
    st = _openclaw_store(tmp_path / "repo", tmp_path / "home")
    st.write("injection.json", {"rendered": "Facts:\n- one", "rendered_ids": ["a"]})
    out, repo, home = _openclaw_node(tmp_path, "main")
    assert out["hooks"] == ["agent_end", "before_prompt_build", "session_end", "session_start"]
    assert out["mwOpts"] == {"runtimes": ["openclaw"]} and out["cmdName"] == "te"
    facts = {"appendSystemContext": "Facts:\n- one"}
    # the same text on every prompt, and again after the reload, from the stored start
    assert out["first"] == out["again"] == out["reloaded"] == facts
    assert out["five"] == [facts] * 5 and out["both"] == [facts, facts]
    assert out.get("mw") is None                                    # no redirect for one call
    assert out["header"]["type"] == "session" and out["header"]["cwd"] == str(repo)
    assert out["segments"] == 1 and out["s6segments"] == 1
    ids = [i for i, _ in out["chain"]]
    assert out["chain"] == [list(p) for p in zip(ids, [None] + ids[:-1])]   # linear, no branches
    assert out["roles"] == ["user", "assistant", "toolResult", "user"]     # each message once
    assert out["names"] == ["bash", "bash"]
    assert str(repo) in out["cmd"]["text"] and "no repository" in out["nocmd"]["text"]
    assert out.get("cron") is out.get("incognito") is out.get("noRepo") is out.get("codex") is None
    assert "no repository" in out["codexCmd"]["text"] and not out["noLog"]
    # What the dispatcher recorded: one segment, started, then started again from its log.
    sess = st.read(os.path.join("sessions", "S1#1.json"))
    assert sess["source"] == "resume" and sess["cwd"] == str(repo)
    assert "end_reason" not in sess                     # not an exec run: no end at its exit
    assert [t["turn_key"] for t in sess["turns"]] == ["S1#1:r1", "S1#1:r2"]   # one turn per run
    assert [t["turn_key"] for t in st.read(os.path.join("sessions", "S6#1.json"))["turns"]] == []
    assert st.read(os.path.join("sessions", "S6#1.json"))["turn"]["turn_key"] == "S6#1:w1"


def test_an_openclaw_session_that_outlives_its_fold_goes_on_in_a_new_segment(tmp_path):
    """OpenClaw keeps one session for days; the dispatcher folds a quiet session once. A prompt
    after the quiet stretch starts the next segment, whose start folds the first; the next
    segment's fold counts only its own turn."""
    from tokeneconomizer.plugin import hook
    import io
    out, repo, home = _openclaw_node(tmp_path, "segments", TE_FOLD_SYNC="1")
    assert out["segments"] == 2 and out["second"] == ["user", "assistant"]
    st = _openclaw_store(repo, home)
    seg2 = os.path.join(str(home), "hosts", "openclaw", "sessions", "L#2.jsonl")
    env = {"TOKENECONOMIZER_HOME": str(home), "TE_FOLD_SYNC": "1", "TE_MICROSKILLS": "envfacts",
           "GIT_CEILING_DIRECTORIES": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    hook.main(["--host", "openclaw"], io.StringIO(json.dumps({
        "hook_event_name": "SessionEnd", "session_id": "L#2", "transcript_path": seg2,
        "cwd": str(repo), "reason": "other"})), io.StringIO(), env)
    ledger = {r["session_id"]: r for r in st.read_lines("ledger.jsonl")}
    assert set(ledger) == {"L#1", "L#2"}
    assert ledger["L#1"]["requests"] == ledger["L#2"]["requests"] == 1


def test_a_segment_change_ends_the_quiet_segment_so_both_are_folded(tmp_path):
    """The next segment begins after 9 quiet minutes, before the fold would pick up the old one
    (10): it is ended there, so it folds then, and a session that stops after the next turn
    leaves no segment waiting for a SessionStart that may never come."""
    out, repo, home = _openclaw_node(tmp_path, "ended", TE_FOLD_SYNC="1")
    assert out["segments"] == 2
    st = _openclaw_store(repo, home)
    sess = {k: st.read(os.path.join("sessions", f"M#{k}.json")) for k in (1, 2)}
    assert sess[1]["end_reason"] == "segment" and sess[2]["end_reason"] == "idle"
    # The second end's fold runs detached in the plugin; here, inline, past the hook's budget,
    # so another session's start picks it up as an ended session.
    from tokeneconomizer.plugin import hook
    import io
    env = {"TOKENECONOMIZER_HOME": str(home), "TE_FOLD_SYNC": "1", "TE_MICROSKILLS": "envfacts",
           "GIT_CEILING_DIRECTORIES": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    hook.main(["--host", "openclaw"], io.StringIO(json.dumps({
        "hook_event_name": "SessionStart", "session_id": "other", "source": "startup",
        "transcript_path": "", "cwd": str(repo)})), io.StringIO(), env)
    ledger = {r["session_id"]: r for r in st.read_lines("ledger.jsonl")}
    assert set(ledger) == {"M#1", "M#2"}
    assert ledger["M#1"]["requests"] == ledger["M#2"]["requests"] == 1


def test_a_segment_is_ended_only_after_its_last_run_is_logged(tmp_path):
    """The next prompt can start a segment before the old run's `agent_end`: the old segment's
    SessionEnd, whose fold reads its log, waits for that `agent_end` to write the run."""
    out, repo, home = _openclaw_node(tmp_path, "late", TE_FOLD_SYNC="1")
    assert out["first"] == ["user", "assistant"] and out["second"] == ["user", "assistant"]
    st = _openclaw_store(repo, home)
    ledger = {r["session_id"]: r for r in st.read_lines("ledger.jsonl")}
    assert ledger["N#1"]["requests"] == 1                # folded after the run was logged
    assert not (home / "errors.log").exists() or "not yet logged" not in (home / "errors.log").read_text()


def test_two_replies_in_one_millisecond_are_both_logged_and_priced(tmp_path):
    """The log knows a message by its content, not by (role, timestamp, toolCallId): two
    assistant messages that share a millisecond are two entries, each with its usage, and a
    repeated batch adds neither again."""
    from tokeneconomizer import trace
    out, repo, home = _openclaw_node(tmp_path, "sameMs")
    entries = [json.loads(ln) for ln in open(out["path"])][1:]
    assert [e["message"]["role"] for e in entries] == ["user", "assistant", "assistant"]
    usage = trace.pi_session_usage(out["path"])
    assert usage["requests"] == 2 and usage["output_tokens"] == 4


def test_the_openclaw_plugin_takes_a_checkout_as_the_core_does(tmp_path):
    """`inRepo` is `paths.find_repo_root`'s rule: a versioned home is no checkout for the
    directories below it, and an empty `.git` directory is none at all."""
    out, repo, home = _openclaw_node(tmp_path, "repo")
    assert out.get("home") is out.get("empty") is None and out["noLog"] == 0 and out["real"]
    from tokeneconomizer.plugin.paths import find_repo_root
    assert find_repo_root(str(home / "user" / "ws"), {"HOME": str(home / "user")}) is None
    assert find_repo_root(str(home / "empty"), {"HOME": "/nonexistent"}) is None


def test_an_openclaw_agent_exec_run_is_ended_and_folded_when_its_process_exits(tmp_path):
    """`openclaw agent exec` (2026.10.8) fires no `session_end` and exits right after it prints,
    without waiting for `agent_end` handlers (run 40774930744): in that process the plugin sends
    Stop blocking and SessionEnd at exit, so the run is folded with its turn closed. Only argv
    marks such a process; the gateway's never reads `agent … exec`."""
    out, repo, home = _openclaw_node(tmp_path, "exec", TE_FOLD_SYNC="1")
    assert out["detected"] == [False, False, True, True, False]
    st = _openclaw_store(repo, home)
    sess = st.read(os.path.join("sessions", "X#1.json"))
    assert sess["end_reason"] == "exit"
    assert [t["turn_key"] for t in sess["turns"]] == ["X#1:r1"]      # Stop closed it first
    ledger = {r["session_id"]: r for r in st.read_lines("ledger.jsonl")}
    assert ledger["X#1"]["requests"] == 1


def test_the_openclaw_quiet_limit_is_inside_the_folds(tmp_path):
    """A segment takes no turn once the dispatcher may have folded it."""
    from tokeneconomizer.plugin import fold
    src = open(os.path.join(PLUGIN, "openclaw", "index.ts"), encoding="utf-8").read()
    quiet_ms = int(re.search(r"QUIET_MS = ([\d_]+);", src).group(1).replace("_", ""))
    assert quiet_ms < fold.STALE_S * 1000


def test_the_plugin_carries_the_core_as_symlinks_an_install_copy_follows(tmp_path):
    """Claude Code installs a COPY of plugin/ into its cache, even from a local checkout, so the
    core must be reachable from inside plugin/."""
    import shutil
    assert os.listdir(os.path.join(PLUGIN, "lib")) == ["tokeneconomizer"]
    link = os.path.join(PLUGIN, "lib", "tokeneconomizer")
    assert os.path.islink(link) and os.readlink(link) == "../../tokeneconomizer"
    assert os.path.isfile(os.path.join(link, "plugin", "hook.py"))
    # What an install does: copy the directory, following links. The copy runs on its own.
    copy = str(tmp_path / "cache" / "tokeneconomizer" / "0.1.0")
    shutil.copytree(PLUGIN, copy, symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    out = subprocess.run([os.path.join(copy, "scripts", "te"), "doctor"], capture_output=True,
                         text=True, env=dict(os.environ, TOKENECONOMIZER_HOME=str(tmp_path / "h")),
                         timeout=60)
    assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]


def test_a_codex_install_copy_finds_the_core_in_the_marketplace_it_came_from(tmp_path):
    """Codex installs a COPY of plugin/ that drops the `lib` symlink (0.153.1), so the copy
    finds the core through the marketplace's `source` in Codex's config.toml."""
    import shutil
    home = tmp_path / "codex"
    copy = home / "plugins" / "cache" / "tokeneconomizer" / "tokeneconomizer" / "0.1.0"
    shutil.copytree(PLUGIN, str(copy), symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "lib"))
    (copy / "lib").mkdir()

    def te(source):
        (home / "config.toml").write_text(
            'check_for_update_on_startup = false\n\n[marketplaces.tokeneconomizer]\n'
            f'source_type = "local"\nsource = "{source}"\n')
        return subprocess.run([str(copy / "scripts" / "te"), "doctor"], capture_output=True,
                              text=True, timeout=60, cwd=str(tmp_path),
                              env=dict(os.environ, TOKENECONOMIZER_HOME=str(tmp_path / "h"),
                                       TE_CORE_PATH="", PYTHONPATH=""))
    out = te(REPO)
    assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]
    out = te(str(tmp_path / "nowhere"))
    assert out.returncode == 1 and "core package was not found" in out.stderr
    assert "was not found for the plugin at" in (tmp_path / "h" / "errors.log").read_text()
    # A git marketplace (`codex plugin marketplace add owner/repo`): config.toml names the URL,
    # and Codex keeps its checkout under .tmp/marketplaces (seen on 0.153.1).
    (home / ".tmp" / "marketplaces").mkdir(parents=True)
    os.symlink(REPO, str(home / ".tmp" / "marketplaces" / "tokeneconomizer"))
    out = te("https://github.com/microsoft/TokenEconomizer.git")
    assert out.returncode == 0 and "python" in out.stdout, out.stderr[-500:]


def test_codex_gets_its_commands_as_plugin_skills_and_no_one_else_does():
    """Codex has no plugin slash commands; its plugins carry skills. Ours live outside `skills/`,
    which Claude Code and Copilot would load beside their slash commands. Codex lists a plugin's
    skills under the plugin's name (0.163.0), so `status` is `$tokeneconomizer:status`, as the
    slash command is `/tokeneconomizer:status`; each description names TokenEconomizer, which is
    what a request in plain words, or a Codex that lists the bare name, matches on."""
    from tokeneconomizer.plugin import cli
    manifest = _json(PLUGIN, ".codex-plugin", "plugin.json")
    assert manifest["skills"] == "./codex-skills"
    assert not os.path.exists(os.path.join(PLUGIN, "skills"))
    root = os.path.join(PLUGIN, "codex-skills")
    subcommands = set(cli.build_parser()._subparsers._group_actions[0].choices)
    names = sorted(os.listdir(root))
    assert names == ["config", "microskills", "repos", "status", "toolprune"]
    for name in names:
        text = open(os.path.join(root, name, "SKILL.md"), encoding="utf-8").read()
        assert text.startswith(f'---\nname: "{name}"\ndescription: >\n')
        assert "TokenEconomizer" in text.split("\n")[3]
        cmd = re.search(r'bin/te" --host codex --cwd "\$\(pwd\)" (\w+)', text)
        assert cmd and cmd.group(1) in subcommands and cmd.group(1) == name


def test_the_commands_keep_their_earlier_names(tmp_path, monkeypatch, capsys):
    """`te facts` / `te untool` and `/tokeneconomizer:facts` / `:untool` were the names before
    the mechanisms were renamed; they run exactly what `microskills` / `toolprune` run."""
    from tokeneconomizer.plugin import cli
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    for new, old in (("microskills", "facts"), ("toolprune", "untool")):
        outs = []
        for name in (new, old):
            assert cli.main(["--cwd", str(work), "--host", "claude", name]) == 0
            outs.append(capsys.readouterr().out)
        assert outs[0] == outs[1] and outs[0]
        body = {}
        for name in (new, old):
            text = open(os.path.join(PLUGIN, "slash-commands", f"{name}.md"), encoding="utf-8").read()
            body[name] = re.search(r'"\$te" --cwd "\$\(pwd\)" (\w+)', text).group(1)
        assert body[new] == body[old] == new


def test_toolprune_note_after_refresh_catalog(tmp_path, monkeypatch, capsys):
    """Right after `refresh-catalog`, the repository has not folded a session with the catalog
    yet; the note says so instead of asking for the command just run."""
    from tokeneconomizer.plugin import cli
    home = tmp_path / "home"
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    assert cli.main(["--cwd", str(work), "--host", "claude", "toolprune"]) == 0
    assert "without `te --host claude toolprune refresh-catalog`" in capsys.readouterr().out
    (home / "hosts" / "claude").mkdir(parents=True)
    (home / "hosts" / "claude" / "catalog.json").write_text("{}", encoding="utf-8")
    assert cli.main(["--cwd", str(work), "--host", "claude", "toolprune"]) == 0
    out = capsys.readouterr().out
    assert "joins this repository's evidence from its next session" in out
    assert "without `te --host" not in out


def test_codex_sees_no_claude_format_commands_folder():
    """Codex turns a plugin's `commands/` folder into skills of its own, which would list every
    command twice beside `codex-skills/`; Claude Code and Copilot read `slash-commands/` instead."""
    assert not os.path.exists(os.path.join(PLUGIN, "commands"))
    for manifest in (".claude-plugin/plugin.json", ".github/plugin/plugin.json"):
        with open(os.path.join(PLUGIN, manifest), encoding="utf-8") as fh:
            assert json.load(fh)["commands"] == "./slash-commands/"


# ---- Hermes: a Python plugin loaded into Hermes' own process --------------------------------
HERMES_SHIM = os.path.join(PLUGIN, "hermes", "__init__.py")


def _load_hermes_shim(path=HERMES_SHIM):
    """The shim as Hermes loads it: a package from a file location (plugins_loader.py)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"te_hermes_{abs(hash(path))}", path,
        submodule_search_locations=[os.path.dirname(path)])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _HermesCtxV0215:
    """PluginContext as hermes-agent v0.21.5 (`f97608f`) exposes it to `register()`.

    `session_cwd`, `session_surface` and `post_message` are not on that release."""
    def __init__(self):
        self.hooks, self.sections, self.commands = {}, {}, {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_system_prompt_section(self, sid, fn, position="after_memory"):
        self.sections[sid] = fn

    def register_command(self, name, fn, description="", args_hint=""):
        self.commands[name] = fn


class _HermesCtx:
    """The parts of Hermes' PluginContext the shim uses."""
    def __init__(self, cwd, raise_cwd=False):
        self.cwd, self.raise_cwd = cwd, raise_cwd
        self.hooks, self.sections, self.commands, self.posted = {}, {}, {}, []

    def session_cwd(self):
        if self.raise_cwd:
            raise RuntimeError("no session")
        return self.cwd

    def session_surface(self):
        return "cli"

    def post_message(self, text):
        self.posted.append(text)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_system_prompt_section(self, sid, fn, position="after_memory"):
        self.sections[sid] = fn

    def register_command(self, name, fn, description="", args_hint=""):
        self.commands[name] = fn


def _yaml_list(path, key):
    """The items of a top-level `key:` list in a plain YAML file (no YAML library here)."""
    items, inside = [], False
    for line in open(path, encoding="utf-8"):
        if line.startswith(f"{key}:"):
            inside = True
        elif inside and line.startswith("  - "):
            items.append(line[4:].strip())
        elif inside and line.strip():
            break
    return items


def test_the_hermes_manifest_names_the_hooks_the_shim_registers(tmp_path):
    ctx = _HermesCtx(str(tmp_path))
    _load_hermes_shim().register(ctx)
    manifest = os.path.join(PLUGIN, "hermes", "plugin.yaml")
    assert set(_yaml_list(manifest, "provides_hooks")) == set(ctx.hooks)
    # Never `pre_tool_call`: Hermes fails closed when a pre-tool hook throws or times out.
    assert "pre_tool_call" not in ctx.hooks and set(ctx.sections) == {"tokeneconomizer"}
    assert set(ctx.commands) == {"te"}
    text = open(manifest, encoding="utf-8").read()
    assert re.search(r'^name: tokeneconomizer$', text, re.M)
    version = re.search(r'^version: "([^"]+)"$', text, re.M).group(1)
    assert version == _json(PLUGIN, ".claude-plugin", "plugin.json")["version"]


def test_the_hermes_shim_is_stdlib_only_and_spawns_the_dispatcher():
    for mod, level in _imports(HERMES_SHIM):
        assert level == 0 and mod.split(".")[0] in sys.stdlib_module_names, mod
    src = open(HERMES_SHIM, encoding="utf-8").read()
    assert '_run("te-hook", ["--host", "hermes"]' in src


def test_every_hermes_callback_fails_open(tmp_path, monkeypatch):
    """A dispatcher that dies, answers garbage or a context that raises: every callback answers
    Hermes' "no change" (None; "" for the section), and the tool result stays as it was."""
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    broken = tmp_path / "plugin"
    (broken / "hermes").mkdir(parents=True)
    shutil.copy(HERMES_SHIM, broken / "hermes" / "__init__.py")
    (broken / "scripts").mkdir()
    hook = broken / "scripts" / "te-hook"
    hook.write_text("#!/bin/sh\necho 'not json'\nexit 3\n")
    hook.chmod(0o755)
    for raise_cwd in (False, True):
        ctx = _HermesCtx(str(tmp_path), raise_cwd=raise_cwd)
        _load_hermes_shim(str(broken / "hermes" / "__init__.py")).register(ctx)
        assert ctx.sections["tokeneconomizer"]({"session_id": "s", "platform": "cli"}) == ""
        kw = {"session_id": "s", "turn_id": "t", "platform": "cli"}
        assert ctx.hooks["on_session_start"](**kw) is None
        assert ctx.hooks["subagent_start"](child_session_id="c") is None
        assert ctx.hooks["pre_llm_call"](**kw, user_message="hi", is_first_turn=True) is None
        assert ctx.hooks["transform_tool_result"](
            tool_name="terminal", args={"command": "ls"}, result='{"exit_code": 1}',
            status="error", error_message="exit 1", **kw) is None
        assert ctx.hooks["on_session_end"](completed=True, interrupted=False, **kw) is None
        assert ctx.hooks["on_session_finalize"](session_id="s", platform="cli") is None
        assert isinstance(ctx.commands["te"]("status", session_id="s"), str)
    assert "hermes" in (tmp_path / "home" / "errors.log").read_text()   # the raise was logged


def test_a_v0215_context_without_session_cwd_injects_and_runs_every_hook(tmp_path, monkeypatch):
    """hermes-agent v0.21.5 has no `session_cwd`. Cron pins the directory on
    `agent.runtime_cwd` and does not chdir; the session row and the process cwd are the
    fallbacks. `microskills=on` is stored as `envfacts+distiller`, and facts render."""
    import types
    from tokeneconomizer.plugin.config import load_config
    from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
    from tokeneconomizer.plugin.store import Store
    from fixtures.hermes import hermes_state_db

    env = _hook_env(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TE_MICROSKILLS", raising=False)
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    ident = repo_identity(str(repo), env)
    st = Store(repo_state_dir(ident, "hermes", env))
    cfg_dir = os.path.dirname(st.base)
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"microskills": "on"}, fh)
    assert load_config(str(tmp_path / "home"), repo_dir=cfg_dir, env=env).microskills \
        == "envfacts+distiller"
    fact = "Facts:\n- the suite is `python -m pytest`"
    st.write("injection.json", {"rendered": fact, "rendered_ids": ["a"]})

    held = {"cwd": str(repo)}
    runtime = types.ModuleType("agent.runtime_cwd")
    runtime.scoped_session_cwd = lambda: held["cwd"]
    saved = {name: sys.modules.get(name) for name in ("agent", "agent.runtime_cwd")}
    sys.modules["agent"] = types.ModuleType("agent")
    sys.modules["agent.runtime_cwd"] = runtime
    try:
        ctx = _HermesCtxV0215()
        assert not hasattr(ctx, "session_cwd")
        _load_hermes_shim().register(ctx)
        section = ctx.sections["tokeneconomizer"]
        assert fact in section({"session_id": "s-runtime", "platform": "cron"})
        kw = {"session_id": "s-runtime", "turn_id": "t1", "platform": "cron"}
        for name in ("on_session_start", "pre_llm_call", "transform_tool_result",
                     "on_session_end", "on_session_finalize"):
            assert name in ctx.hooks
        assert ctx.hooks["on_session_start"](**kw) is None
        assert ctx.hooks["subagent_start"](child_session_id="kid") is None
        assert ctx.hooks["pre_llm_call"](**kw, user_message="hi", is_first_turn=True) is None
        assert ctx.hooks["transform_tool_result"](
            tool_name="terminal", args={"command": "ls"}, result="ok", status="ok", **kw) is None
        assert ctx.hooks["on_session_end"](
            completed=True, interrupted=False, failed=False, **kw) is None
        assert ctx.hooks["on_session_finalize"](
            session_id="s-runtime", platform="cron", reason="end") is None
        assert "TokenEconomizer" in ctx.commands["te"]("status", session_id="s-runtime")

        held["cwd"] = ""
        hermes_state_db(str(home / "state.db"), {"id": "s-record", "cwd": str(repo)})
        assert fact in section({"session_id": "s-record", "platform": "cli"})

        monkeypatch.chdir(repo)
        assert fact in section({"session_id": "s-process", "platform": "cli"})
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
    log = tmp_path / "home" / "errors.log"
    assert not log.exists(), log.read_text() if log.exists() else ""


def test_a_hermes_cwd_keeps_a_trailing_space_and_a_punctuated_home(tmp_path, monkeypatch):
    """A checkout whose name ends in a space stays that path. A profile home whose
    path contains `#` or `?` still yields `sessions.cwd`."""
    from fixtures.hermes import hermes_state_db
    shim = _load_hermes_shim()
    spaced = str(tmp_path / "project ")
    assert shim._usable_cwd(spaced) == spaced
    assert shim._usable_cwd("  auto  ") == "" and shim._usable_cwd("  ") == ""
    home = tmp_path / "dev #1?"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    hermes_state_db(str(home / "state.db"), {"id": "s", "cwd": spaced})
    assert shim._recorded_cwd("s") == spaced


def test_the_hermes_shim_drives_the_dispatcher_end_to_end(tmp_path, monkeypatch):
    """Through the real launcher: a session start pins the session to the repository, a tool
    result passes through unchanged while the guard has nothing to say, and `/te` answers."""
    for k, v in _hook_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    work = tmp_path / "work"
    work.mkdir()
    ctx = _HermesCtx(str(work))
    _load_hermes_shim().register(ctx)
    assert ctx.sections["tokeneconomizer"]({"session_id": "s1", "platform": "cli",
                                            "cwd": str(work)}) == ""
    homes = list((tmp_path / "home").rglob("sessions/s1.json"))
    assert len(homes) == 1
    rec = json.loads(homes[0].read_text())
    assert rec["host"] == "hermes" and rec["transcript"] == \
        str(tmp_path / "hermes" / "state.db") + "#s1"
    assert ctx.hooks["transform_tool_result"](
        tool_name="terminal", args={"command": "ls"}, session_id="s1", turn_id="t1",
        result='{"output": "a", "exit_code": 0, "error": null}', status="ok") is None
    # A subagent's events never reach the dispatcher.
    ctx.hooks["subagent_start"](child_session_id="kid")
    assert ctx.sections["tokeneconomizer"]({"session_id": "kid", "platform": "cli"}) == ""
    assert not list((tmp_path / "home").rglob("sessions/kid.json"))
    assert "TokenEconomizer" in ctx.commands["te"]("status", session_id="s1")


def test_a_hermes_turn_of_many_calls_is_one_guarded_turn(tmp_path, monkeypatch):
    """Through the shim and the real launcher: a turn's calls fold into one guard, so a stuck
    turn reaches the redirect (appended to the result the model reads), a repeated
    `pre_llm_call` for the same turn does not reopen it, and `on_session_end` closes it."""
    from tests.test_live_hooks import _stuck
    for k, v in _hook_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    work = tmp_path / "work"
    work.mkdir()
    ctx = _HermesCtx(str(work))
    _load_hermes_shim().register(ctx)
    h = ctx.hooks
    assert ctx.sections["tokeneconomizer"]({"session_id": "s", "platform": "cli",
                                            "cwd": str(work)}) == ""

    def turn(tid, calls):
        out = []
        h["pre_llm_call"](session_id="s", turn_id=tid, is_first_turn=False)
        for k, (name, args, result) in enumerate(calls):
            if k == 3:                                   # a second call for the same turn
                h["pre_llm_call"](session_id="s", turn_id=tid, is_first_turn=False)
            failed = '"exit_code": 1' in result
            out.append(h["transform_tool_result"](
                tool_name=name, args=args, result=result, session_id="s", turn_id=tid,
                status="error" if failed else "ok", error_message="exit 1" if failed else None))
        h["on_session_end"](session_id="s", turn_id=tid, completed=True, interrupted=False,
                            failed=False)
        return out

    for k in range(5):
        turn(f"g{k}", [("read_file", {"path": "/r/a.py"}, '{"content": "ok"}')] * 8)
    stuck = _stuck("hermes")
    outs = turn("stuck", stuck)
    redirected = [k for k, o in enumerate(outs) if o and "Process check (automated)" in o]
    assert redirected and outs[redirected[0]].startswith(stuck[redirected[0]][2])
    (rec,) = (tmp_path / "home").rglob("sessions/s.json")
    turns = json.loads(rec.read_text())["turns"]
    assert [(t["turn_key"], t["tool_calls"]) for t in turns] == [
        (f"g{k}", 8) for k in range(5)] + [("stuck", len(stuck))]
    assert turns[-1]["nudges"] == 1


def test_the_hermes_shim_logs_a_launcher_that_is_missing_or_too_slow(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    shim = _load_hermes_shim()
    monkeypatch.setattr(shim, "_scripts", lambda: None)
    assert shim._run("te-hook", ["--host", "hermes"], "{}", 1) == ""
    slow = tmp_path / "scripts"
    slow.mkdir()
    (slow / "te-hook").write_text("#!/bin/sh\nsleep 5\n")
    (slow / "te-hook").chmod(0o755)
    monkeypatch.setattr(shim, "_scripts", lambda: str(slow))
    assert shim._run("te-hook", ["--host", "hermes"], "{}", 0.2) == ""
    log = (tmp_path / "home" / "errors.log").read_text()
    assert "no launchers found" in log and "no answer within 0.2 s" in log


def test_a_finalized_hermes_subagent_is_forgotten(tmp_path, monkeypatch):
    monkeypatch.setenv("TE_DISABLE", "1")                # no dispatcher needed here
    ctx = _HermesCtx(str(tmp_path))
    shim = _load_hermes_shim()
    calls = []
    monkeypatch.setattr(shim, "_hook", lambda p, *a: calls.append(p) or {})
    shim.register(ctx)
    ctx.hooks["subagent_start"](child_session_id="kid")
    ctx.hooks["on_session_finalize"](session_id="kid", platform="subagent")
    assert calls == []                                   # no SessionEnd for a subagent
    ctx.hooks["pre_llm_call"](session_id="kid", turn_id="t")
    assert [c["session_id"] for c in calls] == ["kid"]   # the id is no longer held


def test_a_hermes_session_end_waits_for_a_tool_result_in_flight(tmp_path, monkeypatch):
    import threading
    import time
    ctx = _HermesCtx(str(tmp_path))
    shim = _load_hermes_shim()
    order, started = [], threading.Event()

    def fake_hook(p, *a):
        if p["hook_event_name"] == "PostToolUse":
            started.set()
            time.sleep(0.3)
        order.append(p["hook_event_name"])
        return {}
    monkeypatch.setattr(shim, "_hook", fake_hook)
    shim.register(ctx)
    t = threading.Thread(target=ctx.hooks["transform_tool_result"], kwargs=dict(
        tool_name="terminal", args={"command": "ls"}, result="{}", session_id="s",
        turn_id="t", status="ok"))
    t.start()
    started.wait(5)
    ctx.hooks["on_session_finalize"](session_id="s", platform="cli")
    t.join()
    assert order == ["PostToolUse", "SessionEnd"]


def test_the_hermes_bundle_is_a_plugin_directory_on_its_own(tmp_path):
    out = tmp_path / "market"
    subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_plugin.py"),
                    "--out", str(out)], check=True, capture_output=True)
    bundle = out / "hermes" / "tokeneconomizer"
    assert (bundle / "plugin.yaml").exists() and (bundle / "__init__.py").exists()
    assert os.access(bundle / "scripts" / "te-hook", os.X_OK)
    assert (bundle / "lib" / "tokeneconomizer" / "plugin" / "hook.py").exists()
    mod = _load_hermes_shim(str(bundle / "__init__.py"))
    assert mod._scripts() == str(bundle / "scripts")
