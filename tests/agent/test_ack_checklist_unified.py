"""The unified helper card: one message with steps, live tool calls and result."""

from __future__ import annotations

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl

pytestmark = pytest.mark.unified_card

HEAD = "\U0001f504 fixing the bugs \u2192 test build + PR, ~10\u201325 min"
STEPS = ("find", "fix", "test")
SPACER = "\u2800"


@pytest.fixture(autouse=True)
def _clean():
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    yield
    ac.reset_for_tests()


def _card(did="d1", say="", steps=STEPS, header=HEAD):
    r = fl.DispatchReceipt(
        delegation_id=did, seq=1, goals=("g",), summary=header, steps=tuple(steps), say=say
    )
    return ac.card_for([r])


def _todos(*statuses):
    return [{"id": f"s{i + 1}", "status": s} for i, s in enumerate(statuses)]


def _running_card(did="d1"):
    card = _card(did, say="on it")
    kw = dict(delegation_id=did, model="claude-sonnet-5-5")
    ac.on_child_event("subagent.start", **kw)
    ac.on_child_event("subagent.todo", delegation_id=did, todos=_todos("completed", "in_progress", "pending"))
    ac.on_child_event(
        "subagent.tool", tool_name="terminal", preview="pytest -q",
        args={"command": "pytest -q"}, tool_count=14, **kw,
    )
    return card


def test_running_card_shape():
    card = _running_card()
    cl = card.parts[0].checklist
    cl.started -= 372  # 6m12s of work
    assert card.render().splitlines() == [
        "on it",
        "",
        "\U0001f500 **fixing the bugs**",
        "> Goal: test build + PR \u00b7 ~10\u201325 min",
        "> Sonnet 5.5 \u00b7 14 calls \u00b7 6m12s",
        f"> {SPACER}",
        "> \u2713 find",
        "> \u25b8 fix",
        "> \u25cb test",
        f"> {SPACER}",
        "> *now: pytest -q*",
    ]


def test_initial_text_has_the_say_title_outcome_and_steps_but_no_activity():
    card = _card(say="got it")
    lines = card.initial_text.splitlines()
    assert lines[:3] == ["got it", "", "\U0001f500 **fixing the bugs**"]
    assert lines[3] == "> Goal: test build + PR \u00b7 ~10\u201325 min"
    assert "now:" not in card.initial_text and "call" not in card.initial_text


def test_card_without_steps_is_header_and_outcome_only():
    card = _card(steps=())
    assert card.initial_text.splitlines() == [
        "\U0001f500 **fixing the bugs**",
        "> Goal: test build + PR \u00b7 ~10\u201325 min",
    ]


def test_done_flips_header_drops_eta_and_swaps_tool_line_for_summary():
    card = _running_card()
    cl = card.parts[0].checklist
    ac.on_child_event(
        "subagent.tool_done", delegation_id="d1", tool_name="terminal", tool_count=31,
    )
    ac.on_child_event(
        "subagent.complete", delegation_id="d1", status="completed", outcome="done",
        summary="did stuff\nResult: done\nPlain summary: The fix is in and tests pass.",
    )
    cl.ended = cl.started + 840
    lines = card.render().splitlines()
    assert lines[2] == "\U0001f500 **fixing the bugs** \u00b7 done"
    assert lines[3] == "> Goal: test build + PR", "the ETA goes away"
    assert lines[4] == "> Sonnet 5.5 \u00b7 31 calls \u00b7 14m"
    assert lines[6:9] == ["> \u2713 find", "> \u2713 fix", "> \u2713 test"]
    assert lines[-1] == "> The fix is in and tests pass."
    assert "now:" not in card.render() and "before:" not in card.render(), "no live tool line once finished"
    assert "~" not in card.render()


def test_done_clock_stops_at_the_end_time():
    card = _running_card()
    cl = card.parts[0].checklist
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed", summary="Plain summary: ok.")
    frozen = cl.render_unified(now=cl.started + 5)
    later = cl.render_unified(now=cl.started + 5000)
    assert frozen == later


def test_failed_card_marks_the_running_step_and_keeps_the_reason():
    card = _running_card()
    ac.on_child_event(
        "subagent.complete", delegation_id="d1", status="completed", outcome="not_done",
        summary="Result: not done\nPlain summary: The folder does not exist.",
    )
    lines = card.render().splitlines()
    assert lines[2] == "\U0001f342 **fixing the bugs** \u00b7 failed"
    assert "> \u2713 find" in lines and "> \u2717 fix" in lines and "> \u25cb test" in lines
    assert "> The folder does not exist." in lines
    assert "~" not in lines[3]


def test_failed_status_gives_the_failed_header():
    card = _running_card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="failed", summary="boom")
    assert card.render().splitlines()[2] == "\U0001f342 **fixing the bugs** \u00b7 failed"


def test_stopped_card_shape():
    card = _running_card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="interrupted")
    lines = card.render().splitlines()
    assert lines[2] == "\U0001f342 **fixing the bugs** \u00b7 stopped"
    assert "> \u2713 find" in lines and "> \u2013 fix" in lines and "> \u25cb test" in lines
    assert "now:" not in card.render()
    assert "~" not in lines[3]


def test_activity_events_update_calls_doing_and_model():
    card = _card()
    cl = card.parts[0].checklist
    assert (cl.model, cl.calls, cl.doing) == ("", 0, "")
    assert ac.on_child_event("subagent.start", delegation_id="d1", model="claude-opus-5-5") is True
    assert cl.model == "Opus 5.5" and cl.started is not None
    assert ac.on_child_event(
        "subagent.tool", delegation_id="d1", tool_name="read_file", preview="a.py",
        args={"path": "a.py"}, tool_count=3,
    ) is True
    assert cl.calls == 3 and cl.doing.startswith("read_file")
    # the tool count only moves forward, and the model is set once
    ac.on_child_event("subagent.tool_done", delegation_id="d1", tool_name="read_file", tool_count=2, model="gpt-6-sol")
    assert cl.calls == 3 and cl.model == "Opus 5.5"
    ac.on_child_event("subagent.tool_done", delegation_id="d1", tool_name="read_file", tool_count=4)
    assert cl.calls == 4
    assert "Opus 5.5 \u00b7 4 calls" in card.render()


def test_one_tool_call_is_singular():
    card = _card()
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="terminal", tool_count=1)
    text = card.render()
    assert "1 call " in text and "1 calls" not in text


def test_unchanged_activity_reports_no_change():
    _card()
    kw = dict(delegation_id="d1", tool_name="terminal", preview="ls", tool_count=2, tool_call_id="c1")
    assert ac.on_child_event("subagent.tool", **kw) is True
    assert ac.on_child_event("subagent.tool", **kw) is False


def test_activity_before_the_part_exists_is_ignored_safely():
    assert ac.on_child_event("subagent.start", delegation_id="nope", model="x") is False
    assert ac.on_child_event("subagent.tool", delegation_id="nope", tool_name="t", tool_count=9) is False
    assert ac.on_child_event("subagent.tool", tool_name="t") is False  # no id at all
    card = _card()
    cl = card.parts[0].checklist
    assert cl.calls == 0 and cl.doing == "", "nothing was buffered for later"


def test_activity_after_finish_does_not_change_the_card():
    card = _running_card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed", summary="Plain summary: ok.")
    done = card.render()
    assert ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="t", tool_count=99) is False
    assert card.render() == done


def test_activity_touches_the_card_only_when_unified():
    card = _card()
    touched = []
    card.on_change = lambda: touched.append(1)
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="terminal", preview="ls", tool_count=1)
    assert touched == [1]
    ac.set_style_for_tests("classic")
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="terminal", preview="pwd", tool_count=2)
    assert touched == [1], "a classic card does not edit on activity"
    assert card.parts[0].checklist.calls == 2, "but the numbers are kept for a later switch"


