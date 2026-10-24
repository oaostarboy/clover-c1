"""Reserved activity uses the live reducer, with no visible pre-acceptance card."""
from __future__ import annotations

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl

pytestmark = pytest.mark.unified_card


@pytest.fixture(autouse=True)
def isolated_cards(monkeypatch):
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    monkeypatch.setattr(ac.time, "monotonic", lambda: 100.0)
    yield
    ac.reset_for_tests()


def accept(did, count):
    part = ac.open_part(did, "🔄 **Public task**", ("authorized first", "authorized second"),
                        expected=count, goals=[f"public goal {i}" for i in range(count)],
                        child_steps=[("authorized first", "authorized second")] * count)
    assert part is not None
    return part.checklist


def observed(cl):
    return [{name: (list(getattr(obj, name)) if name in ("open", "recent")
                    else getattr(obj, name))
             for name in ("model", "calls", "hidden", "hidden_ids", "open", "recent",
                          "doing", "started", "state", "steps", "step_ids")}
            for obj in cl.kids or [cl]]


def send(did, index, event="subagent.tool", **kw):
    return ac.on_child_event(event, delegation_id=did, task_index=index, **kw)


@pytest.mark.parametrize("count", [1, 2])
def test_long_history_is_identical_to_live_reducer_and_moves_containers(count):
    histories = {}
    for early in (False, True):
        did = f"history-{early}"
        assert ac.reserve(did, expected=count)
        cl = None if early else accept(did, count)
        for index in range(count):
            send(did, index, "subagent.start", model="claude-sonnet-5-5")
            # More than a former FIFO's 256 entries, followed by old-ID repeats
            # and regressing cumulative counts. Each hidden ID counts once.
            for n in range(300):
                send(did, index, tool_name="todo" if n % 2 else "memory",
                     tool_call_id=f"hidden-{n}", tool_count=n + 1)
            send(did, index, tool_name="todo", tool_call_id="hidden-0", tool_count=1)
            for n in range(340):
                name = "terminal" if n % 2 else "read_file"
                send(did, index, tool_name=name, tool_call_id=f"public-{n}",
                     args={"command": f"printf public-{n}", "path": f"public-{n}.txt"},
                     tool_count=301 + n, model="gpt-6-sol")
                if n % 4:
                    # Exercise exact-ID matching, name fallback and unmatched
                    # completion fallback against the existing open-call oracle.
                    send(did, index, "subagent.tool_done", tool_name=name,
                         tool_call_id=f"public-{n}" if n % 3 else "unmatched",
                         is_error=n % 7 == 0, tool_count=301 + n)
            send(did, index, tool_name="todo", tool_call_id="hidden-0", tool_count=5)
        if early:
            assert did not in ac._PARTS and not ac._CARDS
            staged = ac._RESERVED_ACTIVITY[did]
            containers = [(obj.hidden_ids, obj.open, obj.recent) for obj in staged.kids or [staged]]
            cl = accept(did, count)
            assert did not in ac._RESERVED_ACTIVITY
            for obj, (ids, opened, recent) in zip(cl.kids or [cl], containers):
                assert obj.hidden_ids is ids and obj.open is opened and obj.recent is recent
        assert cl is not None
        for obj in cl.kids or [cl]:
            assert obj.hidden == len(obj.hidden_ids) == 300
            assert obj.calls == 340
            assert len(obj.open) <= ac._MAX_OPEN_TOOLS
            assert len(obj.recent) <= ac.RECENT_TOOLS_SINGLE
            assert obj.model == "Sonnet 5.5"
        for index in range(count):
            # Continue the SAME history after handover, including an old hidden
            # ID and a matched/unmatched completion of an early open call.
            send(did, index, tool_name="todo", tool_call_id="hidden-0", tool_count=640)
            send(did, index, "subagent.tool_done", tool_name="read_file", tool_call_id="public-336")
            send(did, index, tool_name="terminal", tool_call_id="after-accept", tool_count=641)
        for obj in cl.kids or [cl]:
            assert obj.hidden == 300 and obj.calls == 341
        histories[early] = observed(cl)
        # Reset registries between oracle legs, keeping the detached snapshot.
        ac.reset_for_tests()
        ac.set_style_for_tests("unified")
    assert histories[True] == histories[False]


