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
        "\U0001f504 **fixing the bugs**",
        "> *\u2192 test build + PR \u00b7 ~10\u201325 min*",
        f"> {SPACER}",
        "> \U0001f340 find",
        "> \U0001f504 fix",
        "> \u2b1c test",
        f"> {SPACER}",
        "> \U0001f527 *terminal pytest -q*",
        "> *Sonnet 5.5 \u00b7 \U0001f6e0 14 tool calls \u00b7 \u23f1 6m12s*",
    ]


def test_initial_text_has_the_say_title_outcome_and_steps_but_no_activity():
    card = _card(say="got it")
    lines = card.initial_text.splitlines()
    assert lines[:3] == ["got it", "", "\U0001f504 **fixing the bugs**"]
    assert lines[3] == "> *\u2192 test build + PR \u00b7 ~10\u201325 min*"
    assert "\U0001f527" not in card.initial_text and "tool call" not in card.initial_text


def test_card_without_steps_is_header_and_outcome_only():
    card = _card(steps=())
    assert card.initial_text.splitlines() == [
        "\U0001f504 **fixing the bugs**",
        "> *\u2192 test build + PR \u00b7 ~10\u201325 min*",
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
    assert lines[2] == "\U0001f340 **fixing the bugs**"
    assert lines[3] == "> *\u2192 test build + PR*", "the ETA goes away"
    assert lines[5:8] == ["> \U0001f340 find", "> \U0001f340 fix", "> \U0001f340 test"]
    assert lines[-2] == "> The fix is in and tests pass."
    assert lines[-1] == "> *Sonnet 5.5 \u00b7 \U0001f6e0 31 tool calls \u00b7 \u23f1 14m*"
    assert "\U0001f527" not in card.render(), "no live tool line once finished"
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
    assert lines[2] == "\U0001f342 **fixing the bugs**"
    assert "> \U0001f340 find" in lines and "> \U0001f342 fix" in lines and "> \u2b1c test" in lines
    assert "> The folder does not exist." in lines
    assert "~" not in lines[3]


def test_failed_status_gives_the_failed_header():
    card = _running_card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="failed", summary="boom")
    assert card.render().splitlines()[2].startswith("\U0001f342 **")


def test_stopped_card_shape():
    card = _running_card()
    ac.on_child_event("subagent.complete", delegation_id="d1", status="interrupted")
    lines = card.render().splitlines()
    assert lines[2] == "\u23f9 **fixing the bugs**"
    assert "> \U0001f340 find" in lines and "> \u23f9 fix" in lines and "> \u2b1c test" in lines
    assert "\U0001f527" not in card.render()
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
    assert "\U0001f6e0 4 tool calls" in card.render()


def test_one_tool_call_is_singular():
    card = _card()
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="terminal", tool_count=1)
    text = card.render()
    assert "\U0001f6e0 1 tool call " in text and "1 tool calls" not in text


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
    assert lines[0] == "\U0001f340 **fixing the bugs**"
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
    assert text.splitlines()[0] == "\U0001f340 **fixing bold things**"


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


def _tool_lines(text):
    return [ln[2:] for ln in text.splitlines() if ln[2:3] in ("\U0001f527", "\u2713", "\u2717")]


def test_single_card_shows_the_open_tool_then_the_two_before_it_newest_first():
    card = _card()
    _tool("d1", "terminal", "ls", 1); _done("d1", "terminal", 1)
    _tool("d1", "terminal", "pytest -q", 2); _done("d1", "terminal", 2, error=True)
    _tool("d1", "read_file", "a.py", 3); _done("d1", "read_file", 3)
    _tool("d1", "web_search", "silas studios booking", 4)
    assert _tool_lines(card.render()) == [
        "\U0001f527 *web_search silas studios booking*",
        "\u2713 *read_file a.py*",
        "\u2717 *terminal pytest -q*",
    ]


def test_single_card_without_an_open_tool_shows_only_the_finished_ones_capped_at_three():
    card = _card()
    for i in range(5):
        _tool("d1", "terminal", f"cmd{i}", i + 1); _done("d1", "terminal", i + 1)
    assert _tool_lines(card.render()) == [
        "\u2713 *terminal cmd4*", "\u2713 *terminal cmd3*", "\u2713 *terminal cmd2*",
    ]


def test_tool_lines_sit_in_the_quote_between_the_steps_and_the_stats():
    card = _card()
    _tool("d1", "terminal", "ls", 1); _done("d1", "terminal", 1)
    _tool("d1", "read_file", "a.py", 2)
    lines = card.render().splitlines()
    assert lines[-4:-1] == [f"> {SPACER}", "> \U0001f527 *read_file a.py*", "> \u2713 *terminal ls*"]
    assert lines[-1].startswith("> *") and "\U0001f6e0 2 tool calls" in lines[-1]


def test_tool_done_pairs_by_call_id_before_name():
    card = _card()
    _tool("d1", "terminal", "first", 1, tool_call_id="a")
    _tool("d1", "terminal", "second", 2, tool_call_id="b")
    _done("d1", "terminal", 2, tool_call_id="a", error=True)
    assert _tool_lines(card.render()) == ["\U0001f527 *terminal second*", "\u2717 *terminal first*"]


