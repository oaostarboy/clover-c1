"""Scorer self-test for the live delegation-mode eval harness.

Every transcript here is written through the real ``SessionDB`` in a temp
home and read back with the same reader the live run uses, so the scorer is
exercised on the real row shapes: assistant ``tool_calls`` JSON, ``tool``
result rows, and delegation-completion user rows rendered by the real
``format_process_notification``.  Only the model/network boundary is absent.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "eval" / "delegation_modes.py"
CASES_PATH = REPO_ROOT / "tests" / "eval" / "delegation_modes.jsonl"


def _load_module():
    spec = importlib.util.spec_from_file_location("delegation_modes_eval", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["delegation_modes_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


dm = _load_module()

BASE_TS = 1_800_000_000.0


# ---------------------------------------------------------------------------
# Transcript recorder: writes rows through the real SessionDB API
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self, db, session_id: str, start: float = BASE_TS):
        self.db = db
        self.sid = session_id
        self.t = start
        self._calls = 0
        db.create_session(session_id, "tui", model="fake-root")

    def _tick(self, dt: float = 1.0) -> float:
        self.t += dt
        return self.t

    def user(self, text: str, *, kind: str | None = None, dt: float = 1.0) -> None:
        self.db.append_message(
            self.sid, "user", text, display_kind=kind, timestamp=self._tick(dt)
        )

    def assistant(self, text=None, calls=None, dt: float = 1.0) -> None:
        self.db.append_message(
            self.sid, "assistant", text, tool_calls=calls, timestamp=self._tick(dt)
        )

    def tool_result(self, call_id: str, name: str, payload, dt: float = 1.0) -> None:
        content = payload if isinstance(payload, str) else json.dumps(payload)
        self.db.append_message(
            self.sid, "tool", content, tool_name=name, tool_call_id=call_id,
            timestamp=self._tick(dt),
        )

    def call(self, name: str, args: dict) -> dict:
        self._calls += 1
        return {
            "id": f"call_{self._calls}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    # -- composite helpers -------------------------------------------------

    def work(self, name: str = "terminal", args: dict | None = None, result="ok") -> None:
        c = self.call(name, args or {"command": "true"})
        self.assistant(None, [c])
        self.tool_result(c["id"], name, {"output": result, "exit_code": 0})

    def delegate(
        self,
        tasks: list[dict],
        *,
        delegation_id: str,
        text: str | None = None,
        status: str = "dispatched",
        args_extra: dict | None = None,
    ) -> str:
        args = {"tasks": tasks, **(args_extra or {})}
        c = self.call("delegate_task", args)
        self.assistant(text, [c])
        if status == "dispatched":
            payload = {
                "status": "dispatched",
                "mode": "background",
                "count": len(tasks),
                "delegation_id": delegation_id,
                "goals": [t.get("goal", "") for t in tasks],
                "note": "Running in the background.",
            }
        else:
            payload = {
                "status": "rejected",
                "mode": "background",
                "started": False,
                "reason": "capacity",
                "error": "no capacity",
                "note": "",
            }
        self.tool_result(c["id"], "delegate_task", payload)
        return c["id"]

    def completion_single(
        self,
        delegation_id: str,
        *,
        goal: str,
        status: str = "completed",
        summary: str = "Finished the job; files are in place.",
        error: str | None = None,
        kind: str | None = "internal_notification",
        dt: float = 30.0,
    ) -> None:
        from tools.process_registry import format_process_notification

        text = format_process_notification(
            {
                "type": "async_delegation",
                "delegation_id": delegation_id,
                "goal": goal,
                "role": "leaf",
                "model": "fake-worker",
                "status": status,
                "summary": summary,
                "error": error,
                "api_calls": 4,
                "duration_seconds": 21.5,
                "dispatched_at": self.t - dt,
                "completed_at": self.t,
            }
        )
        self.user(text, kind=kind, dt=dt)

    def completion_batch(
        self,
        delegation_id: str,
        goals: list[str],
        *,
        statuses: list[str] | None = None,
        kind: str | None = "internal_notification",
        dt: float = 40.0,
    ) -> None:
        from tools.process_registry import format_process_notification

        statuses = statuses or ["completed"] * len(goals)
        results = [
            {
                "task_index": i,
                "status": st,
                "summary": f"Result for task {i}",
                "api_calls": 2,
                "duration_seconds": 9.0,
            }
            for i, st in enumerate(statuses)
        ]
        text = format_process_notification(
            {
                "type": "async_delegation",
                "delegation_id": delegation_id,
                "is_batch": True,
                "goals": goals,
                "results": results,
                "role": "leaf",
                "model": "fake-worker",
                "total_duration_seconds": 33,
                "dispatched_at": self.t - dt,
                "completed_at": self.t,
            }
        )
        self.user(text, kind=kind, dt=dt)


@pytest.fixture()
def db(tmp_path):
    from clover_state import SessionDB

    database = SessionDB(db_path=tmp_path / ".clover" / "state.db")
    yield database
    database.close()


def _score(db, sid: str) -> dict:
    return dm.score_transcript(db.get_messages(sid))


# ---------------------------------------------------------------------------
# Mode classification
# ---------------------------------------------------------------------------


def test_direct_no_dispatch(db):
    r = Recorder(db, "s-direct")
    r.user("What is my disk usage by folder?")
    r.work("terminal", {"command": "du -sh *"})
    r.assistant("Your biggest folder is Videos at 41 GB.", dt=3.0)

    s = _score(db, "s-direct")

    assert s["mode"] == "direct"
    assert s["accepted_dispatches"] == 0
    assert s["first_dispatch_ordinal"] is None
    assert s["tool_calls_before_first_dispatch"] is None
    assert s["tool_calls_total"] == 1
    assert s["tiers"] == []
    assert s["limit_hits"] == []
    assert s["premature_success"] == []
    # direct: the acknowledgement is the answer itself (user row -> final text)
    assert s["ack_seconds"] == pytest.approx(5.0, abs=0.01)


def test_single_one_call_one_task(db):
    r = Recorder(db, "s-single")
    r.user("Write a 3000-word market report on EV chargers in Canada.")
    r.delegate(
        [{"goal": "Write the 3000-word EV charger report", "tier": "code",
          "context": "Plan: outline, draft, check word count."}],
        delegation_id="deleg-1",
        text="On it: a helper is writing the report and I will post it here.",
    )
    r.assistant("A helper is writing it now; I will check it and send it over.", dt=2.0)
    r.completion_single("deleg-1", goal="Write the 3000-word EV charger report")
    r.assistant("Here is the report, checked at 3,012 words.", dt=5.0)

    s = _score(db, "s-single")

    assert s["mode"] == "single"
    assert s["accepted_dispatches"] == 1
    assert s["first_wave_tasks"] == 1
    assert s["tiers"] == ["code"]
    assert s["first_dispatch_ordinal"] == 1
    assert s["tool_calls_before_first_dispatch"] == 0
    assert s["completion_turns"] == 1
    assert s["premature_success"] == []
    assert s["ack_seconds"] == pytest.approx(1.0, abs=0.01)


def test_parallel_one_call_many_tasks(db):
    r = Recorder(db, "s-par")
    r.user("25 best indie horror games, Steam score, length, rank and pitch each.")
    tasks = [{"goal": f"Research horror games batch {i}", "tier": "read"} for i in range(5)]
    r.delegate(tasks, delegation_id="deleg-9", text="Splitting this across five helpers.")
    r.completion_batch("deleg-9", [t["goal"] for t in tasks])
    r.assistant("Merged list of 25 games, ranked.", dt=4.0)

    s = _score(db, "s-par")

    assert s["mode"] == "parallel"
    assert s["first_wave_tasks"] == 5
    assert s["tiers"] == ["read"] * 5
    assert s["first_dispatch_ordinal"] == 1
    assert s["premature_success"] == []


def test_parallel_two_single_task_calls_in_one_root_turn(db):
    r = Recorder(db, "s-par2")
    r.user("Compare the pricing pages of Slack and Teams.")
    r.delegate([{"goal": "Summarise Slack pricing page"}], delegation_id="d-a")
    r.delegate([{"goal": "Summarise Teams pricing page"}], delegation_id="d-b")
    r.completion_single("d-a", goal="Summarise Slack pricing page")
    r.completion_single("d-b", goal="Summarise Teams pricing page")
    r.assistant("Comparison done.", dt=3.0)

    s = _score(db, "s-par2")

    assert s["mode"] == "parallel"
    assert s["accepted_dispatches"] == 2
    assert s["staged_dispatches"] == 0


def test_staged_second_dispatch_from_completion_turn_after_verified_stage(db):
    r = Recorder(db, "s-staged")
    r.user("Add a feature flag for dark mode, write tests, then review the diff.")
    r.delegate(
        [{"goal": "Add the dark-mode feature flag and its tests", "tier": "code"}],
        delegation_id="stage-1",
        text="A helper is building it; a reviewer follows.",
    )
    r.assistant("Building it now; I will have it reviewed next.", dt=2.0)
    r.completion_single("stage-1", goal="Add the dark-mode feature flag and its tests")
    # root verifies stage 1 before chaining
    r.work("read_file", {"path": "flags.py"}, result="DARK_MODE = False")
    r.delegate(
        [{"goal": "Review the diff of the dark-mode flag change for correctness",
          "tier": "check"}],
        delegation_id="stage-2",
    )
    r.assistant("Reviewer is on it.", dt=2.0)
    r.completion_single(
        "stage-2", goal="Review the diff of the dark-mode flag change for correctness",
        summary="Diff is clean; two nits noted.",
    )
    r.assistant("Built, tested and reviewed. Two small nits noted.", dt=4.0)

    s = _score(db, "s-staged")

    assert s["mode"] == "staged"
    assert s["accepted_dispatches"] == 2
    assert s["staged_dispatches"] == 1
    assert s["repair_dispatches"] == 0
    assert s["tiers"] == ["code", "check"]
    assert s["stage_verify_calls"] == [1]
    assert s["premature_success"] == []


@pytest.mark.parametrize("kind", ["internal_notification", "async_delegation_complete", None])
def test_completion_row_recognised_for_every_gateway_row_type(db, kind):
    """The gateway types the row internal_notification, the TUI/desktop gateway
    async_delegation_complete; the body marker alone must also be enough."""
    r = Recorder(db, f"s-kind-{kind}")
    r.user("Write the report.")
    r.delegate([{"goal": "Write the report"}], delegation_id="k1")
    r.completion_single("k1", goal="Write the report", kind=kind)
    r.delegate([{"goal": "Review the report for factual errors", "tier": "check"}],
               delegation_id="k2")
    r.completion_single("k2", goal="Review the report for factual errors", kind=kind)
    r.assistant("Written and reviewed.", dt=2.0)

    s = _score(db, f"s-kind-{kind}")

    assert s["completion_turns"] == 2
    assert s["mode"] == "staged"
    assert dm.pending_work(db.get_messages(f"s-kind-{kind}")) == []


def test_repair_redispatch_of_same_task_is_not_staged(db):
    r = Recorder(db, "s-repair")
    r.user("Write a 3000-word market report on EV chargers in Canada.")
    r.delegate(
        [{"goal": "Write the 3000-word EV charger report", "tier": "code"}],
        delegation_id="try-1",
    )
    r.completion_single(
        "try-1", goal="Write the 3000-word EV charger report",
        status="error", summary="", error="worker crashed",
    )
    r.delegate(
        [{"goal": "Write the 3000-word EV charger report", "tier": "code"}],
        delegation_id="try-2",
    )
    r.completion_single("try-2", goal="Write the 3000-word EV charger report")
    r.assistant("The report is attached.", dt=3.0)

    s = _score(db, "s-repair")

    assert s["mode"] == "single"
    assert s["staged_dispatches"] == 0
    assert s["repair_dispatches"] == 1
    assert s["accepted_dispatches"] == 2


def test_redo_of_a_succeeded_stage_with_same_goal_is_not_staged(db):
    r = Recorder(db, "s-redo")
    r.user("Build the landing page.")
    r.delegate([{"goal": "Build the landing page for the bakery"}], delegation_id="a")
    r.completion_single("a", goal="Build the landing page for the bakery")
    r.delegate([{"goal": "Build the landing page for the bakery"}], delegation_id="b")
    r.completion_single("b", goal="Build the landing page for the bakery")
    r.assistant("Done.", dt=2.0)

    s = _score(db, "s-redo")

    assert s["mode"] == "single"
    assert s["repair_dispatches"] == 1


def test_rejected_dispatch_is_not_an_accepted_hand_off(db):
    r = Recorder(db, "s-rej")
    r.user("Transcode this video.")
    r.delegate([{"goal": "Transcode the video"}], delegation_id="x", status="rejected")
    r.work("terminal", {"command": "ffmpeg -i in.mov out.mp4"})
    r.assistant("Transcoded.", dt=2.0)

    s = _score(db, "s-rej")

    assert s["mode"] == "direct"
    assert s["rejected_dispatches"] == 1
    assert s["accepted_dispatches"] == 0
    # the attempt still counts for how early the root tried to hand off
    assert s["first_dispatch_ordinal"] == 1


def test_control_actions_and_legacy_goal_shape(db):
    r = Recorder(db, "s-ctrl")
    r.user("Do the thing.")
    c = r.call("delegate_task", {"action": "list"})
    r.assistant(None, [c])
    r.tool_result(c["id"], "delegate_task", {"children": []})
    legacy = r.call("delegate_task", {"goal": "Legacy single goal", "context": "ctx"})
    r.assistant(None, [legacy])
    r.tool_result(
        legacy["id"], "delegate_task",
        {"status": "dispatched", "mode": "background", "count": 1, "delegation_id": "leg"},
    )

    s = _score(db, "s-ctrl")

    assert s["mode"] == "single"
    assert s["accepted_dispatches"] == 1
    assert s["first_dispatch_ordinal"] == 2


# ---------------------------------------------------------------------------
# Tool-call position, tiers, user-visible hits, premature success
# ---------------------------------------------------------------------------


def test_tool_calls_before_first_dispatch_and_default_tier_label(db):
    r = Recorder(db, "s-pos")
    r.user("Clone the repo, run the full tests and summarise the failures.")
    r.work("terminal", {"command": "git status"})
    r.delegate([{"goal": "Clone, test, summarise"}], delegation_id="p1")

    s = _score(db, "s-pos")

    assert s["first_dispatch_ordinal"] == 2
    assert s["tool_calls_before_first_dispatch"] == 1
    assert s["tiers"] == ["(default)"]


def test_limit_mention_in_user_visible_text_is_a_hit_but_tool_text_is_not(db):
    r = Recorder(db, "s-limit")
    r.user("25 best indie horror games.")
    r.work("terminal", {"command": "sleep 1"}, result="request timeout after 30s, limit reached")
    r.delegate([{"goal": "Research games"}], delegation_id="l1",
               text="My first attempt timed out at the 5-minute limit, so a helper has it.")
    r.completion_single("l1", goal="Research games")
    r.assistant("Here is the ranked list.", dt=2.0)

    s = _score(db, "s-limit")

    matched = sorted(h["match"].lower() for h in s["limit_hits"])
    assert matched == ["attempt", "limit", "timed out"]
    assert all("timeout after 30s" not in h["snippet"] for h in s["limit_hits"])


def test_clean_text_has_no_limit_hits(db):
    r = Recorder(db, "s-clean")
    r.user("Rewrite this paragraph.")
    r.assistant("The meeting begins at nine, so please arrive a few minutes early.", dt=1.0)

    assert _score(db, "s-clean")["limit_hits"] == []


def test_premature_success_claim_before_results_return(db):
    r = Recorder(db, "s-prem")
    r.user("25 best indie horror games, ranked.")
    r.delegate(
        [{"goal": f"Research part {i}"} for i in range(5)],
        delegation_id="pm",
        text="All 25 games are done and ranked.",
    )
    r.completion_batch("pm", [f"Research part {i}" for i in range(5)])
    r.assistant("Merged and checked: here is the final ranked list.", dt=3.0)

    s = _score(db, "s-prem")

    assert len(s["premature_success"]) == 1
    assert "done and ranked" in s["premature_success"][0]["snippet"]


def test_honest_acknowledgement_and_claim_after_results_are_not_premature(db):
    r = Recorder(db, "s-honest")
    r.user("Write the report.")
    r.delegate(
        [{"goal": "Write the report"}],
        delegation_id="h1",
        text="A helper is writing it; I will post it here when it is ready.",
    )
    r.assistant("I'll check it as soon as it lands.", dt=1.0)
    r.completion_single("h1", goal="Write the report")
    r.assistant("The report is done and checked. Here it is.", dt=3.0)

    assert _score(db, "s-honest")["premature_success"] == []


def test_premature_claim_after_second_stage_dispatch_is_caught(db):
    r = Recorder(db, "s-prem2")
    r.user("Build the page, then audit it.")
    r.delegate([{"goal": "Build the landing page"}], delegation_id="b1")
    r.completion_single("b1", goal="Build the landing page")
    r.delegate(
        [{"goal": "Audit the landing page for accessibility"}],
        delegation_id="b2",
        text="The audit is complete and the page passes.",
    )
    r.completion_single("b2", goal="Audit the landing page for accessibility")
    r.assistant("Audit finished: two issues found.", dt=2.0)

    s = _score(db, "s-prem2")

    assert s["mode"] == "staged"
    assert len(s["premature_success"]) == 1


# ---------------------------------------------------------------------------
# Per-case verdicts, overrides
# ---------------------------------------------------------------------------


def _case(**kw):
    base = {
        "id": "c", "prompt": "p", "expected_mode": "direct",
        "live_evidence": False, "override": False,
    }
    base.update(kw)
    return base


def test_judge_run_override_respected(db):
    r = Recorder(db, "s-ov-ok")
    r.user("Do it yourself: list the 25 best indie horror games.")
    r.work("web_search", {"query": "best indie horror"})
    r.assistant("Here are 25 games.", dt=2.0)
    case = _case(id="override-direct", override=True, expected_mode="direct")

    verdict = dm.judge_run(_score(db, "s-ov-ok"), case)

    assert verdict["correct"] is True
    assert verdict["override_violated"] is False


def test_judge_run_override_violated(db):
    r = Recorder(db, "s-ov-bad")
    r.user("Do it yourself: list the 25 best indie horror games.")
    r.delegate([{"goal": "List the games"}], delegation_id="ov")
    case = _case(id="override-direct", override=True, expected_mode="direct")

    verdict = dm.judge_run(_score(db, "s-ov-bad"), case)

    assert verdict["correct"] is False
    assert verdict["override_violated"] is True


def test_judge_run_dispatch_within_calls(db):
    r = Recorder(db, "s-within")
    r.user("25 horror games")
    r.work("terminal")
    r.work("web_search")
    r.delegate([{"goal": f"part {i}"} for i in range(2)], delegation_id="w")
    case = _case(expected_mode="parallel", dispatch_within_calls=2)

    verdict = dm.judge_run(_score(db, "s-within"), case)

    assert verdict["correct"] is True
    assert verdict["dispatch_within_calls_ok"] is False


# ---------------------------------------------------------------------------
# Pass bar
# ---------------------------------------------------------------------------


def _bar_fixture():
    cases = dm.load_cases(CASES_PATH)
    runs = []
    for case in cases:
        reps = 3 if case["live_evidence"] else 1
        for rep in range(reps):
            score = {
                "mode": case["expected_mode"],
                "limit_hits": [],
                "premature_success": [],
                "first_dispatch_ordinal": 1 if case["expected_mode"] != "direct" else None,
            }
            runs.append(
                {"case_id": case["id"], "rep": rep, "error": None,
                 "score": score, **dm.judge_run(score, case)}
            )
    return cases, runs


def _check(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


def test_bar_passes_on_perfect_run():
    cases, runs = _bar_fixture()
    result = dm.evaluate_bar(runs, cases)
    assert result["passed"] is True, result


def test_bar_allows_two_misses_but_not_three():
    cases, runs = _bar_fixture()
    plain = [c["id"] for c in cases if not c["live_evidence"] and not c["override"]]

    def flip(ids):
        out = []
        for run in runs:
            if run["case_id"] in ids:
                run = {**run, "correct": False}
            out.append(run)
        return out

    assert dm.evaluate_bar(flip(set(plain[:2])), cases)["passed"] is True
    third = dm.evaluate_bar(flip(set(plain[:3])), cases)
    assert third["passed"] is False
    assert _check(third, "cases_correct")["ok"] is False


def test_bar_override_miss_fails_even_with_enough_correct():
    cases, runs = _bar_fixture()
    ov = next(c["id"] for c in cases if c["override"])
    runs = [{**r, "correct": False} if r["case_id"] == ov else r for r in runs]

    result = dm.evaluate_bar(runs, cases)

    assert result["passed"] is False
    assert _check(result, "overrides_correct")["ok"] is False
    assert _check(result, "cases_correct")["ok"] is True


def test_bar_live_evidence_needs_every_rep():
    cases, runs = _bar_fixture()
    ev = next(c["id"] for c in cases if c["live_evidence"])
    bad = False
    out = []
    for r in runs:
        if r["case_id"] == ev and not bad:
            r = {**r, "correct": False}
            bad = True
        out.append(r)

    result = dm.evaluate_bar(out, cases)

    assert result["passed"] is False
    assert _check(result, "live_evidence_every_rep")["ok"] is False


def test_bar_limit_mention_and_premature_success_are_zero_tolerance():
    cases, runs = _bar_fixture()
    runs[0] = {**runs[0], "score": {**runs[0]["score"], "limit_hits": [{"match": "limit"}]}}
    result = dm.evaluate_bar(runs, cases)
    assert _check(result, "limit_mentions")["ok"] is False

    cases, runs = _bar_fixture()
    runs[1] = {**runs[1], "score": {**runs[1]["score"], "premature_success": [{"snippet": "x"}]}}
    result = dm.evaluate_bar(runs, cases)
    assert _check(result, "premature_success")["ok"] is False


def test_bar_horror_dispatch_position_and_run_errors():
    cases, runs = _bar_fixture()
    horror = next(c["id"] for c in cases if c.get("dispatch_within_calls"))
    late = []
    for r in runs:
        if r["case_id"] == horror:
            score = {**r["score"], "first_dispatch_ordinal": 5}
            r = {**r, "score": score, **dm.judge_run(score, next(c for c in cases if c["id"] == horror))}
        late.append(r)
    result = dm.evaluate_bar(late, cases)
    assert _check(result, "dispatch_within_calls")["ok"] is False

    cases, runs = _bar_fixture()
    runs[0] = {**runs[0], "error": "gateway exited"}
    assert _check(dm.evaluate_bar(runs, cases), "run_errors")["ok"] is False


def test_bar_is_configurable():
    cases, runs = _bar_fixture()
    runs[0] = {**runs[0], "score": {**runs[0]["score"], "limit_hits": [{"match": "limit"}]}}
    bar = dm.parse_bar_overrides(["max_limit_mentions=1", "min_correct=10"])
    assert bar == {"max_limit_mentions": 1, "min_correct": 10}

    result = dm.evaluate_bar(runs, cases, bar)

    assert _check(result, "limit_mentions")["ok"] is True
    with pytest.raises(ValueError):
        dm.parse_bar_overrides(["no_such_knob=1"])


# ---------------------------------------------------------------------------
# Case file contract
# ---------------------------------------------------------------------------


def test_case_file_contract():
    cases = dm.load_cases(CASES_PATH)

    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    assert len(cases) == dm.DEFAULT_BAR["of"]
    assert {c["expected_mode"] for c in cases} <= set(dm.MODES)
    assert all(c["prompt"].strip() for c in cases)
    evidence = [c for c in cases if c["live_evidence"]]
    overrides = [c for c in cases if c["override"]]
    assert len(evidence) == 3
    assert len(overrides) == 2
    # every mode is represented, so no mode can silently go unmeasured
    assert {c["expected_mode"] for c in cases} == set(dm.MODES)
    # overrides force the opposite of what the same request would pick unaided
    assert {c["expected_mode"] for c in overrides} == {"direct", "single"}
    assert sum(1 for c in cases if c.get("dispatch_within_calls")) == 1


def test_load_cases_rejects_bad_rows(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"id": "x", "prompt": "p", "expected_mode": "swarm"}) + "\n")
    with pytest.raises(ValueError):
        dm.load_cases(bad)


# ---------------------------------------------------------------------------
# Settling, reader, event tracker
# ---------------------------------------------------------------------------


def test_pending_work_reports_unfinished_dispatch_and_open_tool_call(db):
    r = Recorder(db, "s-pend")
    r.user("Write the report.")
    r.delegate([{"goal": "Write the report"}], delegation_id="pd")
    r.assistant("A helper has it.", dt=1.0)

    pending = dm.pending_work(db.get_messages("s-pend"))
    assert any("pd" in p for p in pending)

    r.completion_single("pd", goal="Write the report")
    assert dm.pending_work(db.get_messages("s-pend"))  # root has not answered yet
    r.assistant("Here is the report.", dt=2.0)
    assert dm.pending_work(db.get_messages("s-pend")) == []

    c = r.call("terminal", {"command": "ls"})
    r.assistant(None, [c])
    assert dm.pending_work(db.get_messages("s-pend"))


def test_wait_settled_waits_for_quiet_and_idle(db):
    r = Recorder(db, "s-wait")
    r.user("hi")
    r.assistant("hello", dt=1.0)
    now = {"t": 0.0}
    idle = {"v": False}

    def clock():
        return now["t"]

    def sleep(dt):
        now["t"] += dt
        if now["t"] >= 10:
            idle["v"] = True

    ok = dm.wait_settled(
        lambda: db.get_messages("s-wait"), lambda: idle["v"],
        quiet_seconds=5, timeout=100, poll_seconds=1, clock=clock, sleep=sleep,
    )
    assert ok is True
    assert now["t"] >= 10


def test_wait_settled_times_out_when_helper_never_returns(db):
    r = Recorder(db, "s-never")
    r.user("do it")
    r.delegate([{"goal": "x"}], delegation_id="nv")
    r.assistant("A helper has it.", dt=1.0)
    now = {"t": 0.0}

    ok = dm.wait_settled(
        lambda: db.get_messages("s-never"), lambda: True,
        quiet_seconds=1, timeout=30, poll_seconds=5,
        clock=lambda: now["t"], sleep=lambda dt: now.__setitem__("t", now["t"] + dt),
    )
    assert ok is False


def test_read_transcript_reads_live_db_from_second_handle(db, tmp_path):
    r = Recorder(db, "s-read")
    r.user("hello")
    r.assistant("hi", dt=1.0)

    rows = dm.read_transcript(db.db_path, "s-read")

    assert [m["role"] for m in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "hello"


def test_rpc_event_tracker_counts_turns_for_its_session_only():
    t = dm.RpcEventTracker("abc123")
    t.feed({"jsonrpc": "2.0", "method": "event",
            "params": {"type": "message.start", "session_id": "abc123"}})
    t.feed({"jsonrpc": "2.0", "method": "event",
            "params": {"type": "message.start", "session_id": "other"}})
    assert t.running_turns == 1
    assert t.completed_turns == 0
    t.feed({"jsonrpc": "2.0", "method": "event",
            "params": {"type": "message.complete", "session_id": "abc123",
                       "payload": {"text": "done", "status": "complete"}}})
    assert t.running_turns == 0
    assert t.completed_turns == 1
    t.feed({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})
    assert t.responses[7] == {"result": {"ok": True}}
    t.feed({"jsonrpc": "2.0", "method": "event",
            "params": {"type": "error", "session_id": "abc123",
                       "payload": {"message": "agent init failed"}}})
    assert t.errors == ["agent init failed"]


def test_wait_settled_reports_each_poll_to_on_poll(db):
    r = Recorder(db, "s-poll")
    r.user("hi")
    r.assistant("hello", dt=1.0)
    now = {"t": 0.0}
    seen: list[int] = []

    ok = dm.wait_settled(
        lambda: db.get_messages("s-poll"), lambda: True,
        quiet_seconds=3, timeout=100, poll_seconds=1,
        clock=lambda: now["t"], sleep=lambda dt: now.__setitem__("t", now["t"] + dt),
        on_poll=lambda msgs: seen.append(len(msgs)),
    )

    assert ok is True
    assert len(seen) >= 3 and set(seen) == {2}


def test_rpc_event_tracker_counts_event_types_for_its_session_only():
    t = dm.RpcEventTracker("abc123")
    for sid, kind in [("abc123", "tool.start"), ("abc123", "tool.start"),
                      ("other", "tool.start"), ("abc123", "thinking.delta")]:
        t.feed({"jsonrpc": "2.0", "method": "event",
                "params": {"type": kind, "session_id": sid}})
    assert t.event_counts == {"tool.start": 2, "thinking.delta": 1}


def test_gateway_connection_keeps_child_stderr_and_reports_progress(tmp_path):
    """A real child process: its stderr must survive on disk and a quiet wait must say so."""
    fake_root = tmp_path / "root"
    pkg = fake_root / "tui_gateway"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "entry.py").write_text(
        "import sys, time\n"
        "print('{\"jsonrpc\": \"2.0\", \"method\": \"event\", \"params\": {\"type\": \"gateway.ready\"}}', flush=True)\n"
        "print('child-warning: lockowners unavailable', file=sys.stderr, flush=True)\n"
        "sys.stdin.read()\n"
    )
    log = tmp_path / "logs" / "case-r0.stderr.log"
    beats: list[str] = []

    conn = dm._GatewayConnection(
        tmp_path, sys.executable, fake_root, ready_timeout=20,
        log_path=log, progress=beats.append, heartbeat_seconds=0.0,
    )
    try:
        conn.pump(0.1)
        deadline = time.monotonic() + 10
        while "child-warning" not in (log.read_text() if log.exists() else ""):
            assert time.monotonic() < deadline, "stderr never reached the log"
            conn.pump(0.1)
    finally:
        conn.close()

    assert "child-warning: lockowners unavailable" in log.read_text()
    assert beats and "running_turns=0" in beats[0]


# ---------------------------------------------------------------------------
# End to end: runner + report on recorded transcripts (fake driver only)
# ---------------------------------------------------------------------------


class _ReplaySession:
    def __init__(self, db, sid, play):
        self.stored_session_id = sid
        self._db, self._play = db, play

    def submit(self, prompt: str) -> None:
        self._play(self._db, self.stored_session_id, prompt)

    def wait_first_turn(self, timeout: float) -> bool:
        return True

    def idle(self) -> bool:
        return True

    def interrupt(self) -> None:
        pass


class _ReplayDriver:
    """Plays a scripted transcript into the real SessionDB on submit."""

    def __init__(self, db, script):
        self.db, self.script = db, script
        self.sessions: list[str] = []

    def session(self, *, model, provider, cwd):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            sid = f"replay-{len(self.sessions)}"
            self.sessions.append(sid)
            yield _ReplaySession(self.db, sid, self.script)

        return _cm()


def _script(db, sid, prompt):
    r = Recorder(db, sid)
    r.user(prompt)
    if "horror" in prompt.lower() and "yourself" not in prompt.lower():
        r.delegate([{"goal": f"games {i}"} for i in range(5)], delegation_id=f"d-{sid}")
        r.completion_batch(f"d-{sid}", [f"games {i}" for i in range(5)])
        r.assistant("Merged, ranked list of 25.", dt=3.0)
    else:
        r.work("terminal", {"command": "true"})
        r.assistant("Done, here is the answer.", dt=3.0)


def test_run_eval_end_to_end_on_recorded_transcripts(db, tmp_path):
    cases = [
        _case(id="horror", prompt="25 best indie horror games, ranked",
              expected_mode="parallel", live_evidence=True, dispatch_within_calls=2),
        _case(id="disk", prompt="What is my disk usage by folder?", expected_mode="direct"),
    ]
    out = tmp_path / "out"
    driver = _ReplayDriver(db, _script)
    now = {"t": 0.0}

    runs = dm.run_eval(
        cases, driver,
        reader=lambda sid: db.get_messages(sid),
        root_model="fake-root", root_provider=None,
        reps=1, evidence_reps=3, out_dir=out,
        turn_timeout=10, settle_timeout=60, quiet_seconds=1, poll_seconds=1,
        clock=lambda: now["t"],
        sleep=lambda dt: now.__setitem__("t", now["t"] + dt),
    )

    assert [(r["case_id"], r["rep"]) for r in runs] == [
        ("horror", 0), ("horror", 1), ("horror", 2), ("disk", 0),
    ]
    assert all(r["error"] is None and r["settled"] for r in runs)
    assert all(r["correct"] for r in runs)
    assert runs[0]["score"]["mode"] == "parallel"
    assert runs[3]["score"]["mode"] == "direct"

    bar = dm.evaluate_bar(runs, cases, {"min_correct": 2, "of": 2})
    assert bar["passed"] is True
    meta = {"profile": "dev", "root_model": "fake-root"}
    dm.write_report(out, runs, cases, bar, meta)
    data = json.loads((out / "results.json").read_text())
    assert data["bar"]["passed"] is True
    assert len(data["runs"]) == 4
    summary = (out / "summary.txt").read_text()
    assert "horror" in summary and "PASS" in summary


def test_run_eval_snapshots_transcript_and_reports_while_waiting(db, tmp_path):
    out = tmp_path / "out"
    now = {"t": 0.0}
    said: list[tuple[str, bool]] = []
    snapshot = out / "transcripts" / "disk-r0.json"

    class _SlowSession(_ReplaySession):
        calls = 0

        def idle(self) -> bool:
            _SlowSession.calls += 1
            return _SlowSession.calls > 8

    class _SlowDriver(_ReplayDriver):
        def session(self, *, model, provider, cwd):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                sid = "slow-0"
                self.sessions.append(sid)
                yield _SlowSession(self.db, sid, self.script)

            return _cm()

    def say(msg: str) -> None:
        said.append((msg, snapshot.exists()))

    runs = dm.run_eval(
        [_case(id="disk", prompt="What is my disk usage by folder?", expected_mode="direct")],
        _SlowDriver(db, _script),
        reader=lambda sid: db.get_messages(sid),
        root_model="fake-root", root_provider=None, reps=1, evidence_reps=3, out_dir=out,
        turn_timeout=10, settle_timeout=600, quiet_seconds=1, poll_seconds=5,
        clock=lambda: now["t"], sleep=lambda dt: now.__setitem__("t", now["t"] + dt),
        progress=say, heartbeat_seconds=10,
    )

    assert runs[0]["settled"] and runs[0]["correct"]
    assert any("session slow-0 started" in m for m, _ in said)
    waiting = [(m, existed) for m, existed in said if " rows, " in m]
    assert waiting, "no heartbeat while waiting for the run to settle"
    assert all(existed for _, existed in waiting), "heartbeat came before the transcript snapshot"


def test_main_exit_codes_and_dry_run(tmp_path, monkeypatch, db, capsys):
    home = tmp_path / "profile-home"
    home.mkdir()
    monkeypatch.setattr(dm, "resolve_profile_home", lambda name: home)

    assert dm.main(
        ["--profile", "dev", "--root-model", "m", "--out", str(tmp_path / "o"),
         "--cases", str(CASES_PATH), "--dry-run"]
    ) == 0
    planned = capsys.readouterr().out
    assert "horror" in planned.lower()

    # a driver that fails every run → harness errors → exit 2, not a bar miss
    class _Boom:
        def session(self, **kw):
            raise RuntimeError("gateway would not start")

    rc = dm.main(
        ["--profile", "dev", "--root-model", "m", "--out", str(tmp_path / "o2"),
         "--cases", str(CASES_PATH), "--only", "disk-usage"],
        driver_factory=lambda home, args: _Boom(),
        reader_factory=lambda home: (lambda sid: []),
    )
    assert rc == 2
    assert (tmp_path / "o2" / "results.json").exists()


# ---------------------------------------------------------------------------
# Per-rep isolation: a disposable copy of ONE frozen baseline of the profile
# ---------------------------------------------------------------------------


def _make_profile(root: Path) -> Path:
    """A real-looking profile home: config, secrets, skills with references,
    memories, and a state.db that already holds a session."""
    import yaml
    from clover_state import SessionDB

    home = root / "real-profile"
    (home / "skills" / "research" / "product-comparison-research" / "references").mkdir(parents=True)
    (home / "skills" / "research" / "product-comparison-research" / "SKILL.md").write_text(
        "---\nname: product-comparison-research\ndescription: Compare products.\n---\nv1 steps\n")
    (home / "skills" / "research" / "product-comparison-research" / "references" / "games.md").write_text(
        "save cheap data first, then hand the slow part to a helper\n")
    (home / "skills" / "devops" / "disk-triage").mkdir(parents=True)
    (home / "skills" / "devops" / "disk-triage" / "SKILL.md").write_text("---\nname: disk-triage\n---\nbody\n")
    (home / "memories").mkdir()
    (home / "memories" / "MEMORY.md").write_text("likes terse answers\n")
    (home / ".env").write_text("OPENROUTER_API_KEY=sk-not-real\n")
    (home / "auth.json").write_text('{"providers": {}}\n')
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "some/model", "provider": "openrouter"},
        "approvals": {"mode": "off"},
        "delegation": {"default_tier": "fast", "tiers": {"fast": {"model": "x/y"}}},
        "auxiliary": {"vision": {"model": "v/m"}, "background_review": {"enabled": True}},
        "curator": {"enabled": True, "interval_hours": 12},
    }))
    # A decoy: the real DB already holds a session with the id the replay
    # driver will use, scoring as DIRECT where the rep's own run is PARALLEL.
    real_db = SessionDB(db_path=home / "state.db")
    Recorder(real_db, "replay-0").user("25 best indie horror games, ranked")
    real_db.close()
    return home


def _tree(home: Path) -> dict:
    """Every file under home -> (bytes, mtime_ns); detects any write at all."""
    return {
        str(p.relative_to(home)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(home.rglob("*")) if p.is_file()
    }


class _CopyDriver(_ReplayDriver):
    """Replay driver bound to ONE rep's home; the foreground 'learns' by
    patching a skill in that home, as skill_manage would."""

    def __init__(self, home: Path, script, touch_skill: bool = False):
        from clover_state import SessionDB

        super().__init__(SessionDB(db_path=home / "state.db"), script)
        self.home, self.touch_skill = home, touch_skill

    def session(self, *, model, provider, cwd):
        if self.touch_skill:
            skill = self.home / "skills" / "research" / "product-comparison-research" / "SKILL.md"
            skill.write_text(skill.read_text() + "\nstep 6 added mid-eval\n")
        return super().session(model=model, provider=provider, cwd=cwd)


HORROR = dict(id="horror", prompt="25 best indie horror games, ranked",
              expected_mode="parallel", live_evidence=True, dispatch_within_calls=2)


def _run_isolated(tmp_path, profile, *, touch_skill=False, cases=None, evidence_reps=2):
    baseline = dm.FrozenBaseline.freeze(profile, tmp_path / "out" / "baseline")
    homes: list[Path] = []

    def driver_factory(home):
        homes.append(home)
        return _CopyDriver(home, _script, touch_skill=touch_skill)

    def reader_factory(home):
        return lambda sid: dm.read_transcript(home / "state.db", sid)

    now = {"t": 0.0}
    runs = dm.run_eval(
        cases or [_case(**HORROR)], None, reader=None,
        rep_env=dm.make_rep_env(baseline, driver_factory, reader_factory),
        root_model="fake-root", root_provider=None, reps=1, evidence_reps=evidence_reps,
        out_dir=tmp_path / "out", turn_timeout=10, settle_timeout=60,
        quiet_seconds=1, poll_seconds=1,
        clock=lambda: now["t"], sleep=lambda dt: now.__setitem__("t", now["t"] + dt),
    )
    return baseline, runs, homes


def test_every_rep_gets_its_own_copy_made_from_one_frozen_baseline(tmp_path):
    profile = _make_profile(tmp_path)
    baseline, runs, homes = _run_isolated(tmp_path, profile, touch_skill=True, evidence_reps=3)

    assert len(runs) == 3 and all(r["error"] is None and r["settled"] for r in runs)
    assert len(homes) == 3 and len(set(homes)) == 3
    assert profile not in homes and all(profile not in h.parents for h in homes)
    assert [r["home"] for r in runs] == [str(h) for h in homes]
    skill = Path("skills/research/product-comparison-research/SKILL.md")
    for h in homes:
        assert (h / "skills" / "research" / "product-comparison-research" / "references" / "games.md").exists()
        assert (h / "memories" / "MEMORY.md").read_text() == "likes terse answers\n"
        assert (h / ".env").exists() and (h / "auth.json").exists()
    # each rep's foreground patched ITS copy once; no rep saw another rep's
    # patch, because every copy comes from the baseline, not from the last rep
    for h in homes:
        assert (h / skill).read_text().count("step 6 added mid-eval") == 1
    assert "step 6" not in (baseline.root / skill).read_text()


def test_the_copy_has_no_state_db_so_each_rep_gets_a_fresh_session_db(tmp_path):
    profile = _make_profile(tmp_path)
    baseline = dm.FrozenBaseline.freeze(profile, tmp_path / "out" / "baseline")
    assert (profile / "state.db").exists()
    assert not (baseline.root / "state.db").exists()
    copy = baseline.make_home("horror-r0")
    assert not (copy / "state.db").exists()


def test_background_review_and_curator_are_off_in_the_copy_everything_else_kept(tmp_path):
    import yaml

    profile = _make_profile(tmp_path)
    before = yaml.safe_load((profile / "config.yaml").read_text())
    _, runs, homes = _run_isolated(tmp_path, profile)

    for h in homes:
        cfg = yaml.safe_load((h / "config.yaml").read_text())
        assert cfg["auxiliary"]["background_review"]["enabled"] is False
        assert cfg["curator"]["enabled"] is False
        # keep everything else: model, delegation tiers, approvals, siblings
        assert cfg["model"] == before["model"]
        assert cfg["approvals"] == before["approvals"]
        assert cfg["delegation"] == before["delegation"]
        assert cfg["auxiliary"]["vision"] == before["auxiliary"]["vision"]
        assert cfg["curator"]["interval_hours"] == 12
    # and the real profile's config still has them on
    after = yaml.safe_load((profile / "config.yaml").read_text())
    assert after == before
    assert after["auxiliary"]["background_review"]["enabled"] is True
    assert after["curator"]["enabled"] is True


@pytest.mark.parametrize("raw", [
    None,                                   # no config.yaml at all
    "model: {default: m}\n",                # no auxiliary / curator sections
    "auxiliary: null\ncurator: 5\n",        # wrong shapes
])
def test_copy_disables_review_and_curator_whatever_the_config_looked_like(tmp_path, raw):
    import yaml

    profile = tmp_path / "bare"
    profile.mkdir()
    if raw is not None:
        (profile / "config.yaml").write_text(raw)
    baseline = dm.FrozenBaseline.freeze(profile, tmp_path / "baseline")
    cfg = yaml.safe_load((baseline.make_home("c-r0") / "config.yaml").read_text())
    assert cfg["auxiliary"]["background_review"]["enabled"] is False
    assert cfg["curator"]["enabled"] is False


def test_unreadable_config_is_a_harness_error_not_a_silent_review_on_run(tmp_path):
    profile = tmp_path / "broken"
    profile.mkdir()
    (profile / "config.yaml").write_text("auxiliary: [unclosed\n")
    baseline = dm.FrozenBaseline.freeze(profile, tmp_path / "baseline")
    with pytest.raises(ValueError, match="config.yaml"):
        baseline.make_home("c-r0")


def test_the_real_profile_is_never_written(tmp_path):
    profile = _make_profile(tmp_path)
    before = _tree(profile)
    _run_isolated(tmp_path, profile, touch_skill=True, evidence_reps=2)
    assert _tree(profile) == before


def test_scoring_reads_the_copys_session_db_not_the_real_one(tmp_path):
    from clover_state import SessionDB

    profile = _make_profile(tmp_path)
    # the decoy in the real DB is a DIRECT-looking, never-dispatching session
    real = SessionDB(db_path=profile / "state.db")
    assert [m["role"] for m in real.get_messages("replay-0")] == ["user"]
    real.close()

    _, runs, homes = _run_isolated(tmp_path, profile)
    assert all(r["session_id"] == "replay-0" for r in runs)
    # parallel + correct could only come from the copy's own transcript
    assert all(r["score"]["mode"] == "parallel" and r["correct"] for r in runs)
    for h in homes:
        copy_db = SessionDB(db_path=h / "state.db")
        assert len(copy_db.get_messages("replay-0")) > 1
        copy_db.close()


def test_baseline_records_a_sha256_for_every_skill_md(tmp_path):
    import hashlib

    profile = _make_profile(tmp_path)
    baseline = dm.FrozenBaseline.freeze(profile, tmp_path / "baseline")
    rel = "skills/research/product-comparison-research/SKILL.md"
    assert set(baseline.skill_sha256) == {rel, "skills/devops/disk-triage/SKILL.md"}
    assert baseline.skill_sha256[rel] == hashlib.sha256((profile / rel).read_bytes()).hexdigest()
    # frozen: editing the live profile afterwards changes neither the hashes
    # nor the copies
    (profile / rel).write_text("edited after the freeze\n")
    assert baseline.skill_sha256[rel] != hashlib.sha256((profile / rel).read_bytes()).hexdigest()
    assert "edited" not in (baseline.make_home("x-r0") / rel).read_text()


def test_main_runs_every_rep_against_a_copy_and_records_the_skill_hashes(
        tmp_path, monkeypatch, capsys):
    profile = _make_profile(tmp_path)
    monkeypatch.setattr(dm, "resolve_profile_home", lambda name: profile)
    seen: list[Path] = []

    def driver_factory(home, args):
        seen.append(home)
        return _CopyDriver(home, _script)

    def reader_factory(home):
        return lambda sid: dm.read_transcript(home / "state.db", sid)

    out = tmp_path / "o"
    rc = dm.main(
        ["--profile", "dev", "--root-model", "m", "--out", str(out),
         "--cases", str(CASES_PATH), "--only", "disk-usage", "--reps", "2",
         "--quiet-seconds", "0", "--poll-seconds", "0"],
        driver_factory=driver_factory, reader_factory=reader_factory,
    )
    capsys.readouterr()
    assert rc in (0, 1)  # the pass bar is not the point here, isolation is
    assert len(seen) == 2 and len(set(seen)) == 2 and profile not in seen
    data = json.loads((out / "results.json").read_text())
    assert data["meta"]["skill_sha256"] == {
        "skills/research/product-comparison-research/SKILL.md": __import__("hashlib").sha256(
            (profile / "skills/research/product-comparison-research/SKILL.md").read_bytes()
        ).hexdigest(),
        "skills/devops/disk-triage/SKILL.md": __import__("hashlib").sha256(
            (profile / "skills/devops/disk-triage/SKILL.md").read_bytes()
        ).hexdigest(),
    }
    assert [r["home"] for r in data["runs"]] == [str(h) for h in seen]
    assert data["meta"]["isolation"]["background_review"] is False
    assert data["meta"]["isolation"]["curator"] is False


def test_baseline_refuses_a_directory_that_would_overwrite_the_profile(tmp_path):
    profile = _make_profile(tmp_path)
    before = _tree(profile)
    for bad in (profile, profile / "skills", tmp_path):
        with pytest.raises(ValueError, match="overlaps"):
            dm.FrozenBaseline.freeze(profile, bad)
    assert _tree(profile) == before


# ---------------------------------------------------------------------------
# Per-case fixtures: the files a prompt names must exist under {workdir}
# ---------------------------------------------------------------------------

FIXTURES = REPO_ROOT / "tests" / "eval" / "fixtures"


def _pytest_in(directory: Path, *args: str):
    import os
    import subprocess

    # A bare environment: the fixture suite must not inherit this run's pytest
    # plugins, addopts or credentials, only what is needed to find python.
    keep = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONNOUSERSITE")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=directory, env=env, capture_output=True, text=True, timeout=120,
    )
    return done.returncode, done.stdout + done.stderr


class _CwdRecordingDriver(_ReplayDriver):
    """Records, at session start (before any prompt), what the rep's cwd holds."""

    def __init__(self, db, script, probe):
        super().__init__(db, script)
        self.probe, self.cwds, self.seen = probe, [], []

    def session(self, *, model, provider, cwd):
        self.cwds.append(Path(cwd))
        self.seen.append(self.probe(Path(cwd)))
        return super().session(model=model, provider=provider, cwd=cwd)