@pytest.mark.parametrize("count", [1, 2])
def test_completion_before_first_activity_fences_until_and_after_acceptance(count):
    ac.reserve("fenced", expected=count)
    send("fenced", 0, "subagent.todo", todos=[{"id": "s1", "status": "completed"}])
    send("fenced", 0, "subagent.complete", status="completed", outcome="done")
    send("fenced", 0, "subagent.start", model="gpt-6-sol")
    send("fenced", 0, tool_name="terminal", tool_count=90)
    send("fenced", 0, "subagent.todo", todos=[{"id": "s1", "status": "pending"}])
    cl = accept("fenced", count)
    obj = cl.kids[0] if cl.kids else cl
    assert obj.state == ac.DONE
    assert obj.calls == 0 and obj.model == "" and obj.started is None
    assert obj.steps[0][1] == "completed"
    before = observed(cl)
    send("fenced", 0, tool_name="terminal", tool_count=100)
    assert observed(cl) == before
    if cl.kids:
        assert cl.state == ac.RUNNING and cl.kids[1].state == ac.PENDING


def test_unnamed_completion_keeps_its_original_child_when_later_sibling_starts():
    states = {}
    for early in (False, True):
        did = f"unnamed-{early}"
        ac.reserve(did, expected=2)
        cl = None if early else accept(did, 2)
        ac.on_child_event("subagent.complete", delegation_id=did, status="failed")
        send(did, 1, "subagent.start", model="gpt-6-sol")
        if early:
            cl = accept(did, 2)
        assert cl is not None
        states[early] = observed(cl)
    assert states[True] == states[False]
    assert [obj["state"] for obj in states[True]] == [ac.FAILED, ac.RUNNING]


def test_rejection_same_id_reuse_has_no_activity_or_completion_or_plan_leftovers():
    ac.reserve("reuse", checklists=[[{"id": "saved-private", "content": "saved plan", "status": "pending"}]])
    send("reuse", 0, tool_name="todo", tool_call_id="old-hidden", tool_count=1)
    send("reuse", 0, "subagent.complete", status="failed")
    ac.release("reuse")
    assert all("reuse" not in registry for registry in
               (ac._RESERVED, ac._RESERVED_ACTIVITY, ac._RESERVED_CHECKLISTS, ac._EARLY))
    ac.reserve("reuse")  # additive count argument preserves one-helper callers
    cl = accept("reuse", 1)
    assert cl.state == ac.RUNNING and cl.calls == cl.hidden == 0
    assert not cl.hidden_ids and not cl.open and not cl.recent
    assert cl.steps[0][0] == "authorized first" and "saved-private" not in cl.step_ids


@pytest.mark.parametrize("invalid", [-1, 2, 1000000, True, "0"])
def test_invalid_child_index_cannot_bind_a_fallback_identity(invalid):
    ac.reserve("identity", expected=2)
    send("identity", invalid, tool_name="terminal", subagent_id="untrusted", tool_count=90)
    send("identity", invalid, "subagent.complete", subagent_id="untrusted", status="failed")
    cl = accept("identity", 2)
    assert all(k.state == ac.PENDING and not k.sid and not k.calls for k in cl.kids)


def test_bound_subagent_identity_mismatch_cannot_mutate_or_fence_child():
    ac.reserve("identity", expected=2)
    ac.on_child_event("subagent.start", delegation_id="identity", subagent_id="known")
    send("identity", 0, tool_name="terminal", subagent_id="other", tool_count=90)
    send("identity", 0, "subagent.complete", subagent_id="other", status="failed")
    cl = accept("identity", 2)
    assert cl.kids[0].sid == "known" and cl.kids[0].state == ac.RUNNING
    assert cl.kids[0].calls == 0 and cl.kids[1].state == ac.PENDING


