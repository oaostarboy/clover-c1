"""A user skin can ship its own chat message pack (``messages:``), not just Clo.

The pack re-skins the chat messages Clo words today; a kind the pack has no
lines for, and any skin without ``messages``, keep the stock text.  Nothing
falls back to Clo's lines.
"""

import random
from types import SimpleNamespace

import pytest

from agent import clover_flavor
from agent.background_review import _clover_review_notice
from clover_cli import skin_engine
from gateway import clover_acks
from plugins.platforms.telegram import adapter as telegram_adapter

CRIMSON = """\
name: crimson
description: test skin with its own message pack
messages:
  mark: "🔻"
  lucky_mark: ""
  done_mark: "🎯"
  fail_mark: "🩸"
  faces:
    busy: ["(⌐■_■)", "(¬_¬)"]
    restarting: ["(ง •̀_•́)ง"]
  lines:
    interrupt: ["target acquired, standing by"]
    queued: ["stacked behind the current one"]
    restarting: ["cycling, back shortly"]
    back_online: ["back in the field"]
    busy: ["the target's occupied"]
    model_substitute: ["no {requested} on {provider}"]
    user: ["profile updated"]
    memory: ["logged it"]
    mixed: ["logged and sharpened"]
    tidy: ["scrubbed the log"]
  review_items:
    about_you: "🪪 profile"
    note: "📌"
    new_skill: "⚙ new tool"
  hello: ["🎯 online. ask away."]
  ui:
    model_title: "🎯 *Choose model*"
    using: "Active"
    deny_mark: "🩸"
"""
PLAIN = "name: plain\ndescription: no message pack\n"

STOCK_RESTART = "⚠️ Gateway restarting — Your current task will be interrupted."
TAIL = "Your task got paused."


def _install(monkeypatch, name, body):
    skins = skin_engine.get_clover_home() / "skins"
    skins.mkdir(parents=True, exist_ok=True)
    (skins / f"{name}.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", "default")
    skin_engine.set_active_skin(name)


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    monkeypatch.setenv("CLOVER_LANGUAGE", "en")
    clover_flavor.reset()
    clover_flavor.reset_status_lines()
    clover_flavor.reset_review_notices()
    clover_acks.reset()
    yield
    clover_flavor.reset()
    clover_flavor.reset_status_lines()
    clover_flavor.reset_review_notices()
    clover_acks.reset()


@pytest.fixture
def crimson(monkeypatch):
    _install(monkeypatch, "crimson", CRIMSON)


def test_messages_block_loads_onto_the_skin(crimson):
    skin = skin_engine.get_active_skin()
    assert skin.messages["mark"] == "🔻"
    assert clover_flavor.active_pack()["done_mark"] == "🎯"


def test_busy_ack_is_reskinned_functional_sentence_kept(crimson):
    assert clover_acks.active("interrupt")
    text = clover_acks.build_ack("interrupt", "chat", "I'll respond shortly.", " (2 min elapsed)")
    assert text == "🔻 target acquired, standing by (2 min elapsed). I'll respond shortly."


def test_restart_notice_is_reskinned_and_tail_kept(crimson):
    out = clover_acks.notice("restarting", "telegram:1", STOCK_RESTART, TAIL, rng=random.Random(1))
    assert out == f"🔻(ง •̀_•́)ง cycling, back shortly. {TAIL}"


def test_back_online_wears_the_done_mark(crimson):
    out = clover_acks.notice("back_online", "telegram:1", "stock", "", rng=random.Random(1))
    assert out == "🎯 back in the field"


def test_kind_without_lines_keeps_stock_and_never_borrows_clo(crimson):
    assert clover_acks.notice("draining", "c", "stock drain", "tail") == "stock drain"
    assert clover_acks.notice("shutting_down", "c", "stock stop", "") == "stock stop"
    assert clover_acks.active("steer") is False
    assert clover_acks.stop_ack("⚡ Stopped. You can continue.", "c") == "⚡ Stopped. You can continue."
    assert clover_flavor.status_line("error", "c", "stock error", "tail") == "stock error"


def test_status_line_is_reskinned_with_tail_and_placeholders(crimson):
    out = clover_flavor.status_line(
        "busy", "chat", "stock", "retrying in 5s", rng=random.Random(3))
    assert out.startswith("🔻(") and out.endswith(". retrying in 5s")
    assert "the target's occupied" in out
    sub = clover_flavor.status_line(
        "model_substitute", "chat", "stock", "so I used *b*.", joiner=", ",
        requested="a", provider="p")
    assert sub == "🔻 no a on p, so I used *b*."


def test_no_repeat_back_to_back_logic_is_shared(crimson):
    class Stuck:
        def choice(self, seq):
            return seq[0]

    first = clover_acks.notice("restarting", "c", "s", "", rng=Stuck())
    assert first == "🔻(ง •̀_•́)ง cycling, back shortly"
    # a one-line pack can't vary, but must not crash on a stubborn RNG
    assert clover_acks.notice("restarting", "c", "s", "", rng=Stuck()) == first


