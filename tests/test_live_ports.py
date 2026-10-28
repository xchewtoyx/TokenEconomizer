"""The live-ports driver (`scripts/live/ports.py`), offline: the budget guard's decision and the
assertions over the plugin's state. The model path itself runs only in GitHub Actions."""
import importlib.util
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "live_ports", os.path.join(ROOT, "scripts", "live", "ports.py"))
ports = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ports)

MODEL = "z-ai/glm-5.3-flash"


def _openrouter(monkeypatch, weekly, tools=True, listed=True):
    def get(path, key=None):
        if path == "/models":
            return {"data": [{"id": MODEL, "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
                              "supported_parameters": ["tools"] if tools else []}] if listed else []}
        if path.endswith("/endpoints"):
            return {"data": {"endpoints": [{"pricing": {"prompt": "0.0000002", "completion": "0.000001"}}]}}
        assert path == "/key" and key == "k"
        return {"data": {"usage_weekly": weekly}}
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


def test_the_estimate_prices_the_worst_endpoint_and_the_guard_stops_at_the_budget(
        monkeypatch, tmp_path):
    est = ports.estimate(0.0000002, 0.000001)       # the dearer endpoint, not the listed price
    assert est == pytest.approx(5 * (300_000 * 2e-7 + 15_000 * 1e-6) * 1.25)
    _openrouter(monkeypatch, weekly=0.0)
    assert _guard(monkeypatch, tmp_path) == (0, {"run": "true", "estimate": f"{est:.4f}"})
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