def test_unknown_activity_and_payload_child_count_cannot_authorize_children():
    send("unknown", 0, tool_name="terminal", tool_count=9, child_count=1000000)
    assert not ac._PARTS and not ac._RESERVED_ACTIVITY and not ac._CARDS
    ac.reserve("one")
    send("one", 999, tool_name="terminal", tool_count=9, child_count=1000000)
    cl = accept("one", 1)
    assert not cl.kids and cl.calls == 0


def test_authorized_snapshot_identities_win_over_activity_arguments():
    saved = [[{"id": "trusted-alpha", "content": "authorized alpha", "status": "in_progress"}],
             [{"id": "trusted-beta", "content": "authorized beta", "status": "pending"}]]
    ac.reserve("saved", checklists=saved)
    saved[0][0]["id"] = "mutated-after-reserve"
    send("saved", 0, tool_name="terminal", tool_count=1,
         args={"steps": ["invented"], "todos": [{"id": "forged", "content": "invented"}]})
    cl = accept("saved", 2)
    assert [k.step_ids for k in cl.kids] == [["trusted-alpha"], ["trusted-beta"]]
    assert [k.steps[0][0] for k in cl.kids] == ["authorized alpha", "authorized beta"]
    assert cl.kids[0].calls == 1


def test_reservation_global_cap_eviction_abandon_and_reset_cleanup(monkeypatch):
    monkeypatch.setattr(ac, "_MAX_PARTS", 3)
    for i in range(5):
        did = f"cap-{i}"
        ac.reserve(did, expected=2, checklists=[[], []])
        send(did, 0, tool_name="terminal", tool_count=1)
        send(did, 1, "subagent.complete", status="failed")
    for registry in (ac._RESERVED, ac._RESERVED_ACTIVITY, ac._RESERVED_CHECKLISTS, ac._EARLY):
        assert set(registry) == {"cap-2", "cap-3", "cap-4"}
    assert not ac._PARTS and not ac._CARDS
    ac.abandon("cap-2")
    assert all("cap-2" not in r for r in (ac._RESERVED, ac._RESERVED_ACTIVITY, ac._RESERVED_CHECKLISTS, ac._EARLY))
    ac.reset_for_tests()
    assert not any((ac._RESERVED, ac._RESERVED_ACTIVITY, ac._RESERVED_CHECKLISTS, ac._EARLY))


def test_foreground_reservation_forwards_trusted_goal_count():
    assert fl.reserve_ack_part("launch", {"work": "check", "outcome": "verified"}, ["a", "b"])
    assert len(ac._RESERVED_ACTIVITY["launch"].kids) == 2


@pytest.mark.parametrize("index", [1, None])
def test_count_free_sibling_completion_does_not_fence_provisional_activity(index):
    ac.reserve("legacy")
    identity = {"task_index": index}
    if index is None:
        identity["subagent_id"] = "unresolved-sibling"
    ac.on_child_event("subagent.todo", delegation_id="legacy", **identity,
                      todos=[{"id": "s1", "content": "Read sibling form", "status": "in_progress"}])
    ac.on_child_event("subagent.complete", delegation_id="legacy", **identity, status="failed")
    send("legacy", 0, tool_name="terminal", tool_count=1)
    staged = ac._RESERVED_ACTIVITY["legacy"]
    assert staged.state == ac.RUNNING and staged.calls == 1
    assert [event for event, _ in ac._EARLY["legacy"]] == ["subagent.todo", "subagent.complete"]
    assert ac._EARLY["legacy"][-1][1]["task_index"] == index
    assert not staged.kids  # early identity/count payloads never authorize children
    cl = accept("legacy", 2)
    if index == 1:
        assert cl.kids[0].state == ac.PENDING
        assert cl.kids[1].state == ac.FAILED
        assert cl.kids[1].steps[0][0] == "Read sibling form"