def test_owns_delegation_is_true_only_for_a_known_id_under_unified():
    _card()
    assert ac.owns_delegation("d1") is True
    assert ac.owns_delegation("other") is False
    assert ac.owns_delegation("") is False
    assert ac.owns_delegation(None) is False
    ac.set_style_for_tests("classic")
    assert ac.owns_delegation("d1") is False


def test_classic_render_is_the_plain_checklist():
    card = _running_card()
    ac.set_style_for_tests("classic")
    assert card.render().splitlines() == [
        "on it", "", HEAD,
        "\U0001f340 find", "\U0001f504 fix", "\u2b1c test",
    ]


def test_events_before_the_receipt_still_replay_into_the_unified_card():
    ac.on_child_event("subagent.todo", delegation_id="d9", todos=_todos("completed", "in_progress", "pending"))
    ac.on_child_event("subagent.complete", delegation_id="d9", status="completed", summary="Plain summary: early.")
    card = _card("d9")
    lines = card.render().splitlines()
    assert lines[0] == "\U0001f500 **fixing the bugs** \u00b7 done"
    assert "> early." in lines


def test_match_unbound_still_finds_the_card_after_the_clock_moves():
    card = _card(say="hi")
    ac.on_child_event("subagent.start", delegation_id="d1", model="claude-opus-5-5")
    assert ac.match_unbound(card.initial_text) is card


def test_ack_text_is_the_stable_initial_text(monkeypatch):
    from agent import router_backstop as rb

    r = fl.DispatchReceipt(delegation_id="d1", seq=1, goals=("g",), summary=HEAD, steps=STEPS)
    text = rb._ack_text([r])
    card = ac.card_for([r])
    assert text == card.initial_text
    ac.on_child_event("subagent.start", delegation_id="d1", model="claude-opus-5-5")
    assert ac.match_unbound(text) is card


def test_markdown_in_title_and_summary_cannot_break_the_card():
    card = _card(header="\U0001f504 fixing *bold* things \u2192 a *b*, ~5 min", steps=("one",))
    ac.on_child_event(
        "subagent.complete", delegation_id="d1", status="completed",
        summary="Plain summary: It **works** now.",
    )
    text = card.render()
    assert "*bold*" not in text and "**works**" not in text
    assert text.splitlines()[0] == "\U0001f500 **fixing bold things** \u00b7 done"


def test_through_the_real_relay_a_child_tool_call_lands_on_the_card():
    from types import SimpleNamespace

    from tools.delegate_tool import _build_child_progress_callback

    card = _card()
    parent = SimpleNamespace(tool_progress_callback=lambda *a, **k: None, _delegate_spinner=None)
    cb = _build_child_progress_callback(
        0, "goal", parent, 1, subagent_id="sa-1", depth=0, model="claude-sonnet-5-5",
        session_ref={"delegation_id": "d1", "title": "t", "provider": "anthropic"},
    )
    cb("subagent.start", preview="goal")
    cb("tool.started", "terminal", "pytest -q", {"command": "pytest -q"})
    cl = card.parts[0].checklist
    assert cl.model == "Sonnet 5.5"
    assert cl.doing.startswith("terminal")


# -- the config switch ------------------------------------------------------


def _write_config(tmp_path, monkeypatch, text, reset_cache=True):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    try:  # the readonly loader may memoise by file mtime/size
        from clover_cli import config as cfg

        for name in ("_invalidate_config_cache", "invalidate_config_cache", "clear_config_cache"):
            fn = getattr(cfg, name, None)
            if callable(fn):
                fn()
    except Exception:
        pass
    if reset_cache:  # drop the test override and cache; read the real config
        ac.set_style_for_tests(None)


def test_default_style_is_unified(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "display:\n  tool_progress: all\n")
    monkeypatch.setattr(ac, "_STYLE_TTL", 0)
    assert ac.card_style() == "unified" and ac.unified() is True


def test_config_helper_card_classic_switches_the_style_back(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "_STYLE_TTL", 0)
    _write_config(tmp_path, monkeypatch, "display:\n  helper_card: classic\n")
    assert ac.card_style() == "classic" and ac.unified() is False
    card = _card()
    assert card.render().splitlines()[0] == HEAD, "classic header, not the bold title"
    assert ac.owns_delegation("d1") is False


def test_config_switch_takes_effect_after_the_cache_expires(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "display:\n  helper_card: classic\n")
    monkeypatch.setattr(ac, "_STYLE_TTL", 0)
    assert ac.card_style() == "classic"
    _write_config(tmp_path, monkeypatch, "display:\n  helper_card: unified\n", reset_cache=False)
    assert ac.card_style() == "unified"
    _write_config(tmp_path, monkeypatch, "display:\n  helper_card: CLASSIC \n", reset_cache=False)
    assert ac.card_style() == "classic", "case and spaces do not matter"


def test_unknown_value_falls_back_to_unified(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "_STYLE_TTL", 0)
    _write_config(tmp_path, monkeypatch, "display:\n  helper_card: sparkly\n")
    assert ac.card_style() == "unified"


def test_style_is_cached_inside_the_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "_STYLE_TTL", 3600)
    _write_config(tmp_path, monkeypatch, "display:\n  helper_card: classic\n")
    assert ac.card_style() == "classic"
    _write_config(tmp_path, monkeypatch, "display:\n  helper_card: unified\n", reset_cache=False)
    assert ac.card_style() == "classic", "still the cached answer"


def test_unreadable_config_means_unified(monkeypatch):
    import clover_cli.config as cfg

    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(cfg, "load_config_readonly", boom)
    monkeypatch.setattr(ac, "_STYLE_TTL", 0)
    ac.set_style_for_tests(None)
    assert ac.card_style() == "unified"


# -- recent tool calls ------------------------------------------------------


def _tool(did, name, preview, n, **kw):
    ac.on_child_event("subagent.tool", delegation_id=did, tool_name=name, preview=preview,
                      args={"command": preview}, tool_count=n, **kw)


def _done(did, name, n, error=False, **kw):
    ac.on_child_event("subagent.tool_done", delegation_id=did, tool_name=name, tool_count=n,
                      is_error=error, **kw)


_EM = "\u2003\u2003"  # a multi-subagent card indents everything under a subagent's name


def _flat(text):
    """The card text without the per-subagent indent (for shape checks)."""
    return text.replace(_EM, "")


def _tool_lines(text):
    text = _flat(text)
    return [ln[2:] for ln in text.splitlines() if ln[2:].startswith(("*now: ", "*before: "))]


def test_single_card_shows_the_open_tool_then_the_two_before_it_newest_first():
    card = _card()
    _tool("d1", "terminal", "ls", 1); _done("d1", "terminal", 1)
    _tool("d1", "terminal", "pytest -q", 2); _done("d1", "terminal", 2, error=True)
    _tool("d1", "read_file", "a.py", 3); _done("d1", "read_file", 3)
    _tool("d1", "web_search", "silas studios booking", 4)
    assert _tool_lines(card.render()) == [
        "*now: web_search silas studios booking*",
        "*before: read_file a.py*",
        "*before: pytest -q (failed)*",
    ]


def test_single_card_without_an_open_tool_shows_only_the_finished_ones_capped_at_three():
    card = _card()
    for i in range(5):
        _tool("d1", "terminal", f"cmd{i}", i + 1); _done("d1", "terminal", i + 1)
    assert _tool_lines(card.render()) == [
        "*before: cmd4*", "*before: cmd3*", "*before: cmd2*",
    ]


def test_tool_lines_come_after_the_steps_and_a_spacer_and_stats_sit_under_the_goal():
    card = _card()
    _tool("d1", "terminal", "ls", 1); _done("d1", "terminal", 1)
    _tool("d1", "read_file", "a.py", 2)
    lines = card.render().splitlines()
    assert lines[-3:] == [f"> {SPACER}", "> *now: read_file a.py*", "> *before: ls*"]
    assert lines[1] == "> Goal: test build + PR \u00b7 ~10\u201325 min"
    assert lines[2] == "> 2 calls \u00b7 0s"