def test_tool_done_without_an_id_pairs_the_newest_open_call_of_that_tool_then_the_newest_open():
    card = _card()
    _tool("d1", "read_file", "a.py", 1)
    _tool("d1", "terminal", "ls", 2)
    _tool("d1", "read_file", "b.py", 3)
    _done("d1", "read_file", 3)  # newest open read_file = b.py
    assert _tool_lines(card.render()) == [
        "\U0001f527 *terminal ls*", "\U0001f527 *read_file a.py*", "\u2713 *read_file b.py*",
    ]
    _done("d1", "web_search", 3)  # unknown name: the newest open call (the terminal)
    assert _tool_lines(card.render()) == [
        "\U0001f527 *read_file a.py*", "\u2713 *terminal ls*", "\u2713 *read_file b.py*",
    ]


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
    assert "> All good." in text and "tool calls" in text


def test_tool_lines_use_the_same_phrase_as_before():
    card = _card()
    _tool("d1", "read_file", "/srv/site/contact.html", 1)
    ac.on_child_event("subagent.tool", delegation_id="d1", tool_name="read_file",
                      preview="/srv/site/contact.html", args={"path": "/srv/site/contact.html"}, tool_count=2)
    assert "\U0001f527 *read_file contact.html*" in card.render()


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
    lines = card.render().splitlines()
    assert lines[:4] == [
        "on it", "", "\U0001f504 **checking the 3 sites** \u00b7 1 of 3 done",
        "> *\u2192 outcome \u00b7 ~5 min*",
    ]
    assert "> \U0001f340 **nice & tidy site**" in lines
    assert "> All pages load and the quote form works." in lines
    assert "> \U0001f504 **silas studios site**" in lines
    assert "> \U0001f527 *web_search silas studios booking*" in lines
    assert "> \u2713 *read_file contact*" in lines
    assert lines.index("> \U0001f527 *web_search silas studios booking*") < lines.index(
        "> \u2713 *read_file contact*")
    assert "> *Sonnet 5.5 \u00b7 \U0001f6e0 9 tool calls \u00b7 \u23f1 1m40s*" in lines
    assert "> *Sonnet 5.5 \u00b7 \U0001f6e0 4 tool calls \u00b7 \u23f1 1m12s*" in lines
    assert "> \u2b1c **Check the pizzeria bros pamphlet**" in lines
    assert "> *waiting to start*" in lines
    assert lines[-1].startswith("> *3 helpers \u00b7 \U0001f6e0 13 tool calls \u00b7 \u23f1 ")
    assert lines[-2] == f"> {SPACER}"
    assert "ignored" not in card.render()


def test_multi_helper_without_a_title_uses_the_goal_then_a_number():
    card = _multi_card(goals=("Check nice and tidy site works. Then report.", ""))
    text = card.render()
    assert "**Check nice and tidy site works**" in text
    assert "**helper 2**" in text


def test_child_title_from_the_event_beats_the_goal():
    card = _multi_card()
    ac.on_child_event("subagent.start", **_kid("dm", 2, title="pizza pamphlet", goal="whatever else"))
    assert "**pizza pamphlet**" in card.render()


def test_each_running_helper_shows_at_most_two_tool_lines():
    card = _multi_card(goals=("a", "b"))
    for n in range(4):
        _tool("dm", "terminal", f"cmd{n}", n + 1, task_index=1); _done("dm", "terminal", n + 1, task_index=1)
    _tool("dm", "web_search", "q", 5, task_index=1)
    assert _tool_lines(card.render()) == ["\U0001f527 *web_search q*", "\u2713 *terminal cmd3*"]
    _done("dm", "web_search", 5, task_index=1)
    assert _tool_lines(card.render()) == ["\u2713 *web_search q*", "\u2713 *terminal cmd3*"]


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
    lines = card.render().splitlines()
    assert lines[2] == "\U0001f340 **checking the 3 sites**"
    assert lines[3] == "> *\u2192 outcome*"
    assert [ln for ln in lines if "\U0001f340 **" in ln and ln.startswith(">")] == [
        "> \U0001f340 **nice & tidy site**", "> \U0001f340 **silas studios site**",
        "> \U0001f340 **Check the pizzeria bros pamphlet**",
    ]
    assert "> Booking works." in lines and "> Pamphlet is fine." in lines
    assert _tool_lines(card.render()) == []
    assert lines[-1].endswith("\u23f1 1m40s*") and "3 helpers" in lines[-1]
    assert card.finished


def test_multi_finished_with_one_failed_and_one_stopped_is_failed_and_shows_the_count():
    card = _multi_card(goals=("a", "b", "c"))
    ac.on_child_event("subagent.complete", status="completed", summary="Plain summary: fine.", **_kid("dm", 0))
    ac.on_child_event("subagent.complete", status="completed", outcome="not_done",
                      summary="Result: not done\nPlain summary: Site is down.", **_kid("dm", 1))
    ac.on_child_event("subagent.complete", status="interrupted", summary="", **_kid("dm", 2))
    lines = card.render().splitlines()
    assert lines[0] == "\U0001f342 **checking the 3 sites** \u00b7 1 of 3 done"
    assert "> \U0001f340 **a**" in lines and "> \U0001f342 **b**" in lines and "> \u23f9 **c**" in lines
    assert "> Site is down." in lines