@pytest.mark.parametrize("reservation", [{"expected": 1}, {"checklists": [[]]}])
def test_explicit_single_child_still_rejects_sibling_early_events(reservation):
    ac.reserve("explicit", **reservation)
    send("explicit", 1, "subagent.todo", todos=[{"id": "s1", "status": "completed"}])
    send("explicit", 1, "subagent.complete", status="failed")
    assert not ac._EARLY.get("explicit")
    send("explicit", 0, tool_name="terminal", tool_count=1)
    cl = accept("explicit", 1)
    assert cl.state == ac.RUNNING and cl.calls == 1


@pytest.mark.parametrize("reservation", [{"expected": 1}, {"checklists": [[]]}])
def test_explicit_single_pending_activity_rejects_publisher_sibling_index(reservation):
    ac.reserve("pending-one", **reservation)
    staged = ac._RESERVED_ACTIVITY["pending-one"]
    before = observed(staged)
    for event in ("subagent.start", "subagent.tool", "subagent.tool_done"):
        send("pending-one", 1, event, subagent_id="outside-bound", model="claude-sonnet-5-5",
             tool_name="Read", tool_call_id="outside-call", tool_count=90,
             args={"file_path": "outside.txt"})
        assert observed(staged) == before
    assert "pending-one" not in ac._PARTS and not ac._CARDS
    cl = accept("pending-one", 1)
    assert cl.calls == 0 and cl.model == "" and cl.started is None
    send("pending-one", 0, "subagent.start", model="claude-sonnet-5-5")
    send("pending-one", 0, tool_name="Read", tool_call_id="native-call", tool_count=1)
    assert cl.model == "Sonnet 5.5" and cl.calls == 1 and len(cl.open) == 1


@pytest.mark.parametrize("identity", [{"task_index": 0}, {}])
def test_count_free_completion_before_activity_keeps_terminal_fence(identity):
    ac.reserve("legacy-fence")
    for status in ("pending", "in_progress"):
        ac.on_child_event("subagent.todo", delegation_id="legacy-fence", **identity,
                          todos=[{"id": "s1", "status": status}])
    assert len(ac._EARLY["legacy-fence"]) == 1
    ac.on_child_event("subagent.complete", delegation_id="legacy-fence", **identity, status="failed")
    send("legacy-fence", 1, "subagent.complete", status="completed", outcome="done")
    send("legacy-fence", 0, tool_name="terminal", tool_count=9)
    assert ac._RESERVED_ACTIVITY["legacy-fence"].calls == 0
    ac.on_child_event("subagent.todo", delegation_id="legacy-fence", **identity,
                      todos=[{"id": "s1", "status": "pending"}])
    assert len(ac._EARLY["legacy-fence"]) == 3
    assert ac._EARLY["legacy-fence"][0][1]["todos"][0]["status"] == "in_progress"
    assert ac._EARLY["legacy-fence"][1][1]["task_index"] == 0
    cl = accept("legacy-fence", 2)
    assert [kid.state for kid in cl.kids] == [ac.FAILED, ac.DONE]
    send("legacy-fence", 0, tool_name="terminal", tool_count=10)
    assert cl.kids[0].calls == cl.kids[1].calls == 0


def test_rejected_explicit_same_id_reuse_restores_unknown_count_replay():
    ac.reserve("reuse-legacy", expected=1)
    assert "reuse-legacy" in ac._RESERVED_EXPLICIT
    ac.release("reuse-legacy")
    assert "reuse-legacy" not in ac._RESERVED_EXPLICIT
    ac.reserve("reuse-legacy")
    send("reuse-legacy", 1, "subagent.todo",
         todos=[{"id": "s1", "content": "Read sibling form", "status": "in_progress"}])
    send("reuse-legacy", 1, "subagent.complete", status="failed")
    cl = accept("reuse-legacy", 2)
    assert cl.kids[1].steps[0][0] == "Read sibling form"
    assert cl.kids[1].state == ac.FAILED and cl.kids[0].state == ac.PENDING
    assert "reuse-legacy" not in ac._RESERVED_EXPLICIT
