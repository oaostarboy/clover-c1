"""Per-helper checklists: ``tasks[i].steps`` seeds each helper's own todo list,
and the card routes every helper's todo events to ITS steps by ``task_index``."""

from __future__ import annotations

import json
import re
import time
from types import SimpleNamespace

import pytest

import tools.async_delegation as ad
import tools.delegate_tool as dt
from agent import ack_checklist as ac
from agent import foreground_time_limit as fl

pytestmark = pytest.mark.unified_card

HEAD = "\U0001f504 checking the 3 sites \u2192 a report, ~5 min"
_CREDS = {'model': 'm', 'provider': None, 'base_url': None, 'api_key': None,
          'api_mode': None, 'command': None, 'args': None}


@pytest.fixture(autouse=True)
def _clean():
    ad._reset_for_tests()
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    yield
    ad._reset_for_tests()
    ac.reset_for_tests()


# -- schema ------------------------------------------------------------------

def test_task_schema_offers_optional_steps_in_one_short_sentence():
    item = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]
    steps = item["properties"]["steps"]
    assert steps["type"] == "array" and steps["items"] == {"type": "string"}
    assert "steps" not in item["required"]
    assert steps["description"].count(".") == 1 and len(steps["description"]) < 100


def test_task_steps_use_the_same_validation_as_handoff_steps():
    raw = ["  a  ", "", 5, "x" * 200, "b\nsecond", "c", "d", "e", "f", "g"]
    out = ac.clean_steps(raw)
    assert len(out) == 6 and out[0] == "a" and all(len(x) <= ac.MAX_STEP_CHARS for x in out)


# -- seeding -------------------------------------------------------------------

def _root():
    from run_agent import AIAgent

    agent = AIAgent(
        provider='custom', api_mode='chat_completions',
        base_url='http://127.0.0.1:1/v1', api_key='local-test-credential',
        model='chosen-orchestrator', model_pinned=True,
        enabled_toolsets=['todo', 'delegation', 'file', 'terminal'],
        max_iterations=6, quiet_mode=True, tool_progress_mode='off',
        skip_context_files=True, load_soul_identity=False,
        skip_memory=True, skip_background_review=True, platform='telegram',
    )
    fl.begin_turn(agent)
    return agent


class _Store:
    def __init__(self):
        self.items = None

    def write(self, items, *a, **k):
        self.items = items


class _Child:
    def __init__(self, index):
        self._delegate_role = 'leaf'
        self._subagent_id = f'sa-{index}'
        self.session_id = f'cs-{index}'
        self._todo_store = _Store()

    def close(self): pass

    def get_activity_summary(self):
        return {'api_call_count': 0, 'current_tool': None, 'last_activity_ts': time.time()}


@pytest.fixture
def built(monkeypatch):
    kids, contexts = [], []

    def _build(**kw):
        c = _Child(kw['task_index'])
        kids.append(c)
        contexts.append(kw.get('task_context') or kw.get('context'))
        kw['parent_agent']._active_children.append(c)
        return c

    monkeypatch.setattr(dt, '_build_child_agent', _build)
    monkeypatch.setattr(dt, '_run_single_child', lambda *a, **k: {
        'task_index': a[0], 'status': 'completed', 'summary': 's', 'api_calls': 1,
        'duration_seconds': 0.0, 'model': 'm', 'exit_reason': 'completed'})
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **k: _CREDS)
    monkeypatch.setattr(dt, '_get_max_async_children', lambda: 3)
    return SimpleNamespace(kids=kids, contexts=contexts)


def _handoff():
    return {"work": "checking the 3 sites", "outcome": "a report", "steps": ["ignored for many"]}


