"""Host consumers of the trusted human-origin receipt, through the real path.

Real Telegram adapter -> GatewayRunner -> AIAgent.run_conversation -> SessionDB
row + receipt -> workstream human consumer -> scoped notepad record -> the next
ordinary turn's current-turn sidecar.  Only the provider/network edge is
scripted (see ``Harness`` in test_trusted_human_origin_ingress).  Nothing here
installs a reader, plants a row, or calls a consumer helper directly.
"""

import sqlite3

import pytest

from agent import workstreams
from cron import notepad
from tests.gateway.test_trusted_human_origin_ingress import (  # noqa: F401
    Harness,
    network_guard,
)

SETTINGS = {
    "enabled": True,
    "recall": {"enabled": True},
    "suggestions": {"enabled": True},
}


def _scope(h, *, user_id="12345"):
    return workstreams.scope_for_surface(
        platform="telegram", chat_type="dm", chat_id="12345", user_id=user_id,
        project=str(h.project.resolve()),
    )


def _user_content(request):
    return [m for m in request["messages"] if m.get("role") == "user"][-1]["content"]


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=SETTINGS)
    yield h
    h.close()


def test_correction_from_real_human_turn_is_recalled_on_the_next_turn(enabled):
    enabled.inbound_human("Correction: the deployment target is staging-2", message_id=100)

    rows = notepad.list_workstreams(_scope(enabled), kinds=["correction"])
    assert len(rows) == 1
    record = rows[0]
    assert "staging-2" in record["summary"]
    assert record["approved"] and record["status"] == "open"
    assert record["source"].startswith("human:")
    assert record["session_id"] == enabled.session_id()

    # The receipt was consumed after the effect committed.
    first_row = enabled.user_rows()[0]
    assert enabled.db.get_human_origin(enabled.session_id(), first_row["id"]) is None
    with sqlite3.connect(enabled.db_path) as conn:
        assert conn.execute(
            "SELECT consumed_by FROM human_origin_receipts").fetchall() == [
                ("workstream_human_hooks",)]
    # Turn 1 request carried no sidecar of its own correction.
    assert "[ws:" not in _user_content(enabled.scripted.requests[0])

    enabled.inbound_human("Which deployment target is configured?", message_id=101)
    api_text = _user_content(enabled.scripted.requests[1])
    assert api_text.startswith("Which deployment target is configured?")
    assert "staging-2" in api_text
    assert f"[ws:{record['workstream_id']}@r1]" in api_text
    # Persisted api_content equals the serialized provider bytes.
    stored = enabled.user_rows()[1]
    assert stored["content"] == "Which deployment target is configured?"
    assert stored.get("api_content") == api_text
