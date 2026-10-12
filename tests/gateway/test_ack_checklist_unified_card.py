"""Gateway side of the unified helper card: no 🔀 card for an owned dispatch,
and the ack message's running clock is refreshed while the helper works."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from gateway import delegation_activity as da
from gateway.platforms.base import SendResult
from tests.gateway.test_delegation_activity import (
    FakeTelegramAdapter,
    _child_cb,
    _make_publisher,
    _turn_runner,
)

pytestmark = pytest.mark.unified_card

HEAD = "\U0001f504 fixing the bugs \u2192 test build + PR, ~10 min"
STEPS = ("find", "fix", "test")
OWNED = "deleg_aaaa0001"  # the id _child_cb gives its events


class _Adapter:
    def __init__(self) -> None:
        self.sends: List[str] = []
        self.edits: List[Dict[str, Any]] = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append(content)
        return SendResult(success=True, message_id="m42")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edits.append({"id": message_id, "text": content, "finalize": finalize})
        return SendResult(success=True, message_id=message_id)


@pytest.fixture(autouse=True)
def _clean():
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    da._ACK_EDITORS.clear()
    yield
    ac.reset_for_tests()
    da._ACK_EDITORS.clear()


def _card(did=OWNED):
    r = fl.DispatchReceipt(delegation_id=did, seq=1, goals=("g",), summary=HEAD, steps=STEPS)
    return ac.card_for([r])


async def _bind(adapter, card, *, interval=0.0):
    sent = await adapter.send("chat", card.initial_text)
    editor = da.AckCardEditor(
        card, adapter, "chat", sent.message_id, asyncio.get_running_loop(),
        min_interval=interval, clock=time.monotonic,
    )
    card.target = ("a", "chat", "m42")
    return editor


async def _settle(editor):
    for _ in range(50):
        await asyncio.sleep(0)
        t = editor._task
        if t is None or t.done():
            return


# -- the publisher ----------------------------------------------------------


async def _drive(adapter, *, did=OWNED):
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    cb = _child_cb(runner, delegation_id=did)
    cb("subagent.start", preview="Audit the gateway auth mixin")
    cb("tool.started", "read_file", "x.py", {"path": "x.py"})
    cb("tool.completed", "read_file", None, None, duration=0.2, is_error=False, result="body")
    cb("subagent.complete", status="completed", summary="Plain summary: All good.", duration_seconds=3)
    await pub.drain()
    return pub


@pytest.mark.asyncio
async def test_publisher_posts_no_roster_card_for_an_owned_delegation_under_unified():
    card = _card()
    adapter = FakeTelegramAdapter()
    pub = await _drive(adapter)
    assert adapter.status_calls == [] and adapter.sends == [], "no 🔀 card, no summary"
    assert pub.tracker.group_ids() == [], "nothing tracked for an owned id"
    # ...and the unified card did get the activity and the result
    cl = card.parts[0].checklist
    assert cl.model == "Opus 5.5" and cl.calls >= 1
    assert cl.state == ac.DONE and cl.summary == "All good."
    await pub.aclose()


@pytest.mark.asyncio
async def test_publisher_still_posts_the_roster_card_under_classic():
    _card()
    ac.set_style_for_tests("classic")
    adapter = FakeTelegramAdapter()
    pub = await _drive(adapter)
    assert adapter.status_calls or adapter.sends, "classic keeps the separate 🔀 card"
    assert pub.tracker.group_ids() != []
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_delegation_without_a_card_gets_a_unified_card_never_the_roster():
    # unified is on, but no ack part exists for this id (a nested helper, a
    # dispatch path that skipped the card): the first event opens a plain
    # titled unified card.  The 🔀 roster card is never posted.
    _card("some_other_id")
    adapter = FakeTelegramAdapter()
    pub = await _drive(adapter)
    assert adapter.status_calls == [], "no 🔀 roster card"
    assert adapter.sends, "the unified card was posted instead"
    assert "Audit the gateway auth mixin" not in adapter.sends[0]["content"] or "**" in adapter.sends[0]["content"]
    await pub.aclose()


@pytest.mark.asyncio
async def test_turn_card_absorb_has_nothing_to_absorb_for_owned_delegations():
    _card()
    adapter = FakeTelegramAdapter()
    from gateway import delegation_activity as mod

    pub = mod.DelegationActivityPublisher(
        adapter=adapter, chat_id="chat-A", loop=asyncio.get_running_loop(),
        min_interval=0.0, auto_heartbeat=False, expandable=True, combined=True,
    )
    runner = _turn_runner(pub)
    cb = _child_cb(runner)
    cb("subagent.start", preview="g")
    cb("subagent.complete", status="completed", summary="Plain summary: Done.", duration_seconds=1)
    await pub.drain()
    workers, ids = pub.absorb_finished()
    assert workers == [] and ids == []
    from agent.delegation_activity import turn_card_body

    assert turn_card_body(workers, 0.0) == [], "no empty worker rows on the turn card"
    pub.end_turn()
    await pub.drain()
    assert adapter.sends == [] and adapter.status_calls == []
    await pub.aclose()


# -- the editor's clock tick -------------------------------------------------


@pytest.mark.asyncio
async def test_editor_schedules_a_tick_while_running_under_unified(monkeypatch):
    adapter, card = _Adapter(), _card()
    editor = await _bind(adapter, card)
    calls: List[float] = []
    loop = asyncio.get_running_loop()
    real = loop.call_later
    monkeypatch.setattr(loop, "call_later", lambda d, fn, *a: calls.append(d) or real(3600, fn, *a))
    editor._tick_later()
    assert calls == [da.ACK_TICK_SECONDS] and editor._ticking is True
    editor._tick_later()
    assert calls == [da.ACK_TICK_SECONDS], "one pending tick at a time"


@pytest.mark.asyncio
async def test_editor_schedules_no_tick_under_classic(monkeypatch):
    adapter, card = _Adapter(), _card()
    editor = await _bind(adapter, card)
    ac.set_style_for_tests("classic")
    calls: List[float] = []
    monkeypatch.setattr(asyncio.get_running_loop(), "call_later", lambda *a: calls.append(a))
    editor._tick_later()
    assert calls == [] and editor._ticking is False


@pytest.mark.asyncio
async def test_editor_schedules_no_tick_once_finished_or_gone(monkeypatch):
    adapter, card = _Adapter(), _card()
    editor = await _bind(adapter, card)
    calls: List[Any] = []
    monkeypatch.setattr(asyncio.get_running_loop(), "call_later", lambda *a: calls.append(a))
    ac.on_child_event("subagent.complete", delegation_id=OWNED, status="completed", summary="Plain summary: ok.")
    assert card.finished
    editor._tick_later()
    assert calls == []
    editor.gone = True
    editor._tick_later()
    assert calls == []


@pytest.mark.asyncio
async def test_run_with_unchanged_text_arms_the_tick(monkeypatch):
    adapter, card = _Adapter(), _card()
    editor = await _bind(adapter, card)
    armed: List[int] = []
    monkeypatch.setattr(editor, "_tick_later", lambda: armed.append(1))
    editor.notify()
    await _settle(editor)
    assert armed == [1] and adapter.edits == [], "same text: no edit, just the next tick"


@pytest.mark.asyncio
async def test_the_tick_refreshes_the_running_clock_with_an_edit(monkeypatch):
    monkeypatch.setattr(da, "ACK_TICK_SECONDS", 0.05)
    adapter, card = _Adapter(), _card()
    ac.on_child_event("subagent.start", delegation_id=OWNED, model="claude-opus-5-5")
    editor = await _bind(adapter, card)
    editor.notify()
    await _settle(editor)
    assert adapter.edits == []
    card.parts[0].checklist.started -= 30  # the helper has been at it a while
    await asyncio.sleep(0.3)
    await _settle(editor)
    assert adapter.edits, "the clock was refreshed with no other change"
    assert "Opus 5.5 \u00b7 \u23f1 30s" in adapter.edits[-1]["text"], "the clock moved on"
    assert len(adapter.sends) == 1, "an edit, never a new message"


@pytest.mark.asyncio
async def test_ticks_stop_when_the_helper_finishes(monkeypatch):
    monkeypatch.setattr(da, "ACK_TICK_SECONDS", 0.05)
    adapter, card = _Adapter(), _card()
    ac.on_child_event("subagent.start", delegation_id=OWNED, model="claude-opus-5-5")
    editor = await _bind(adapter, card)
    editor.notify()
    await _settle(editor)
    ac.on_child_event("subagent.complete", delegation_id=OWNED, status="completed", summary="Plain summary: ok.")
    await asyncio.sleep(0.2)
    await _settle(editor)
    edits_after_finish = len(adapter.edits)
    assert adapter.edits[-1]["finalize"] is True
    assert adapter.edits[-1]["text"].splitlines()[0].startswith("> \U0001f340 **fixing the bugs** \u00b7 done")
    await asyncio.sleep(0.3)
    assert len(adapter.edits) == edits_after_finish, "no edits after the final one"
    assert editor._ticking is False


def _multi(did=OWNED, goals=("g1", "g2")):
    r = fl.DispatchReceipt(delegation_id=did, seq=1, goals=goals, summary=HEAD, steps=())
    return ac.card_for([r])


@pytest.mark.asyncio
async def test_publisher_posts_no_roster_card_for_a_multi_helper_dispatch_under_unified():
    _multi()
    assert ac.owns_delegation(OWNED) is True
    adapter = FakeTelegramAdapter()
    pub = await _drive(adapter)
    assert adapter.status_calls == [] and adapter.sends == [], "no 🔀 card, no summary"
    assert pub.tracker.group_ids() == []
    await pub.aclose()


@pytest.mark.asyncio
async def test_publisher_still_posts_the_roster_card_for_a_multi_helper_dispatch_under_classic():
    _multi()
    ac.set_style_for_tests("classic")
    adapter = FakeTelegramAdapter()
    pub = await _drive(adapter)
    assert adapter.status_calls or adapter.sends
    await pub.aclose()


@pytest.mark.asyncio
async def test_editor_ticks_a_multi_helper_card_while_it_runs(monkeypatch):
    adapter, card = _Adapter(), _multi("dm")
    editor = await _bind(adapter, card)
    calls: List[Any] = []
    monkeypatch.setattr(asyncio.get_running_loop(), "call_later", lambda *a: calls.append(a))
    editor._tick_later()
    assert len(calls) == 1 and calls[0][0] == da.ACK_TICK_SECONDS


@pytest.mark.asyncio
async def test_multi_helper_events_edit_the_one_message_in_place():
    adapter, card = _Adapter(), _multi("dm", goals=("check a", "check b"))
    editor = await _bind(adapter, card)
    ac.on_child_event("subagent.tool", delegation_id="dm", task_index=1, tool_name="terminal",
                      preview="npm test", args={"command": "npm test"}, tool_count=2, subagent_id="s2")
    await _settle(editor)
    assert adapter.edits and adapter.edits[-1]["id"] == "m42"
    text = adapter.edits[-1]["text"]
    assert "*now: npm test*" in text and "**check a** \u00b7 waiting" in text
    assert adapter.edits[-1]["finalize"] is True  # live edits are formatted
    for i in (0, 1):
        ac.on_child_event("subagent.complete", delegation_id="dm", task_index=i, status="completed",
                          summary="Plain summary: ok.")
    await _settle(editor)
    assert adapter.edits[-1]["finalize"] is True
    assert adapter.edits[-1]["text"].splitlines() == [
        "> \U0001f340 **fixing the bugs** \u00b7 done", "> *2 done \u00b7 \U0001f6e0 2 \u00b7 \u23f1 0s*"]
    assert "**>" not in adapter.edits[-1]["text"], "finished: short blockquote, not expandable"


@pytest.mark.asyncio
async def test_a_six_helper_edit_fits_the_telegram_limit():
    goals = tuple(f"Check site number {i}" for i in range(6))
    adapter, card = _Adapter(), _multi("dm", goals=goals)
    editor = await _bind(adapter, card)
    for i in range(6):
        ac.on_child_event("subagent.complete", delegation_id="dm", task_index=i, status="completed",
                          summary="Plain summary: " + " ".join(["word"] * 70) + ".")
    await _settle(editor)
    sent = adapter.edits[-1]["text"]
    assert len(sent) <= 4000 and sent.splitlines() == [
        "> \U0001f340 **fixing the bugs** \u00b7 done", "> *6 done*"]


@pytest.mark.asyncio
async def test_classic_editor_text_is_unchanged_for_a_multi_dispatch():
    card = _multi("dm")
    ac.set_style_for_tests("classic")
    assert card.render() == HEAD


# -- live edits keep their formatting ---------------------------------------


@pytest.mark.asyncio
async def test_editor_always_edits_with_finalize_true_so_telegram_formats_it():
    adapter, card = _Adapter(), _card()
    editor = await _bind(adapter, card)
    ac.on_child_event("subagent.todo", delegation_id=OWNED, todos=[{"id": "s1", "status": "in_progress"}])
    ac.on_child_event("subagent.tool", delegation_id=OWNED, tool_name="read_file", preview="x.py",
                      args={"path": "x.py"}, tool_count=1)
    await _settle(editor)
    assert adapter.edits and not card.finished
    assert all(e["finalize"] is True for e in adapter.edits), "mid-run edits must be formatted"
    ac.on_child_event("subagent.complete", delegation_id=OWNED, status="completed", summary="Plain summary: ok.")
    await _settle(editor)
    assert all(e["finalize"] is True for e in adapter.edits)


def _telegram_format(text: str) -> str:
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    return TelegramAdapter(PlatformConfig(enabled=True, token="fake-token")).format_message(text)


def test_a_live_card_renders_without_raw_markdown_through_telegram_format():
    card = _card()
    ac.on_child_event("subagent.start", delegation_id=OWNED, model="claude-sonnet-5-5")
    ac.on_child_event("subagent.todo", delegation_id=OWNED, todos=[
        {"id": "s1", "status": "completed"}, {"id": "s2", "status": "in_progress"}, {"id": "s3", "status": "pending"}])
    ac.on_child_event("subagent.tool", delegation_id=OWNED, tool_name="skill_view", preview="github",
                      args={"name": "github"}, tool_call_id="a", tool_count=1)
    ac.on_child_event("subagent.tool_done", delegation_id=OWNED, tool_name="skill_view", tool_call_id="a")
    ac.on_child_event("subagent.tool", delegation_id=OWNED, tool_name="terminal", preview="pytest -q",
                      args={"command": "pytest -q"}, tool_call_id="b", tool_count=2)
    raw = card.render()
    out = _telegram_format(raw)
    assert "**" not in out, out
    assert "\\*\\*" not in out, "bold must be real bold, not escaped asterisks"
    bare = [ln for ln in out.splitlines() if ln.strip() in (">", "\\>")]
    assert bare == [], f"a bare quote-bar line would show as a lone '>': {out!r}"
    assert any(ln.startswith(">") for ln in out.splitlines()), "the body is quoted"
    assert "\u2800" in raw and "\n>\n" not in raw + "\n"
    assert "todo" not in raw.lower()


# -- no early 🔀 card for an owned helper ----------------------------------


@pytest.mark.asyncio
async def test_reserved_delegation_never_gets_a_roster_card_from_its_first_events():
    # The dispatch reserved the id; the part only opens afterwards.
    ac.reserve(OWNED)
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    cb = _child_cb(runner, delegation_id=OWNED)
    cb("subagent.queued", preview="Read the Windows check result")
    cb("subagent.start", preview="Read the Windows check result")
    await pub.drain()
    ac.open_part(OWNED, HEAD, STEPS)
    cb("tool.started", "read_file", "x.py", {"path": "x.py"})
    await pub.drain()
    assert adapter.status_calls == [] and adapter.sends == []
    assert pub.tracker.group_ids() == []
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_roster_card_already_posted_is_removed_once_the_delegation_is_owned():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    cb = _child_cb(runner, delegation_id=OWNED)
    # (unified mode itself never posts a roster card any more; a style switched
    # mid-run is the way left to have one up before the unified card owns the id)
    ac.set_style_for_tests("classic")
    cb("subagent.start", preview="Read the Windows check result")
    await pub.drain()
    assert adapter.status_calls, "precondition: the early 🔀 card was posted"
    ac.set_style_for_tests("unified")
    posted = list(adapter.status_calls)
    # ownership arrives late (the old race)
    _card()
    cb("tool.started", "read_file", "x.py", {"path": "x.py"})
    for _ in range(5):
        await asyncio.sleep(0)
    await pub.drain()
    assert adapter.deleted, "the live 🔀 card was taken down"
    assert adapter.cards == {}
    cb("subagent.complete", status="completed", summary="Plain summary: All good.", duration_seconds=3)
    await pub.drain()
    pub.end_turn()
    await pub.drain()
    assert adapter.sends == [], "no summary message for an owned delegation"
    assert len(adapter.status_calls) == len(posted), "nothing more was posted for it"
    await pub.aclose()


# -- Telegram: bold title on top, the rest in a collapsed expandable quote ----

SAY = "on it, sorting it out."


def _tg_adapter():
    from types import SimpleNamespace

    a = _Adapter()
    a.platform = SimpleNamespace(value="telegram")
    return a


def _say_card(did=OWNED):
    r = fl.DispatchReceipt(delegation_id=did, seq=1, goals=("g",), summary=HEAD, steps=STEPS, say=SAY)
    card = ac.card_for([r])
    card.split_opener = True  # the opener is its own message
    return card


def _editor(adapter, card):
    return da.AckCardEditor(card, adapter, "chat", "m1", asyncio.new_event_loop(), min_interval=0.0)


def test_telegram_title_is_the_first_line_inside_the_collapsed_quote():
    card = _say_card()
    text = _editor(_tg_adapter(), card)._render()
    lines = text.splitlines()
    assert lines[0] == "**> \U0001f500 **fixing the bugs**", "title opens the expandable quote, no emoji"
    assert lines[1] == f"> *{da.TAP_HINT_LIVE}*" and da.TAP_HINT_LIVE == "tap to watch \U0001f340"
    assert lines[2] == "> \u2800", "the 3-line cut lands on empty space"
    assert lines[3] == "> Goal: test build + PR \u00b7 ~10 min", "exactly ONE spacer after the hint, then Goal:"
    assert text.endswith("||") and text.count("||") == 1 and text.count("**>") == 1
    assert all(ln.startswith("> ") for ln in lines[1:]) and "> \u25b8 find" in text
    assert "\U0001f504" not in text and text.count("\U0001f340") == 1, "the only clover is in the hint"


def test_telegram_finished_card_is_one_plain_line_without_a_quote_or_hint():
    card = _say_card()
    ac.on_child_event("subagent.start", delegation_id=OWNED, model="m")
    ac.on_child_event("subagent.complete", delegation_id=OWNED, status="completed",
                      summary="Result: done\nPlain summary: All fixed.")
    text = _editor(_tg_adapter(), card)._render()
    lines = text.splitlines()
    assert lines[0] == "> \U0001f340 **fixing the bugs** \u00b7 done" and lines[1].startswith("> *") and len(lines) == 2
    assert "**>" not in text and "||" not in text and "tap to" not in text and "All fixed." not in text


def test_other_platforms_keep_the_plain_quote():
    text = _editor(_Adapter(), _say_card())._render()
    assert "**>" not in text and "||" not in text and "tap to" not in text
    assert text.splitlines()[0] == "\U0001f500 **fixing the bugs**" and "> \u25b8 find" in text


def test_classic_style_is_not_made_expandable():
    ac.set_style_for_tests("classic")
    card = _say_card()
    text = _editor(_tg_adapter(), card)._render()
    assert "**>" not in text and text == card.render()


def _three_subagent_card():
    r = fl.DispatchReceipt(delegation_id="dm", seq=1, goals=("a", "b", "c"), summary=HEAD, steps=(), say=SAY,
                           child_steps=(("check the repo", "count files"), ("scan", "report"), ("x", "y")))
    card = ac.card_for([r])
    card.split_opener = True
    for i in range(3):
        ac.on_child_event("subagent.start", delegation_id="dm", task_index=i, task_count=3,
                          title=f"site {i}", model="m")
    ac.on_child_event("subagent.tool", delegation_id="dm", task_index=0, task_count=3,
                      tool_name="terminal", preview="ls", tool_call_id="t1")
    ac.on_child_event("subagent.complete", delegation_id="dm", task_index=1, task_count=3, status="completed",
                      summary="Result: done\nPlain summary: Site 1 is tidy.")
    return card


def test_multi_subagent_title_keeps_k_of_n_done_inside_one_quote():
    text = _editor(_tg_adapter(), _three_subagent_card())._render()
    lines = text.splitlines()
    assert lines[0].startswith("**> \U0001f500 **fixing the bugs**") and "1 of 3 done" in lines[0]
    assert lines[1] == "> *tap to watch \U0001f340*" and lines[2] == "> \u2800"
    assert text.count("**>") == 1 and text.count("||") == 1
    assert all(ln.startswith("> ") for ln in lines[1:]) and lines[-1].endswith("||")


def test_multi_subagent_spacing_one_spacer_between_blocks_and_before_footer():
    text = _editor(_tg_adapter(), _three_subagent_card())._render()
    lines = text.splitlines()
    spacer = "> \u2800"
    # No two blank lines in a row anywhere (hint spacer, blocks, footer).
    assert not any(a == spacer and b == spacer for a, b in zip(lines, lines[1:]))
    # Hint, ONE spacer, then straight to the outcome line.
    assert lines[1:4] == ["> *tap to watch \U0001f340*", spacer, "> Goal: test build + PR \u00b7 ~10 min"]
    assert lines[-2] == spacer and lines[-1].startswith("> *2 running \u00b7 1 done") and "helpers" not in text
    # Three subagent sections: a spacer before each name line, plus the one before the footer.
    names = [i for i, ln in enumerate(lines) if ln.startswith("> ") and "**" in ln and i > 0]
    assert len(names) == 3 and all(lines[i - 1] == spacer for i in names)


def test_multi_subagent_lines_are_indented_under_the_unindented_bold_name():
    em = "\u2003\u2003"
    text = _editor(_tg_adapter(), _three_subagent_card())._render()
    lines = [ln[2:] for ln in text.splitlines()[1:]]
    for i, ln in enumerate(lines):
        if "**site" in ln:
            assert not ln.startswith((em, " ", "\u2003")), "the name line is not indented"
            under = []
            for nxt in lines[i + 1:]:
                if nxt in ("\u2800", "") or "**site" in nxt:
                    break
                under.append(nxt)
            assert under and all(u.startswith(em) and not u.startswith(em + "\u2003") for u in under), under
    assert "\n> " + em + "*" in text or "> " + em + "\U0001f527" in text


def test_single_subagent_card_has_no_indent():
    text = _editor(_tg_adapter(), _say_card())._render()
    assert "\u2003" not in text


def test_format_message_keeps_title_inside_one_expandable_quote_and_the_em_indent():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    fm = lambda t: TelegramAdapter.format_message(object.__new__(TelegramAdapter), t)
    out = fm(_editor(_tg_adapter(), _say_card())._render())
    lines = out.splitlines()
    assert lines[0] == "**> \U0001f500 *fixing the bugs*"
    assert lines[1] == "> _tap to watch \U0001f340_" and lines[2] == "> \u2800"
    assert out.count("**>") == 1 and out.endswith("||")
    multi = fm(_editor(_tg_adapter(), _three_subagent_card())._render())
    assert multi.splitlines()[0].startswith("**> \U0001f500 *fixing the bugs* \u00b7 1 of 3 done")
    assert "> \u2003\u2003\u25b8 check the repo" in multi, "EM SPACE indent survives escaping"
    assert multi.count("**>") == 1 and multi.endswith("||")


@pytest.mark.asyncio
async def test_card_text_sent_under_the_opener_is_the_expandable_one():
    adapter = _tg_adapter()
    card = _say_card()
    card.split_opener = True
    sent = await adapter.send("chat", card.say_text())
    await da.bind_ack_card(adapter, "chat", card.say_text(), sent)
    assert adapter.sends[0] == SAY
    assert adapter.sends[1].startswith("**> \U0001f500 **fixing the bugs**\n> *tap to watch")


def test_telegram_format_of_a_joined_card_with_a_long_running_name_and_an_ended_part_gone():
    r1 = fl.DispatchReceipt(delegation_id="a", seq=1, goals=("g",), steps=("one",),
                            summary="\U0001f504 finishing the small fast model fix \u2192 an outcome, ~5 min")
    r2 = fl.DispatchReceipt(delegation_id="b", seq=2, goals=("g",), steps=("two",),
                            summary="\U0001f504 disks report \u2192 an outcome, ~5 min")
    r3 = fl.DispatchReceipt(delegation_id="c", seq=3, goals=("g",), steps=("three",),
                            summary="\U0001f504 network check \u2192 an outcome, ~5 min")
    card = ac.card_for([r1, r2, r3])
    cl = card.parts[0].checklist
    cl.model, cl.calls, cl.started = "Sonnet 5.5", 20, time.monotonic() - 355
    ac.on_child_event("subagent.complete", delegation_id="b", status="completed", summary="Plain summary: ok.")
    out = _telegram_format(da._to_expandable(card.render(include_say=False), da.TAP_HINT_LIVE, bold_title=True))
    assert "\u25a0 *finishing the small fast model fix*\n" in out, "the name has the line to itself"
    assert "disks report" not in out and "2 jobs" in out.splitlines()[0]
    stats = next(ln for ln in out.splitlines() if "Sonnet 5" in ln)
    assert stats.startswith("> \u2003\u2003_Sonnet 5") and "\U0001f6e0 20 calls" in stats
    assert out.index("Goal:") < out.index("Sonnet 5"), "the Goal sits right under the name; the stats close the section"


def _tg_format(text):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    return TelegramAdapter(PlatformConfig(enabled=True, token="fake-token")).format_message(text)


@pytest.mark.asyncio
async def test_the_finishing_edit_swaps_the_expandable_quote_for_one_plain_line():
    adapter, card = _Adapter(), _say_card()
    adapter.platform = type("P", (), {"value": "telegram"})()
    editor = await _bind(adapter, card)
    ac.on_child_event("subagent.start", delegation_id=OWNED, model="claude-opus-5-5")
    editor.notify()
    await _settle(editor)
    live = adapter.edits[-1]["text"]
    assert live.startswith("**> ") and live.endswith("||") and "tap to watch" in live
    ac.on_child_event("subagent.complete", delegation_id=OWNED, status="completed",
                      summary="Plain summary: All fixed.")
    await _settle(editor)
    last = adapter.edits[-1]
    assert last["finalize"] is True and last["id"] == "m42", "an edit of the same message, formatted"
    assert last["text"].startswith("> \U0001f340 **fixing the bugs** \u00b7 done")
    assert "**>" not in last["text"] and "||" not in last["text"]
    assert all(ln.startswith("> ") for ln in last["text"].splitlines()), "short blockquote: the bar stays"
    assert "tap to" not in last["text"] and "All fixed" not in last["text"]
    assert editor._closed and card.finished, "a finished card stops following the bottom"
    n = len(adapter.edits)
    assert await editor.repost_live_cards() == 0 and len(adapter.edits) == n


def test_telegram_formats_the_finished_one_liners_as_plain_text():
    ok = _tg_format("\U0001f340 **checking your second-GPU options** \u00b7 done \u00b7 Opus 5.5 \u00b7 \U0001f6e0 66 \u00b7 \u23f1 8m42s")
    assert ok == "\U0001f340 *checking your second\\-GPU options* \u00b7 done \u00b7 Opus 5\\.5 \u00b7 \U0001f6e0 66 \u00b7 \u23f1 8m42s"
    bad = _tg_format("\U0001f342 **fixing the bugs** \u00b7 failed\n" + ac._INDENT + "The folder does not exist.")
    assert bad.startswith("\U0001f342 *fixing the bugs* \u00b7 failed\n") and not bad.startswith(">")
    stopped = _tg_format("\U0001f940 **fixing the bugs** \u00b7 stopped")
    assert stopped == "\U0001f940 *fixing the bugs* \u00b7 stopped"
