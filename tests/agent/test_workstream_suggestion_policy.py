"""Deterministic natural-prevalence evaluation, with real scoped SQLite.

No judge/provider is called. Labels are scored independently, including a
known missed paraphrase rather than a fixture consisting only of easy wins.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent import workstreams as ws
from agent import workstream_suggestions as surface
from cron import notepad as np
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401

NOW = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path / "profile"))
    scope = ws.make_scope(str(tmp_path / "project"), root="local")
    settings = ws.load_settings({"proactive": {
        "enabled": True, "mode": "active", "suggestions": {
            "enabled": True, "max_per_session": 1, "cooldown_seconds": 3600}}})
    row = np.create_workstream(scope, kind="obligation", title="packaging audit",
                               summary="package release audit", fingerprint="package",
                               approved=True, now=NOW)
    return scope, settings, row


def gate(policy, *, text="package release audit", query="package release audit",
         timing=True, session="one", now=NOW, judge=None):
    scope, settings, row = policy
    candidate = ws.Candidate(row["workstream_id"], text, ws.relevance(query, text))
    return surface.gate_candidate(candidate, scope, settings, session_id=session,
                                  timing=timing, now=now, judge_verdict=judge)


def test_controlled_natural_prevalence_scores_each_label_separately(policy):
    scope, settings, row = policy
    duplicate = ws.Candidate(row["workstream_id"], "review release packaging", 1)
    ws.record_offer(scope, row["workstream_id"], duplicate, settings,
                    session_id="previous", now=NOW - timedelta(days=1))
    cases = [
        ("useful_now", gate(policy)),
        ("useful_now", gate(policy, query="check package release before shipping")),
        *[("wrong_time", gate(policy, timing=False)) for _ in range(5)],
        *[("wrong_time", gate(policy, timing=None)) for _ in range(2)],
        *[("duplicate", surface.gate_candidate(
            duplicate, scope, settings, session_id="one", timing=True, now=NOW))
          for _ in range(4)],
        *[("no_help", gate(policy, query=q)) for q in (
            "weather tomorrow", "write poem", "database latency", "coffee beans",
            "music volume", "garden roses")],
        ("missed_need", gate(policy, query="is the distribution bundle ready")),
    ]
    scores = ws.score_gate(cases)
    assert scores == {
        "useful_now": {"n": 2, "correct": 2},
        "wrong_time": {"n": 7, "correct": 7},
        "duplicate": {"n": 4, "correct": 4},
        "no_help": {"n": 6, "correct": 6},
        "missed_need": {"n": 1, "correct": 0},
    }
    assert cases[-1][1].reason == "irrelevant"  # measured lexical limitation
    # Persist actual measurement in pytest output captured by the worker logs.
    print("NATURAL_PREVALENCE=" + __import__("json").dumps(scores, sort_keys=True))


@pytest.mark.parametrize("judge", ["authorize", "accept", "offer", "unknown"])
def test_judge_cannot_upgrade_missing_timing_or_grant_authority(policy, judge):
    decision = gate(policy, timing=None, judge=judge)
    assert decision.action == "defer" and decision.surface is False
    scope, _, row = policy
    assert np.get_authority(scope, row["workstream_id"]) is None
    assert np.list_receipts(scope, kinds=["authorized", "executed", "delivered"]) == []


@pytest.mark.parametrize("judge", ["suppress", "defer"])
def test_judge_can_only_make_an_eligible_offer_more_conservative(policy, judge):
    decision = gate(policy, judge=judge)
    assert decision.action == judge and decision.reason == "judge" and not decision.surface


def test_durable_session_cap_counts_response_owner_artifacts_not_only_helper_counters(policy):
    scope, settings, row = policy
    candidate = ws.Candidate(row["workstream_id"], "review release packaging", 1)
    np.add_receipt(scope, row["workstream_id"], kind="proposed", owner="gateway_response",
                   source_id="offer:" + candidate.fp,
                   detail='{"state":"offered_local","session_id":"one"}',
                   now=NOW - timedelta(days=1))
    assert gate(policy).reason == "session_cap"
    assert gate(policy, session="two").surface is True


def test_per_scope_artifact_cap_bounds_multiple_sessions(policy):
    from dataclasses import replace
    scope, settings, row = policy
    settings = replace(settings, threads_max_items=1)
    old = ws.Candidate(row["workstream_id"], "review release packaging", 1)
    ws.record_offer(scope, row["workstream_id"], old, settings,
                    session_id="previous", now=NOW - timedelta(days=1))
    candidate = ws.Candidate(row["workstream_id"], "check package release audit", 1)
    decision = surface.gate_candidate(candidate, scope, settings,
                                     session_id="new", timing=True, now=NOW)
    assert decision.action == "defer" and decision.reason == "scope_cap"


def test_unknown_silence_remains_unknown_and_prior_offer_cooldown_is_scope_wide(policy):
    scope, settings, row = policy
    ws.record_offer(scope, row["workstream_id"], ws.Candidate(row["workstream_id"], "different", 1),
                    settings, session_id="previous", now=NOW)
    assert gate(policy).reason == "cooldown"
    assert ws.feedback_state(scope, row["workstream_id"]) == "unknown"
    assert np.list_feedback(scope) == []
