"""Behavior tests for the built-in ``clover`` skin ("Clo" the clover sprite)."""

import io
import sys
from datetime import datetime

import pytest

from agent import clover_flavor
from agent.display import (
    KawaiiSpinner,
    get_cute_tool_message,
    get_done_mark,
    get_fail_mark,
    get_tool_emoji,
)
from clover_cli import skin_engine
from clover_cli.config import DEFAULT_CONFIG

LEAF, LUCKY = "☘️", "🍀"


class FakeRng:
    """Deterministic stand-in: ``lucky`` for the 1-in-50 roll, ``mix`` for the 1-in-3 mix."""

    def __init__(self, lucky=1, mix=1):
        self.lucky, self.mix = lucky, mix
        self.calls = []

    def randrange(self, n):
        self.calls.append(n)
        return self.lucky if n == clover_flavor.LUCKY_ODDS else self.mix

    def choice(self, seq):
        return seq[0]


class Clock:
    def __init__(self, hour):
        self.hour = hour

    def __call__(self):
        return datetime(2026, 1, 1, self.hour, 0)


class Mono:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


FACES = ["☘️(◕ᴗ◕✿)"]
VERBS = ["watering the clovers"]


@pytest.fixture(autouse=True)
def clover_active(monkeypatch):
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", skin_engine.DEFAULT_SKIN_NAME)
    clover_flavor.reset()
    yield
    clover_flavor.reset()


def _turn(**kw):
    kw.setdefault("rng", FakeRng())
    kw.setdefault("clock", Clock(13))
    kw.setdefault("monotonic", Mono())
    return clover_flavor.begin_turn(**kw)


# --- default + explicit skin -------------------------------------------------


def test_default_skin_is_clover():
    assert DEFAULT_CONFIG["display"]["skin"] == "clover"
    skin_engine.init_skin_from_config({})
    assert skin_engine.get_active_skin().name == "clover"
    skin_engine.init_skin_from_config({"display": {"skin": ""}})
    assert skin_engine.get_active_skin().name == "clover"
    assert skin_engine.get_active_skin_name() == "clover"


@pytest.mark.parametrize("name", ["default", "ares", "mono"])
def test_explicit_skin_wins(name):
    skin_engine.init_skin_from_config({"display": {"skin": name}})
    assert skin_engine.get_active_skin().name == name
    assert skin_engine.get_active_skin().spinner.get("flavor") is None
    assert get_tool_emoji("terminal") != "🪵"


def test_clover_skin_uses_leaf_faces_and_verbs():
    skin = skin_engine.load_skin("clover")
    assert all(f.startswith(LEAF) for f in skin.spinner["thinking_faces"])
    assert all(f.startswith(LEAF) for f in skin.spinner["waiting_faces"])
    assert "counting leaves (1, 2, 3... 4?!)" in KawaiiSpinner.get_thinking_verbs()
    assert KawaiiSpinner.get_thinking_faces() == skin.spinner["thinking_faces"]


# --- lucky turn ---------------------------------------------------------------


def test_lucky_roll_happens_once_per_turn():
    rng = FakeRng(lucky=0)
    turn = _turn(rng=rng)
    assert turn.lucky
    face1, verb1 = turn.status(FACES, VERBS)
    face2, verb2 = turn.status(FACES, VERBS)
    turn.face(FACES)
    assert rng.calls.count(clover_flavor.LUCKY_ODDS) == 1
    assert face1.startswith(LUCKY) and face2.startswith(LUCKY)
    assert not face1.startswith(LEAF)
    assert verb1 == "lucky turn!"  # first spinner line only
    assert verb2 == VERBS[0]
    assert turn.face(FACES).startswith(LUCKY)


def test_unlucky_turn_keeps_three_leaf_prefix():
    turn = _turn(rng=FakeRng(lucky=7))
    assert not turn.lucky
    face, verb = turn.status(FACES, VERBS)
    assert face.startswith(LEAF) and verb == VERBS[0]


def test_next_turn_rerolls():
    _turn(rng=FakeRng(lucky=0))
    second = _turn(rng=FakeRng(lucky=3))
    assert not second.lucky


def test_pick_thinking_routes_through_turn():
    _turn(rng=FakeRng(lucky=0))
    face, verb = KawaiiSpinner.pick_thinking()
    assert face.startswith(LUCKY) and verb == "lucky turn!"
    assert KawaiiSpinner.pick_waiting_face().startswith(LUCKY)


