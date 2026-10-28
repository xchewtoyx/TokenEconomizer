"""The live-ports driver (`scripts/live/ports.py`), offline: the budget guard's decision and the
assertions over the plugin's state. The model path itself runs only in GitHub Actions."""
import importlib.util
import json
import os
import sys
import time
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "live_ports", os.path.join(ROOT, "scripts", "live", "ports.py"))
ports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ports)

MODEL = "z-ai/glm-5.3-flash"


def _openrouter(monkeypatch, weekly, tools=True, listed=True, endpoints=True):
    def get(path, key=None):
        if path == "/models":
            return {"data": [{"id": MODEL, "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
                              "supported_parameters": ["tools"] if tools else []}] if listed else []}
        if path.endswith("/endpoints"):
            if endpoints is not True:
                if isinstance(endpoints, Exception):
                    raise endpoints
                return endpoints
            return {"data": {"endpoints": [{"pricing": {"prompt": "0.0000002", "completion": "0.000001"}}]}}
        assert path == "/key" and key == "k"
        return {"data": {"usage_weekly": weekly, "usage": 10.0}}
    monkeypatch.setattr(ports, "_get", get)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")


def _guard(monkeypatch, tmp_path, budget=2.0):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(ports, "ROOT", str(tmp_path))
    (tmp_path / "plugin" / "hermes").mkdir(parents=True, exist_ok=True)
    rc = ports.main(["guard", "--platform", "hermes", "--model", MODEL, "--budget", str(budget)])
    return rc, dict(l.split("=", 1) for l in out.read_text().split()) if out.exists() else {}


def test_the_reserve_prices_the_dearest_endpoint_and_the_guard_stops_at_the_budget(
        monkeypatch, tmp_path):
    est = ports.estimate(0.0000002, 0.000001)       # the endpoint's price, not the listed one
    assert est == pytest.approx(5 * (300_000 * 2e-7 + 15_000 * 1e-6) * 1.25)
    _openrouter(monkeypatch, weekly=0.0)
    assert _guard(monkeypatch, tmp_path) == (0, {"run": "true", "reserve": f"{est:.4f}"})
    _openrouter(monkeypatch, weekly=2.0 - est / 2)
    assert _guard(monkeypatch, tmp_path)[1]["run"] == "false"


def test_a_pinned_model_that_is_gone_or_takes_no_tools_fails_the_job(monkeypatch, tmp_path):
    for kw in ({"listed": False}, {"tools": False}):
        _openrouter(monkeypatch, weekly=0.0, **kw)
        with pytest.raises(SystemExit) as e:
            _guard(monkeypatch, tmp_path)
        assert e.value.code == 1


