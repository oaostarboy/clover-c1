"""A persistence error cannot invite a blind model/tool retry."""
import pytest

from agent.error_surface import LAYER_DISK, build_error_surface_from_result


@pytest.mark.parametrize("cause", ["locked", "compression", "turn_lease", "corrupt", "disk", "unknown"])
def test_persistence_failure_is_local_and_not_automatically_retryable(cause):
    reason = "session_persistence_failed:" + cause
    descriptor = build_error_surface_from_result({
        "error": "Final save not confirmed; external action may already have run.",
        "failed": True, "failure_reason": reason, "failure_retryable": True,
    })
    assert descriptor == {"layer": LAYER_DISK, "code": reason, "retryable": False}


def test_recovered_but_stopped_turn_cannot_offer_blind_retry():
    descriptor = build_error_surface_from_result({
        "error": "Transcript saved after contention; turn already stopped.",
        "failed": True, "failure_reason": "session_persistence_recovered",
    })
    assert descriptor == {"layer": LAYER_DISK, "code": "session_persistence_recovered", "retryable": False}
