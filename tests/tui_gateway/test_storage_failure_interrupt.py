"""Storage failure wins terminal status even if Stop also interrupted the turn."""
import types

from tests.tui_gateway.test_failed_turn_retention import emits, turn_env, _session, _events
from tui_gateway import server


def test_interrupted_storage_failure_retains_error_instead_of_clearing_work(emits, turn_env):
    agent = types.SimpleNamespace(
        session_id="session-key",
        run_conversation=lambda *a, **k: {
            "final_response": "Action already ran. Session save failed; reconcile before retrying.",
            "error": "Session save failed; action already ran.",
            "failed": True, "interrupted": True, "partial": True,
            "failure_reason": "session_persistence_failed:locked",
        },
        clear_interrupt=lambda: None,
    )
    session = _session(agent=agent, running=True)
    server._start_inflight_turn(session, "approved action")
    server._append_inflight_delta(session, "Action already ran.")
    server._run_prompt_submit("rid", "sid", session, "approved action")
    payload = _events(emits, "message.complete")[0]
    assert payload["status"] == "error"
    assert payload["partial"] is True
    assert payload["error"] == "Session save failed; action already ran."
    snapshot = server._inflight_snapshot(session)
    assert snapshot is not None
    assert snapshot["status"] == "error"
    assert snapshot["assistant"] == "Action already ran."
    assert snapshot["user"] == "approved action"
    assert snapshot["error_surface"] == {
        "layer": "disk", "code": "session_persistence_failed:locked", "retryable": False,
    }
