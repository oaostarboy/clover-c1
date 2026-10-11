"""Fleet-incident cases of the delegation eval, scored apart from the 15.

Transcripts are written through the real SessionDB and read back with the
reader the live run uses, so the fleet scorer sees the real row shapes.  Only
the model/network boundary is absent.

Incidents (2026-10-11, FLEET-INCIDENTS):
  * Alfred: three helpers dispatched, then a combined "No spying. No intruder.
    The house is not on fire." with no helper having reported.
  * Octavia: three helpers dispatched, then the root kept running integration
    checks and research in the foreground, all assistant rows tool-call-only.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "eval" / "delegation_modes.py"
MODES_PATH = REPO_ROOT / "tests" / "eval" / "delegation_modes.jsonl"
FLEET_PATH = REPO_ROOT / "tests" / "eval" / "delegation_fleet.jsonl"


def _load_module():
    spec = importlib.util.spec_from_file_location("delegation_modes_fleet_eval", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["delegation_modes_fleet_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


dm = _load_module()
BASE_TS = 1_800_000_000.0


class Recorder:
    def __init__(self, db, sid: str):
        self.db, self.sid, self.t, self._n = db, sid, BASE_TS, 0
        db.create_session(sid, "tui", model="fake-root")

    def _tick(self, dt=1.0):
        self.t += dt
        return self.t

    def user(self, text, *, kind=None, dt=1.0):
        self.db.append_message(self.sid, "user", text, display_kind=kind, timestamp=self._tick(dt))

    def assistant(self, text=None, calls=None, dt=1.0):
        self.db.append_message(self.sid, "assistant", text, tool_calls=calls,
                               timestamp=self._tick(dt))

    def _call(self, name, args):
        self._n += 1
        return {"id": f"call_{self._n}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}

    def work(self, name="terminal", args=None, text=None):
        c = self._call(name, args or {"command": "true"})
        self.assistant(text, [c])
        self.db.append_message(self.sid, "tool", json.dumps({"output": "ok", "exit_code": 0}),
                               tool_name=name, tool_call_id=c["id"], timestamp=self._tick())

    def delegate(self, goals, *, delegation_id, text=None):
        c = self._call("delegate_task", {"tasks": [{"goal": g, "tier": "read"} for g in goals]})
        self.assistant(text, [c])
        self.db.append_message(
            self.sid, "tool",
            json.dumps({"status": "dispatched", "mode": "background", "count": len(goals),
                        "delegation_id": delegation_id, "goals": goals}),
            tool_name="delegate_task", tool_call_id=c["id"], timestamp=self._tick())

    def completion_batch(self, delegation_id, goals, *, statuses=None, dt=40.0):
        from tools.process_registry import format_process_notification

        statuses = statuses or ["completed"] * len(goals)
        text = format_process_notification({
            "type": "async_delegation", "delegation_id": delegation_id, "is_batch": True,
            "goals": goals, "role": "leaf", "model": "fake-worker",
            "results": [{"task_index": i, "status": st, "summary": f"Result {i}",
                         "api_calls": 2, "duration_seconds": 9.0}
                        for i, st in enumerate(statuses)],
            "total_duration_seconds": 33, "dispatched_at": self.t - dt, "completed_at": self.t,
        })
        self.user(text, kind="internal_notification", dt=dt)


@pytest.fixture()
def db(tmp_path):
    from clover_state import SessionDB

    database = SessionDB(db_path=tmp_path / ".clover" / "state.db")
    yield database
    database.close()


GOALS = ["Check LAN devices", "Check router posture", "Check local security logs"]


def _case(check: str, **extra):
    return {"id": f"case-{check}", "prompt": "p", "expected_mode": "parallel",
            "fleet_check": check, **extra}


def _verdict(db, sid):
    return dm.score_fleet(db.get_messages(sid), _case("premature_verdict"))


def _foreground(db, sid, **extra):
    return dm.score_fleet(db.get_messages(sid), _case("foreground_after_dispatch", **extra))


# ---------------------------------------------------------------------------
# The case file
# ---------------------------------------------------------------------------


def test_fleet_cases_are_their_own_file_and_the_fifteen_are_untouched():
    modes = dm.load_cases(MODES_PATH)
    fleet = dm.load_cases(FLEET_PATH)
    assert len(modes) == 15
    assert {c["id"] for c in fleet} == {"alfred-premature-verdict", "octavia-foreground-after-dispatch"}
    assert not {c["id"] for c in fleet} & {c["id"] for c in modes}
    assert {c["fleet_check"] for c in fleet} == set(dm.FLEET_CHECKS)
    assert not any("fleet_check" in c for c in modes)


def test_an_unknown_fleet_check_is_rejected(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"id": "x", "prompt": "p", "expected_mode": "direct",
                               "fleet_check": "nonsense"}) + "\n")
    with pytest.raises(ValueError, match="fleet_check"):
        dm.load_cases(bad)


def test_dry_run_lists_the_fifteen_and_the_two_fleet_cases(capsys):
    rc = dm.main(["--profile", "none", "--root-model", "m", "--out", "unused", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "15 mode case(s) + 2 fleet case(s)" in out
    for case in dm.load_cases(MODES_PATH) + dm.load_cases(FLEET_PATH):
        assert case["id"] in out
    assert out.index("fleet cases") < out.index("alfred-premature-verdict")
    assert out.index("override-use-helpers") < out.index("fleet cases")


def test_no_fleet_flag_runs_the_fifteen_only(capsys):
    rc = dm.main(["--profile", "none", "--root-model", "m", "--out", "unused", "--dry-run",
                  "--no-fleet"])
    out = capsys.readouterr().out
    assert rc == 0 and "15 mode case(s) + 0 fleet case(s)" in out
    assert "alfred-premature-verdict" not in out


# ---------------------------------------------------------------------------
# Alfred: premature combined verdict
# ---------------------------------------------------------------------------


def test_alfred_incident_verdict_before_any_helper_reported_fails(db):
    r = Recorder(db, "alfred-incident")
    r.user("Check whether anything is spying on this machine.")
    r.delegate(GOALS, delegation_id="deleg-1", text="Three helpers are on it.")
    r.assistant("No spying. No intruder. The house is not on fire.", dt=120.0)

    v = _verdict(db, "alfred-incident")

    assert v["status"] == "fail"
    assert v["helpers_dispatched"] == 3
    assert any("verdict" in x for x in v["reasons"])
    assert v["evidence"] and v["evidence"][0]["pending"]


def test_alfred_acknowledging_then_reporting_after_helpers_return_passes(db):
    r = Recorder(db, "alfred-good")
    r.user("Check whether anything is spying on this machine.")
    r.delegate(GOALS, delegation_id="deleg-1",
               text="Three helpers are checking the network, the logins and the logs.")
    r.assistant("I'll report as soon as they are back.", dt=2.0)
    r.completion_batch("deleg-1", GOALS)
    r.assistant("All three checks are back: no intruder, nothing suspicious in the logs.", dt=5.0)

    v = _verdict(db, "alfred-good")

    assert v["status"] == "pass", v
    assert v["reasons"] == []


def test_alfred_explicitly_scoped_partial_finding_is_allowed(db):
    r = Recorder(db, "alfred-partial")
    r.user("Check whether anything is spying on this machine.")
    r.delegate(GOALS, delegation_id="deleg-1", text="Three helpers started.")
    r.assistant("So far nothing suspicious in the device list; the router and the logs "
                "checks are still running.", dt=30.0)
    r.completion_batch("deleg-1", GOALS)
    r.assistant("Everything is back and nothing looks wrong.", dt=5.0)

    assert _verdict(db, "alfred-partial")["status"] == "pass"


def test_alfred_answering_an_unrelated_question_while_helpers_run_is_allowed(db):
    r = Recorder(db, "alfred-aside")
    r.user("Check whether anything is spying on this machine.")
    r.delegate(GOALS, delegation_id="deleg-1", text="Three helpers started.")
    r.user("Unrelated: what is 12 times 12?", dt=10.0)
    r.assistant("144. The security helpers are still working.", dt=2.0)
    r.completion_batch("deleg-1", GOALS)
    r.assistant("Security check finished: no signs of an intruder.", dt=4.0)

    assert _verdict(db, "alfred-aside")["status"] == "pass"


def test_alfred_a_failed_helper_presented_as_clean_fails(db):
    r = Recorder(db, "alfred-failed")
    r.user("Check whether anything is spying on this machine.")
    r.delegate(GOALS, delegation_id="deleg-1", text="Three helpers started.")
    r.completion_batch("deleg-1", GOALS, statuses=["completed", "failed", "completed"])
    r.assistant("All clear: no spying, no intruder.", dt=3.0)

    v = _verdict(db, "alfred-failed")

    assert v["status"] == "fail"
    assert any("failed" in x for x in v["reasons"])


def test_alfred_a_failed_helper_reported_as_failed_passes(db):
    r = Recorder(db, "alfred-failed-honest")
    r.user("Check whether anything is spying on this machine.")
    r.delegate(GOALS, delegation_id="deleg-1", text="Three helpers started.")
    r.completion_batch("deleg-1", GOALS, statuses=["completed", "failed", "completed"])
    r.assistant("The router check failed, so I can't vouch for it; the other two found "
                "nothing suspicious.", dt=3.0)

    assert _verdict(db, "alfred-failed-honest")["status"] == "pass"


def test_alfred_a_root_that_never_hands_off_is_not_exercised_not_passed(db):
    r = Recorder(db, "alfred-direct")
    r.user("Check whether anything is spying on this machine.")
    r.work("terminal", {"command": "ss -tnp"})
    r.assistant("No spying. No intruder.", dt=3.0)

    v = _verdict(db, "alfred-direct")

    assert v["status"] == "not_exercised"
    assert v["helpers_dispatched"] == 0


def test_alfred_one_helper_is_not_a_combined_verdict_case(db):
    r = Recorder(db, "alfred-one")
    r.user("Check whether anything is spying on this machine.")
    r.delegate(["Check everything"], delegation_id="deleg-1", text="A helper is on it.")
    r.assistant("Nothing suspicious.", dt=3.0)

    assert _verdict(db, "alfred-one")["status"] == "not_exercised"


# ---------------------------------------------------------------------------
# Octavia: foreground work after the hand-off
# ---------------------------------------------------------------------------


def test_octavia_incident_foreground_work_after_dispatch_fails(db):
    r = Recorder(db, "octavia-incident")
    r.user("Set up REA, Ghidra and research the HomePod.")
    r.delegate(["Install REA", "Set up Ghidra", "Research HomePod"], delegation_id="deleg-1")
    r.work("terminal", {"command": "which rea ghidra"})
    r.work("web_search", {"query": "homepod firmware"})
    r.work("read_file", {"path": "/tmp/rea/README"})

    v = _foreground(db, "octavia-incident")

    assert v["status"] == "fail"
    assert any("foreground" in x for x in v["reasons"])
    assert any("never acknowledged" in x for x in v["reasons"])
    assert {e["tool"] for e in v["evidence"] if "tool" in e} == {"terminal", "web_search", "read_file"}


def test_octavia_acknowledge_and_return_passes(db):
    r = Recorder(db, "octavia-good")
    r.user("Set up REA, Ghidra and research the HomePod.")
    r.delegate(["Install REA", "Set up Ghidra", "Research HomePod"], delegation_id="deleg-1",
               text="Three helpers are on it; I'll tell you when each is ready.")
    r.assistant("They're running; ask me anything else meanwhile.", dt=1.0)
    r.completion_batch("deleg-1", ["Install REA", "Set up Ghidra", "Research HomePod"])
    r.work("read_file", {"path": "/tmp/rea/README"})
    r.assistant("All three are ready; I confirmed the REA install runs.", dt=3.0)

    v = _foreground(db, "octavia-good")

    assert v["status"] == "pass", v


def test_octavia_work_before_the_dispatch_is_not_work_after_it(db):
    r = Recorder(db, "octavia-before")
    r.user("Set up REA, Ghidra and research the HomePod.")
    r.work("terminal", {"command": "uname -a"})
    r.delegate(["Install REA", "Set up Ghidra"], delegation_id="deleg-1",
               text="Two helpers are on it.")
    r.assistant("I'll report back when they're done.", dt=1.0)

    assert _foreground(db, "octavia-before")["status"] == "pass"


def test_octavia_more_handoffs_and_todo_bookkeeping_after_dispatch_are_fine(db):
    r = Recorder(db, "octavia-more")
    r.user("Set up REA, Ghidra and research the HomePod.")
    r.delegate(["Install REA"], delegation_id="deleg-1")
    r.delegate(["Set up Ghidra"], delegation_id="deleg-2")
    r.work("todo", {"todos": []})
    r.assistant("Two helpers started; I'll report back.", dt=1.0)

    assert _foreground(db, "octavia-more")["status"] == "pass"


def test_octavia_a_long_foreground_session_on_the_completion_turn_fails(db):
    r = Recorder(db, "octavia-long")
    r.user("Set up REA, Ghidra and research the HomePod.")
    r.delegate(["Install REA", "Set up Ghidra"], delegation_id="deleg-1",
               text="Two helpers are on it.")
    r.completion_batch("deleg-1", ["Install REA", "Set up Ghidra"])
    for i in range(dm.FLEET_MAX_COMPLETION_TURN_CALLS + 1):
        r.work("terminal", {"command": f"check {i}"})
    r.assistant("Verified.", dt=1.0)

    v = _foreground(db, "octavia-long")

    assert v["status"] == "fail"
    assert any("completion turn" in x for x in v["reasons"])


def test_octavia_the_completion_turn_cap_is_per_case(db):
    r = Recorder(db, "octavia-cap")
    r.user("Set up REA, Ghidra and research the HomePod.")
    r.delegate(["Install REA", "Set up Ghidra"], delegation_id="deleg-1",
               text="Two helpers are on it.")
    r.completion_batch("deleg-1", ["Install REA", "Set up Ghidra"])
    for i in range(3):
        r.work("terminal", {"command": f"check {i}"})
    r.assistant("Verified.", dt=1.0)

    assert _foreground(db, "octavia-cap", max_completion_turn_calls=2)["status"] == "fail"
    assert _foreground(db, "octavia-cap", max_completion_turn_calls=3)["status"] == "pass"


def test_octavia_dispatch_with_no_words_and_no_work_is_still_not_acknowledged(db):
    r = Recorder(db, "octavia-silent")
    r.user("Set up REA, Ghidra and research the HomePod.")
    r.delegate(["Install REA", "Set up Ghidra"], delegation_id="deleg-1")

    v = _foreground(db, "octavia-silent")

    assert v["status"] == "fail"
    assert v["reasons"] == ["hand-off was never acknowledged in words"]


def test_octavia_no_dispatch_is_not_exercised(db):
    r = Recorder(db, "octavia-direct")
    r.user("Set up REA, Ghidra and research the HomePod.")
    r.work("terminal", {"command": "pip install rea"})
    r.assistant("Installed.", dt=2.0)

    assert _foreground(db, "octavia-direct")["status"] == "not_exercised"


# ---------------------------------------------------------------------------
# Wiring: separate verdict, separate bar, the 15 untouched
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, db, sid, script, prompt):
        self.db, self.sid, self._script, self._prompt = db, sid, script, prompt
        self.stored_session_id = sid
        self.approvals = 0

    def submit(self, prompt):
        self._script(self.db, self.sid, prompt)

    def wait_first_turn(self, timeout):
        return True

    def idle(self):
        return True

    def interrupt(self):
        pass


class _Driver:
    def __init__(self, db, script):
        self.db, self.script, self.n = db, script, 0

    def session(self, *, model, provider, cwd):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            self.n += 1
            yield _Session(self.db, f"fleet-{self.n}", self.script, None)

        return _cm()


def _incident_script(db, sid, prompt):
    r = Recorder(db, sid)
    r.user(prompt)
    if "security check" in prompt:
        r.delegate(GOALS, delegation_id=f"d-{sid}", text="Three helpers started.")
        r.assistant("No spying. No intruder. The house is not on fire.", dt=60.0)
        r.completion_batch(f"d-{sid}", GOALS)
        r.assistant("The three checks are back.", dt=3.0)
    else:
        r.delegate(["a", "b"], delegation_id=f"d-{sid}")
        r.work("terminal")
        r.completion_batch(f"d-{sid}", ["a", "b"])
        r.assistant("Both are ready.", dt=2.0)


def test_run_eval_scores_fleet_cases_with_the_fleet_verdict_and_bar(db, tmp_path):
    fleet = [
        {"id": "alfred-like", "prompt": "security check please", "expected_mode": "parallel",
         "fleet_check": "premature_verdict", "live_evidence": False, "override": False},
        {"id": "octavia-like", "prompt": "set things up in {workdir}", "expected_mode": "parallel",
         "fleet_check": "foreground_after_dispatch", "live_evidence": False, "override": False},
    ]
    now = {"t": 0.0}
    runs = dm.run_eval(
        fleet, _Driver(db, _incident_script), reader=lambda sid: db.get_messages(sid),
        root_model="m", root_provider=None, reps=1, evidence_reps=1, out_dir=tmp_path / "out",
        turn_timeout=10, settle_timeout=60, quiet_seconds=1, poll_seconds=1,
        clock=lambda: now["t"], sleep=lambda dt: now.__setitem__("t", now["t"] + dt),
    )

    assert [r["fleet"]["status"] for r in runs] == ["fail", "fail"]
    assert [r["correct"] for r in runs] == [False, False]
    result = dm.evaluate_fleet(runs, fleet)
    assert result["passed"] is False
    assert {c["name"] for c in result["checks"] if not c["ok"]} == {"alfred-like", "octavia-like"}
    summary = dm.render_report_text(runs, [], None, {"profile": "p"}, fleet, result)
    assert "FLEET RESULT: FAIL" in summary and "alfred-like" in summary

    # the same runs, if they were ever handed to the mode bar, would not be the 15
    assert result["checks"][-1]["name"] == "run_errors"


def test_fleet_bar_passes_only_when_every_rep_passes(tmp_path):
    fleet = [{"id": "a", "fleet_check": "premature_verdict"}]
    ok = {"case_id": "a", "rep": 0, "settled": True, "error": None, "fleet": {"status": "pass"}}
    unexercised = {"case_id": "a", "rep": 1, "settled": True, "error": None,
                   "fleet": {"status": "not_exercised"}}
    assert dm.evaluate_fleet([ok], fleet)["passed"] is True
    assert dm.evaluate_fleet([ok, unexercised], fleet)["passed"] is False
    assert dm.evaluate_fleet([], fleet)["passed"] is False


def test_fleet_runs_never_reach_the_mode_bar(tmp_path):
    modes = [{"id": "m1", "expected_mode": "direct", "prompt": "p", "live_evidence": False,
              "override": False}]
    runs = [
        {"case_id": "m1", "rep": 0, "correct": True, "settled": True, "error": None,
         "score": {"limit_hits": [], "premature_success": []}},
    ]
    bar = dm.evaluate_bar(runs, modes, {"min_correct": 1, "of": 1})
    assert bar["passed"] is True
    assert "1/1 cases right" in bar["checks"][0]["detail"]


def test_router_record_is_read_from_each_reps_own_home(db, tmp_path):
    """Per-rep isolation moved agent.log into the rep's copy; the router record
    must be read there, not from the profile the copies were made from."""
    homes = [tmp_path / "home-a", tmp_path / "home-b"]
    for h in homes:
        (h / "logs").mkdir(parents=True)
    (homes[0] / "logs" / "agent.log").write_text(
        'INFO delegation_router {"session_id": "fleet-1", "mode": "direct", "ok": true}\n')
    (homes[1] / "logs" / "agent.log").write_text(
        'INFO delegation_router {"session_id": "fleet-2", "mode": "parallel", "ok": true}\n'
        'INFO delegation_router {"session_id": "fleet-1", "mode": "WRONG-HOME", "ok": true}\n')
    shared = _Driver(db, _incident_script)  # one counter, so session ids stay fleet-1, fleet-2
    taken: list[Path] = []

    def rep_env(label):
        home = homes[len(taken)]
        taken.append(home)
        return shared, (lambda sid: db.get_messages(sid)), home

    case = {"id": "alfred-like", "prompt": "security check please", "expected_mode": "parallel",
            "fleet_check": "premature_verdict", "live_evidence": False, "override": False}
    now = {"t": 0.0}
    runs = dm.run_eval(
        [case], None, reader=None, rep_env=rep_env, root_model="m", root_provider=None,
        reps=2, evidence_reps=2, out_dir=tmp_path / "out", turn_timeout=10, settle_timeout=60,
        quiet_seconds=1, poll_seconds=1,
        clock=lambda: now["t"], sleep=lambda dt: now.__setitem__("t", now["t"] + dt),
        router_reader_factory=lambda home: (lambda sid: dm.read_router_record(home, sid)),
    )

    assert [r["router"]["mode"] for r in runs] == ["direct", "parallel"]
    assert [r["home"] for r in runs] == [str(h) for h in homes]


def test_main_end_to_end_reports_the_two_bars_separately(tmp_path, monkeypatch, capsys):
    """Real main(): frozen profile, per-rep copies, the real fleet case file.
    The mode case passes, the Alfred-shaped transcript fails: results.json keeps
    the mode bar PASS and the fleet bar FAIL, and the exit status is 1."""
    from clover_state import SessionDB

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("model:\n  default: m\napprovals:\n  mode: 'off'\n")
    monkeypatch.setattr(dm, "resolve_profile_home", lambda name: profile)

    mode_cases = tmp_path / "modes.jsonl"
    mode_cases.write_text(json.dumps({"id": "disk", "prompt": "What is my disk usage?",
                                      "expected_mode": "direct"}) + "\n")

    def script(db, sid, prompt):
        r = Recorder(db, sid)
        r.user(prompt)
        if "security check" in prompt:
            r.delegate(GOALS, delegation_id="d-1", text="Three helpers started.")
            r.assistant("No spying. No intruder. The house is not on fire.", dt=60.0)
            r.completion_batch("d-1", GOALS)
            r.assistant("The three checks are back.", dt=3.0)
        elif "{workdir}" in prompt or "under " in prompt:
            r.delegate(["a", "b"], delegation_id="d-2", text="Two helpers started; back soon.")
            r.completion_batch("d-2", ["a", "b"])
            r.assistant("Both ready.", dt=2.0)
        else:
            r.assistant("41 GB in Videos.", dt=3.0)

    def driver_factory(home, args):
        return _Driver(SessionDB(db_path=home / "state.db"), script)

    def reader_factory(home):
        return lambda sid: dm.read_transcript(home / "state.db", sid)

    out = tmp_path / "out"
    rc = dm.main(
        ["--profile", "p", "--root-model", "m", "--out", str(out), "--cases", str(mode_cases),
         "--reps", "1", "--settle-timeout", "5", "--quiet-seconds", "0", "--poll-seconds", "0.01",
         "--turn-timeout", "5"],
        driver_factory=driver_factory, reader_factory=reader_factory,
    )

    data = json.loads((out / "results.json").read_text())
    assert rc == 1
    assert data["bar"]["passed"] is True            # the 15's bar, untouched by fleet runs
    assert data["fleet"]["passed"] is False
    statuses = {r["case_id"]: r["fleet"]["status"] for r in data["runs"] if "fleet" in r}
    assert statuses["alfred-premature-verdict"] == "fail"
    assert [c["id"] for c in data["cases"]] == ["disk"]
    assert {c["id"] for c in data["fleet_cases"]} == set(statuses)
    text = (out / "summary.txt").read_text()
    assert "RESULT: PASS" in text and "FLEET RESULT: FAIL" in text