def test_tool_done_pairs_by_call_id_before_name():
    card = _card()
    _tool("d1", "terminal", "first", 1, tool_call_id="a")
    _tool("d1", "terminal", "second", 2, tool_call_id="b")
    _done("d1", "terminal", 2, tool_call_id="a", error=True)
    assert _tool_lines(card.render()) == ["*now: second*", "*before: first (failed)*"]


def test_tool_done_without_an_id_pairs_the_newest_open_call_of_that_tool_then_the_newest_open():
    card = _card()
    _tool("d1", "read_file", "a.py", 1)
    _tool("d1", "terminal", "ls", 2)
    _tool("d1", "read_file", "b.py", 3)
    _done("d1", "read_file", 3)  # newest open read_file = b.py
    # one open tool shown ("now" = the newest open), then the finished one
    assert _tool_lines(card.render()) == ["*now: ls*", "*before: read_file b.py*"]
    _done("d1", "web_search", 3)  # unknown name: the newest open call (the terminal)
    assert _tool_lines(card.render()) == ["*now: read_file a.py*", "*before: ls*", "*before: read_file b.py*"]


def test_a_stray_tool_done_with_nothing_open_changes_nothing():
    card = _card()
    assert ac.on_child_event("subagent.tool_done", delegation_id="d1", tool_name="x", tool_count=0) is False
    assert _tool_lines(card.render()) == []


def test_each_helper_keeps_a_deque_of_three():
    card = _card()
    for i in range(6):
        _tool("d1", "terminal", f"c{i}", i + 1); _done("d1", "terminal", i + 1)
    assert [p for p, _ in card.parts[0].checklist.recent] == ["terminal c3", "terminal c4", "terminal c5"]


def test_finished_card_has_no_tool_lines_only_result_and_stats():
    card = _running_card()
    _done("d1", "terminal", 15)
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed",
                      summary="Plain summary: All good.")
    text = card.render()
    assert _tool_lines(text) == []
    assert "> All good." in text and "15 calls" in text


def test_tool_lines_use_the_same_phrase_as_before():
    card = _card()
    _tool("d1", "read_file", "/srv/site/contact.html", 1)
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="read_file",
                      preview="/srv/site/contact.html", args={"path": "/srv/site/contact.html"}, tool_count=2)
    assert "*now: read_file contact.html*" in card.render()


# -- several helpers: one unified card --------------------------------------


GOALS = ("Check nice and tidy site works. Then report.", "Review silas studios site booking flow",
         "Check the pizzeria bros pamphlet")
MHEAD = "\U0001f504 checking the 3 sites \u2192 outcome, ~5 min"


def _multi_card(did="dm", goals=GOALS, header=MHEAD, say="", steps=("ignored",)):
    r = fl.DispatchReceipt(delegation_id=did, seq=1, goals=tuple(goals), summary=header,
                           steps=tuple(steps), say=say)
    return ac.card_for([r])


def _kid(did, i, **kw):
    return dict(delegation_id=did, task_index=i, task_count=3, subagent_id=f"sa-{i}", **kw)


def _live_multi():
    """Helper 0 done, helper 1 running, helper 2 not started."""
    card = _multi_card(say="on it")
    ac.on_child_event("subagent.start", **_kid("dm", 0, model="claude-sonnet-5-5", title="nice & tidy site"))
    ac.on_child_event("subagent.tool", tool_name="terminal", preview="curl x", args={"command": "curl x"},
                      tool_count=9, **_kid("dm", 0))
    ac.on_child_event("subagent.complete", status="completed", outcome="done",
                      summary="Result: done\nPlain summary: All pages load and the quote form works.",
                      **_kid("dm", 0, title="nice & tidy site"))
    ac.on_child_event("subagent.start", **_kid("dm", 1, model="claude-sonnet-5-5", title="silas studios site"))
    _tool("dm", "read_file", "silasstudios.ca/contact", 3, **{"task_index": 1})
    _done("dm", "read_file", 3, task_index=1)
    ac.on_child_event("subagent.tool", tool_name="web_search", preview="silas studios booking",
                      args={"query": "silas studios booking"}, tool_count=4, **_kid("dm", 1))
    return card


def test_multi_card_shape_while_running():
    card = _live_multi()
    cl = card.parts[0].checklist
    cl.kids[0].started -= 100; cl.kids[0].ended = cl.kids[0].started + 100
    cl.kids[1].started -= 72
    lines = _flat(card.render()).splitlines()
    assert lines[:5] == [
        "on it", "", "\U0001f500 **checking the 3 sites** \u00b7 1 of 3 done",
        "> Goal: outcome \u00b7 ~5 min", f"> {SPACER}",
    ]
    assert "> **nice & tidy site** \u00b7 done \u00b7 Sonnet 5.5 \u00b7 9 calls \u00b7 1m40s" in lines
    assert "> All pages load and the quote form works." in lines
    assert "> **silas studios site** \u00b7 Sonnet 5.5 \u00b7 4 calls \u00b7 1m12s" in lines
    assert "> *now: web_search silas studios booking*" in lines
    assert "> *before: read_file contact*" in lines
    assert lines.index("> *now: web_search silas studios booking*") < lines.index(
        "> *before: read_file contact*")
    assert "> **Check the pizzeria bros pamphlet** \u00b7 waiting" in lines
    assert "waiting to start" not in "\n".join(lines)
    assert lines[-1].startswith("> *3 subagents \u00b7 13 calls \u00b7 ")
    assert lines[-2] == f"> {SPACER}"
    assert "ignored" not in _flat(card.render())


def test_multi_helper_without_a_title_uses_the_goal_then_a_number():
    card = _multi_card(goals=("Check nice and tidy site works. Then report.", ""))
    text = _flat(card.render())
    assert "**Check nice and tidy site works**" in text
    assert "**subagent 2**" in text


def test_child_title_from_the_event_beats_the_goal():
    card = _multi_card()
    ac.on_child_event("subagent.start", **_kid("dm", 2, title="pizza pamphlet", goal="whatever else"))
    assert "**pizza pamphlet**" in card.render()


def test_each_running_helper_shows_at_most_two_tool_lines():
    card = _multi_card(goals=("a", "b"))
    for n in range(4):
        _tool("dm", "terminal", f"cmd{n}", n + 1, task_index=1); _done("dm", "terminal", n + 1, task_index=1)
    _tool("dm", "web_search", "q", 5, task_index=1)
    assert _tool_lines(card.render()) == ["*now: web_search q*", "*before: cmd3*"]
    _done("dm", "web_search", 5, task_index=1)
    assert _tool_lines(card.render()) == ["*before: web_search q*", "*before: cmd3*"]


def test_tools_are_kept_per_helper():
    card = _multi_card(goals=("a", "b"))
    _tool("dm", "terminal", "one", 1, task_index=0)
    _tool("dm", "read_file", "two", 1, task_index=1, subagent_id="sa-1")
    kids = card.parts[0].checklist.kids
    assert [o[2] for o in kids[0].open] == ["terminal one"]
    assert [o[2] for o in kids[1].open] == ["read_file two"]


def test_children_are_found_by_subagent_id_when_there_is_no_task_index():
    card = _multi_card(goals=("a", "b"))
    ac.on_child_event("subagent.tool", delegation_id="dm", subagent_id="sa-9", tool_name="terminal",
                      preview="x", tool_count=1)
    ac.on_child_event("subagent.tool", delegation_id="dm", subagent_id="sa-8", tool_name="terminal",
                      preview="y", tool_count=1)
    ac.on_child_event("subagent.tool", delegation_id="dm", subagent_id="sa-9", tool_name="terminal",
                      preview="z", tool_count=2)
    kids = card.parts[0].checklist.kids
    assert [k.sid for k in kids] == ["sa-9", "sa-8"]
    assert kids[0].calls == 2 and kids[1].calls == 1