# --- growing spinner ----------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed,frame",
    [(0, "🫘"), (4.9, "🫘"), (5, "🌱"), (19.9, "🌱"), (20, "🌿"), (59.9, "🌿"), (60, "☘️"), (900, "☘️")],
)
def test_growth_frames_by_elapsed(elapsed, frame):
    assert clover_flavor.growth_frame(elapsed) == frame


def test_spinner_uses_growth_mode_under_clover_skin():
    spinner = KawaiiSpinner("thinking", spinner_type="brain")
    assert spinner.spinner_type == "clover_growth"
    assert spinner.spinner_frames == ["🫘", "🌱", "🌿", "☘️"]


def test_other_skins_keep_their_spinner_type():
    skin_engine.set_active_skin("default")
    assert KawaiiSpinner("x", spinner_type="moon").spinner_type == "moon"


# --- time of day / long task ----------------------------------------------------


@pytest.mark.parametrize(
    "hour,expected",
    [(0, ("☘️(－_－)zzZ", "night shift")), (5, ("☘️(－_－)zzZ", "night shift")),
     (6, ("☘️(◕ᴗ◕)☀️", "morning dew")), (9, ("☘️(◕ᴗ◕)☀️", "morning dew"))],
)
def test_time_of_day_status_when_mix_hits(hour, expected):
    turn = _turn(rng=FakeRng(mix=0), clock=Clock(hour))
    assert turn.status(FACES, VERBS) == expected


@pytest.mark.parametrize("hour", [10, 13, 23])
def test_no_time_of_day_status_outside_windows(hour):
    turn = _turn(rng=FakeRng(mix=0), clock=Clock(hour))
    assert turn.status(FACES, VERBS) == (FACES[0], VERBS[0])


def test_time_of_day_is_mixed_not_constant():
    turn = _turn(rng=FakeRng(mix=1), clock=Clock(3))
    assert turn.status(FACES, VERBS) == (FACES[0], VERBS[0])


def test_long_task_status_after_three_minutes():
    mono = Mono()
    turn = _turn(rng=FakeRng(mix=0), clock=Clock(13), monotonic=mono)
    assert turn.status(FACES, VERBS) == (FACES[0], VERBS[0])
    mono.t = 181
    assert turn.status(FACES, VERBS) == ("☘️(；￣Д￣)", "still digging")
    mono.t = 100
    assert turn.status(FACES, VERBS) == (FACES[0], VERBS[0])


def test_long_task_wins_over_time_of_day():
    mono = Mono()
    turn = _turn(rng=FakeRng(mix=0), clock=Clock(3), monotonic=mono)
    mono.t = 181
    assert turn.status(FACES, VERBS)[1] == "still digging"


def test_special_faces_follow_lucky_prefix():
    turn = _turn(rng=FakeRng(lucky=0, mix=0), clock=Clock(3))
    face, verb = turn.status(FACES, VERBS)
    assert face == "🍀(－_－)zzZ" and verb == "lucky turn!"


# --- errors ---------------------------------------------------------------------


def test_error_then_recovery_status():
    turn = _turn()
    turn.note_tool_result(True)
    assert turn.status(FACES, VERBS) == ("☘️(╥﹏╥)", "bad luck, retrying")
    assert turn.status(FACES, VERBS) == (FACES[0], VERBS[0])  # one-shot
    turn.note_tool_result(False)
    assert turn.status(FACES, VERBS) == ("☘️(ﾉ◕ヮ◕)ﾉ", "luck's back!")
    assert turn.status(FACES, VERBS) == (FACES[0], VERBS[0])


def test_success_without_prior_error_is_quiet():
    turn = _turn()
    turn.note_tool_result(False)
    assert turn.status(FACES, VERBS) == (FACES[0], VERBS[0])


def test_waiting_face_does_not_swallow_error_status():
    turn = _turn()
    turn.note_tool_result(True)
    turn.face(FACES)
    assert turn.status(FACES, VERBS)[1] == "bad luck, retrying"


def test_module_level_note_reaches_current_turn():
    _turn()
    clover_flavor.note_tool_result(True)
    assert KawaiiSpinner.pick_thinking()[1] == "bad luck, retrying"


