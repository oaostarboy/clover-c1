"""Proof logging for the helper card (a "card stayed up" report can be shown
from the log), and the duplicate-send diagnostic only warns when something
WAS already out.  Never logs message text."""
from __future__ import annotations

import asyncio
import logging

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from gateway import delegation_activity as da
from gateway.platforms.base import SendResult

pytestmark = pytest.mark.unified_card

SECRET_TEXT = "zzsecretcardtextzz"


@pytest.fixture(autouse=True)
def _clean():
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    da._ACK_EDITORS.clear()
    yield
    ac.set_style_for_tests(None)
    ac.reset_for_tests()
    da._ACK_EDITORS.clear()


class _Chat:
    def __init__(self, edit_result=None, raise_exc=None):
        self.edit_result = edit_result
        self.raise_exc = raise_exc

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="7")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.edit_result or SendResult(success=True, message_id=message_id)


def _editor(chat):
    r = fl.DispatchReceipt(delegation_id="deleg_log0001", seq=1, goals=("g",),
                           summary=f"\U0001f504 {SECRET_TEXT}", steps=("a",))
    card = ac.card_for([r])
    return card, da.AckCardEditor(card, chat, "chat", "7", asyncio.get_running_loop(), min_interval=0.0)


@pytest.mark.asyncio
async def test_finalize_is_logged_once_at_info_without_text(caplog):
    caplog.set_level(logging.INFO, logger="gateway.delegation_activity")
    card, editor = _editor(_Chat())
    ac.on_child_event("subagent.complete", delegation_id="deleg_log0001", status="completed",
                      summary="Plain summary: ok.")
    assert card.finished
    assert await editor._edit(editor._render())
    assert await editor._edit(editor._render() + " ")
    lines = [r for r in caplog.records if "helper card finalized" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert SECRET_TEXT not in caplog.text


@pytest.mark.asyncio
async def test_edit_failures_are_logged_at_info_without_text(caplog):
    caplog.set_level(logging.INFO, logger="gateway.delegation_activity")
    _card, editor = _editor(_Chat(edit_result=SendResult(success=False, error="Bad Request: flood")))
    assert not await editor._edit("x " + SECRET_TEXT)
    _card, editor2 = _editor(_Chat(raise_exc=RuntimeError("network down " + SECRET_TEXT)))
    assert not await editor2._edit("y " + SECRET_TEXT)
    msgs = [r for r in caplog.records if r.levelno == logging.INFO and "helper card edit" in r.getMessage()]
    assert any("failed" in r.getMessage() and "flood" in r.getMessage() for r in msgs)
    assert any("raised" in r.getMessage() and "RuntimeError" in r.getMessage() for r in msgs)
    assert SECRET_TEXT not in caplog.text


def test_the_duplicate_send_note_is_debug_when_nothing_went_out():
    from gateway.run import _final_send_dup_log_level as level

    assert level(False, False, False) == logging.DEBUG
    assert level(True, False, False) == logging.WARNING
    assert level(False, True, False) == logging.WARNING
    assert level(False, False, True) == logging.WARNING
