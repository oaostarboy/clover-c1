"""W4 read-only regressions against S0, not fixes or a release approval.

These synthetic attacks exercise real SessionDB and the existing host seam.
Known failures remain visible so the owning builder can add a fix under TDD.
"""
import sqlite3

import pytest
import yaml

from tests.agent.test_presence_shadow import host, prepare, present  # noqa: F401
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


@pytest.mark.parametrize("field", ["valence", "arousal", "confidence"])
def test_oversized_synthetic_number_uses_neutral_fallback_without_crashing(host, field):
    from agent import presence_shadow as presence
    turn = prepare(host, "Carry thought: deployment packaging verification")
    sample = {"valence": -0.8, "arousal": 0.9, "confidence": 0.9}
    sample[field] = 10 ** 400  # Valid structured integer, outside finite float range.
    assert presence.admit_affect(host.agent, turn, sample, now=host.clock[0])
    assert getattr(host.db._presence_pending.reading, field) == 0


def test_source_owner_delete_before_assessment_rejects_stale_synthetic_reading(host):
    from agent import presence_shadow as presence
    turn = prepare(host, "Carry thought: deployment packaging verification")
    with sqlite3.connect(host.home / "state.db") as conn:
        conn.execute("DELETE FROM messages WHERE id=?", (turn.row_id,))
    assert not presence.admit_affect(host.agent, turn, {
        "valence": -1, "arousal": 1, "confidence": 1}, now=host.clock[0])


def test_revocation_during_source_read_prevents_offer_commit(host, monkeypatch):
    prepare(host, "Carry thought: deployment packaging verification")
    prepare(host, "deployment packaging verification")
    original = host.db.get_messages_around

    def read_then_revoke(*args, **kwargs):
        row = original(*args, **kwargs)
        host.config["presence"]["enabled"] = False
        (host.home / "config.yaml").write_text(yaml.safe_dump(host.config), encoding="utf-8")
        return row

    monkeypatch.setattr(host.db, "get_messages_around", read_then_revoke)
    proposal = present(host)
    assert proposal is None or proposal.action == "hold"
    assert host.db._presence_pending is None or not host.db._presence_pending.offered_event_id