def test_multi_header_counts_done_helpers():
    card = _multi_card(goals=("a", "b", "c"))
    head = lambda: card.render().splitlines()[0]
    assert head().endswith("\u00b7 0 of 3 done")
    ac.on_child_event("subagent.complete", status="completed", summary="ok", **_kid("dm", 0))
    assert head().endswith("\u00b7 1 of 3 done")
    ac.on_child_event("subagent.complete", status="failed", summary="boom", **_kid("dm", 1))
    assert head().endswith("\u00b7 1 of 3 done"), "a failed helper is not done"


def test_multi_finished_all_ok_drops_the_count_and_the_eta():
    card = _live_multi()
    ac.on_child_event("subagent.complete", status="completed", outcome="done",
                      summary="Plain summary: Booking works.", **_kid("dm", 1))
    ac.on_child_event("subagent.complete", status="completed", summary="Plain summary: Pamphlet is fine.",
                      **_kid("dm", 2))
    cl = card.parts[0].checklist
    for k in cl.kids:
        k.started = k.started or cl.kids[0].started
        k.ended = cl.kids[0].started + 100
    cl.ended = cl.kids[0].started + 100
    lines = _flat(card.render()).splitlines()
    assert lines[2] == "\U0001f500 **checking the 3 sites** \u00b7 done"
    assert lines[3] == "> Goal: outcome", "the ETA goes away"
    assert [ln.split(" \u00b7 ")[0] for ln in lines if ln.startswith("> **") and " \u00b7 done" in ln] == [
        "> **nice & tidy site**", "> **silas studios site**", "> **Check the pizzeria bros pamphlet**",
    ]
    assert "> Booking works." in lines and "> Pamphlet is fine." in lines
    assert _tool_lines(_flat(card.render())) == []
    assert lines[-1].endswith("1m40s*") and "3 subagents" in lines[-1]
    assert card.finished


def test_multi_finished_with_one_failed_and_one_stopped_is_failed_and_shows_the_count():
    card = _multi_card(goals=("a", "b", "c"))
    ac.on_child_event("subagent.complete", status="completed", summary="Plain summary: fine.", **_kid("dm", 0))
    ac.on_child_event("subagent.complete", status="completed", outcome="not_done",
                      summary="Result: not done\nPlain summary: Site is down.", **_kid("dm", 1))
    ac.on_child_event("subagent.complete", status="interrupted", summary="", **_kid("dm", 2))
    lines = _flat(card.render()).splitlines()
    assert lines[0] == "\U0001f342 **checking the 3 sites** \u00b7 1 of 3 done \u00b7 1 failed \u00b7 1 stopped"
    heads = [ln for ln in lines if ln.startswith("> ") and "**" in ln]
    assert heads == ["> **a** \u00b7 done", "> \U0001f342 **b** \u00b7 failed", "> **c** \u00b7 stopped"]
    assert "> Site is down." in lines


def test_multi_stopped_without_a_failure_is_stopped():
    card = _multi_card(goals=("a", "b"))
    ac.on_child_event("subagent.complete", status="completed", summary="ok", **_kid("dm", 0))
    ac.on_child_event("subagent.complete", status="cancelled", **_kid("dm", 1))
    assert card.render().splitlines()[0] == "\U0001f342 **checking the 3 sites** \u00b7 1 of 2 done \u00b7 1 stopped"


def test_max_iterations_counts_as_failed_for_a_multi_helper():
    card = _multi_card(goals=("a", "b"))
    ac.on_child_event("subagent.complete", status="completed", exit_reason="max_iterations", **_kid("dm", 0))
    ac.on_child_event("subagent.complete", status="completed", summary="ok", **_kid("dm", 1))
    lines = card.render().splitlines()
    assert lines[0].startswith("\U0001f342 **") and "1 failed" in lines[0]
    assert "> \U0001f342 **a** \u00b7 failed" in lines


def test_multi_footer_totals_and_a_summary_cut_on_a_word():
    card = _multi_card(goals=("a", "b"))
    cl = card.parts[0].checklist
    ac.on_child_event("subagent.start", **_kid("dm", 0))
    ac.on_child_event("subagent.tool", tool_name="t", tool_count=5, **_kid("dm", 0))
    ac.on_child_event("subagent.tool", tool_name="t", tool_count=7, **_kid("dm", 1))
    long = "Plain summary: " + " ".join(["word"] * 80) + "."
    ac.on_child_event("subagent.complete", status="completed", summary=long, **_kid("dm", 0))
    cl.kids[0].started -= 50; cl.kids[1].started = cl.kids[0].started + 10
    lines = _flat(card.render()).splitlines()
    assert "> *2 subagents \u00b7 12 calls \u00b7 " in "\n".join(lines)
    summary = next(ln for ln in lines if ln.startswith("> word"))
    assert len(summary) <= 142 and summary.endswith("\u2026") and not summary.endswith(" \u2026")


def test_multi_complete_before_the_part_exists_is_replayed_per_child():
    ac.on_child_event("subagent.complete", delegation_id="dm", task_index=1, title="second", goal="g2",
                      subagent_id="sa-1", status="completed", outcome="not_done",
                      summary="Result: not done\nPlain summary: Nope.")
    card = _multi_card(goals=("g1", "g2"))
    lines = _flat(card.render()).splitlines()
    assert any(ln.startswith("> \U0001f342 **second** \u00b7 failed") for ln in lines) and "> Nope." in lines
    assert "> **g1** \u00b7 waiting" in lines


def test_multi_activity_before_the_part_exists_is_still_dropped():
    assert ac.on_child_event("subagent.tool", delegation_id="dm", task_index=0, tool_name="t", tool_count=3) is False
    card = _multi_card(goals=("a", "b"))
    assert card.parts[0].checklist.kids[0].calls == 0


def test_multi_events_touch_the_card():
    card = _multi_card(goals=("a", "b"))
    touched = []
    card.on_change = lambda: touched.append(1)
    ac.on_child_event("subagent.tool", delegation_id="dm", task_index=0, tool_name="t", tool_count=1)
    assert touched == [1]


def test_owns_delegation_is_true_for_a_multi_helper_dispatch():
    _multi_card()
    assert ac.owns_delegation("dm") is True
    ac.set_style_for_tests("classic")
    assert ac.owns_delegation("dm") is False


def test_classic_multi_dispatch_is_still_the_plain_line():
    card = _multi_card(header="\U0001f504 two jobs \u2192 both, ~5 min")
    ac.set_style_for_tests("classic")
    assert card.render() == "\U0001f504 two jobs \u2192 both, ~5 min"


def _no_emoji(text):
    """True when no character is an emoji, except the title's \U0001f500 (the card uses plain marks)."""
    return not any(ord(ch) >= 0x1F000 or ch in "\u2b1c\u23f9\u23f1" for ch in text.replace("\U0001f500 **", "**"))


def test_seven_helper_card_is_compact_and_under_the_telegram_limit():
    goals = tuple(f"Check site number {i}" for i in range(7))
    card = _multi_card(goals=goals)
    para = "Plain summary: " + " ".join(f"sentence{j}" for j in range(30)) + "."
    for i in range(7):
        ac.on_child_event("subagent.start", **_kid("dm", i, model="claude-sonnet-5-5"))
        ac.on_child_event("subagent.tool", tool_name="web_search", preview="q" * 70,
                          args={"query": "q" * 70}, tool_count=2, **_kid("dm", i))
    for i in range(7):
        ac.on_child_event("subagent.complete", status="completed", summary=para, **_kid("dm", i))
    text = _flat(card.render())
    assert len(text) < 4000
    assert text.count("**Check site number") == 7 and "7 subagents" in text
    # more than 6 helpers: a finished helper is ONE line, no stats, no emoji
    rows = [ln for ln in text.splitlines() if "**Check site number" in ln]
    assert len(rows) == 7 and all(len(r) < 90 and r.endswith("\u2026") for r in rows)
    assert all(r.startswith("> **Check site number") and " \u00b7 done \u00b7 " in r for r in rows)
    assert text.count(" calls") == 1, "only the totals line has stats"
    assert _no_emoji(text)