def _run_cases(db, tmp_path, cases, probe, *, reps=1):
    driver = _CwdRecordingDriver(db, _script, probe)
    now = {"t": 0.0}
    said: list[str] = []
    runs = dm.run_eval(
        cases, driver, reader=lambda sid: db.get_messages(sid),
        root_model="fake-root", root_provider=None, reps=reps, evidence_reps=reps,
        out_dir=tmp_path / "out", turn_timeout=10, settle_timeout=60,
        quiet_seconds=1, poll_seconds=1,
        clock=lambda: now["t"], sleep=lambda dt: now.__setitem__("t", now["t"] + dt),
        progress=said.append,
    )
    return driver, runs, said


def test_every_case_that_names_files_has_a_fixture_or_generator():
    cases = dm.load_cases(CASES_PATH)
    by_id = {c["id"]: c for c in cases}
    for case_id in ("fix-three-bugs", "feature-flag-review"):
        assert "{workdir}/shop" in by_id[case_id]["prompt"]
        assert (FIXTURES / case_id / "shop").is_dir()
    assert "{workdir}/recordings/conference-day1.mov" in by_id["video-transcode"]["prompt"]


def test_fixtures_are_copied_into_each_reps_workdir_before_the_prompt(db, tmp_path):
    cases = [
        _case(id="fix-three-bugs", prompt="fix {workdir}/shop", expected_mode="parallel"),
        _case(id="disk", prompt="What is my disk usage by folder?", expected_mode="direct"),
    ]
    probe = lambda cwd: sorted(p.name for p in cwd.iterdir())  # noqa: E731
    driver, runs, _ = _run_cases(db, tmp_path, cases, probe, reps=2)

    assert all(r["error"] is None for r in runs)
    bugs = [c for c in driver.cwds if c.name.startswith("fix-three-bugs")]
    assert len(bugs) == 2 and len(set(bugs)) == 2
    for cwd in bugs:
        assert (cwd / "shop" / "cart" / "pricing.py").is_file()
    assert [s for c, s in zip(driver.cwds, driver.seen) if c in bugs] == [["shop"], ["shop"]]
    assert [s for c, s in zip(driver.cwds, driver.seen) if c not in bugs] == [[], []]

    # copies, not links: a rep editing its workdir never reaches the fixture or another rep
    original = (FIXTURES / "fix-three-bugs" / "shop" / "cart" / "pricing.py").read_text()
    (bugs[0] / "shop" / "cart" / "pricing.py").write_text("# edited\n")
    assert (bugs[1] / "shop" / "cart" / "pricing.py").read_text() == original
    assert (FIXTURES / "fix-three-bugs" / "shop" / "cart" / "pricing.py").read_text() == original


