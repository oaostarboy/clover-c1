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
        _tools(_call("delegate_task", {
            "goal": "Build the explainer and verify every output.",
            "handoff": {
                "work": "making your video",
                "outcome": "a verified 30-second narrated MP4",
                "estimated_minutes_min": 5,
                "estimated_minutes_max": 10,
            },
        })),
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
        assert result["final_response"] == (
            "**delegated:** making your video.\n\n"
            "**goal:** a verified 30-second narrated MP4."
        )
        assert "Estimated time" not in result["final_response"]
        assert "async-" not in result["final_response"]
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


@pytest.mark.parametrize(
    ("low", "high"),
    [
        (5, 10),
        (1_000_000, 10_000_000),
        (1e-9, 2e-9),
        (float("inf"), 10),
        (float("nan"), 10),
        (True, 10),
        (10, 5),
        (5, 5),
        (2.5, 7.3333333),
        (1440, 1440),
        (1441, 1441),
    ],
)
def test_accepted_handoff_never_renders_eta_even_with_numeric_metadata(harness, low, high):
    harness.hold = threading.Event()
    agent = _root([
        _tools(_declare("delegate")),
        _tools(_call("delegate_task", {
            "goal": "Build the explainer and verify every output.",
            "handoff": {
                "work": "making your video",
                "outcome": "a verified 30-second narrated MP4",
                "estimated_minutes_min": low,
                "estimated_minutes_max": high,
            },
        })),
        _text("SHOULD NOT BE REACHED"),
    ])
    try:
        result, _ = _run(agent)
        assert result["turn_exit_reason"] == "delegation_handoff"
        assert "**delegated:** making your video." in result["final_response"]
        assert "**goal:** a verified 30-second narrated MP4." in result["final_response"]
        assert "Estimated time" not in result["final_response"]
        assert "e+" not in result["final_response"]
        assert agent.client.chat.completions.create.call_count == 2
    finally:
        harness.hold.set()


def test_accepted_batch_handoff_never_renders_eta_for_multiple_workers(harness):
    harness.hold = threading.Event()
    tasks = [
        {"goal": f"Review release {name}.", "title": f"Release {name}"}
        for name in ("A", "B", "C")
    ]
    agent = _root([
        _tools(_declare("delegate")),
        _tools(_call("delegate_task", {
            "tasks": tasks,
            "handoff": {
                "work": "reviewing three releases",
                "outcome": "three checked release reports",
                "estimated_minutes_min": 12.4,
                "estimated_minutes_max": 18.6,
            },
        })),
        _text("SHOULD NOT BE REACHED"),
    ])
    try:
        result, _ = _run(agent)
        assert result["final_response"].startswith(
            "**delegated:** Workers are reviewing three releases.\n\n"
        )
        assert result["final_response"] == (
            "**delegated:** Workers are reviewing three releases.\n\n"
            "**goal:** three checked release reports."
        )
        assert "Estimated time" not in result["final_response"]
        assert agent.client.chat.completions.create.call_count == 2
    finally:
        harness.hold.set()


def test_accepted_batch_handoff_uses_plural_copy_and_keeps_children_running(harness):
    harness.hold = threading.Event()
    tasks = [
        {"goal": f"Review release {name}.", "title": f"Release {name}"}
        for name in ("A", "B", "C")
    ]
    agent = _root([
        _tools(_declare("delegate")),
        _tools(_call("delegate_task", {
            "tasks": tasks,
            "handoff": {
                "work": "reviewing three releases",
                "outcome": "three checked release reports",
            },
        })),
        _text("SHOULD NOT BE REACHED"),
    ])
    try:
        result, _ = _run(agent)
        text = result["final_response"]
        assert text.startswith("**delegated:** Workers are reviewing three releases.\n\n")
        assert "**goal:** three checked release reports." in text
        assert "Estimated time" not in text
        assert "release reports" in text
        assert agent.client.chat.completions.create.call_count == 2
        dispatch = json.loads(_tool_rows(result)[-1]["content"])
        assert dispatch["count"] == 3
        assert ad.get_durable_delegation(dispatch["delegation_id"])["state"] == "running"
        assert all(not child.interrupted for child in harness.built)
    finally:
        harness.hold.set()


def test_legacy_schema_dispatch_uses_task_summary_without_extra_model_call(harness):
    harness.hold = threading.Event()
    agent = _root([
        _tools(_declare("delegate")),
        _tools(_call("delegate_task", {
            "goal": "Audit Nice & Tidy’s Google Ads for the Montréal market.",
        })),
        _text("SHOULD NOT BE REACHED"),
    ])
    try:
        result, _ = _run(agent)

        assert result["turn_exit_reason"] == "delegation_handoff"
        assert result["final_response"] == (
            "**delegated:** Audit Nice & Tidy’s Google Ads for the Montréal market.\n\n"
            "**goal:** a completed result for Audit Nice & Tidy’s Google Ads for the Montréal market, returned here."
        )
        assert "Estimated time" not in result["final_response"]
        assert agent.client.chat.completions.create.call_count == 2
        assert ad.active_count() == 1
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
    assert directive.text == (
        "**delegated:** Build the explainer.\n\n"
        "**goal:** a completed result for Build the explainer, returned here."
    )
    assert "async-abc" not in directive.text
    assert dc.completion_directive(root) is None, "consumed once"