def test_six_helpers_still_get_the_full_layout():
    card = _multi_card(goals=tuple(f"job {i}" for i in range(6)))
    for i in range(6):
        ac.on_child_event("subagent.start", **_kid("dm", i, model="claude-sonnet-5-5"))
        ac.on_child_event("subagent.complete", status="completed", summary="Plain summary: ok.", **_kid("dm", i))
    text = _flat(card.render())
    assert text.count("\n> ok.") == 6 and text.count(" \u00b7 done \u00b7 Sonnet 5.5") == 6


def test_four_helper_summaries_are_cut_before_titles_when_the_budget_is_tight():
    goals = tuple(f"Check site number {i}" for i in range(4))
    card = _multi_card(goals=goals)
    para = "Plain summary: " + " ".join(f"sentence{j}" for j in range(30)) + "."
    for i in range(4):
        ac.on_child_event("subagent.start", **_kid("dm", i, model="claude-sonnet-5-5"))
        ac.on_child_event("subagent.tool", tool_name="web_search", preview="q" * 70,
                          args={"query": "q" * 70}, tool_count=2, **_kid("dm", i))
    for i in range(4):
        ac.on_child_event("subagent.complete", status="completed", summary=para, **_kid("dm", i))
    full = card.render()
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        mp.setattr(ac, "CARD_BUDGET", len(full) - 100)
        cut = card.render()
    finally:
        mp.undo()
    assert len(cut) < len(full)
    assert cut.count("**Check site number") == 4 and cut.count(" calls") == 5


def test_cut_summaries_come_before_tool_lines(monkeypatch):
    goals = tuple(f"job {i}" for i in range(3))
    card = _multi_card(goals=goals)
    for i in range(3):
        _tool("dm", "terminal", f"a{i}", 1, task_index=i); _done("dm", "terminal", 1, task_index=i)
        _tool("dm", "terminal", f"b{i}", 2, task_index=i)
    full = card.render()
    monkeypatch.setattr(ac, "CARD_BUDGET", len(full) - 10)
    cut = card.render()
    assert len(cut) < len(full) and cut.count("now: ") == 3 and cut.count("before: ") == 0


def test_two_unified_cards_in_one_message_are_separated_by_a_blank_line():
    r1 = fl.DispatchReceipt(delegation_id="a", seq=1, goals=("g",), summary=HEAD, steps=("one",))
    r2 = fl.DispatchReceipt(delegation_id="b", seq=2, goals=("g",), summary=HEAD, steps=("two",))
    card = ac.card_for([r1, r2])
    text = card.render()
    assert "\n\n\U0001f500 **fixing the bugs**" in text
    assert text.count("\U0001f500 **fixing the bugs**") == 2


# -- the helper's own todo bookkeeping is not a tool line ----------------------


def test_todo_is_not_shown_as_a_recent_or_open_tool_line():
    card = _running_card()
    did = "d1"
    kw = dict(delegation_id=did, model="claude-sonnet-5-5")
    ac.on_child_event("subagent.tool", tool_name="todo", preview="updating 3 task(s)",
                      args={"todos": []}, tool_call_id="t1", tool_count=15, **kw)
    cl = card.parts[0].checklist
    assert [name for _, name, _ in cl.open] == ["terminal"], "todo never becomes the doing line"
    assert "todo" not in cl.doing
    ac.on_child_event("subagent.tool_done", tool_name="todo", tool_call_id="t1", tool_count=15, **kw)
    ac.on_child_event("subagent.tool_done", tool_name="terminal", tool_count=16, **kw)
    text = card.render()
    assert "todo" not in text.lower()
    assert [p for p, _ in cl.recent] and all("todo" not in p for p, _ in cl.recent)
    assert cl.calls == 15, "16 calls, one of them the hidden todo: 15 shown"


def test_todo_is_not_shown_for_several_helpers_either():
    r = fl.DispatchReceipt(delegation_id="d2", seq=1, goals=("a", "b"), summary=HEAD)
    card = ac.card_for([r])
    kw = dict(delegation_id="d2", task_index=0)
    ac.on_child_event("subagent.start", **kw)
    ac.on_child_event("subagent.tool", tool_name="todo", preview="x", args={}, tool_call_id="t", **kw)
    ac.on_child_event("subagent.tool_done", tool_name="todo", tool_call_id="t", **kw)
    assert "todo" not in card.render().lower()


def test_reserve_makes_the_delegation_owned_before_the_part_opens():
    assert not ac.owns_delegation("early1")
    assert ac.reserve("early1") is True
    assert ac.owns_delegation("early1")
    ac.release("early1")
    assert not ac.owns_delegation("early1")
    ac.reserve("early2")
    ac.open_part("early2", HEAD, STEPS)
    assert ac.owns_delegation("early2")


def test_reserve_ack_part_only_for_a_handoff_that_will_open_a_part():
    assert fl.reserve_ack_part("r1", None) is False
    assert not ac.owns_delegation("r1")
    assert fl.reserve_ack_part("r1", {"work": "fixing the bugs", "outcome": "test build", "steps": ["a"]}) is True
    assert ac.owns_delegation("r1")
    fl.release_ack_part("r1")
    assert not ac.owns_delegation("r1")


# -- small card fixes ---------------------------------------------------------


def test_the_relayed_plain_summary_wins_over_a_truncated_summary():
    card = _card()
    ac.on_child_event("subagent.start", delegation_id="d1", model="claude-sonnet-5-5")
    # what delegate_tool sends: summary cut before its 'Plain summary:' line
    ac.on_child_event(
        "subagent.complete", delegation_id="d1", status="completed",
        summary="I haven't tried them in a live Telegram chat. The test bot runs the new code. Commit: `abc`",
        plain_summary="The fix is in and the test bot is running it.",
    )
    assert card.parts[0].checklist.summary == "The fix is in and the test bot is running it."
    assert "The fix is in and the test bot is running it." in card.render()
    assert "haven't tried" not in card.render()


def test_without_a_relayed_plain_summary_the_old_extraction_still_works():
    card = _card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed",
                      summary="Result: done\nPlain summary: Old style works.")
    assert card.parts[0].checklist.summary == "Old style works."


def test_a_long_command_with_an_absolute_path_is_one_short_line():
    long_cmd = "git -C /home/starboy/Projects/claude-code-build/claude-code rev-parse --show-toplevel"
    card = _card()
    ac.on_child_event("subagent.start", delegation_id="d1")
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="terminal", preview=long_cmd,
                      args={"command": long_cmd}, tool_call_id="a", tool_count=1)
    line = next(ln for ln in card.render().splitlines() if "now:" in ln)
    assert "/home/starboy" not in line and "Projects" not in line
    assert "\u2026 claude-code" in line and "\u2026/" not in line
    assert len(line.removeprefix("> ").strip("*")) <= len("now: ") + ac.TOOL_PHRASE_SINGLE


@pytest.mark.parametrize("raw,expect", [
    ("terminal cd /home/starboy && ls", "terminal cd ~ && ls"),
    ("terminal pytest -q", "terminal pytest -q"),
    ("read_file /home/starboy/Projects/clover/agent/ack_checklist.py", "read_file \u2026 ack_checklist.py"),
])
def test_short_tool_phrase(raw, expect):
    assert ac.short_tool_phrase(raw) == expect


def test_short_tool_phrase_cuts_on_a_word_with_an_ellipsis():
    out = ac.short_tool_phrase("terminal " + " ".join(["wordy"] * 30))
    assert len(out) <= ac.TOOL_LINE_MAX and out.endswith("\u2026")
    assert not out[:-1].endswith("wor"), "cut on a word"


