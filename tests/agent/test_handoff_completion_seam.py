"""Normal root completion after an accepted handoff (Patch 3, part 1).

The root's turn ends through the ordinary conversation-loop exit once a
background dispatch has been accepted (or the allowance is spent and the model
keeps asking), instead of hoping the model stops. A real ``run_conversation``
runs against a scripted provider; ``todo``, ``write_file`` and ``delegate_task``
are the real tools and the real async registry holds the job. Only the child's
own conversation is a stand-in.
"""
from __future__ import annotations

import json
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tools.async_delegation as ad
from agent import delegation_checkpoint as dc
from tests.agent.test_delegation_checkpoint import _agent
from tests.tools.test_delegate_strict_background import harness  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _clean_registry():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _call(name, args, call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}", type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _tools(*calls):
    msg = SimpleNamespace(content="", tool_calls=list(calls))
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _text(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _declare(mode):
    return _call("todo", {"delegation": {"mode": mode, "reason": "Operational reason."}})


def _root(provider_script):
    agent = _agent(max_iterations=30)
    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = list(provider_script)
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._disable_streaming = True
    return agent


def _run(agent, text="Make the explainer and the video."):
    with (
        patch.object(agent, "_persist_session") as persist,
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(text)
    return result, persist


def _tool_rows(result, name_hint=None):
    return [m for m in result["messages"] if m.get("role") == "tool"]


def test_accepted_handoff_ends_turn_without_a_second_provider_call(harness, tmp_path):
    harness.hold = threading.Event()
    agent = _root([
        _tools(_declare("delegate")),
        _tools(_call("delegate_task", {"goal": "Build the explainer and verify every output."})),
        _text("SHOULD NOT BE REACHED"),
    ])
    deltas = []
    agent.stream_delta_callback = deltas.append
    try:
        result, persist = _run(agent)

        assert agent.client.chat.completions.create.call_count == 2
        assert result["turn_exit_reason"] == "delegation_handoff"
        dispatch = json.loads(_tool_rows(result)[-1]["content"])
        delegation_id = dispatch["delegation_id"]
        assert result["final_response"] != "SHOULD NOT BE REACHED"
        assert delegation_id in result["final_response"]
        roles = [m["role"] for m in result["messages"]]
        assert roles[-3:] == ["assistant", "tool", "assistant"], "role alternation intact"
        assert result["messages"][-1]["content"] == result["final_response"]
        assert result["final_response"] in [d for d in deltas if isinstance(d, str)]
        assert persist.called, "the normal turn-end persistence still ran"
        # The job was not cancelled by the root ending.
        row = ad.get_durable_delegation(delegation_id)
        assert row["state"] == "running"
        assert ad.active_count() == 1
        assert all(not child.interrupted for child in harness.built)
        assert not harness.hold.is_set()
    finally:
        harness.hold.set()


def test_persistence_failure_does_not_claim_a_handoff(harness):
    harness.hold = threading.Event()
    agent = _root([
        _tools(_declare("delegate")),
        _tools(_call("delegate_task", {"goal": "Build the explainer and verify every output."})),
        _text("SHOULD NOT BE REACHED"),
    ])
    real = agent._execute_tool_calls

    def execute_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        if dc.get_checkpoint(agent).exit_armed:
            agent._incremental_persistence_failed = True

    agent._execute_tool_calls = execute_then_fail
    try:
        result, _ = _run(agent)

        assert result["turn_exit_reason"] == "session_persistence_failed"
        assert "background job" not in result["final_response"], "no handoff text on a failed persist"
        assert agent.client.chat.completions.create.call_count == 2
    finally:
        harness.hold.set()


def _spend_the_allowance(tmp_path):
    return [
        _tools(_call("write_file", {"path": str(tmp_path / f"w{i}.txt"), "content": "x"}))
        for i in range(5)
    ]


def test_blocked_twice_ends_with_an_honest_blocker(tmp_path):
    agent = _root([
        _tools(_declare("direct")),
        *_spend_the_allowance(tmp_path),
        _tools(_call("write_file", {"path": str(tmp_path / "w6.txt"), "content": "x"})),
        _tools(_call("write_file", {"path": str(tmp_path / "w7.txt"), "content": "x"})),
        _text("SHOULD NOT BE REACHED"),
    ])

    result, persist = _run(agent)

    assert agent.client.chat.completions.create.call_count == 8
    assert result["turn_exit_reason"] == dc.FOREGROUND_EXHAUSTED
    assert "no background job was started" in result["final_response"]
    assert not (tmp_path / "w6.txt").exists() and not (tmp_path / "w7.txt").exists()
    assert persist.called
    assert result["messages"][-1]["role"] == "assistant"


def test_three_parallel_blocked_calls_in_one_message_do_not_trigger_the_exit(tmp_path):
    parallel = [
        _call("write_file", {"path": str(tmp_path / f"p{i}.txt"), "content": "x"}, f"p{i}")
        for i in range(3)
    ]
    agent = _root([
        _tools(_declare("direct")),
        *_spend_the_allowance(tmp_path),
        _tools(*parallel),
        _text("Stopping here with an honest status."),
    ])

    result, _ = _run(agent)

    assert agent.client.chat.completions.create.call_count == 8
    assert result["final_response"] == "Stopping here with an honest status."
    assert result["turn_exit_reason"].startswith("text_response")
    assert not any((tmp_path / f"p{i}.txt").exists() for i in range(3))
    blocked_rows = [m for m in _tool_rows(result) if m["tool_call_id"].startswith("p")]
    assert len(blocked_rows) == 3, "every blocked call still gets a canonical result row"


# ── the directive itself: consumed once, honest, ownership-aware ───────────

def _unit_root():
    return SimpleNamespace(
        valid_tool_names={"todo", "delegate_task"}, platform="cli",
        _delegate_depth=0, _subagent_id=None, session_id="s",
        _interrupt_requested=False, _active_children=[],
        _active_children_lock=None,
        _delegation_checkpoint=dc.DelegationCheckpoint(),
    )


def _blocked_write(root, call_id):
    verdict = dc.admit(root, "write_file", {}, call_id)
    if verdict.admission is not None:
        root._delegation_checkpoint.finish(verdict.admission)
    return verdict


def test_handoff_directive_is_consumed_once_and_names_the_job():
    root = _unit_root()
    cp = root._delegation_checkpoint
    cp.declare("delegate", "Long phase.")
    assert cp.ticket().accept_handoff(
        delegation_id="async-abc", goals=["Build the explainer."], subagent_ids=["sa-1"]
    )

    directive = dc.completion_directive(root)

    assert directive.reason == "delegation_handoff"
    assert "async-abc" in directive.text and "Build the explainer." in directive.text
    assert dc.completion_directive(root) is None, "consumed once"


def test_exhausted_text_names_an_unconsumed_handoff_and_never_denies_it():
    root = _unit_root()
    cp = root._delegation_checkpoint
    cp.declare("delegate", "Long phase.")
    cp.ticket().accept_handoff(delegation_id="async-abc", goals=["g"], subagent_ids=[])
    assert dc.completion_directive(root).reason == "delegation_handoff"

    assert _blocked_write(root, "a").blocked
    assert dc.completion_directive(root) is None  # first blocked message: model speaks
    assert _blocked_write(root, "b").blocked
    directive = dc.completion_directive(root)

    assert directive.reason == dc.FOREGROUND_EXHAUSTED
    assert "async-abc" in directive.text
    assert "no background job was started" not in directive.text


def test_exhausted_text_without_a_handoff_claims_nothing_about_what_remains():
    root = _unit_root()
    cp = root._delegation_checkpoint
    cp.declare("direct", "Small job.")
    for i in range(5):
        assert not _blocked_write(root, f"ok{i}").blocked
    assert _blocked_write(root, "a").blocked
    assert dc.completion_directive(root) is None
    assert _blocked_write(root, "b").blocked

    directive = dc.completion_directive(root)

    assert "no background job was started" in directive.text
    assert "remaining" not in directive.text.lower()


def test_parallel_blocked_calls_in_one_message_count_once():
    root = _unit_root()
    root._delegation_checkpoint.declare("direct", "Small job.")
    for i in range(5):
        _blocked_write(root, f"ok{i}")
    for call_id in ("a", "b", "c"):
        assert _blocked_write(root, call_id).blocked

    assert dc.completion_directive(root) is None
    assert root._delegation_checkpoint.turn_blocks == 1


def test_a_new_request_clears_an_unconsumed_handoff_exit():
    root = _unit_root()
    cp = root._delegation_checkpoint
    cp.declare("delegate", "Long phase.")
    cp.ticket().accept_handoff(delegation_id="async-abc", goals=["g"], subagent_ids=[])

    dc.begin_turn(root, None)

    assert dc.completion_directive(root) is None
