"""Clo-style background self-improvement notice (clover skin only)."""

import json
import random
from types import SimpleNamespace

import pytest

from agent import clover_flavor
from agent.background_review import (
    _clover_review_notice,
    summarize_background_review_actions,
)
from clover_cli import skin_engine


@pytest.fixture(autouse=True)
def _clover_skin(monkeypatch):
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", skin_engine.DEFAULT_SKIN_NAME)
    skin_engine.set_active_skin("clover")
    clover_flavor.reset_review_notices()
    yield
    skin_engine.set_active_skin("default")
    clover_flavor.reset_review_notices()


def _box(message):
    """The chat notice is a fenced code box; return its lines."""
    assert message.startswith(FENCE + "\n") and message.endswith("\n" + FENCE), message
    return message[len(FENCE) + 1:-len(FENCE) - 1].split("\n")


FENCE = "`" * 3


_ids = iter(range(10_000))


def _call(tool, args, result):
    cid = f"call_{next(_ids)}"
    return [
        {"role": "assistant", "tool_calls": [
            {"id": cid, "function": {"name": tool, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": cid, "content": json.dumps(result)},
    ]


def _mem(action, target="memory", content="", old_text="", message="Entry added."):
    return _call("memory", {"action": action, "target": target, "content": content,
                            "old_text": old_text},
                 {"success": True, "message": message, "target": target})


def _skill(action, name, message, **extra):
    change = extra.pop("change", {})
    return _call("skill_manage", {"action": action, "name": name, **extra},
                 {"success": True, "message": message, "_change": change})


def _notice(msgs, mode="verbose", session="chat-1", **kw):
    agent = SimpleNamespace(session_id=session, memory_notifications=mode)
    return _clover_review_notice(agent, msgs, [])


def test_add_only_header_has_clover_and_items_use_brain_and_dna():
    msgs = _mem("add", content="Likes tea") + _mem("add", "user", content="Name is Ant") \
        + _skill("create", "tea-brewing", "Skill created.", change={"description": "d"})
    cli, message = _notice(msgs)
    header, *items = _box(message)
    assert header.startswith("☘️(") and "🍀" in header and "🧹" not in header
    assert items == ["🧠 Likes tea", "🫶 about you: Name is Ant", "🧬 new skill: tea-brewing"]
    assert cli[1:] == ["🧠 Likes tea", "🫶 about you: Name is Ant", "🧬 new skill: tea-brewing"]


def test_remove_only_header_uses_broom():
    msgs = _mem("remove", old_text="Old note", message="Entry removed.") \
        + _mem("remove", "user", old_text="Old fact", message="Entry removed.")
    _, message = _notice(msgs)
    header, *items = _box(message)
    assert "🧹" in header and "🍀" not in header
    assert items == ["🧹 Old note", "🧹 Old fact"]


def test_mixed_replace_skill_improved_and_removed():
    msgs = _mem("replace", content="Prefers mate", old_text="tea", message="Entry replaced.") \
        + _skill("patch", "demo", "Patched SKILL.md in skill 'demo'.", change={"old": "a", "new": "b"}) \
        + _skill("delete", "stale", "Skill 'stale' deleted.") \
        + _mem("remove", old_text="Junk", message="Entry removed.")
    _, message = _notice(msgs)
    header, *items = _box(message)
    assert "🍀" in header  # something was saved, so saved wins over tidy
    assert items == ["🧠 updated: Prefers mate", "🧬 improved: demo",
                     "🧹 removed skill: stale", "🧹 Junk"]


def test_skill_rewritten_counts_as_improved():
    msgs = _skill("edit", "demo", "Skill updated.", change={"description": "new body"})
    _, message = _notice(msgs)
    assert _box(message)[1] == "🧬 improved: demo"


def test_previews_lose_stray_asterisks_and_newlines():
    msgs = _mem("add", content="**bold** note\nsecond line*")
    _, message = _notice(msgs)
    assert _box(message)[1] == "🧠 bold** note second line"
    assert _box(_notice(_mem("add", content="**wrapped**"))[1])[1] == "🧠 wrapped"


def test_default_mode_falls_back_to_generic_lines():
    msgs = _mem("add") + _skill("create", "s1", "Skill 's1' created.")
    _, message = _notice(msgs, mode="on")
    assert _box(message)[1:] == ["🧠 a note for later", "🧬 new skill: s1"]


def test_off_mode_produces_nothing_to_render():
    items = []
    assert summarize_background_review_actions(_mem("add", content="x"), [],
                                               notification_mode="off", structured=items) == []
    assert items == []


def test_no_back_to_back_header_repeat_per_chat():
    rng = random.Random(7)
    heads = [clover_flavor.render_review_notice([("memory", "add", "x")], "chat-a", rng)[1]
             .split("\n")[1] for _ in range(60)]
    assert all(a != b for a, b in zip(heads, heads[1:]))
    tidy = [clover_flavor.render_review_notice([("memory", "remove", "x")], "chat-b", rng)[1]
            .split("\n")[1] for _ in range(60)]
    assert all(a != b for a, b in zip(tidy, tidy[1:]))


def test_no_repeat_even_with_a_stuck_rng():
    class Stuck:
        def choice(self, seq):
            return seq[0]

    heads = [clover_flavor.render_review_notice([("memory", "add", "x")], "c", Stuck())[1]
             .split("\n")[1] for _ in range(5)]
    assert all(a != b for a, b in zip(heads, heads[1:]))


def test_default_skin_keeps_todays_text_byte_for_byte():
    skin_engine.set_active_skin("default")
    msgs = _mem("add", content="Likes tea") + _skill("create", "s1", "Skill created.",
                                                     change={"description": "brews"})
    assert _notice(msgs) is None
    assert summarize_background_review_actions(msgs, [], notification_mode="verbose") == [
        "Memory ➕ Likes tea", "📝 Skill 's1' created: brews"]
    assert summarize_background_review_actions(_mem("add"), [], notification_mode="on") == [
        "Memory updated"]


def test_structured_collection_does_not_change_returned_actions():
    msgs = _mem("add", content="a") + _mem("remove", old_text="b", message="Entry removed.") \
        + _skill("patch", "n", "Patched SKILL.md in skill 'n'.", change={"old": "x", "new": "y"})
    for mode in ("on", "verbose"):
        plain = summarize_background_review_actions(msgs, [], notification_mode=mode)
        items = []
        assert summarize_background_review_actions(
            msgs, [], notification_mode=mode, structured=items) == plain
        assert len(items) == len(plain)


# --- each kind of change reads differently -------------------------------------------

def _header(msgs, mode="on"):
    return _box(_notice(msgs, mode=mode)[1])[0]


def test_profile_only_reads_as_about_you_and_differs_from_a_note():
    pools = clover_flavor.REVIEW_POOLS
    user_hdr = _header(_mem("add", "user"))
    assert any(user_hdr.endswith(line) for line in pools["user"][1])
    assert _box(_notice(_mem("add", "user"), mode="on")[1])[1] == "🫶 something about you"
    mem_hdr = _header(_mem("add"))
    assert any(mem_hdr.endswith(line) for line in pools["memory"][1])
    assert _box(_notice(_mem("add"), mode="on")[1])[1] == "🧠 a note for later"


def test_skill_only_and_mixed_headers():
    pools = clover_flavor.REVIEW_POOLS
    hdr = _header(_skill("create", "s1", "Skill 's1' created."))
    assert any(hdr.endswith(line) for line in pools["skill"][1])
    hdr = _header(_mem("add") + _skill("create", "s1", "Skill 's1' created."))
    assert any(hdr.endswith(line) for line in pools["mixed"][1])


def test_header_pools_do_not_share_lines():
    seen = [line for _, lines in clover_flavor.REVIEW_POOLS.values() for line in lines]
    assert len(seen) == len(set(seen))


def test_chat_notice_is_a_code_box_that_backticks_cannot_break():
    msg = clover_flavor.render_review_notice([("memory", "add", "uses ``` fences")], "box")[1]
    lines = _box(msg)
    assert msg.count(FENCE) == 2
    assert lines[1] == "🧠 uses \'\'\' fences"
