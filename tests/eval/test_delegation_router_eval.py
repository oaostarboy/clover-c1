"""Self-test for the router-only eval and the router record in the mode eval.

No model is called: ``route`` is injected, and the mode eval's reader stands in
for the profile's agent.log.  The pure parts (accuracy, percentiles, the log
record parser) are exercised on the shapes the production router writes.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROUTER_SCRIPT = REPO_ROOT / "scripts" / "eval" / "delegation_router.py"
MODES_SCRIPT = REPO_ROOT / "scripts" / "eval" / "delegation_modes.py"
CASES_PATH = REPO_ROOT / "tests" / "eval" / "delegation_modes.jsonl"
HELDOUT_PATH = REPO_ROOT / "tests" / "eval" / "delegation_router_heldout.jsonl"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rt = _load("delegation_router_eval", ROUTER_SCRIPT)
dm = _load("delegation_modes_eval_for_router", MODES_SCRIPT)


class FakeVerdict:
    def __init__(self, mode, source="router", reason="r"):
        self.mode = mode
        self.source = source
        self.reason = reason
        self.tasks = ()


def _cases(*specs):
    return [{"id": i, "prompt": p, "expected_mode": m, "live_evidence": False, "override": False}
            for i, p, m in specs]


# ── percentiles ────────────────────────────────────────────────────────────

def test_percentile_nearest_rank():
    values = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
    assert rt.percentile(values, 50) == 500
    assert rt.percentile(values, 95) == 1000
    assert rt.percentile([42], 95) == 42
    assert rt.percentile([], 50) is None


# ── router-only run ────────────────────────────────────────────────────────

def test_every_case_runs_reps_times_and_is_scored_against_its_expected_mode():
    cases = _cases(("a", "prompt a", "direct"), ("b", "prompt b", "parallel"))
    answers = {"prompt a": "direct", "prompt b": "single"}
    seen = []

    def route(message, recent_turns=()):
        seen.append(message)
        return FakeVerdict(answers[message])

    runs = rt.run_router_eval(cases, route, reps=3)

    assert len(runs) == 6 and sorted(seen) == ["prompt a"] * 3 + ["prompt b"] * 3
    a = [r for r in runs if r["case_id"] == "a"]
    b = [r for r in runs if r["case_id"] == "b"]
    assert all(r["correct"] for r in a)
    assert not any(r["correct"] for r in b)
    assert {r["mode"] for r in b} == {"single"}


def test_a_failed_route_is_a_miss_not_a_crash():
    cases = _cases(("a", "p", "direct"))

    def route(message, recent_turns=()):
        return None

    (run,) = rt.run_router_eval(cases, route, reps=1)
    assert run["mode"] is None and run["correct"] is False and run["error"] is None


def test_an_exception_from_route_is_recorded_and_the_run_continues():
    cases = _cases(("a", "p", "direct"), ("b", "q", "direct"))

    def route(message, recent_turns=()):
        if message == "p":
            raise RuntimeError("boom")
        return FakeVerdict("direct")

    runs = rt.run_router_eval(cases, route, reps=1)
    assert runs[0]["error"].startswith("RuntimeError")
    assert runs[1]["correct"] is True


def test_the_workdir_placeholder_is_filled_in():
    cases = _cases(("a", "clone it into {workdir}/click", "single"))
    seen = []

    def route(message, recent_turns=()):
        seen.append(message)
        return FakeVerdict("single")

    rt.run_router_eval(cases, route, reps=1)
    assert "{workdir}" not in seen[0]


def test_latency_is_measured_with_the_injected_clock():
    cases = _cases(("a", "p", "direct"))
    ticks = iter([10.0, 10.25, 20.0, 20.75])

    def route(message, recent_turns=()):
        return FakeVerdict("direct")

    runs = rt.run_router_eval(cases, route, reps=2, clock=lambda: next(ticks))
    assert [r["latency_ms"] for r in runs] == [250, 750]


# ── summary ────────────────────────────────────────────────────────────────

def _run(case_id, expected, mode, ms, source="router", error=None):
    return {"case_id": case_id, "rep": 0, "expected_mode": expected, "mode": mode,
            "latency_ms": ms, "source": source, "correct": mode == expected, "error": error,
            "reason": ""}


def test_summary_reports_accuracy_per_case_and_latency_percentiles():
    runs = [_run("a", "direct", "direct", 400), _run("a", "direct", "direct", 600),
            _run("b", "parallel", "single", 900), _run("b", "parallel", "parallel", 1100),
            _run("c", "direct", None, 5000)]
    s = rt.summarize(runs)

    assert s["n"] == 5 and s["correct"] == 3
    assert s["accuracy"] == pytest.approx(0.6)
    assert s["failed_routes"] == 1
    assert s["cases"]["b"]["modes"] == ["single", "parallel"]
    assert s["cases"]["b"]["correct"] == 1
    assert s["p50_ms"] == 900 and s["p95_ms"] == 5000


def test_overrides_are_scored_but_leave_the_latency_numbers_alone():
    runs = [_run("o", "direct", "direct", 0, source="override"),
            _run("a", "direct", "direct", 800)]
    s = rt.summarize(runs)
    assert s["correct"] == 2
    assert s["p50_ms"] == 800 and s["p95_ms"] == 800


def test_render_has_a_row_per_case_and_the_headline_numbers():
    runs = [_run("a", "direct", "direct", 400), _run("b", "parallel", "single", 900)]
    out = rt.render(rt.summarize(runs))
    assert "a" in out and "b" in out
    assert "accuracy" in out.lower() and "p50" in out and "p95" in out


# ── main(): dry run, injected route, exit status ───────────────────────────

def test_main_runs_the_shipped_cases_with_an_injected_route(tmp_path, capsys):
    def route(message, recent_turns=()):
        return FakeVerdict("direct")

    out_dir = tmp_path / "out"
    code = rt.main(["--cases", str(CASES_PATH), "--reps", "2", "--out", str(out_dir)], route_fn=route)

    assert code == 0
    data = json.loads((out_dir / "results.json").read_text())
    n_cases = sum(1 for line in CASES_PATH.read_text().splitlines() if line.strip())
    assert len(data["runs"]) == n_cases * 2
    assert "p95_ms" in data["summary"]
    assert "accuracy" in capsys.readouterr().out.lower()


def test_main_fails_below_the_requested_accuracy(tmp_path):
    def route(message, recent_turns=()):
        return FakeVerdict("direct")

    code = rt.main(["--cases", str(CASES_PATH), "--min-accuracy", "0.99", "--out", str(tmp_path / "o")],
                   route_fn=route)
    assert code == 1


def test_main_only_runs_the_named_cases(tmp_path):
    seen = []

    def route(message, recent_turns=()):
        seen.append(message)
        return FakeVerdict("direct")

    rt.main(["--cases", str(CASES_PATH), "--only", "pc-diagnostic", "--reps", "1",
             "--out", str(tmp_path / "o")], route_fn=route)
    assert seen == ["Full PC diagnostic"]


# ── the mode eval records the router's verdict and latency per rep ─────────

RECORD = {"session_id": "s-1", "mode": "parallel", "latency_ms": 812, "attempts": 1,
          "source": "router", "tasks": 3, "ok": True}


def test_the_router_log_record_parser_finds_this_sessions_record():
    lines = [
        "2026-10-11 10:00:00 INFO agent.delegation_router: delegation_router "
        + json.dumps({**RECORD, "session_id": "other"}),
        "2026-10-11 10:00:01 INFO something else entirely",
        "2026-10-11 10:00:02 INFO agent.delegation_router: delegation_router " + json.dumps(RECORD),
    ]
    assert dm.parse_router_records(lines, "s-1") == RECORD
    assert dm.parse_router_records(lines, "missing") is None
    assert dm.parse_router_records(["delegation_router {not json"], "s-1") is None


def test_run_eval_keeps_the_router_record_on_each_run(tmp_path):
    class Sess:
        stored_session_id = "s-1"
        approvals = 0
        idle = lambda self: True  # noqa: E731

        def submit(self, prompt):
            pass

        def wait_first_turn(self, timeout):
            return True

        def interrupt(self):
            pass

    class Driver:
        def session(self, **kw):
            from contextlib import nullcontext

            return nullcontext(Sess())

    case = {"id": "x", "prompt": "hi", "expected_mode": "direct", "live_evidence": False,
            "override": False}
    runs = dm.run_eval(
        [case], Driver(), reader=lambda sid: [], router_reader=lambda sid: dict(RECORD),
        root_model="m", root_provider=None, reps=1, evidence_reps=1, out_dir=tmp_path,
        turn_timeout=1, settle_timeout=1, quiet_seconds=0, poll_seconds=0,
        clock=lambda: 0.0, sleep=lambda s: None,
    )
    assert runs[0]["router"] == RECORD


def test_the_summary_lists_router_verdicts_when_present_and_is_unchanged_otherwise():
    case = {"id": "x", "prompt": "hi", "expected_mode": "parallel", "live_evidence": False,
            "override": False}
    base = {"case_id": "x", "rep": 0, "expected_mode": "parallel", "error": None, "settled": True,
            "score": {"mode": "parallel"}, "correct": True}
    meta = {"profile": "p", "root_model": "m"}
    bar = {"checks": [], "passed": True}

    without = dm.render_summary([base], [case], bar, meta)
    with_router = dm.render_summary([{**base, "router": RECORD}], [case], bar, meta)

    assert "router" not in without.lower()
    assert "router" in with_router.lower() and "812" in with_router and "parallel" in with_router


# ── the held-out set and the warm-up ───────────────────────────────────────

def test_the_held_out_set_is_a_real_held_out_set():
    """Different texts, every mode covered, no override cases (those never reach
    the model) and the same loader: it can be fed straight to the eval."""
    held = rt.load_cases(HELDOUT_PATH)
    main = rt.load_cases(CASES_PATH)
    assert len(held) >= 10
    assert {c["id"] for c in held}.isdisjoint({c["id"] for c in main})
    assert {c["prompt"] for c in held}.isdisjoint({c["prompt"] for c in main})
    assert {c["expected_mode"] for c in held} == set(rt.MODES)
    assert not any(c.get("override") for c in held)
    # Paraphrases, not copies: the same intended mode, different wording.
    for case in held:
        assert case["prompt"].strip() and case["id"].startswith("h-")


def test_an_injected_router_is_not_warmed_up(tmp_path):
    calls = []

    def route(message, recent_turns=()):
        calls.append(message)
        return FakeVerdict("direct")

    rt.main(["--cases", str(CASES_PATH), "--only", "pc-diagnostic", "--reps", "1",
             "--out", str(tmp_path / "o")], route_fn=route)
    assert calls == ["Full PC diagnostic"]


def _real_path(monkeypatch, tmp_path, argv):
    """Run ``main`` on the real-router path with ``route`` faked; returns the
    ``(message, settings)`` of every router call."""
    import agent.delegation_router as router_module

    seen = []

    def route(message, recent_turns=(), *, settings=None, max_tasks=None):
        seen.append((message, settings))
        return FakeVerdict("direct")

    monkeypatch.setattr(router_module, "route", route)
    monkeypatch.setattr(router_module, "load_settings",
                        lambda: router_module.RouterSettings(timeout_seconds=5.0))
    rt.main(["--cases", str(CASES_PATH), "--only", "pc-diagnostic", "--reps", "1",
             "--out", str(tmp_path / "o"), *argv])
    return seen


def test_the_real_router_path_warms_up_with_a_long_deadline_and_does_not_count_it(
        monkeypatch, tmp_path):
    seen = _real_path(monkeypatch, tmp_path, [])
    assert len(seen) == 2
    (_, warm), (message, measured) = seen
    assert message == "Full PC diagnostic"
    assert warm.timeout_seconds >= 60 and measured.timeout_seconds == 5.0
    results = json.loads((tmp_path / "o" / "results.json").read_text())
    assert len(results["runs"]) == 1


def test_no_warmup_times_the_first_call(monkeypatch, tmp_path):
    seen = _real_path(monkeypatch, tmp_path, ["--no-warmup"])
    assert [m for m, _ in seen] == ["Full PC diagnostic"]