def test_each_child_todo_list_is_seeded_from_its_own_steps(built):
    root = _root()
    tasks = [
        {"goal": "check the nice and tidy site thoroughly", "steps": ["open it", "check links"]},
        {"goal": "check the silas studios site thoroughly"},
        {"goal": "check the pizzeria bros pamphlet", "steps": ["read it", "list typos", "report"]},
    ]
    out = json.loads(dt.delegate_task(parent_agent=root, tasks=tasks, handoff=_handoff(), background=True))
    assert out["status"] == "dispatched", out
    assert [c._todo_store.items for c in built.kids] == [
        ac.seed_todos(("open it", "check links")),
        None,
        ac.seed_todos(("read it", "list typos", "report")),
    ]
    rec = list(root._fg_own_dispatches.values())[0]
    assert rec.child_steps == (("open it", "check links"), (), ("read it", "list typos", "report"))


def test_a_single_task_keeps_the_handoff_steps(built):
    root = _root()
    ho = {"work": "fixing x", "outcome": "a PR", "steps": ["one", "two"]}
    dt.delegate_task(parent_agent=root, tasks=[{"goal": "fix x thoroughly and report back", "steps": ["ignored"]}],
                     handoff=ho, background=True)
    assert built.kids[0]._todo_store.items == ac.seed_todos(("one", "two"))
    assert list(root._fg_own_dispatches.values())[0].child_steps == ()


def test_no_task_steps_means_nothing_is_seeded(built):
    root = _root()
    dt.delegate_task(parent_agent=root, tasks=[{"goal": "check the first site thoroughly"},
                                               {"goal": "check the second site thoroughly"}],
                     handoff=_handoff(), background=True)
    assert [c._todo_store.items for c in built.kids] == [None, None]


# -- routing by task_index -----------------------------------------------------

def _todos(*statuses):
    return [{"id": f"s{i + 1}", "status": s} for i, s in enumerate(statuses)]


def _card3(steps=(("open", "links"), (), ("read", "typos", "report"))):
    r = fl.DispatchReceipt(
        delegation_id="dm", seq=1, goals=("a", "b", "c"), summary=HEAD, steps=(),
        child_steps=tuple(tuple(s) for s in steps),
    )
    card = ac.card_for([r])
    for i, t in enumerate(("nice & tidy site", "silas studios site", "pizzeria bros pamphlet")):
        ac.on_child_event("subagent.start", delegation_id="dm", task_index=i, task_count=3, title=t)
    return card


def _rows(card, i):
    return [tuple(r) for r in card.parts[0].checklist.kids[i].steps]


def test_todo_events_tick_only_their_own_helpers_steps():
    card = _card3()
    assert _rows(card, 0) == [("open", "in_progress"), ("links", "pending")]
    ac.on_child_event("subagent.todo", delegation_id="dm", task_index=2,
                      todos=_todos("completed", "in_progress", "pending"))
    ac.on_child_event("subagent.todo", delegation_id="dm", task_index=0,
                      todos=_todos("in_progress", "pending"))
    assert [r[1] for r in card.parts[0].checklist.kids[2].steps] == ["completed", "in_progress", "pending"]
    assert [r[1] for r in card.parts[0].checklist.kids[0].steps] == ["in_progress", "pending"]
    assert card.parts[0].checklist.kids[1].steps == []


def test_a_finished_helper_keeps_its_steps_with_final_icons():
    card = _card3()
    ac.on_child_event("subagent.todo", delegation_id="dm", task_index=0, todos=_todos("completed", "in_progress"))
    ac.on_child_event("subagent.complete", delegation_id="dm", task_index=0, task_count=3, status="completed",
                      summary="Result: done\nPlain summary: Tidy.")
    ac.on_child_event("subagent.complete", delegation_id="dm", task_index=2, task_count=3, status="failed",
                      summary="Plain summary: broke.")
    kids = card.parts[0].checklist.kids
    assert [r[1] for r in kids[0].steps] == ["completed", "completed"]
    assert [r[1] for r in kids[2].steps] == ["failed", "pending", "pending"], "where it stopped"
    text = card.render()
    assert "\U0001f340 **nice & tidy site** \u00b7 done" in text, "a done helper is one line"
    assert "\u2713 open" not in text and "Tidy." not in text


