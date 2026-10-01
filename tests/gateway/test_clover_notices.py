"""Clo-style gateway lifecycle notices under the clover skin; other skins stay stock."""

import random

import pytest

from agent import clover_flavor
from clover_cli import skin_engine
from gateway import clover_acks

TAILS = {
    "restarting": "Your task got paused. Message me after and I'll pick up where we left off.",
    "shutting_down": "Your task got stopped.",
    "restart_requested": "If I'm not back in a minute, run `clover gateway restart` on the computer.",
    "restart_in_progress": "",
    "draining": "Finishing 2 task(s) first.",
    "back_online": "",
    "job_interrupted": "'nightly' got cut off by the restart, so there's no result this time.",
    "update_rolled_back": (
        "The update didn't start right, so I went back to the version you had. "
        "Nothing was lost. You can try again later."
    ),
}
STOCK = "stock text"


@pytest.fixture(autouse=True)
def clover_active(monkeypatch):
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", "clover")
    monkeypatch.setenv("CLOVER_LANGUAGE", "en")
    clover_flavor.reset()
    clover_acks.reset()
    yield
    clover_flavor.reset()
    clover_acks.reset()


def _split(text, kind):
    faces, lines = clover_acks.ACK_POOLS[kind]
    face = next(f for f in faces if text.startswith(f + " "))
    line = next(x for x in lines if text[len(face) + 1:].startswith(x))
    return face, line


@pytest.mark.parametrize("kind", sorted(TAILS))
def test_notice_is_face_line_and_tail(kind):
    rng = random.Random(1)
    tail = TAILS[kind]
    for _ in range(30):
        text = clover_acks.notice(kind, "chat", STOCK, tail, rng=rng)
        face, line = _split(text, kind)
        expected = f"{face} {line}. {tail}" if tail else f"{face} {line}"
        assert text == expected
        assert face.startswith("🍀" if kind == "back_online" else "☘️")


@pytest.mark.parametrize("kind", sorted(TAILS))
def test_notice_never_repeats_a_pair_back_to_back(kind):
    rng = random.Random(7)
    picks = [
        _split(clover_acks.notice(kind, "chat-a", STOCK, TAILS[kind], rng=rng), kind)
        for _ in range(20)
    ]
    assert all(a != b for a, b in zip(picks, picks[1:]))
    assert len(set(picks)) > 1


@pytest.mark.parametrize("kind", sorted(TAILS))
@pytest.mark.parametrize("skin", ["default", "ares", "mono", "slate"])
def test_other_skins_return_stock_exactly(monkeypatch, kind, skin):
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", skin)
    assert clover_acks.notice(kind, "c", STOCK, TAILS[kind]) is STOCK


def test_non_english_returns_stock(monkeypatch):
    monkeypatch.setenv("CLOVER_LANGUAGE", "de")
    assert clover_acks.notice("restarting", "c", STOCK, "x") is STOCK


def test_back_online_is_always_lucky_even_with_unlucky_turn():
    class First:
        def choice(self, seq):
            return seq[0]

    clover_flavor.begin_turn(rng=random.Random(3), monotonic=lambda: 0)
    clover_flavor.current_turn().lucky = False
    assert clover_acks.notice("back_online", "c", STOCK, rng=First()).startswith("🍀(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧ I'm back!")


def test_lucky_turn_swaps_leaf_only_for_leaf_faces():
    class Lucky:
        def randrange(self, n):
            return 0

        def choice(self, seq):
            return seq[0]

    clover_flavor.begin_turn(rng=Lucky())
    assert clover_acks.notice("draining", "c", STOCK, "x", rng=Lucky()).startswith("🍀(っ˘ω˘ς) ")
    assert clover_acks.notice("back_online", "c", STOCK, rng=Lucky()).startswith("🍀(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧ ")


def test_kinds_have_independent_memory_for_one_chat():
    class First:
        def choice(self, seq):
            return seq[0]

    a = clover_acks.notice("restarting", "c", STOCK, "x", rng=First())
    b = clover_acks.notice("shutting_down", "c", STOCK, "x", rng=First())
    assert a.startswith("☘️(￣▽￣)ゞ restarting") and b.startswith("☘️(´• ω •`)ﾉ heading out")