def test_handoff_rejects_bad_public_text_and_unknowns_invalid_eta():
    root = _unit_root()
    cp = root._delegation_checkpoint
    cp.declare("delegate", "Long phase.")
    assert cp.ticket().accept_handoff(
        delegation_id="internal-job-token",
        goals=["first.", "second.", "third.", "fourth."],
        handoff={
            "work": "reviewing async_private-id",
            "outcome": "a report from /secret/files",
            "estimated_minutes_min": 0,
            "estimated_minutes_max": 9,
        },
    )
    text = dc.completion_directive(root).text
    assert text == (
        "**delegated:** task details are unavailable in this summary.\n\n"
        "**goal:** the workers' results will return to this conversation."
    )
    assert "Estimated time" not in text
    assert "internal-job-token" not in text
    assert "async_private-id" not in text
    assert "/secret/files" not in text
    assert "first." not in text and "fourth." not in text


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


# ── rollback: delegation.checkpoint.enabled=false is the whole feature ─────

def test_rollback_switch_leaves_a_background_dispatch_to_the_model(harness):
    """With the checkpoint disabled an accepted background dispatch must behave
    like baseline: the loop asks the provider again instead of ending the turn
    with the deterministic handoff text."""
    harness.hold = threading.Event()
    disabled = dc.CheckpointSettings(enabled=False)
    agent = _root([
        _tools(_call("delegate_task", {"goal": "Build the explainer and verify every output."})),
        _text("Carrying on in the normal way."),
    ])
    try:
        with patch.object(dc, "load_settings", return_value=disabled):
            result, _ = _run(agent)

        assert agent.client.chat.completions.create.call_count == 2
        assert result["final_response"] == "Carrying on in the normal way."
        assert result["turn_exit_reason"].startswith("text_response")
        checkpoint = dc.get_checkpoint(agent)
        assert checkpoint.phase == dc.PHASE_FOREGROUND
        # The dispatched job itself is untouched.
        assert ad.active_count() == 1
    finally:
        harness.hold.set()


def test_rollback_switch_grants_no_handoff_and_no_exit_directive():
    root = _unit_root()
    root._delegation_checkpoint = dc.DelegationCheckpoint(
        dc.CheckpointSettings(enabled=False)
    )
    ticket = dc.ticket_for(root)

    accepted = ticket is not None and ticket.accept_handoff(
        delegation_id="async-abc", goals=["g"], subagent_ids=[]
    )

    assert accepted is False
    assert dc.completion_directive(root) is None
    assert root._delegation_checkpoint.phase == dc.PHASE_FOREGROUND


# ── status text states only what is true about the owned job ───────────────

def _consumed_handoff_root(*, goals=("render the video",), receipts=None):
    root = _unit_root()
    cp = root._delegation_checkpoint
    cp.declare("delegate", "Long phase.")
    assert cp.ticket().accept_handoff(
        delegation_id="async-abc", goals=list(goals), subagent_ids=[]
    )
    assert dc.completion_directive(root).reason == "delegation_handoff"
    # The result is delivered and its receipt consumed by an internal turn.
    ready = receipts if receipts is not None else (lambda ids: tuple(ids))
    cp.begin_turn(
        preserve=True, settings=cp.settings, kind="internal_notification",
        receipt_probe=ready,
    )
    # A later internal turn of the same request (no further receipt).
    cp.begin_turn(
        preserve=True, settings=cp.settings, kind="internal_notification",
        receipt_probe=lambda ids: (),
    )
    return root


def test_block_text_does_not_claim_a_finished_job_is_still_running():
    root = _consumed_handoff_root()

    verdict = _blocked_write(root, "a")

    assert verdict.block_code == dc.HANDOFF_ACTIVE
    assert "already running" not in verdict.block_message
    assert "already delivered" in verdict.block_message
    assert "write_file" in verdict.block_message  # still says the call was not run


def test_exit_text_after_a_consumed_handoff_does_not_deny_the_handoff():
    root = _consumed_handoff_root()
    assert _blocked_write(root, "a").blocked
    assert dc.completion_directive(root) is None
    assert _blocked_write(root, "b").blocked

    directive = dc.completion_directive(root)

    assert directive.reason == dc.FOREGROUND_EXHAUSTED
    assert "no background job was started" not in directive.text
    assert "async-abc" in directive.text
    assert "already finished" in directive.text or "already delivered" in directive.text
    assert "still running" not in directive.text


def test_report_only_receipt_beyond_the_window_cap_is_not_called_running():
    root = _unit_root()
    cp = root._delegation_checkpoint
    cp.settings = dc.CheckpointSettings(max_integration_windows=1)
    cp.declare("delegate", "Fan out.")
    assert cp.ticket().accept_handoff(delegation_id="d", goals=["a", "b"], subagent_ids=[])
    dc.completion_directive(root)
    cp.begin_turn(preserve=True, settings=cp.settings, kind="internal_notification",
                  receipt_probe=lambda ids: ("d:child:0",))
    cp.begin_turn(preserve=True, settings=cp.settings, kind="internal_notification",
                  receipt_probe=lambda ids: ("d:child:1",))

    # child 1 was delivered (report-only) and child 0's window is gone: both
    # receipts are consumed, so nothing is running any more.
    verdict = _blocked_write(root, "a")

    assert "already running" not in verdict.block_message


def test_block_text_for_a_job_whose_result_has_not_returned_still_says_so():
    root = _unit_root()
    cp = root._delegation_checkpoint
    cp.declare("delegate", "Long phase.")
    assert cp.ticket().accept_handoff(delegation_id="async-abc", goals=["g"], subagent_ids=[])
    dc.completion_directive(root)
    cp.begin_turn(preserve=True, settings=cp.settings, kind="internal_notification",
                  receipt_probe=lambda ids: ())

    verdict = _blocked_write(root, "a")

    assert "has not yet returned" in verdict.block_message
    assert "already finished" not in verdict.block_message