def test_a_port_missing_from_the_tree_is_skipped_without_a_call(monkeypatch, tmp_path):
    monkeypatch.setattr(ports, "_get", lambda *a, **k: pytest.fail("no call expected"))
    monkeypatch.setattr(ports, "ROOT", str(tmp_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    assert ports.main(["guard", "--platform", "openclaw", "--model", MODEL]) == 0
    assert (tmp_path / "out").read_text() == "run=false\n"


def test_the_checks_fail_on_plugin_faults_and_call_a_model_without_tools_inconclusive(tmp_path):
    home = tmp_path / "home"
    d = home / "repos" / "k" / "hermes"
    (d / "sessions").mkdir(parents=True)
    for i in range(5):
        (d / "sessions" / f"s{i}.json").write_text(json.dumps({"started": i}))
    rows = [{"kind": "session", "session_id": f"s{i}", "model": MODEL, "input_tokens": 10,
             "output_tokens": 2, "effective_tokens": 20.0, "priced_by": "fallback"} for i in range(4)]
    (d / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (home / "errors.log").write_text("known noise\n")
    res = ports.check(str(home), "hermes", str(tmp_path), allow_errors=["known"],
                      folded_at_end={"s1": False})
    assert [s for s, w in res if w.startswith("the plugin recorded")] == ["PASS"]
    assert [s for s, w in res if w.startswith("s0: ledger row")] == ["PASS"]
    assert ("FAIL", "s4: no ledger row") in res
    assert ("FAIL", f"s1: not folded within {ports.FOLD_WAIT_S}s of its end (no SessionEnd "
                    "from the agent?); folded afterwards by `te fold`") in res
    assert ("INCONCLUSIVE", "model did not use tools: no shell call in any session") in res
    assert [s for s, w in res if w.startswith("errors.log")] == ["PASS"]
    (d / "events.jsonl").write_text(json.dumps({"kind": "inject", "session_id": "s4",
                                                "chars": 300}) + "\n")
    assert ("PASS", "fact injected at the start of 1 later session(s)") in ports.check(
        str(home), "hermes", str(tmp_path))


@pytest.mark.parametrize("endpoints", [
    urllib.error.URLError("down"), {"data": {"endpoints": []}}, {"data": {}},
    {"data": {"endpoints": [{"pricing": {"prompt": "nan", "completion": "0.000001"}}]}},
    {"data": {"endpoints": [{"pricing": {"prompt": "-1", "completion": "0.000001"}}]}}])
def test_without_valid_endpoint_prices_the_guard_skips_rather_than_use_the_listed_price(
        monkeypatch, tmp_path, endpoints):
    _openrouter(monkeypatch, weekly=0.0, endpoints=endpoints)
    assert _guard(monkeypatch, tmp_path) == (0, {"run": "false"})


def _sleeper():
    return [sys.executable, "-c", "import time; time.sleep(60)"]


def test_the_watchdog_kills_the_agent_once_the_run_spends_its_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(ports, "WATCH_S", 0.05)
    out, err, env = str(tmp_path / "o"), str(tmp_path / "e"), dict(os.environ)
    reads = iter([0.01, 0.02, 0.5, 0.6])
    t = time.monotonic()
    rc, why = ports.run_watched(_sleeper(), str(tmp_path), env, out, err,
                                spent=lambda: next(reads), trip=0.25)
    assert rc is None and why.startswith("run reserve reached") and time.monotonic() - t < 10

    def down():
        raise urllib.error.URLError("down")
    rc, why = ports.run_watched(_sleeper(), str(tmp_path), env, out, err, spent=down, trip=0.25)
    assert rc is None and why.startswith("spend unknown")
    rc, why = ports.run_watched([sys.executable, "-c", "print('done')"], str(tmp_path), env,
                                out, err, spent=lambda: 0.0, trip=0.25)
    assert (rc, why) == (0, None) and (tmp_path / "o").read_text() == "done\n"


def test_a_zero_trip_lets_a_free_model_run_until_it_costs_money():
    assert not ports.over_trip(0.0, 0.0) and ports.over_trip(0.0001, 0.0)
    assert not ports.over_trip(0.1, 0.25) and ports.over_trip(0.25, 0.25)


def _run(monkeypatch, tmp_path, reserve, totals):
    """cmd_run with the agent and the fold stubbed: the key's all-time usage reads `totals` in
    turn (the first read is the run's baseline). Returns (exit code, sessions started)."""
    started = []
    reads = iter(totals)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(ports, "key_usage", lambda key: (0.0, next(reads)))
    monkeypatch.setattr(ports, "agent_cmd", lambda *a: ["true"])
    monkeypatch.setattr(ports, "wait_folded", lambda *a, **k: {})
    monkeypatch.setattr(ports, "check", lambda *a, **k: [("PASS", "stubbed")])

    def watched(cmd, cwd, env, out, err, spent, trip):
        started.append(trip)
        open(out, "w").close()
        open(err, "w").close()
        return 0, None
    monkeypatch.setattr(ports, "run_watched", watched)
    rc = ports.main(["run", "--platform", "hermes", "--model", MODEL, "--work",
                     str(tmp_path / "w"), "--reserve", str(reserve)])
    return rc, started


def test_a_session_that_ends_past_the_trip_stops_the_run_before_the_next(monkeypatch, tmp_path):
    # baseline 1.00; session 1 ends with 0.30 spent, past the 0.25 trip of a 0.50 reserve
    rc, started = _run(monkeypatch, tmp_path, 0.50, [1.0, 1.0, 1.30])
    assert rc == 1 and started == [0.25]


def test_a_free_model_runs_every_session_while_its_spend_stays_zero(monkeypatch, tmp_path):
    rc, started = _run(monkeypatch, tmp_path, 0.0, [1.0] * 6)
    assert rc == 0 and started == [0.0] * ports.SESSIONS
    rc, started = _run(monkeypatch, tmp_path / "b", 0.0, [1.0, 1.0, 1.0, 1.01])
    assert rc == 1 and len(started) == 2                 # it began to cost money: stopped
