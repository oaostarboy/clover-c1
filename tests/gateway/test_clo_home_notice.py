"""Clo notices on every restart/drain surface, not just active chats.

Owner saw the stock "⚠️ Gateway restarting — …" in his home chat after the
Clo notices shipped: the home-channel broadcast (idle chat, no running task)
still sent the stock text. Same for the drain replies and the
"♻ Gateway restarted successfully" notice.
"""

import re

from gateway import run as gw_run


SRC = open(gw_run.__file__, encoding="utf-8").read()


def _sends_after(marker: str, window: int = 1500) -> str:
    i = SRC.index(marker)
    return SRC[i:i + window]


def test_home_channel_shutdown_notice_goes_through_clo():
    block = _sends_after('home = self.config.get_home_channel(platform)')
    assert "_clover_acks.notice(" in block
    assert "adapter.send(str(home.chat_id), msg" not in block
    assert "adapter.send(str(home.chat_id), home_msg" in block


def test_every_drain_reply_goes_through_clo():
    for stock in (
        'not accepting another turn right now."',
        'not accepting new work right now."',
    ):
        for m in re.finditer(re.escape(stock), SRC):
            around = SRC[max(0, m.start() - 400): m.end() + 400]
            assert '_clover_acks.notice(\n' in around or "_clover_acks.notice(" in around, stock


def test_restarted_successfully_notice_goes_through_clo():
    i = SRC.index('"♻ Gateway restarted successfully. Your session continues."')
    assert "_clover_acks.notice(" in SRC[i - 200:i]


def test_notice_output_under_clover_and_stock_otherwise(monkeypatch):
    from gateway import clover_acks

    stock = "⚠️ Gateway restarting — Your current task will be interrupted."
    monkeypatch.setattr(clover_acks, "active", lambda: True)
    out = clover_acks.notice("restarting", "telegram:1", stock, "Your task got paused.")
    assert out != stock and out.startswith("☘️") and out.endswith("Your task got paused.")
    out = clover_acks.notice("draining", "telegram:1", "⏳ stock", "Send it again once I'm back.")
    assert out.startswith("☘️") and out.endswith("Send it again once I'm back.")
    monkeypatch.setattr(clover_acks, "active", lambda: False)
    assert clover_acks.notice("restarting", "telegram:1", stock, "x") == stock