# ── call sites ──────────────────────────────────────────────────────────────

import json  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import gateway.run as gateway_run  # noqa: E402
from gateway.config import HomeChannel, Platform  # noqa: E402
from gateway.platforms.base import MessageEvent, MessageType  # noqa: E402
from tests.gateway.restart_test_helpers import (  # noqa: E402
    make_restart_runner,
    make_restart_source,
)


def _face_line(text, kind):
    return _split(text, kind)


def _set_skin(monkeypatch, name):
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", name)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart,kind,tail", [
    (True, "restarting", TAILS["restarting"]),
    (False, "shutting_down", TAILS["shutting_down"]),
])
async def test_active_chat_shutdown_notice_is_clo_style(restart, kind, tail):
    runner, adapter = make_restart_runner()
    runner._restart_requested = restart
    runner._running_agents["agent:main:telegram:dm:123456"] = MagicMock()

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 1
    face, line = _face_line(adapter.sent[0], kind)
    assert adapter.sent[0] == f"{face} {line}. {tail}"


@pytest.mark.asyncio
async def test_active_chat_shutdown_notice_stock_on_other_skin(monkeypatch):
    _set_skin(monkeypatch, "default")
    runner, adapter = make_restart_runner()
    runner._restart_requested = True
    runner._running_agents["agent:main:telegram:dm:123456"] = MagicMock()

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.sent == [
        "⚠️ Gateway restarting — Your current task will be interrupted. "
        "Send any message after restart and I'll try to resume where you left off."
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("restart,cause", [(True, "restart"), (False, "shutdown")])
async def test_cron_interrupted_notice_is_clo_style(restart, cause):
    from gateway.run import GatewayRunner

    runner, adapter = make_restart_runner()
    runner._restart_requested = restart
    runner._thread_metadata_for_target = (
        GatewayRunner._thread_metadata_for_target.__get__(runner, GatewayRunner)
    )
    job = {"id": "j1", "name": "nightly", "deliver": "telegram:123456"}
    target = {"platform": "telegram", "chat_id": "123456", "thread_id": None}

    with patch("cron.jobs.get_job", return_value=job), \
         patch("cron.scheduler._resolve_delivery_targets", return_value=[target]):
        sent = await GatewayRunner._notify_interrupted_cron_jobs(runner, ["j1"])

    assert sent == 1
    face, line = _face_line(adapter.sent[0], "job_interrupted")
    assert adapter.sent[0] == (
        f"{face} {line}. 'nightly' got cut off by the {cause}, "
        "so there's no result this time."
    )


@pytest.mark.asyncio
async def test_cron_interrupted_notice_stock_on_other_skin(monkeypatch):
    from gateway.run import GatewayRunner

    _set_skin(monkeypatch, "mono")
    runner, adapter = make_restart_runner()
    runner._restart_requested = True
    runner._thread_metadata_for_target = (
        GatewayRunner._thread_metadata_for_target.__get__(runner, GatewayRunner)
    )
    job = {"id": "j1", "name": "nightly", "deliver": "telegram:123456"}
    target = {"platform": "telegram", "chat_id": "123456", "thread_id": None}
    with patch("cron.jobs.get_job", return_value=job), \
         patch("cron.scheduler._resolve_delivery_targets", return_value=[target]):
        await GatewayRunner._notify_interrupted_cron_jobs(runner, ["j1"])
    assert adapter.sent == [
        "⚠️ Cron job 'nightly' was interrupted — the gateway is restarting and "
        "killed the run before it finished. No result was produced for this run."
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("skin,clo", [("clover", True), ("default", False)])
async def test_home_channel_back_online(monkeypatch, tmp_path, skin, clo):
    _set_skin(monkeypatch, skin)
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM, chat_id="42", name="Home",
    )

    await runner._send_home_channel_startup_notifications()

    text = adapter.sent[0]
    if clo:
        face, line = _face_line(text, "back_online")
        assert face.startswith("🍀") and text == f"{face} {line}"
    else:
        assert text == "♻️ Gateway online — Clover is back and ready."


@pytest.mark.asyncio
async def test_restart_command_reply(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    event = MessageEvent(text="/restart", message_type=MessageType.TEXT,
                         source=make_restart_source(chat_id="42"), message_id="m1")

    result = await runner._handle_restart_command(event)

    face, line = _face_line(str(result), "restart_requested")
    assert str(result) == f"{face} {line}. {TAILS['restart_requested']}"


@pytest.mark.asyncio
async def test_restart_command_reply_stock_on_other_skin(monkeypatch, tmp_path):
    _set_skin(monkeypatch, "slate")
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    event = MessageEvent(text="/restart", message_type=MessageType.TEXT,
                         source=make_restart_source(chat_id="42"), message_id="m1")
    result = await runner._handle_restart_command(event)
    assert str(result) == (
        "♻ Restarting gateway. If you aren't notified within 60 seconds, "
        "restart from the console with `clover gateway restart`."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("skin", ["clover", "default"])
async def test_restart_already_in_progress_and_draining(monkeypatch, tmp_path, skin):
    _set_skin(monkeypatch, skin)
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    runner, _adapter = make_restart_runner()
    runner._draining = True
    runner._running_agent_count = lambda: 0
    event = MessageEvent(text="/restart", message_type=MessageType.TEXT,
                         source=make_restart_source(chat_id="42"), message_id="m1")

    in_progress = str(await runner._handle_restart_command(event))
    runner._running_agent_count = lambda: 2
    draining = str(await runner._handle_restart_command(event))

    if skin == "clover":
        face, line = _face_line(in_progress, "restart_in_progress")
        assert in_progress == f"{face} {line}"
        face, line = _face_line(draining, "draining")
        assert draining == f"{face} {line}. Finishing 2 task(s) first."
    else:
        assert in_progress == "⏳ Gateway restart already in progress..."
        assert draining == "⏳ Draining 2 active agent(s) before restart..."


@pytest.mark.asyncio
async def test_failed_receipt_with_post_update_sha_is_not_reported_as_success(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    receipts = tmp_path / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    (receipts / "latest.json").write_text(json.dumps({
        "outcome": "failed", "started_at": "2999-01-01T00:00:00+00:00",
        "post_update": {"sha": "a" * 40},
    }), encoding="utf-8")
    runner, adapter = make_restart_runner()
    paths = [tmp_path / n for n in ("p", "c", "o", "e", "q")]
    # This run's pending marker, so the receipt above counts as THIS run's
    # (a receipt with no readable marker is treated as stale).
    paths[0].write_text("{}", encoding="utf-8")

    await runner._conclude_update_after_updater_death(
        *paths, adapter=adapter, chat_id="42", session_key=None,
        metadata=None, platform=Platform.TELEGRAM,
    )

    assert "did not complete" in adapter.sent[0]
    assert "finished" not in adapter.sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("skin,clo", [("clover", True), ("default", False)])
async def test_update_rolled_back_notice(monkeypatch, tmp_path, skin, clo):
    _set_skin(monkeypatch, skin)
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    receipts = tmp_path / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    (receipts / "latest.json").write_text(json.dumps({
        "outcome": "rolled-back", "started_at": "2999-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    runner, adapter = make_restart_runner()
    paths = [tmp_path / n for n in ("p", "c", "o", "e", "q")]
    # This run's pending marker, so the receipt above counts as THIS run's.
    paths[0].write_text("{}", encoding="utf-8")

    await runner._conclude_update_after_updater_death(
        *paths, adapter=adapter, chat_id="42", session_key=None,
        metadata=None, platform=Platform.TELEGRAM,
    )

    text = adapter.sent[0]
    if clo:
        face, line = _face_line(text, "update_rolled_back")
        assert text == f"{face} {line}. {TAILS['update_rolled_back']}"
    else:
        assert text == (
            "The update didn't start correctly, so I went back to the version "
            "you had before. Nothing was lost. You can try again later."
        )


def test_watcher_keeps_stock_text_and_imports_no_gateway_code():
    import subprocess
    import sys

    code = (
        "import sys; import clover_cli.update_restart_watcher as w; "
        "assert \"you had before\" in w.ROLLBACK_MESSAGE; "
        "bad=[m for m in sys.modules if m=='gateway' or m.startswith('gateway.')]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
