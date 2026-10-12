"""The ship-gate eval set and the eval script's list-of-modes / gate support.

No model is called: the real gate is a manual run (docs/delegation-decisions.md,
"Ship gate").  These tests pin the fixture's shape and the scoring rules.
"""
from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tests" / "fixtures" / "router_eval" / "ship_gate.jsonl"


@pytest.fixture(scope="module")
def ev():
    path = REPO_ROOT / "scripts" / "eval" / "delegation_router.py"
    spec = importlib.util.spec_from_file_location("delegation_router_eval_gate", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cases(ev):
    return ev.load_cases(GATE)


def test_the_set_is_about_thirty_cases_covering_every_mode(ev, cases):
    assert 28 <= len(cases) <= 34
    modes = Counter(ev.expected_label(c["expected_mode"]) for c in cases)
    for mode in ("direct", "single", "parallel", "staged"):
        assert modes[mode] >= 2


def test_the_octavia_cases_are_there_with_their_context(cases):
    by_id = {c["id"]: c for c in cases}
    assert by_id["g-octavia-email-extension"]["prompt"].startswith("Hey, can you email to Julia")
    skill = by_id["g-octavia-send-skill"]
    assert skill["prompt"].endswith("Can you also send the AutoHDR skill?")
    assert [t["role"] for t in skill["recent_turns"]] == ["user", "assistant", "user", "assistant"]
    assert by_id["g-gpu3"]["expected_mode"] == "single"
    assert by_id["g-pcdiag-3agents"]["expected_mode"] == "parallel"
    gated = {c["id"] for c in cases if c.get("gate")}
    assert {"g-gpu3", "g-octavia-email-extension", "g-octavia-send-skill", "g-pcdiag-3agents"} <= gated
    assert all(c["expected_mode"] != "direct" for c in cases if c.get("gate")), "the gate is about not undersizing"


def test_ambiguous_cases_use_a_list_and_only_where_needed(ev, cases):
    ambiguous = [c for c in cases if isinstance(c["expected_mode"], list)]
    assert 1 <= len(ambiguous) <= 3
    assert all(len(c["expected_mode"]) == 2 for c in ambiguous)


def test_a_list_accepts_any_listed_mode_and_nothing_else(ev):
    case = {"id": "a", "prompt": "p", "expected_mode": ["direct", "single"]}

    def route_returning(mode):
        def route(message, recent_turns=()):
            return type("V", (), {"mode": mode, "source": "router", "reason": ""})()
        return route

    verdicts = {m: ev.run_router_eval([case], route_returning(m))[0]["correct"]
                for m in ("direct", "single", "parallel", "staged")}
    assert verdicts == {"direct": True, "single": True, "parallel": False, "staged": False}
    run = ev.run_router_eval([case], route_returning("single"))[0]
    assert run["expected_mode"] == "direct|single"


@pytest.mark.parametrize("bad", [
    '{"id":"x","prompt":"p","expected_mode":[]}',
    '{"id":"x","prompt":"p","expected_mode":["direct","direct"]}',
    '{"id":"x","prompt":"p","expected_mode":["direct","nope"]}',
    '{"id":"x","prompt":"p","expected_mode":3}',
    '{"id":"x","prompt":"p"}',
])
def test_bad_expected_modes_are_rejected(ev, tmp_path, bad):
    path = tmp_path / "c.jsonl"
    path.write_text(bad + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ev.load_cases(path)


def test_the_gate_needs_each_gate_case_right_in_two_of_three_runs(ev):
    cases = [{"id": "a", "prompt": "p", "expected_mode": "single", "gate": True},
             {"id": "b", "prompt": "p", "expected_mode": "single", "gate": True},
             {"id": "c", "prompt": "p", "expected_mode": "direct"}]

    def runs(a, b):
        out = []
        for cid, got in (("a", a), ("b", b), ("c", ["direct"])):
            for i, mode in enumerate(got):
                out.append({"case_id": cid, "rep": i, "expected_mode": "single" if cid != "c" else "direct",
                            "mode": mode, "correct": mode == ("direct" if cid == "c" else "single"),
                            "latency_ms": 1, "source": "router"})
        return out

    ok = ev.summarize(runs(["single", "single", "direct"], ["single"] * 3))
    assert ev.gate_failures(cases, ok) == []
    bad = ev.summarize(runs(["single", "direct", "direct"], ["single"] * 3))
    assert ev.gate_failures(cases, bad) == ["a"]


def test_main_exit_code_follows_the_gate(ev, tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text('{"id":"a","prompt":"p","expected_mode":"single","gate":true,"reps":3}\n'
                    '{"id":"b","prompt":"q","expected_mode":"direct"}\n', encoding="utf-8")

    def make(mode_for_a):
        def route(message, recent_turns=()):
            mode = mode_for_a if message == "p" else "direct"
            return type("V", (), {"mode": mode, "source": "router", "reason": ""})()
        return route

    argv = ["--cases", str(path), "--out", str(tmp_path / "o"), "--ship-gate", "--reps", "1"]
    assert ev.main(argv, route_fn=make("single")) == 0
    assert ev.main(argv, route_fn=make("direct")) == 1
    summary = (tmp_path / "o" / "summary.txt").read_text()
    assert "ship gate: FAIL" in summary and "a" in summary