def test_multi_stopped_without_a_failure_is_stopped():
    card = _multi_card(goals=("a", "b"))
    ac.on_child_event("subagent.complete", status="completed", summary="ok", **_kid("dm", 0))
    ac.on_child_event("subagent.complete", status="cancelled", **_kid("dm", 1))
    assert card.render().splitlines()[0] == "\u23f9 **checking the 3 sites** \u00b7 1 of 2 done"


def test_max_iterations_counts_as_failed_for_a_multi_helper():
    card = _multi_card(goals=("a", "b"))
    ac.on_child_event("subagent.complete", status="completed", exit_reason="max_iterations", **_kid("dm", 0))
    ac.on_child_event("subagent.complete", status="completed", summary="ok", **_kid("dm", 1))
    lines = card.render().splitlines()
    assert lines[0].startswith("\U0001f342") and "> \U0001f342 **a**" in lines


def test_multi_footer_totals_and_a_summary_cut_on_a_word():
    card = _multi_card(goals=("a", "b"))
    cl = card.parts[0].checklist
    ac.on_child_event("subagent.start", **_kid("dm", 0))
    ac.on_child_event("subagent.tool", tool_name="t", tool_count=5, **_kid("dm", 0))
    ac.on_child_event("subagent.tool", tool_name="t", tool_count=7, **_kid("dm", 1))
    long = "Plain summary: " + " ".join(["word"] * 80) + "."
    ac.on_child_event("subagent.complete", status="completed", summary=long, **_kid("dm", 0))
    cl.kids[0].started -= 50; cl.kids[1].started = cl.kids[0].started + 10
    lines = card.render().splitlines()
    assert "> *2 helpers \u00b7 \U0001f6e0 12 tool calls \u00b7 \u23f1 " in "\n".join(lines)
    summary = next(ln for ln in lines if ln.startswith("> word"))
    assert len(summary) <= 142 and summary.endswith("\u2026") and not summary.endswith(" \u2026")


def test_multi_complete_before_the_part_exists_is_replayed_per_child():
    ac.on_child_event("subagent.complete", delegation_id="dm", task_index=1, title="second", goal="g2",
                      subagent_id="sa-1", status="completed", outcome="not_done",
                      summary="Result: not done\nPlain summary: Nope.")
    card = _multi_card(goals=("g1", "g2"))
    lines = card.render().splitlines()
    assert "> \U0001f342 **second**" in lines and "> Nope." in lines
    assert "> \u2b1c **g1**" in lines


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


def test_six_helper_card_stays_under_the_telegram_limit_by_cutting_summaries_first():
    goals = tuple(f"Check site number {i}" for i in range(6))
    card = _multi_card(goals=goals)
    para = "Plain summary: " + " ".join(f"sentence{j}" for j in range(30)) + "."
    for i in range(6):
        ac.on_child_event("subagent.start", **_kid("dm", i, model="claude-sonnet-5-5"))
        ac.on_child_event("subagent.tool", tool_name="web_search", preview="q" * 70,
                          args={"query": "q" * 70}, tool_count=2, **_kid("dm", i))
    for i in range(6):
        ac.on_child_event("subagent.complete", status="completed", summary=para, **_kid("dm", i))
    text = card.render()
    assert len(text) < 4000
    assert text.count("\U0001f340 **Check site number") == 6 and "6 helpers" in text
    # a tighter budget cuts the summaries first (the titles and stats stay)
    full = card.render()
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        mp.setattr(ac, "CARD_BUDGET", len(full) - 300)
        cut = card.render()
    finally:
        mp.undo()
    assert len(cut) < len(full)
    assert cut.count("\U0001f340 **Check site number") == 6 and cut.count("tool calls") == 7


def test_cut_summaries_come_before_tool_lines(monkeypatch):
    goals = tuple(f"job {i}" for i in range(3))
    card = _multi_card(goals=goals)
    for i in range(3):
        _tool("dm", "terminal", f"a{i}", 1, task_index=i); _done("dm", "terminal", 1, task_index=i)
        _tool("dm", "terminal", f"b{i}", 2, task_index=i)
    full = card.render()
    monkeypatch.setattr(ac, "CARD_BUDGET", len(full) - 10)
    cut = card.render()
    assert len(cut) < len(full) and cut.count("\U0001f527") == 3 and cut.count("\u2713") == 0


def test_two_unified_cards_in_one_message_are_separated_by_a_blank_line():
    r1 = fl.DispatchReceipt(delegation_id="a", seq=1, goals=("g",), summary=HEAD, steps=("one",))
    r2 = fl.DispatchReceipt(delegation_id="b", seq=2, goals=("g",), summary=HEAD, steps=("two",))
    card = ac.card_for([r1, r2])
    text = card.render()
    assert "\n\n\U0001f504 **fixing the bugs**" in text
    assert text.count("\U0001f504 **fixing the bugs**") == 2