# --- tool emojis ------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,emoji",
    [("terminal", "🪵"), ("read_file", "🍃"), ("write_file", "🌱"), ("patch", "✂️"),
     ("search_files", "🍄"), ("web_search", "🔭"), ("browser_click", "🧭"),
     ("browser_navigate", "🧭"), ("browser_snapshot", "📸"), ("browser_vision", "👁️"),
     ("web_extract", "📜"), ("execute_code", "⚗️"), ("delegate_task", "🌿"),
     ("memory", "🌰"), ("session_search", "🍂"), ("skill_view", "🧺"),
     ("skills_list", "🧺"), ("skill_manage", "🧺"), ("todo", "🌻"), ("cronjob", "⏳"),
     ("clarify", "🌼"), ("process", "🫖"), ("vision_analyze", "🐞"),
     ("text_to_speech", "🎶"), ("image_generate", "🌸")],
)
def test_clover_tool_emojis(tool, emoji):
    assert get_tool_emoji(tool) == emoji


def test_unlisted_tools_keep_registry_or_default_emoji():
    assert get_tool_emoji("no_such_tool_xyz", default="⚡") == "⚡"


def test_gateway_generic_fallback_is_a_leaf_under_clover_only():
    assert get_tool_emoji("no_such_tool_xyz", default="⚙️") == "☘️"
    skin_engine.set_active_skin("default")
    assert get_tool_emoji("no_such_tool_xyz", default="⚙️") == "⚙️"


def test_completion_line_follows_skin_emoji():
    line = get_cute_tool_message("terminal", {"command": "ls"}, 1.0)
    assert "🪵" in line and "💻" not in line
    skin_engine.set_active_skin("default")
    assert "💻" in get_cute_tool_message("terminal", {"command": "ls"}, 1.0)


def test_xai_video_tools_have_a_real_emoji():
    import tools.xai_video_tools  # noqa: F401  (registers on import)
    from tools.registry import registry

    skin_engine.set_active_skin("default")
    for name in ("xai_video_edit", "xai_video_extend"):
        assert registry.get_emoji(name) == "🎬"
        assert get_tool_emoji(name) == "🎬"


# --- done / failed marks -------------------------------------------------------------


def test_done_and_failed_marks_follow_skin():
    assert (get_done_mark(), get_fail_mark()) == ("🍀", "🥀")
    skin_engine.set_active_skin("default")
    assert (get_done_mark(), get_fail_mark()) == ("✅", "❌")


def test_delegation_card_marks_follow_skin():
    from agent.delegation_activity import _state_icon, group_icon

    class Child:
        state = "completed"

    assert _state_icon("completed") == "🍀"
    assert _state_icon("failed") == "🥀"
    assert group_icon([Child()]) == "🍀"
    skin_engine.set_active_skin("default")
    assert _state_icon("completed") == "✅"
    assert group_icon([Child()]) == "✅"


# --- legacy consoles ----------------------------------------------------------------


def _use_cp1252_console(monkeypatch):
    # Patched inside the test body: pytest's capture re-installs sys.stdout
    # between fixture setup and the call phase.
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    return stream


def test_emoji_safe_detects_encoding():
    assert clover_flavor.emoji_safe(io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    assert not clover_flavor.emoji_safe(io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))


def test_cp1252_console_falls_back_to_classic_look(monkeypatch):
    _use_cp1252_console(monkeypatch)
    faces = KawaiiSpinner.get_thinking_faces()
    verbs = KawaiiSpinner.get_thinking_verbs()
    assert faces == KawaiiSpinner.KAWAII_THINKING
    assert verbs == KawaiiSpinner.THINKING_VERBS
    assert KawaiiSpinner.get_waiting_faces() == KawaiiSpinner.KAWAII_WAITING
    assert get_tool_emoji("terminal") != "🪵"
    assert get_tool_emoji("no_such_tool_xyz", default="⚙️") == "⚙️"
    assert (get_done_mark(), get_fail_mark()) == ("✅", "❌")
    assert KawaiiSpinner("x", spinner_type="moon").spinner_type == "moon"
    _turn(rng=FakeRng(lucky=0))
    face, verb = KawaiiSpinner.pick_thinking()
    assert face in KawaiiSpinner.KAWAII_THINKING
    assert verb in KawaiiSpinner.THINKING_VERBS
    assert not face.startswith(LUCKY) and verb != "lucky turn!"


def test_utf8_console_keeps_clover_look():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    assert clover_flavor.emoji_safe(stream)
