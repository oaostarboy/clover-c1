"""Exact owner identity and cross-thread replay use the real notepad owner."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent import workstreams as ws
from cron import notepad as np
from tests.agent.test_workstream_lineage import incident  # noqa: F401


@pytest.mark.parametrize("other", [
    ("delegation", "Case", "parent"),
    ("delegation", "case ", "parent"),
    ("project_check", "case", "parent"),
    ("delegation", "case", "Parent"),
])
def test_literal_source_and_session_are_never_normalized(incident, other):
    h = incident
    original = ws.record_outcomes(h.scope, [ws.TypedOutcome(
        "Check artifact", "unverified", "delegation", "case", session_id="parent")], h.settings)[0]
    ws.record_outcomes(h.scope, [ws.TypedOutcome(
        "Check artifact", "verified", other[0], other[1], session_id=other[2])], h.settings)
    assert np.get_workstream(h.scope, original["workstream_id"]) == original


def test_concurrent_resolved_replay_creates_no_notes_or_revisions(incident):
    h = incident
    pending = ws.TypedOutcome("Check artifact", "unverified", "project_check", "check", session_id="parent")
    row = ws.record_outcomes(h.scope, [pending], h.settings)[0]
    verified = ws.TypedOutcome("Check artifact", "verified", "project_check", "check",
                               summary="Exact artifact verified", session_id="parent")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: ws.record_outcomes(h.scope, [verified], h.settings), range(16)))
    assert len(np.list_workstreams(h.scope)) == 1
    assert np.get_workstream(h.scope, row["workstream_id"])["status"] == "resolved"
    before = (np.list_workstreams(h.scope), np.list_evidence(h.scope, row["workstream_id"]),
              np.list_history(h.scope, row["workstream_id"]))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: ws.record_outcomes(h.scope, [verified, pending], h.settings), range(16)))
    assert before == (np.list_workstreams(h.scope), np.list_evidence(h.scope, row["workstream_id"]),
                      np.list_history(h.scope, row["workstream_id"]))