def test_shop_with_the_three_bugs_fails_in_each_area(tmp_path):
    dm.seed_case_fixtures("fix-three-bugs", tmp_path)
    shop = tmp_path / "shop"
    # exit 1 is "tests ran and failed"; anything else would be a broken run, not a red suite
    rc, out = _pytest_in(shop)
    assert rc == 1, out
    for area in ("pricing", "export", "throttle"):
        rc, out = _pytest_in(shop, f"tests/test_{area}.py")
        assert rc == 1, (area, out)


def test_feature_flag_shop_is_the_fixed_shop_plus_a_config(tmp_path):
    dm.seed_case_fixtures("feature-flag-review", tmp_path)
    shop = tmp_path / "shop"
    rc, out = _pytest_in(shop)
    assert rc == 0, out
    assert (shop / "config.py").is_file()

    buggy, fixed = FIXTURES / "fix-three-bugs" / "shop", FIXTURES / "feature-flag-review" / "shop"
    rel = lambda root: {p.relative_to(root) for p in root.rglob("*") if p.is_file()}  # noqa: E731
    assert rel(fixed) - rel(buggy) == {Path("config.py")}
    assert rel(buggy) <= rel(fixed)
    changed = {
        p for p in rel(buggy)
        if (buggy / p).read_bytes() != (fixed / p).read_bytes()
    }
    assert changed == {Path("cart/pricing.py"), Path("reports/export.py"), Path("auth/throttle.py")}


