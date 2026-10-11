"""A helper stopped on purpose is "stopped", not a failure, and no status
footer is ever glued onto a silent ([SILENT]) reply."""
from __future__ import annotations

import pytest

from agent import work_lifecycle as wl


@pytest.mark.parametrize("entry", [
    {"status": "interrupted"},
    {"status": "completed", "exit_reason": "interrupted"},
    {"status": "cancelled"},
])
def test_a_stopped_helper_is_stopped_not_bad(entry):
    assert wl.classify_result(entry) == wl.STOPPED


@pytest.mark.parametrize("entry", [
    {"status": "failed"},
    {"status": "completed", "exit_reason": "max_iterations"},
    {"status": "completed", "incomplete": True},
])
def test_real_failures_stay_bad(entry):
    assert wl.classify_result(entry) == wl.BAD


def test_stopped_footer_says_stopped_and_never_not_done():
    work = wl.WorkReport((wl.Slot("make clo the default skin", wl.STOPPED),), wl.WORK_INCOMPLETE)
    footer = wl.status_footer(work)
    assert "was stopped: make clo the default skin" in footer
    assert "not done" not in footer and "did not finish" not in footer
    assert "not a failure" in wl.status_note(work)


def test_failed_footer_unchanged():
    work = wl.WorkReport((wl.Slot("x", wl.BAD),), wl.WORK_INCOMPLETE)
    assert "did not finish properly: x. Treat that part as not done." in wl.status_footer(work)


@pytest.mark.parametrize("text, silent", [
    ("[SILENT]", True), ("  [silent]\n", True), ("NO_REPLY", True), ("no reply", True),
    ("[SILENT] but here is text", False), ("all done", False), ("", False), (None, False),
])
def test_silent_reply_detection(text, silent):
    assert wl.is_silent_reply(text) is silent