def test_todo_calls_are_not_counted_for_one_helper():
    card = _card()
    ac.on_child_event("subagent.start", delegation_id="d1", model="claude-sonnet-5-5")
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="todo", preview="x", tool_call_id="t1", tool_count=1)
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="todo", preview="x", tool_call_id="t2", tool_count=2)
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="terminal", preview="ls",
                      args={"command": "ls"}, tool_call_id="c1", tool_count=3)
    assert card.parts[0].checklist.calls == 1
    assert "1 call" in card.render() and "3 call" not in card.render()
    # a repeated start event for the same todo call counts once
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="todo", preview="x", tool_call_id="t2", tool_count=2)
    ac.on_child_event("subagent.tool_done", delegation_id="d1", tool_name="terminal", tool_call_id="c1", tool_count=3)
    assert card.parts[0].checklist.calls == 1


def test_todo_calls_are_not_counted_per_helper_or_in_the_total():
    card = _multi_card()
    for i in (0, 1):
        ac.on_child_event("subagent.start", **_kid("dm", i, model="claude-sonnet-5-5"))
        ac.on_child_event("subagent.tool", tool_name="todo", preview="x", tool_call_id=f"t{i}", tool_count=1, **_kid("dm", i))
        ac.on_child_event("subagent.tool", tool_name="read_file", preview="a.py", args={"path": "a.py"},
                          tool_call_id=f"r{i}", tool_count=2, **_kid("dm", i))
    cl = card.parts[0].checklist
    assert [k.calls for k in cl.kids[:2]] == [1, 1]
    text = card.render()
    assert "2 calls" in text and "4 calls" not in text


# -- many helpers: the compact layout -----------------------------------------

NAMES = ["alpha site", "bravo site", "charlie site", "delta site", "echo site", "foxtrot site", "golf site", "hotel site"]


def _many(n=8):
    goals = tuple(f"Check {NAMES[i]}" for i in range(n))
    r = fl.DispatchReceipt(delegation_id="mm", seq=1, goals=goals, summary=HEAD, steps=())
    return ac.card_for([r])


def _mk(i, **kw):
    return dict(delegation_id="mm", task_index=i, task_count=8, subagent_id=f"s{i}", **kw)


def test_many_helpers_use_the_compact_layout():
    card = _many(8)
    for i in range(8):
        if i < 2:
            ac.on_child_event("subagent.start", **_mk(i, model="claude-sonnet-5-5", title=NAMES[i]))
            ac.on_child_event("subagent.complete", status="completed", outcome="done",
                              summary="x", plain_summary=f"{NAMES[i]} is fine and all pages load without any trouble at all.",
                              **_mk(i, title=NAMES[i]))
        elif i < 6:
            ac.on_child_event("subagent.start", **_mk(i, model="claude-sonnet-5-5", title=NAMES[i]))
            for n, nm in enumerate(("terminal", "read_file", "web_search")):
                ac.on_child_event("subagent.tool", tool_name=nm, preview=f"p{n}", args={"command": f"p{n}"},
                                  tool_call_id=f"{i}{n}", tool_count=n + 1, **_mk(i))
                if n < 2:
                    ac.on_child_event("subagent.tool_done", tool_name=nm, tool_call_id=f"{i}{n}",
                                      tool_count=n + 1, **_mk(i))
    lines = _flat(card.render()).splitlines()
    text = "\n".join(lines)
    # done helpers: one line each (`**Name** · done · result`), no stats, no emoji
    done = [ln for ln in lines if ln.startswith("> **") and " \u00b7 done \u00b7 " in ln]
    assert len(done) == 2 and all(len(ln) < 90 for ln in done)
    assert done[0].startswith("> **alpha site** \u00b7 done \u00b7 alpha site is fine")
    for nm in NAMES[:2]:
        i = next(j for j, ln in enumerate(lines) if f"**{nm}**" in ln)
        assert "Sonnet" not in lines[i] and "calls" not in lines[i]
    # at most 3 running helpers in full: name + stats, then at most 2 tool lines
    full = [ln for ln in lines if ln.startswith("> **") and "Sonnet 5.5" in ln and "done" not in ln]
    assert len(full) == 3
    for ln in full:
        i = lines.index(ln)
        assert lines[i + 1].startswith("> *now: ") and lines[i + 2].startswith("> *before: ")
        assert lines[i + 3] == f"> {SPACER}"
    # the 4th running helper and the waiting ones are grouped into one line each
    assert "> more running: foxtrot site" in lines
    waiting = [ln for ln in lines if "waiting:" in ln]
    assert waiting == ["> waiting: Check golf site, Check hotel site"]
    assert "waiting to start" not in text and "\U0001f504" not in text and "\u2b1c" not in text
    assert len(text) <= ac.CARD_BUDGET
    assert not any(a == b == f"> {SPACER}" for a, b in zip(lines, lines[1:]))


def test_four_helpers_keep_the_full_layout():
    card = _many(4)
    for i in range(4):
        ac.on_child_event("subagent.start", **_mk(i, model="claude-sonnet-5-5", title=NAMES[i]))
    text = card.render()
    assert "more running:" not in text and "waiting:" not in text
    assert all(f"**{NAMES[i]}**" in text for i in range(4))


def test_many_waiting_names_are_cut_with_an_ellipsis():
    goals = tuple(f"Check site {i}" for i in range(12))
    r = fl.DispatchReceipt(delegation_id="mm", seq=1, goals=goals, summary=HEAD, steps=())
    card = ac.card_for([r])
    line = next(ln for ln in card.render().splitlines() if "waiting:" in ln)
    assert line.startswith("> waiting: Check site 0, ") and line.endswith("\u2026")


# -- no Telegram /command links in any rendered tool line ----------------------

_CMD = __import__("re").compile(r"(?<![\w/])/[A-Za-z]")


@pytest.mark.parametrize("cmd", [
    "du -s -B1 /home/starboy/Projects/claude-code-build/claude-code/clover_c1.egg-info /x/acp_adapter",
    "ls /clover",
    "cat /etc/hosts /var/log/syslog",
    "git -C /home/starboy/Projects/claude-code-build/claude-code rev-parse",
    "rg foo /tmp/x/y/z/clover_c1.eg",
])
def test_no_slash_word_token_in_any_tool_line(cmd):
    assert not _CMD.search(ac.short_tool_phrase(cmd)), ac.short_tool_phrase(cmd)
    card = _card()
    ac.on_child_event("subagent.start", delegation_id="d1")
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="terminal", preview=cmd,
                      args={"command": cmd}, tool_call_id="a", tool_count=1)
    shown = card.render()
    assert not _CMD.search(shown), shown
    ac.on_child_event("subagent.tool_done", delegation_id="d1", tool_name="terminal", tool_call_id="a")
    assert not _CMD.search(card.render())


def test_multi_helper_tool_lines_have_no_command_links():
    r = fl.DispatchReceipt(delegation_id="dm", seq=1, goals=("a", "b"), summary=HEAD, steps=())
    card = ac.card_for([r])
    for i in range(2):
        ac.on_child_event("subagent.start", delegation_id="dm", task_index=i, task_count=2)
        ac.on_child_event("subagent.tool", delegation_id="dm", task_index=i, task_count=2,
                          tool_name="terminal", preview="ls /clover", args={"command": "ls /clover"},
                          tool_call_id=f"t{i}", tool_count=1)
    text = card.render()
    assert "now: ls" in text and not _CMD.search(text), text


def test_ellipsis_path_keeps_a_readable_name_without_a_slash_after_it():
    out = ac.short_tool_phrase("read_file /home/starboy/Projects/clover/agent/ack_checklist.py")
    assert out == "read_file \u2026 ack_checklist.py"


