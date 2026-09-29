"""Council card v2: one live card that follows the chat, ends as ONE message."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import council_card as cc
from gateway import delegation_activity as da
from gateway.council_progress import (
    CouncilCard,
    CouncilRunWatcher,
    make_card,
    render_council_card,
    resolve_card_style,
)
from gateway.platforms.base import SendResult
from gateway.slash_commands import GatewaySlashCommandsMixin

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = (
    ROOT / "optional-skills" / "autonomous-ai-agents" / "council" / "scripts" / "council_run.py"
)
SESSION_ENV = {
    "CLOVER_SESSION_PLATFORM": "telegram",
    "CLOVER_SESSION_CHAT_ID": "123",
    "CLOVER_SESSION_THREAD_ID": "7",
    "CLOVER_SESSION_KEY": "agent:main:telegram:dm:123",
}
QUESTION = (
    "Did we misread_it? Amira wants *sales* [tiers] (v9) - 1.5x! "
    "(Full context with the verbatim emails: /home/u/evidence/pricing.md . Read-only.)"
)
MODELS = {
    "STEELMAN": "Fable 5.1",
    "PROSECUTOR": "GPT-5.6 Sol",
    "PREMISE": "Gemini 3.8 Flash High",
    "PRAGMATIST": "Gemini 3.8 Flash High",
    "OUTSIDER": "Grok 4.6",
    "CHAIRMAN": "Opus 5",
}
SUMMARY = {
    "mode": "full",
    "verdict": "Ship v9 with the sandwich tiers.",
    "why": "Carl promised to talk to Amira.",
    "caveat": "The web page shows no tiers.",
    "stalled": [],
    "elapsed_s": 130,
}


def _load_runner():
    spec = importlib.util.spec_from_file_location("council_run_v2", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveAdapter:
    """Telegram-shaped fake: send/edit/delete; ``live`` is what the chat shows."""

    name = "telegram"

    def __init__(self, *, edit_fails=False):
        self.live: dict[str, str] = {}
        self.n = 0
        self.edit_calls = 0
        self.sent: list[str] = []
        self._edit_fails = edit_fails

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.n += 1
        mid = str(self.n)
        self.live[mid] = content
        self.sent.append(content)
        da.note_outbound(self, chat_id, mid)  # what the real base-class wrapper does
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edit_calls += 1
        if self._edit_fails:
            return SendResult(success=False, error="flood control")
        self.live[str(message_id)] = content
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        self.live.pop(str(message_id), None)
        return True

    def council_messages(self):
        return [m for m in self.live.values() if "🏛 Council" in m]


class NoDeleteAdapter(LiveAdapter):
    delete_message = None


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for reg in (da._LIVE, da._LAST_OUT, da._FOLLOW_TIMERS, da._BOARDS, cc._CARD_IDS):
        reg.clear()
    cc._ORPHANS_SWEPT.clear()
    monkeypatch.setattr(da, "FOLLOW_DEBOUNCE_SECONDS", 0.01)
    yield
    for handle in list(da._FOLLOW_TIMERS.values()):
        handle.cancel()
    for reg in (da._LIVE, da._LAST_OUT, da._FOLLOW_TIMERS, da._BOARDS, cc._CARD_IDS):
        reg.clear()


def _seat(work, name, gist, stage=1, done=True):
    path = work / f"stage{stage}-{name}.md"
    path.write_text(f"GIST: {gist}\n\nbody body body\n", encoding="utf-8")
    if done:
        (work / f"stage{stage}-{name}.md.done").write_text("", encoding="utf-8")


def _state(stage="arguments", seat_done=0, review_done=0, elapsed=0, stalled=(), status="running", mode="full"):
    return {
        "status": status, "mode": mode, "stage": stage, "seat_done": seat_done, "seat_total": 5,
        "review_done": review_done, "review_total": 5, "stalled": list(stalled), "elapsed_s": elapsed,
    }


def _card(adapter, work, clock=None, **kw):
    work.mkdir(parents=True, exist_ok=True)
    return cc.CouncilLiveCard(
        adapter, "chat-A", "run-1", None, work=work, question=QUESTION, mode="full",
        models=MODELS, expandable=True, clock=clock or Clock(), min_edit=0.0, **kw,
    )


async def _settle():
    await asyncio.sleep(0.15)


def _escaped_ok(formatted: str) -> bool:
    """No bare MarkdownV2 special outside the entities the card deliberately uses."""
    stripped = re.sub(r"\\.", "", formatted)
    stripped = stripped.replace("**>", "").replace("||", "")
    return not re.search(r"[.!()\[\]{}=+~`#-]", stripped)


# ── renders ────────────────────────────────────────────────────────────────


def test_running_frame_has_question_stage_line_and_a_block_per_seat(tmp_path):
    work = tmp_path
    _seat(work, "STEELMAN", "Ship it; the upside is real")
    _seat(work, "PROSECUTOR", "The web page contradicts the ask and this is a very long gist that must be trimmed")
    (work / "stage1-PREMISE.md").write_text("GIST: half writ", encoding="utf-8")  # partial line
    state = _state(seat_done=2, elapsed=92)
    snap = cc.build_snapshot(
        work, state, mode="full", question=QUESTION, models=MODELS,
        stage_started={"arguments": 100.0}, now=112.0,
    )
    frame = cc.render_live(snap, expandable=True)
    assert frame.endswith("||")  # the collapsible quote's terminator
    lines = frame[:-2].split("\n")

    assert lines[0] == "**> 🏛 Council · Full · ⏱ 1m32s"
    assert lines[1] == "> *tap to watch 🏛*"
    assert lines[2] == f"> {cc.SPACER}"
    assert lines[3].startswith("> ❓ **Did we misread_it?")
    assert "pricing.md" not in frame and "/home/" not in frame and "Full context" not in frame
    stage_idx = next(i for i, ln in enumerate(lines) if "Opening" in ln)
    assert lines[stage_idx] == "> ◉ Opening 2/5 · ○ Cross-review 0/5 · ○ Chairman"
    assert stage_idx > 3
    assert "> 🛡️ **Steelman** · Fable 5.1" in lines
    assert "> ⚔️ **Prosecutor** · GPT-5.6 Sol" in lines
    assert "> 🧩 **Premise** · Gemini 3.8 Flash High" in lines
    assert "> 👀 **Outsider** · Grok 4.6" in lines
    assert "> *Ship it; the upside is real*" in lines
    # unfinished seats: thinking with elapsed seconds; a half-written line never shows
    assert lines.count("> *thinking… 12s*") == 3
    assert "half writ" not in frame
    # every take fits a phone line
    for ln in lines:
        if ln.startswith("> *") and "thinking" not in ln and "tap to" not in ln:
            assert len(ln) - 4 <= cc.TAKE_MAX + 1
    # blocks are separated by a spacer, exactly like worker rows
    assert lines.count(f"> {cc.SPACER}") >= 6
    assert not any(p in frame.lower() for p in ("anthropic", "openai", "xai", "gemini-oauth", "provider"))


def test_cross_review_chairman_and_stalled_states(tmp_path):
    work = tmp_path
    for seat in ("STEELMAN", "PROSECUTOR", "PREMISE", "PRAGMATIST"):
        _seat(work, seat, f"{seat.lower()} position")
    _seat(work, "STEELMAN", "reviewer sees a blind spot", stage=2)
    state = _state("cross_review", 4, 1, 60, stalled=["OUTSIDER"])
    snap = cc.build_snapshot(work, state, mode="full", question="Q?", models=MODELS,
                             stage_started={"cross_review": 90.0}, now=105.0)
    frame = cc.render_live(snap, expandable=False)
    assert "> ⚠ Opening 4/5 · ◉ Cross-review 1/5 · ○ Chairman" in frame
    assert "> *review: reviewer sees a blind spot*" in frame
    assert "> *reviewing… 15s*" in frame
    assert "> *⚠ no answer (stalled)*" in frame  # the seat that never answered

    state = _state("chairman", 4, 5, 90, stalled=["OUTSIDER"])
    snap = cc.build_snapshot(work, state, mode="full", question="Q?", models=MODELS,
                             stage_started={"chairman": 100.0}, now=107.0)
    frame = cc.render_live(snap, expandable=False)
    assert "> ⚠ Opening 4/5 · ✓ Cross-review 5/5 · ◉ Chairman" in frame
    assert frame.rstrip().endswith("> 👑 **Chairman** · Opus 5\n> *thinking… 7s*")
    assert "review:" not in frame  # once the chairman runs, seats show their positions
    assert "> *steelman position*" in frame


def test_frame_survives_telegram_markdownv2_with_special_chars(tmp_path):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    _seat(tmp_path, "STEELMAN", "a_b (x) [y] 1.5 - z! done.")
    snap = cc.build_snapshot(tmp_path, _state(seat_done=1, elapsed=5), mode="full", question=QUESTION,
                             models=MODELS, stage_started={}, now=0.0)
    for frame in (
        cc.render_live(snap, expandable=True),
        cc.render_final(snap, SUMMARY | {"verdict": "Ship (v9) - a_b, 1.5x!"}, expandable=True),
    ):
        formatted = TelegramAdapter.format_message(object.__new__(TelegramAdapter), frame)
        assert formatted.startswith("**> ")
        assert _escaped_ok(formatted), formatted
        assert formatted.count("||") == 1  # exactly one quote terminator
        assert "\\_" in formatted and "\\[" in formatted and "\\(" in formatted


def test_elapsed_shows_minutes_only_past_ten():
    assert cc.elapsed_label(92) == "1m32s"
    assert cc.elapsed_label(45) == "45s"
    assert cc.elapsed_label(11 * 60 + 40) == "11m"


def test_pretty_model_names_never_show_provider():
    assert cc.pretty_seat_model("grok-4.6") == "Grok 4.6"
    assert cc.pretty_seat_model("gemini-3.8-flash-high") == "Gemini 3.8 Flash High"
    assert cc.pretty_seat_model("gpt-5.6-sol") == "GPT-5.6 Sol"
    assert cc.pretty_seat_model("claude-fable-5-1") == "Fable 5.1"
    real = cc.load_seat_models(None)  # the shipped roster
    assert real["PROSECUTOR"] and all("openai" not in v.lower() and "xai" not in v.lower() for v in real.values())


# ── follow ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_newer_message_moves_the_card_to_the_bottom(tmp_path):
    ad = LiveAdapter()
    card = _card(ad, tmp_path)
    await card.publish(_state(seat_done=1, elapsed=3))
    first = card.message_id
    assert list(ad.live) == [first] and da._FOLLOW_TIMERS == {}

    await ad.send("chat-A", "parent reply")  # someone else speaks
    await _settle()

    assert len(ad.council_messages()) == 1
    assert first not in ad.live  # old copy deleted
    assert card.message_id != first
    assert max(ad.live, key=int) == card.message_id  # card is the newest message
    assert ad.live[card.message_id].startswith("**> 🏛 Council")


@pytest.mark.asyncio
async def test_own_sends_and_edits_never_trigger_a_move(tmp_path):
    ad = LiveAdapter()
    clock = Clock()
    card = _card(ad, tmp_path, clock)
    await card.publish(_state(seat_done=0, elapsed=1))
    _seat(tmp_path, "STEELMAN", "one")
    clock.t += 5
    await card.publish(_state(seat_done=1, elapsed=6))
    _seat(tmp_path, "PROSECUTOR", "two")
    clock.t += 5
    await card.publish(_state(seat_done=2, elapsed=11))
    await _settle()

    assert ad.n == 1  # posted once; everything after was an edit
    assert ad.edit_calls == 2
    assert da._FOLLOW_TIMERS == {}


@pytest.mark.asyncio
async def test_failing_edits_still_leave_exactly_one_live_card(tmp_path):
    ad = LiveAdapter(edit_fails=True)
    clock = Clock()
    card = _card(ad, tmp_path, clock)
    await card.publish(_state(seat_done=0, elapsed=1))
    for i, seat in enumerate(("STEELMAN", "PROSECUTOR", "PREMISE"), start=1):
        _seat(tmp_path, seat, seat)
        clock.t += 5
        await card.publish(_state(seat_done=i, elapsed=1 + 5 * i))
    assert ad.edit_calls >= 3 and len(ad.council_messages()) == 1  # stale, but never two

    for _ in range(2):  # the chat moves on twice; each move re-posts and sweeps
        await ad.send("chat-A", "reply")
        await _settle()
        assert len(ad.council_messages()) == 1

    await card.finish_done(_state("chairman", 3, 5, 60, status="done"), SUMMARY)
    assert len(ad.council_messages()) == 1
    assert "✅ 🏛 Council" in ad.council_messages()[0]


@pytest.mark.asyncio
async def test_adapter_that_cannot_delete_keeps_editing_in_place(tmp_path):
    ad = NoDeleteAdapter()
    clock = Clock()
    card = _card(ad, tmp_path, clock)
    await card.publish(_state(seat_done=0, elapsed=1))
    first = card.message_id
    await ad.send("chat-A", "parent reply")
    await _settle()
    assert card.message_id == first and ad.n == 2  # no repost, no duplicate

    _seat(tmp_path, "STEELMAN", "one")
    clock.t += 5
    await card.publish(_state(seat_done=1, elapsed=6))
    assert ad.edit_calls == 1 and len(ad.council_messages()) == 1

    await card.finish_done(_state("chairman", 5, 5, 100, status="done"), SUMMARY)
    # the live card IS the single final message: edited in place, nothing extra
    assert len(ad.council_messages()) == 1
    assert ad.live[first].startswith("**> ✅ 🏛 Council")
    assert "**Answer**" in ad.live[first]


@pytest.mark.asyncio
async def test_orphan_card_from_a_previous_gateway_process_is_swept(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "_board_store_path", lambda: tmp_path / "boards.json")
    ad = LiveAdapter()
    ad.live["900"] = "**> 🏛 Council · Full (from before the restart)"
    key = cc._STORE_PREFIX + da._board_store_key(ad, "chat-A")
    da._board_store_set(key, ["900"])
    card = _card(ad, tmp_path)
    await card.publish(_state(seat_done=1, elapsed=2))
    assert "900" not in ad.live and len(ad.council_messages()) == 1


@pytest.mark.asyncio
async def test_updates_are_throttled_and_clock_ticks_alone_are_not_edits(tmp_path):
    ad = LiveAdapter()
    clock = Clock()
    work = tmp_path
    card = cc.CouncilLiveCard(ad, "chat-A", "r", None, work=work, question="Q", mode="full",
                              models=MODELS, clock=clock)  # default 2s throttle
    await card.publish(_state(seat_done=0))
    _seat(work, "STEELMAN", "one")
    clock.t += 0.5
    await card.publish(_state(seat_done=1))  # real change, but <2s after the post
    assert ad.edit_calls == 0
    clock.t += 2
    await asyncio.sleep(2.2)  # the scheduled refresh fires once the window opens
    await card.publish(_state(seat_done=1))
    assert ad.edit_calls == 1
    clock.t += 3  # only the seconds moved
    await card.publish(_state(seat_done=1))
    assert ad.edit_calls == 1


# ── finish ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finish_posts_exactly_one_message_and_leaves_no_live_card(tmp_path):
    ad = LiveAdapter()
    clock = Clock()
    for seat in ("STEELMAN", "PROSECUTOR", "PREMISE", "PRAGMATIST", "OUTSIDER"):
        _seat(tmp_path, seat, f"{seat.title()} says yes")
    card = _card(ad, tmp_path, clock)
    await card.publish(_state("cross_review", 5, 3, 80))
    await ad.send("chat-A", "tool bubble")
    await _settle()
    await card.publish(_state("chairman", 5, 5, 100))
    clock.t += 30
    await card.finish_done(_state("chairman", 5, 5, 130, status="done"), SUMMARY)
    await _settle()

    council = ad.council_messages()
    assert len(council) == 1, council  # card + answer travel together
    final = council[0]
    assert final.startswith("**> ✅ 🏛 Council · Full · ⏱ 2m10s")
    assert "> *tap to read 🏛*" in final
    assert "> ❓ **Did we misread_it?" in final
    assert "> 🛡️ **Steelman** · Fable 5.1" in final and "> *Steelman says yes*" in final
    quote, _, outside = final.partition("||\n\n")
    assert outside.startswith("**Answer**\n• Ship v9 with the sandwich tiers.")
    assert "**Why**" in outside and "**What could change it**" in outside
    assert "Council answer" not in final  # no second title
    assert not any(cc.SPACER in ln and not ln.startswith(">") for ln in final.split("\n"))
    # the answer is not posted as a second message, and no live card remains
    assert sum("Ship v9" in m for m in ad.live.values()) == 1
    assert not [m for m in ad.live.values() if "tap to watch" in m]
    assert card not in [p for ps in da._LIVE.values() for p in ps]
    await ad.send("chat-A", "later")
    await _settle()
    assert len(ad.council_messages()) == 1  # the finished card never follows


@pytest.mark.asyncio
async def test_failed_run_ends_with_one_failure_message(tmp_path):
    ad = LiveAdapter()
    _seat(tmp_path, "STEELMAN", "x")
    card = _card(ad, tmp_path)
    await card.publish(_state("cross_review", 2, 0, 50))
    await card.finish_failed(_state("chairman", 2, 5, 70, status="failed"))
    (msg,) = ad.council_messages()
    assert msg.startswith("**> ❌ 🏛 Council · Full · ⏱ 1m10s")
    assert "Failed at Chairman" in msg
    assert "**Answer**" not in msg
    assert len(ad.live) == 1


# ── same card for both entry points ────────────────────────────────────────


def _watcher(home, adapter, **kw):
    return CouncilRunWatcher(
        homes=lambda: [home],
        resolve_target=lambda origin: (adapter, origin["chat_id"], {"thread_id": origin["thread_id"]}),
        poll_s=0.01,
        **kw,
    )


def _runner_progress(runner, work, stage, status="running", seats=0):
    runner.write_progress(work, status=status, mode="full", stage=stage, seat_done=seats,
                          seat_total=5, review_done=0, review_total=5, started_at=100.0, now=100.0 + seats)


@pytest.mark.asyncio
async def test_agent_launched_run_gets_one_card_and_one_final_message(tmp_path, monkeypatch):
    for key, value in SESSION_ENV.items():
        monkeypatch.setenv(key, value)
    runner = _load_runner()
    work = tmp_path / "council" / "runs" / "council-agent-1"
    work.mkdir(parents=True)
    runner.write_origin(work)
    (work / "question.txt").write_text(QUESTION, encoding="utf-8")
    _runner_progress(runner, work, "arguments", seats=1)

    ad = LiveAdapter()
    (task,) = _watcher(tmp_path, ad).scan_once()
    assert not (work / "gateway-card.json").exists()  # a live card is not delivery
    await asyncio.sleep(0.1)
    assert len(ad.council_messages()) == 1 and "❓ **Did we misread_it?" in ad.council_messages()[0]

    _seat(work, "STEELMAN", "Yes ship it")
    _runner_progress(runner, work, "chairman", seats=5)
    await asyncio.sleep(0.1)
    (work / "summary.json").write_text(json.dumps(SUMMARY), encoding="utf-8")
    _runner_progress(runner, work, "chairman", status="done", seats=5)
    await asyncio.wait_for(task, 5)

    (final,) = ad.council_messages()
    assert (work / "gateway-card.json").exists()
    assert "✅ 🏛 Council" in final and "**Answer**\n• Ship v9" in final
    assert len(ad.live) == 1 and not [m for m in ad.sent if m.startswith("🏛 **Council answer**")]


@pytest.mark.asyncio
async def test_slash_council_produces_one_card_and_one_final_message(tmp_path):
    script = tmp_path / "skills" / "autonomous-ai-agents" / "council" / "scripts" / "council_run.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        "args=sys.argv[1:]\n"
        "run_id=args[args.index('--id')+1]\n"
        "work=pathlib.Path(os.environ['CLOVER_HOME'])/'council'/'runs'/run_id\n"
        "work.mkdir(parents=True)\n"
        "(work/'origin.json').write_text(json.dumps({'platform':'telegram','chat_id':'123','thread_id':'7','created_at':time.time(),'pid':os.getpid()}))\n"
        "def put(stage,status='running',done=0):\n"
        " (work/'progress.json').write_text(json.dumps({'status':status,'mode':'quick','stage':stage,'seat_done':done,'seat_total':3,'review_done':0,'review_total':0,'elapsed_s':done}))\n"
        "put('arguments',done=1)\n"
        "(work/'stage1-STEELMAN.md').write_text('GIST: Ship it\\n\\nbody\\n')\n"
        "time.sleep(1.3)\n"
        "(work/'summary.json').write_text(json.dumps({'mode':'quick','verdict':'Ship it.','next':'Pilot.','dissent':'Scale.','stalled':[],'elapsed_s':2}))\n"
        "put('chairman','done',3)\n",
        encoding="utf-8",
    )
    ad = LiveAdapter()

    class Runner(GatewaySlashCommandsMixin):
        def _council_card_style(self, platform):
            return "v2"

        def _resolve_profile_home_for_source(self, source):
            return tmp_path

        def _thread_metadata_for_source(self, source):
            return {"thread_id": "7"}

        def _adapter_for_source(self, source):
            return ad

    watcher = _watcher(tmp_path, ad)
    watching = asyncio.create_task(watcher.run(interval_s=0.05))
    event = SimpleNamespace(
        source=SimpleNamespace(chat_id="123", platform=SimpleNamespace(value="telegram")),
        get_command_args=lambda: "quick Is this clean?",
    )
    try:
        reply = await Runner()._handle_council_command(event)
        await asyncio.sleep(0.3)
    finally:
        watching.cancel()

    assert reply is None  # the card already delivered the answer; nothing else is sent
    (final,) = ad.council_messages()
    assert "✅ 🏛 Council · Quick" in final and "Is this clean?" in final
    assert "**Answer**\n• Ship it." in final and "> *Ship it*" in final
    assert len(ad.live) == 1 and not watcher.tasks  # watcher stayed out


@pytest.mark.asyncio
async def test_cli_run_without_origin_gets_no_card(tmp_path, monkeypatch):
    for key in SESSION_ENV:
        monkeypatch.delenv(key, raising=False)
    runner = _load_runner()
    work = tmp_path / "council" / "runs" / "council-cli-1"
    work.mkdir(parents=True)
    assert runner.write_origin(work) is False
    _runner_progress(runner, work, "arguments", seats=1)
    ad = LiveAdapter()
    assert _watcher(tmp_path, ad).scan_once() == []
    assert ad.live == {} and not (work / "gateway-card.json").exists()


# ── the duplicate answer ───────────────────────────────────────────────────


def test_runner_does_not_repeat_the_verdict_to_the_agent_when_the_gateway_delivers(tmp_path):
    """Root cause of the answer posted twice.

    The agent launches the runner with terminal(notify_on_complete). The gateway
    watcher posted the answer, AND the runner printed VERDICT/WHY/CAVEAT to
    stdout, which the completion notice injected into the agent's next turn; the
    agent restated it (run council-20260929-183140, 18:33:52 vs 18:34:03).
    """
    runner = _load_runner()
    summary = {"verdict": "UNIQUE-VERDICT-TEXT", "why": "UNIQUE-WHY", "caveat": "UNIQUE-CAVEAT",
               "stalled": [], "attack_severity": "", "ruling": ""}
    report = tmp_path / "report.md"

    plain = "\n".join(runner.final_stdout_lines(0, report, summary))  # CLI: nobody else posts it
    assert "VERDICT: UNIQUE-VERDICT-TEXT" in plain

    (tmp_path / "gateway-card.json").write_text("{}", encoding="utf-8")  # gateway adopted the run
    handed_off = "\n".join(runner.final_stdout_lines(0, report, summary))
    assert "UNIQUE" not in handed_off  # nothing for the agent to relay
    assert "COUNCIL_DONE" in handed_off and "COUNCIL_REPORT=" in handed_off

    failed = "\n".join(runner.final_stdout_lines(1, report, {"stage": "chairman"}))
    assert "COUNCIL_FAILED stage=chairman" in failed


@pytest.mark.asyncio
async def test_answer_text_reaches_the_chat_exactly_once_end_to_end(tmp_path, monkeypatch):
    for key, value in SESSION_ENV.items():
        monkeypatch.setenv(key, value)
    runner = _load_runner()
    work = tmp_path / "council" / "runs" / "council-dup"
    work.mkdir(parents=True)
    runner.write_origin(work)
    _runner_progress(runner, work, "arguments", seats=1)
    ad = LiveAdapter()
    (task,) = _watcher(tmp_path, ad).scan_once()
    summary = dict(SUMMARY, verdict="UNIQUE-VERDICT-TEXT", attack_severity="", ruling="")
    (work / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    _runner_progress(runner, work, "chairman", status="done", seats=5)
    await asyncio.wait_for(task, 5)

    # what the launching agent would read from the runner's stdout
    assert (work / "gateway-card.json").exists()
    agent_sees = "\n".join(runner.final_stdout_lines(0, work / "report.md", summary))
    channels = [m for m in ad.live.values() if "UNIQUE-VERDICT-TEXT" in m] + (
        [agent_sees] if "UNIQUE-VERDICT-TEXT" in agent_sees else []
    )
    assert len(channels) == 1

@pytest.mark.asyncio
async def test_gateway_restarts_mid_council_delivers_once_and_sweeps_live_card(tmp_path, monkeypatch):
    for key, value in SESSION_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(da, "_board_store_path", lambda: tmp_path / "boards.json")
    runner = _load_runner()
    work = tmp_path / "council" / "runs" / "council-restart"
    work.mkdir(parents=True)
    runner.write_origin(work)
    (work / "question.txt").write_text("Restart test?", encoding="utf-8")
    _runner_progress(runner, work, "arguments", seats=1)
    ad = LiveAdapter()
    card = cc.CouncilLiveCard(ad, "123", work.name, work=work, question="Restart test?", mode="full")
    await card.publish(_state())
    live_id = card.message_id
    card._unregister()  # old gateway is gone, persisted id survives
    cc._CARD_IDS.clear()
    cc._ORPHANS_SWEPT.clear()
    assert live_id in ad.live and not (work / "gateway-card.json").exists()
    summary = dict(SUMMARY, verdict="RESTART-VERDICT", attack_severity="", ruling="")
    (work / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    _runner_progress(runner, work, "chairman", status="done", seats=5)
    watcher = _watcher(tmp_path, ad)
    (task,) = watcher.scan_once()
    await asyncio.wait_for(task, 5)
    assert live_id not in ad.live
    assert len(ad.council_messages()) == 1
    assert "RESTART-VERDICT" in ad.council_messages()[0]
    assert not any("tap to watch" in m for m in ad.live.values())
    assert (work / "gateway-card.json").exists()
    agent_sees = "\n".join(runner.final_stdout_lines(0, work / "report.md", summary))
    assert "RESTART-VERDICT" not in agent_sees and "COUNCIL_DONE" in agent_sees
    assert watcher.scan_once() == []
    assert _watcher(tmp_path, ad).scan_once() == []  # next restart: ack blocks replay
    assert len(ad.council_messages()) == 1


# ── rollback + config ──────────────────────────────────────────────────────


def test_classic_style_still_renders_todays_card():
    card = make_card("classic", object(), "c", "run-9", None, work=Path("/nonexistent/a/b/c"))
    assert isinstance(card, CouncilCard) and card.owns_final is False
    state = {"status": "running", "mode": "full", "stage": "cross_review", "seat_done": 5,
             "seat_total": 5, "review_done": 3, "review_total": 5, "elapsed_s": 40}
    assert render_council_card(state) == (
        "🏛 Council · Full\n✓ Opening arguments 5/5\n◉ Cross-review 3/5\n○ Chairman"
    )
    assert render_council_card(dict(state, status="done")) == "🏛 Council · 5 seats · 3 reviews · 40s"


def test_council_card_setting_defaults_to_v2_and_accepts_classic():
    from gateway.display_config import resolve_display_setting

    assert resolve_display_setting({}, "telegram", "council_card", "v2") == "v2"
    cfg = {"display": {"council_card": "classic"}}
    assert resolve_card_style(resolve_display_setting(cfg, "telegram", "council_card", "v2")) == "classic"
    cfg = {"display": {"council_card": "classic", "platforms": {"telegram": {"council_card": "v2"}}}}
    assert resolve_display_setting(cfg, "telegram", "council_card", "v2") == "v2"
    assert resolve_card_style("nonsense") == "v2"


def test_question_is_trimmed_on_a_word_boundary_without_paths():
    long = "Should we migrate the billing system " * 10 + "(see /srv/docs/plan.md)"
    out = cc.clean_question(long)
    assert len(out) <= cc.QUESTION_MAX + 1 and out.endswith("…")
    assert "/srv" not in out and not out[:-1].endswith(" ")
    assert cc.clean_question("Plain question?") == "Plain question?"
