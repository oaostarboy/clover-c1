"""A completion whose task is NOT done always gets a real reply: the prompt
never offers [SILENT] for it.  A clean completion still may be absorbed.

Live bug: deleg_ecceb581 finished "Result: not done"; the completion prompt
said "if this does not change what you already told the user, reply [SILENT]"
and the agent did, so the user never heard it failed.
"""
from __future__ import annotations

import pytest

from tools.process_registry import (
    NOT_DONE_REPLY_NOTE,
    _format_async_delegation,
    async_delegation_not_done,
)

SILENT_OFFER = "reply with exactly [SILENT]"


def _batch(*results, **extra):
    evt = {"type": "async_delegation", "delegation_id": "deleg_nd000001", "is_batch": True,
           "fanout_delivery": "joined", "goals": [f"g{i}" for i in range(len(results))],
           "results": [dict(task_index=i, **r) for i, r in enumerate(results)]}
    evt.update(extra)
    return evt


CLEAN = {"status": "completed", "summary": "All set.\nResult: done\nPlain summary: it works."}


@pytest.mark.parametrize("bad", [
    {"status": "completed", "summary": "Could not reach the box.\nResult: not done\nPlain summary: x."},
    {"status": "failed", "error": "boom"},
    {"status": "error", "summary": "partial"},
    {"status": "timeout"},
    {"status": "completed", "summary": "half of it", "exit_reason": "max_iterations"},
    {"status": "completed", "summary": "cut", "truncated": True},
    {"incomplete": True},
    {"status": "partial", "summary": "some"},
])
def test_a_not_done_task_in_the_batch_removes_the_silent_offer(bad):
    evt = _batch(CLEAN, bad)
    assert async_delegation_not_done(evt)
    text = _format_async_delegation(evt)
    assert SILENT_OFFER not in text
    assert NOT_DONE_REPLY_NOTE in text


def test_a_clean_batch_still_offers_silent():
    evt = _batch(CLEAN, CLEAN)
    assert not async_delegation_not_done(evt)
    text = _format_async_delegation(evt)
    assert SILENT_OFFER in text and NOT_DONE_REPLY_NOTE not in text


def test_missing_helpers_in_a_joined_batch_are_not_done():
    evt = _batch(CLEAN, {"incomplete": True}, incomplete_indexes=[1])
    assert SILENT_OFFER not in _format_async_delegation(evt)


def test_a_batch_that_errored_with_no_results_gets_a_reply():
    evt = {"type": "async_delegation", "delegation_id": "deleg_nd000002", "is_batch": True,
           "results": [], "goals": ["g"], "error": "pool died"}
    assert SILENT_OFFER not in _format_async_delegation(evt)


@pytest.mark.parametrize("evt,silent_ok", [
    ({"status": "completed", "summary": "ok\nResult: done"}, True),
    ({"status": "completed", "summary": "no luck\nResult: not done"}, False),
    ({"status": "error", "error": "x"}, False),
    ({"status": "completed", "summary": "ok", "exit_reason": "max_iterations"}, False),
    # stopped on purpose by the user: may be absorbed, unless it says not done
    ({"status": "interrupted", "summary": "stopped"}, True),
    ({"status": "interrupted", "summary": "stopped\nResult: not done"}, False),
])
def test_single_completion(evt, silent_ok):
    evt = {"type": "async_delegation", "delegation_id": "deleg_nd000003", "goal": "g", **evt}
    text = _format_async_delegation(evt)
    assert (SILENT_OFFER in text) is silent_ok
    assert (NOT_DONE_REPLY_NOTE in text) is (not silent_ok)


def test_the_last_result_line_wins():
    s = "Result: not done at first\n...fixed it.\nResult: done\nPlain summary: ok."
    assert not async_delegation_not_done({"status": "completed", "summary": s})


def test_prose_mentioning_not_done_is_not_a_result_line():
    s = "The old card said not done.\nResult: done"
    assert not async_delegation_not_done({"status": "completed", "summary": s})


def test_the_coalesced_header_drops_silent_when_any_is_not_done():
    from gateway.run import GatewayRunner

    clean = GatewayRunner._format_coalesced_async_delegations(["a", "b"])
    bad = GatewayRunner._format_coalesced_async_delegations(["a", "b"], not_done=True)
    assert SILENT_OFFER in clean
    assert SILENT_OFFER not in bad and "NOT done" in bad