# -- the opener is the turn's own reply; the card is a second message ----------

def test_ack_text_on_a_chat_platform_is_the_opener_alone():
    from types import SimpleNamespace

    from agent import router_backstop as rb

    r = fl.DispatchReceipt(delegation_id="dx", seq=1, goals=("g",), summary=HEAD, steps=STEPS,
                           say="on it, sorting it out.")
    text = rb._ack_text([r], SimpleNamespace(platform="telegram"))
    assert text == "on it, sorting it out."
    card = ac.card_for([r])
    assert card.split_opener is True
    assert "on it" not in card.render() and card.render().startswith("\U0001f500 **fixing the bugs**")
    assert ac.match_unbound_opener(text) is card and ac.match_unbound(text) is None


def test_ack_text_without_agent_or_on_the_cli_keeps_the_whole_text():
    from types import SimpleNamespace

    from agent import router_backstop as rb

    r = fl.DispatchReceipt(delegation_id="dy", seq=1, goals=("g",), summary=HEAD, steps=STEPS, say="got it")
    assert rb._ack_text([r]).startswith("got it\n\n\U0001f500 **fixing the bugs**")
    r2 = fl.DispatchReceipt(delegation_id="dz", seq=1, goals=("g",), summary=HEAD, steps=STEPS, say="got it")
    assert rb._ack_text([r2], SimpleNamespace(platform="cli")).startswith("got it\n\n")


def test_ack_text_without_an_opener_stays_the_card_text():
    from types import SimpleNamespace

    from agent import router_backstop as rb

    r = fl.DispatchReceipt(delegation_id="dq", seq=1, goals=("g",), summary=HEAD, steps=STEPS, say="")
    text = rb._ack_text([r], SimpleNamespace(platform="telegram"))
    assert text.startswith("\U0001f500 **fixing the bugs**") and ac.card_for([r]).split_opener is False


def test_classic_style_ack_text_is_unchanged_on_a_chat_platform():
    from types import SimpleNamespace

    from agent import router_backstop as rb

    ac.set_style_for_tests("classic")
    r = fl.DispatchReceipt(delegation_id="dc", seq=1, goals=("g",), summary=HEAD, steps=STEPS, say="got it")
    text = rb._ack_text([r], SimpleNamespace(platform="telegram"))
    assert text.startswith("got it\n\n") and ac.card_for([r]).split_opener is False


# -- Ant's cleaner layout: few emojis, Goal line, stats by each name ---------------


def _clean_chars(text):
    return text.replace("\U0001f342", "")  # the one allowed status emoji


def test_a_running_title_leads_with_the_roster_icon_and_the_card_has_no_other_emoji():
    card = _running_card()
    text = card.render()
    assert text.splitlines()[2] == "\U0001f500 **fixing the bugs**"
    assert _no_emoji(text) and text.count("\U0001f500") == 1
    multi = _live_multi()
    assert _no_emoji(multi.render()) and multi.render().splitlines()[2].startswith("\U0001f500 **checking the 3 sites**")


def test_the_failure_emoji_appears_only_on_a_failed_or_stopped_title_and_a_failed_name():
    ok = _running_card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed", summary="Plain summary: ok.")
    assert "\U0001f342" not in ok.render()
    ac.reset_for_tests(); ac.set_style_for_tests("unified")
    bad = _running_card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="failed", summary="boom")
    text = bad.render()
    assert text.count("\U0001f342") == 1 and text.splitlines()[2] == "\U0001f342 **fixing the bugs** \u00b7 failed"
    assert "\U0001f500" not in text, "the failure emoji replaces the roster icon"
    assert _no_emoji(_clean_chars(text))
    ac.reset_for_tests(); ac.set_style_for_tests("unified")
    multi = _multi_card(goals=("a", "b"))
    ac.on_child_event("subagent.complete", status="completed", summary="Plain summary: fine.", **_kid("dm", 0))
    ac.on_child_event("subagent.complete", status="failed", summary="Plain summary: broke.", **_kid("dm", 1))
    lines = multi.render().splitlines()
    assert lines[0].startswith("\U0001f342 **") and "> \U0001f342 **b** \u00b7 failed" in lines
    assert "> **a** \u00b7 done" in lines and sum(ln.count("\U0001f342") for ln in lines) == 2
    assert _no_emoji(_clean_chars(multi.render()))


def test_goal_line_is_plain_has_the_eta_while_running_and_loses_it_when_finished():
    card = _running_card()
    assert "> Goal: test build + PR \u00b7 ~10\u201325 min" in card.render().splitlines()
    assert "*Goal" not in card.render()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed", summary="Plain summary: ok.")
    assert "> Goal: test build + PR" in card.render().splitlines() and "~" not in card.render()


def test_no_goal_line_without_an_outcome():
    card = _card(header="\U0001f504 fixing the bugs")
    assert "Goal" not in card.render() and card.render().splitlines()[0] == "\U0001f500 **fixing the bugs**"


def test_a_subagent_header_has_the_model_calls_and_time_and_the_status_word():
    card = _live_multi()
    cl = card.parts[0].checklist
    cl.kids[0].started -= 100; cl.kids[0].ended = cl.kids[0].started + 100
    cl.kids[1].started -= 72
    lines = _flat(card.render()).splitlines()
    assert "> **nice & tidy site** \u00b7 done \u00b7 Sonnet 5.5 \u00b7 9 calls \u00b7 1m40s" in lines
    assert "> **silas studios site** \u00b7 Sonnet 5.5 \u00b7 4 calls \u00b7 1m12s" in lines, "running: no status word"
    assert "> **Check the pizzeria bros pamphlet** \u00b7 waiting" in lines, "waiting: no stats"


def test_one_call_is_singular_in_a_subagent_header_and_the_footer():
    card = _multi_card(goals=("a", "b"))
    ac.on_child_event("subagent.start", **_kid("dm", 0, model="claude-sonnet-5-5"))
    ac.on_child_event("subagent.tool", tool_name="terminal", preview="ls", args={"command": "ls"}, tool_count=1,
                      **_kid("dm", 0))
    text = card.render()
    assert "1 call \u00b7" in text and "1 calls" not in text and "*2 subagents \u00b7 1 call \u00b7" in text


def test_step_marks_are_plain_characters():
    card = _card(steps=("a", "b", "c", "d", "e"))
    cl = card.parts[0].checklist
    cl.steps[0][1], cl.steps[1][1], cl.steps[2][1], cl.steps[3][1], cl.steps[4][1] = (
        "completed", "in_progress", "pending", "failed", "cancelled")
    lines = card.render().splitlines()
    assert lines[-5:] == ["> \u2713 a", "> \u25b8 b", "> \u25cb c", "> \u2717 d", "> \u2013 e"]
    multi = _multi_card(goals=("x", "y"))
    ac.on_child_event("subagent.start", **_kid("dm", 0))
    multi.parts[0].checklist.kids[0].steps[:] = [["s", "completed"], ["t", "in_progress"], ["u", "pending"]]
    assert _EM + "\u2713 s" in multi.render() and _EM + "\u25b8 t" in multi.render() and _EM + "\u25cb u" in multi.render()


def test_a_failed_and_a_stopped_step_are_marked_on_the_finished_card():
    card = _running_card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="failed", summary="boom")
    assert "> \u2717 fix" in card.render().splitlines()
    ac.reset_for_tests(); ac.set_style_for_tests("unified")
    card = _running_card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="interrupted")
    assert "> \u2013 fix" in card.render().splitlines()


def test_multi_tool_lines_are_italic_now_then_before_at_most_two_and_indented():
    card = _multi_card(goals=("a", "b"))
    for n in range(3):
        _tool("dm", "terminal", f"cmd{n}", n + 1, task_index=0); _done("dm", "terminal", n + 1, task_index=0)
    _tool("dm", "terminal", "now-cmd", 4, task_index=0)
    text = card.render()
    assert f"\n> {_EM}*now: now-cmd*\n> {_EM}*before: cmd2*\n" in text
    assert "cmd1" not in text, "at most two tool lines per subagent"
    assert "\U0001f527" not in text and "\u2713 *" not in text and "\u2717 *" not in text