def test_video_fixture_is_skipped_cleanly_when_ffmpeg_is_absent(db, tmp_path, monkeypatch):
    monkeypatch.setattr(dm.shutil, "which", lambda name, *a, **k: None)
    cases = [_case(id="video-transcode", prompt="transcode {workdir}/recordings/x.mov",
                   expected_mode="single")]
    driver, runs, said = _run_cases(db, tmp_path, cases, lambda cwd: sorted(cwd.iterdir()))

    assert runs[0]["error"] is None
    assert driver.seen == [[]]
    assert any("video-transcode" in m and "ffmpeg" in m for m in said)
    assert dm._make_video_fixture(tmp_path / "recordings" / "a.mov") is False
    assert not (tmp_path / "recordings").exists()


def test_failed_ffmpeg_leaves_no_partial_clip(tmp_path, monkeypatch):
    # an executable that rejects every argument stands in for a broken ffmpeg
    monkeypatch.setattr(dm.shutil, "which", lambda name, *a, **k: sys.executable)
    dest = tmp_path / "recordings" / "conference-day1.mov"
    assert dm._make_video_fixture(dest) is False
    assert not dest.exists()


def test_video_fixture_is_a_4k_clip_with_audio_when_ffmpeg_is_present(tmp_path):
    import shutil
    import subprocess

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe not installed")
    dm.seed_case_fixtures("video-transcode", tmp_path)
    clip = tmp_path / "recordings" / "conference-day1.mov"
    assert clip.is_file() and 0 < clip.stat().st_size < 50_000_000
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
         "-of", "json", str(clip)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    video = [s for s in streams if s["codec_type"] == "video"]
    assert [(v["width"], v["height"]) for v in video] == [(3840, 2160)]
    assert any(s["codec_type"] == "audio" for s in streams)