def test_review_notice_header_and_item_icons(crimson):
    items = [("memory", "add", "uses snake_case"), ("user", "add", "name is Ant"),
             ("skill", "create", "deploy")]
    cli, message = clover_flavor.render_review_notice(
        items, "chat", random.Random(1), pack=clover_flavor.active_pack())
    header, *body = message.replace("> ", "").split("\n")
    assert header == "🔻 🎯 logged and sharpened"
    assert body == ["📌 *uses snake_case*", "🪪 profile: *name is Ant*", "⚙ new tool: *deploy*"]
    assert cli[0] == header and cli[1:] == [
        "📌 uses snake_case", "🪪 profile: name is Ant", "⚙ new tool: deploy"]


def test_review_notice_tidy_uses_removed_icon_default(crimson):
    _, message = clover_flavor.render_review_notice(
        [("memory", "remove", "old")], "chat", pack=clover_flavor.active_pack())
    header, item = message.replace("> ", "").split("\n")
    assert header == "🔻 🧹 scrubbed the log"
    assert item == "🧹 *old*"  # review_items.removed not set -> Clo's default icon


def test_review_notice_kind_without_lines_is_stock(crimson):
    assert clover_flavor.render_review_notice(
        [("skill", "create", "deploy")], "chat", pack=clover_flavor.active_pack()) is None


def test_background_review_uses_the_skin_pack(crimson):
    from tests.agent.test_background_review_clover_notice import _mem, _notice

    cli, message = _notice(_mem("add", content="Likes tea"))
    assert message.replace("> ", "").split("\n")[0] == "🔻 🎯 logged it"
    assert cli[1] == "📌 Likes tea"


def test_cron_header_is_reskinned_and_never_rolls_lucky(crimson):
    class NoRoll:
        def randrange(self, n):
            raise AssertionError("no lucky_mark -> no lucky roll")

    assert clover_flavor.cron_header("nightly", rng=NoRoll()) == "🔻 nightly"
    assert clover_flavor.cron_header("nightly", failed=True) == "🩸 nightly didn't finish"


def test_picker_strings_come_from_ui_and_unset_keys_stay_stock(crimson):
    assert telegram_adapter._ui("using", "Current model") == "Active"
    assert telegram_adapter._ui("model_title", "⚙ *Model Configuration*") == "🎯 *Choose model*"
    assert telegram_adapter._ui("deny_mark", "❌") == "🩸"
    assert telegram_adapter._ui("choose_model", "Select a model") == "Select a model"
    assert telegram_adapter._pfx("chosen_mark") == ""


def test_first_hello_uses_the_pack(crimson, tmp_path):
    out = clover_acks.with_first_hello("hi there", "telegram", "dm", "42", tmp_path)
    assert out == "🎯 online. ask away.\n\nhi there"
    assert clover_acks.with_first_hello("again", "telegram", "dm", "42", tmp_path) == "again"


def test_pack_without_hello_says_no_hello(monkeypatch, tmp_path):
    _install(monkeypatch, "quiet", "name: quiet\nmessages:\n  mark: '🔹'\n  lines:\n    error: ['oops']\n")
    assert clover_acks.with_first_hello("hi", "telegram", "dm", "1", tmp_path) == "hi"


@pytest.mark.parametrize("skin", ["plain", "default", "ares"])
def test_skin_without_messages_is_stock_everywhere(monkeypatch, tmp_path, skin):
    _install(monkeypatch, "plain", PLAIN)
    skin_engine.set_active_skin(skin)
    assert clover_flavor.active_pack() is None
    assert clover_acks.active() is False
    assert clover_acks.notice("restarting", "c", STOCK_RESTART, TAIL) == STOCK_RESTART
    assert clover_acks.notice("back_online", "c", "stock") == "stock"
    assert clover_flavor.status_line("busy", "c", "stock", "tail") == "stock"
    assert clover_flavor.cron_header("job") is None
    assert clover_acks.with_first_hello("hi", "telegram", "dm", "1", tmp_path) == "hi"
    assert telegram_adapter._ui("using", "Current model") == "Current model"
    agent = SimpleNamespace(session_id="s", memory_notifications="on")
    assert _clover_review_notice(agent, [], []) is None


def test_non_english_session_keeps_stock(crimson, monkeypatch):
    monkeypatch.setenv("CLOVER_LANGUAGE", "de")
    assert clover_flavor.active_pack() is None
    assert clover_acks.notice("restarting", "c", STOCK_RESTART, TAIL) == STOCK_RESTART


def test_malformed_messages_block_is_ignored_not_fatal(monkeypatch):
    _install(monkeypatch, "junk", "name: junk\nmessages: [1, 2]\n")
    assert skin_engine.get_active_skin().messages == {}
    assert clover_flavor.active_pack() is None
    _install(monkeypatch, "junk2", "name: junk2\nmessages:\n  lines: {busy: 7}\n  faces: nope\n")
    assert clover_flavor.status_line("busy", "c", "stock") == "stock"


def test_clover_skin_pack_is_the_built_in_pack(monkeypatch):
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", "clover")
    assert clover_flavor.active_pack() is clover_flavor.CLOVER_PACK
    faces, lines = clover_acks.ACK_POOLS["back_online"]
    assert all(f.startswith("🍀") for f in faces) and "I'm back!" in lines