def test_a_todo_that_arrives_before_the_part_opens_is_replayed_per_helper():
    ac.on_child_event("subagent.todo", delegation_id="late", task_index=1, todos=_todos("completed", "in_progress"))
    ac.on_child_event("subagent.todo", delegation_id="late", task_index=0, todos=_todos("in_progress", "pending"))
    r = fl.DispatchReceipt(delegation_id="late", seq=1, goals=("a", "b"), summary=HEAD, steps=(),
                           child_steps=(("x", "y"), ("p", "q")))
    card = ac.card_for([r])
    kids = card.parts[0].checklist.kids
    assert [r_[1] for r_ in kids[0].steps] == ["in_progress", "pending"]
    assert [r_[1] for r_ in kids[1].steps] == ["completed", "in_progress"]


# -- rendering -----------------------------------------------------------------

def test_three_helpers_render_name_steps_tools_stats_inside_one_quote():
    card = _card3()
    ac.on_child_event("subagent.todo", delegation_id="dm", task_index=2, todos=_todos("completed", "in_progress", "pending"))
    ac.on_child_event("subagent.tool", delegation_id="dm", task_index=2, task_count=3, tool_name="read_file",
                      preview="x.md", args={"path": "x.md"}, tool_call_id="t", tool_count=1)
    ac.on_child_event("subagent.complete", delegation_id="dm", task_index=0, task_count=3, status="completed",
                      summary="Result: done\nPlain summary: Tidy.")
    lines = card.render().splitlines()
    assert lines[0].startswith("\U0001f500 **3 subagents**") and "1 of 3 done" in lines[0]
    assert lines[1] == "> **checking the 3 sites**"
    assert all(ln.startswith("> ") or not ln for ln in lines[1:])
    text = "\n".join(lines)
    i_nice = text.index("**nice & tidy site**")
    i_silas = text.index("**silas studios site**")
    i_pizza = text.index("**pizzeria bros pamphlet**")
    assert i_nice < i_silas < i_pizza
    pizza = text[i_pizza:]
    # the tool line nests under the step in progress; the steps still to do come after it
    assert pizza.index("\u2713 read") < pizza.index("\u25b8 typos") < pizza.index("\U0001f527 *read_file x.md*")
    assert pizza.index("\U0001f527 *read_file x.md*") < pizza.index("\u25cb report")
    assert "\U0001f340 **nice & tidy site** \u00b7 done" in text[i_nice - 3:i_silas], "a done helper is ONE line"
    assert "open" not in text[i_nice:i_silas] and "\U0001f527" not in text[i_nice:i_silas]


def test_a_helper_without_steps_renders_as_before():
    card = _card3(steps=((), (), ()))
    text = card.render()
    assert "**silas studios site**" in text and "\u25cb" not in text, "no step rows at all"
    assert "typos" not in text


def test_over_six_helpers_finished_ones_still_collapse_to_one_line():
    n = 7
    r = fl.DispatchReceipt(delegation_id="big", seq=1, goals=tuple(f"g{i}" for i in range(n)), summary=HEAD,
                           steps=(), child_steps=tuple(("one", "two") for _ in range(n)))
    card = ac.card_for([r])
    for i in range(n):
        ac.on_child_event("subagent.start", delegation_id="big", task_index=i, task_count=n, title=f"site {i}")
    ac.on_child_event("subagent.complete", delegation_id="big", task_index=0, task_count=n, status="completed",
                      summary="Result: done\nPlain summary: Fine.")
    text = card.render()
    line = next(ln for ln in text.splitlines() if "site 0" in ln)
    assert line.startswith("> \U0001f340 **site 0** \u00b7 done"), "finished helper = one line"
    listed = [ln for ln in text.splitlines() if ln.endswith(" one")]
    assert len(listed) == ac.COMPACT_RUNNING, "only the running helpers shown in full list their steps"


def test_no_command_links_in_the_per_helper_card():
    card = _card3()
    ac.on_child_event("subagent.tool", delegation_id="dm", task_index=1, task_count=3, tool_name="terminal",
                      preview="ls /clover", args={"command": "ls /clover"}, tool_call_id="q", tool_count=1)
    assert not re.search(r"(?<![\w/])/[A-Za-z]", card.render())