def test_a_failed_tool_says_failed_in_words():
    card = _card()
    _tool("d1", "read_file", "a.py", 1); _done("d1", "read_file", 1, error=True)
    assert "> *before: read_file a.py (failed)*" in card.render().splitlines()


def test_tool_phrases_are_capped_so_the_whole_line_fits_one_phone_row():
    long_cmd = "ss -tlpn 2>&1 | head -40; echo ---; ps aux | grep -i telegram | head"
    multi = _multi_card(goals=("a", "b"))
    _tool("dm", "terminal", long_cmd, 1, task_index=0)
    row = next(ln for ln in multi.render().splitlines() if "now:" in ln)
    phrase = row.split("now: ", 1)[1].rstrip("*")
    assert len(phrase) <= ac.TOOL_PHRASE_MULTI and phrase.endswith("\u2026") and not phrase.endswith(" \u2026")
    assert row.startswith("> " + _EM + "*now: "), "the indent is kept"
    ac.reset_for_tests(); ac.set_style_for_tests("unified")
    card = _card()
    _tool("d1", "terminal", long_cmd, 1)
    row = next(ln for ln in card.render().splitlines() if "now:" in ln)
    phrase = row.split("now: ", 1)[1].rstrip("*")
    assert len(phrase) <= ac.TOOL_PHRASE_SINGLE and phrase.endswith("\u2026")
    assert ac.TOOL_PHRASE_MULTI == 34 and ac.TOOL_PHRASE_SINGLE == 40


def test_a_failed_marker_does_not_push_the_phrase_past_the_cap():
    card = _card()
    _tool("d1", "terminal", "word " * 30, 1); _done("d1", "terminal", 1, error=True)
    row = next(ln for ln in card.render().splitlines() if "before:" in ln)
    assert row.endswith(" (failed)*") and len(row.removeprefix("> ").strip("*")) <= len("before: ") + ac.TOOL_PHRASE_SINGLE


def test_the_single_card_reads_title_hint_goal_stats_steps_tools():
    card = _running_card()
    card.parts[0].checklist.started -= 35
    _tool("d1", "terminal", "ls", 15); _done("d1", "terminal", 15)
    text = card.render().splitlines()
    assert text[2:] == [
        "\U0001f500 **fixing the bugs**",
        "> Goal: test build + PR \u00b7 ~10\u201325 min",
        "> Sonnet 5.5 \u00b7 15 calls \u00b7 35s",
        f"> {SPACER}",
        "> \u2713 find", "> \u25b8 fix", "> \u25cb test",
        f"> {SPACER}",
        "> *now: pytest -q*",
        "> *before: ls*",
    ]
    assert "\u2003" not in "\n".join(text), "the single card has no indent"


def test_the_finished_single_card_has_the_result_in_place_of_tool_lines():
    card = _running_card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed",
                      summary="Plain summary: The fix is in.")
    lines = card.render().splitlines()
    assert lines[-1] == "> The fix is in." and lines[-2] == f"> {SPACER}"
    assert not any(a == b == f"> {SPACER}" for a, b in zip(lines, lines[1:]))


def test_a_multi_result_line_is_indented_plain_and_cut_on_a_word():
    card = _multi_card(goals=("a", "b"))
    long = "Plain summary: " + " ".join(["word"] * 80) + "."
    ac.on_child_event("subagent.complete", status="completed", summary=long, **_kid("dm", 0))
    row = next(ln for ln in card.render().splitlines() if "word" in ln)
    assert row.startswith("> " + _EM + "word") and not row.startswith("> " + _EM + "*")
    body = row.removeprefix("> " + _EM)
    assert len(body) <= ac.SUMMARY_CHARS and body.endswith("\u2026") and not body.endswith(" \u2026")


def test_exactly_one_spacer_between_blocks_and_never_two_in_a_row():
    for card in (_running_card(), _live_multi()):
        lines = card.render().splitlines()
        assert not any(a == b == f"> {SPACER}" for a, b in zip(lines, lines[1:]))
        assert lines[-1] != f"> {SPACER}" and "" not in lines[3:]
    ac.reset_for_tests(); ac.set_style_for_tests("unified")
    lines = _multi_card().render().splitlines()
    assert lines[:3] == ["\U0001f500 **checking the 3 sites** \u00b7 0 of 3 done", "> Goal: outcome \u00b7 ~5 min", f"> {SPACER}"]


def test_the_collapsed_telegram_cut_is_title_hint_spacer_goal():
    from gateway import delegation_activity as da

    card = _live_multi()
    quote = da._to_expandable(card.render(include_say=False), da.TAP_HINT_LIVE, bold_title=True)
    lines = quote.splitlines()
    assert lines[:4] == [
        "**> \U0001f500 **checking the 3 sites** \u00b7 1 of 3 done", "> *tap to watch \U0001f340*", f"> {SPACER}",
        "> Goal: outcome \u00b7 ~5 min",
    ]
    assert f"> {SPACER}\n> {SPACER}" not in quote and quote.endswith("||") and quote.count("**>") == 1


def test_all_ended_with_a_failure_shows_the_counts_and_the_title_emoji():
    card = _multi_card(goals=("a", "b", "c"))
    ac.on_child_event("subagent.complete", status="completed", summary="Plain summary: ok.", **_kid("dm", 0))
    ac.on_child_event("subagent.complete", status="failed", summary="Plain summary: bad.", **_kid("dm", 1))
    ac.on_child_event("subagent.complete", status="completed", summary="Plain summary: ok.", **_kid("dm", 2))
    assert card.render().splitlines()[0] == "\U0001f342 **checking the 3 sites** \u00b7 2 of 3 done \u00b7 1 failed"
    ac.reset_for_tests(); ac.set_style_for_tests("unified")
    card = _multi_card(goals=("a", "b"))
    ac.on_child_event("subagent.complete", status="completed", summary="Plain summary: ok.", **_kid("dm", 0))
    ac.on_child_event("subagent.complete", status="completed", summary="Plain summary: ok.", **_kid("dm", 1))
    assert card.render().splitlines()[0] == "\U0001f500 **checking the 3 sites** \u00b7 done"


def test_compaction_starts_above_six_with_plain_one_liners_and_grouped_waiting():
    assert ac.COMPACT_OVER == 6
    goals = tuple(f"job {i}" for i in range(9))
    r = fl.DispatchReceipt(delegation_id="c9", seq=1, goals=goals, summary=HEAD, steps=())
    card = ac.card_for([r])
    kw = lambda i, **k: dict(delegation_id="c9", task_index=i, task_count=9, subagent_id=f"z{i}", **k)
    ac.on_child_event("subagent.start", **kw(0, model="claude-sonnet-5-5", title="job 0"))
    ac.on_child_event("subagent.complete", status="completed", plain_summary="all good here", **kw(0, title="job 0"))
    ac.on_child_event("subagent.start", **kw(1, model="claude-sonnet-5-5", title="job 1"))
    ac.on_child_event("subagent.complete", status="failed", plain_summary="it broke", **kw(1, title="job 1"))
    for i in (2, 3):
        ac.on_child_event("subagent.start", **kw(i, model="claude-sonnet-5-5", title=f"job {i}"))
    lines = _flat(card.render()).splitlines()
    assert "> **job 0** \u00b7 done \u00b7 all good here" in lines
    assert "> \U0001f342 **job 1** \u00b7 failed \u00b7 it broke" in lines
    assert "> waiting: job 4, job 5, job 6, job 7, job 8" in lines
    assert not any(ln.startswith("> \u2b1c") or "\U0001f504" in ln for ln in lines)
