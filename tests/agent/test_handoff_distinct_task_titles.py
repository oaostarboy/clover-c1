"""Auto-handoff (router / backstop) tasks: distinct titles, self-contained
goals with the parent's context, and ``delegation.default_tier``.

Seen live: four tasks that all opened ``For silasstudios.ca, ...`` rendered as
four identical card rows (no ``title`` was sent); goals like "fix the third
remaining pre-launch issue listed in the last review" reached children that
cannot see the parent conversation; the children ran on the parent's model.

The dispatch tests drive the real executor funnel, the real ``delegate_task``
handler and real child construction (only a child's model conversation is
faked), like ``test_router_backstop``.
"""
from __future__ import annotations

import pytest

from agent import delegation_router as dr
from agent import handoff_title as ht
from agent import router_backstop as rb
from tests.agent.test_delegation_checkpoint import run_batch
from tests.agent.test_router_backstop import (  # noqa: F401  (fixtures)
    SETTINGS,
    _clean_registry,
    _root,
    _wait_started,
    _write,
    children,
    clock,
    dispatches,
)

# The four goals the router wrote on 2026-10-13.
SILAS_GOALS = [
    "For silasstudios.ca, remove the AI placeholder photos and videos and every 'placeholder' label and "
    "watermark from the site, replacing them with real photos and videos from the user's Instagram where "
    "available so the site looks as real as possible for partner previews. Report which images were "
    "replaced and which still need real media.",
    "For silasstudios.ca, make the contact form actually send submissions to the user's email (or the "
    "destination already agreed in the conversation) and test that a test submission is delivered. "
    "Report what was changed and the test result.",
    "For silasstudios.ca, fix the third remaining pre-launch issue listed in the last review of what is "
    "left before going live, changing only what is needed for that issue and running the site locally to "
    "confirm it works. Report the change made.",
    "For silasstudios.ca, complete the fourth remaining pre-launch item from the last review, changing "
    "only what is needed for that item and checking the affected pages still load on mobile and desktop. "
    "Report the change made.",
]


def _plan(goals, titles=None, mode="parallel"):
    titles = titles or [""] * len(goals)
    return dr.Verdict(mode=mode, reason="independent fixes", tasks=tuple(
        dr.PlanTask(goal=g, title=t) for g, t in zip(goals, titles)))


# ── distinct titles (pure) ─────────────────────────────────────────────────

def test_the_four_silasstudios_goals_get_four_different_short_titles():
    titles = ht.distinct_task_titles(SILAS_GOALS)

    assert len(titles) == 4
    assert len({ht.title_key(t) for t in titles}) == 4, titles
    for title in titles:
        assert 0 < len(title) <= 60
        assert title.strip().lower() != "for silasstudios.ca"
        assert not title.startswith("For silasstudios.ca")


def test_a_title_the_router_gave_is_kept_when_it_is_distinct():
    titles = ht.distinct_task_titles(SILAS_GOALS[:2], ["Replace placeholder media", "Fix contact form"])
    assert titles == ["Replace placeholder media", "Fix contact form"]


def test_duplicate_router_titles_are_told_apart():
    titles = ht.distinct_task_titles(SILAS_GOALS[:3], ["Pre-launch fix", "pre-launch fix.", "Pre-launch fix"])

    assert len({ht.title_key(t) for t in titles}) == 3, titles
    assert all(len(t) <= 60 for t in titles)


def test_identical_goals_still_get_distinct_titles():
    titles = ht.distinct_task_titles(["Compare three GPUs for me"] * 3)
    assert len({ht.title_key(t) for t in titles}) == 3, titles


def test_a_long_router_title_is_capped_at_60():
    (title,) = ht.distinct_task_titles(["Do the job in the repo"], ["x" * 20 + " " + "word " * 30])
    assert len(title) <= 60


def test_titles_never_raise_on_junk():
    assert ht.distinct_task_titles([]) == []
    assert len(ht.distinct_task_titles(["", None, "   "])) == 3


# ── the router's reply carries a title ─────────────────────────────────────

def test_the_router_schema_carries_a_title_per_task():
    verdict = dr.parse_verdict({"mode": "parallel", "tasks": [
        {"goal": "Replace the placeholder photos on silasstudios.ca", "title": "Replace placeholder media"},
        {"goal": "Make the contact form on silasstudios.ca send email"},
    ]})
    assert [t.title for t in verdict.tasks] == ["Replace placeholder media", ""]


def test_an_unusable_title_never_invalidates_the_plan():
    verdict = dr.parse_verdict({"mode": "single", "tasks": [{"goal": "Build the report", "title": 7}]})
    assert verdict.tasks[0].title == ""


def test_the_router_prompt_asks_for_titles_and_self_contained_goals():
    for prompt in (dr.ROUTER_SYSTEM_PROMPT_V2, dr.router_system_prompt()):
        assert '"title"' in prompt
        assert "ONLY its goal" in prompt
        assert "the last review" in prompt  # named as the thing never to write bare
    assert dr.router_system_prompt().format(max_tasks=3)  # still a .format() template


# ── the parent's context for each helper ───────────────────────────────────

REVIEW = (
    "Here is what is left before silasstudios.ca can go live: 1. AI placeholder photos still on the home "
    "page. 2. Contact form does not send email. 3. The booking page has no mobile menu. "
    "4. No sitemap.xml or robots.txt. Want me to fix these?"
)
HISTORY = [("user", "review silasstudios.ca for launch"), ("assistant", REVIEW)]


def test_the_excerpt_holds_the_parent_message_the_plan_refers_to():
    excerpt = rb.conversation_excerpt(HISTORY)

    assert "The booking page has no mobile menu" in excerpt
    assert "review silasstudios.ca for launch" in excerpt
    assert excerpt.index("review silasstudios.ca") < excerpt.index("booking page")


def test_the_excerpt_is_capped_and_keeps_the_newest_turn_and_its_end():
    huge = [("user", "u" * 5000), ("assistant", "a" * 6000 + " FINAL-OFFER"), ("user", "ok " * 3000)]
    excerpt = rb.conversation_excerpt(huge)

    assert len(excerpt) <= rb.EXCERPT_CHARS + 400  # the framing line rides on top of the cap
    assert "FINAL-OFFER" in excerpt


def test_the_excerpt_reads_the_history_without_changing_it():
    history = [("user", "q"), ("assistant", REVIEW)]
    before = list(history)
    rb.conversation_excerpt(history)
    assert history == before


def test_no_history_means_no_excerpt():
    assert rb.conversation_excerpt([]) == ""
    assert rb.conversation_excerpt(None) == ""
    assert rb.conversation_excerpt([("tool", "x"), ("assistant", "  ")]) == ""


def test_helper_context_includes_the_excerpt_and_stays_inside_its_budget():
    state = rb.RouterTurn(verdict=_plan(SILAS_GOALS), user_message="yes, fix all of them",
                          settings=SETTINGS, started_at=0.0, history=list(HISTORY))
    text = rb._helper_context(state, 2, 0, "")

    assert "The booking page has no mobile menu" in text
    assert "yes, fix all of them" in text
    assert len(text) <= rb.CONTEXT_CHARS

    state.history = [("assistant", "z" * 100_000)]
    state.user_message = "w" * 100_000
    assert len(rb._helper_context(state, 0, 0, "f" * 100_000)) <= rb.CONTEXT_CHARS


def test_a_later_stage_keeps_the_excerpt_through_the_durable_plan():
    from types import SimpleNamespace

    carried = rb.conversation_excerpt(HISTORY)
    pseudo = SimpleNamespace(verdict=_plan(SILAS_GOALS), user_message="yes", notes_path="", excerpt=carried)
    assert "The booking page has no mobile menu" in rb._helper_context(pseudo, 3, 1, "")


# ── the real dispatch ──────────────────────────────────────────────────────

pytestmark = pytest.mark.real_delegation_router


def _handoff(agent, tmp_path, verdict, history=HISTORY, message="yes, fix all of them"):
    rb.begin(agent, verdict, message, SETTINGS, history=[
        {"role": r, "content": t} for r, t in history])
    run_batch(agent, [_write(tmp_path, "a.txt"), _write(tmp_path, "b.txt"), _write(tmp_path, "c.txt")])


def test_the_auto_handoff_sends_a_distinct_title_per_task(tmp_path, children, dispatches):
    agent = _root()
    _handoff(agent, tmp_path, _plan(SILAS_GOALS))
    _wait_started(children, 4)

    tasks = dispatches[0]["tasks"]
    titles = [t["title"] for t in tasks]
    assert len(titles) == 4
    assert len({ht.title_key(t) for t in titles}) == len(titles), titles
    assert all(0 < len(t) <= 60 for t in titles)


def test_the_auto_handoff_titles_every_task_even_a_single_one(tmp_path, children, dispatches):
    agent = _root()
    _handoff(agent, tmp_path, _plan(["Compare three GPUs for the render box"], mode="single"))
    _wait_started(children, 1)

    (task,) = dispatches[0]["tasks"]
    assert task["title"] and len(task["title"]) <= 60


def test_the_auto_handoff_gives_each_child_the_parent_excerpt(tmp_path, children, dispatches):
    agent = _root()
    before = [dict(m) for m in [{"role": r, "content": t} for r, t in HISTORY]]
    _handoff(agent, tmp_path, _plan(SILAS_GOALS[2:]))
    _wait_started(children, 2)

    for task in dispatches[0]["tasks"]:
        assert "The booking page has no mobile menu" in task["context"]
        assert len(task["context"]) <= rb.CONTEXT_CHARS
    assert before == [{"role": r, "content": t} for r, t in HISTORY]


def test_the_auto_handoff_applies_delegation_default_tier(tmp_path, children, dispatches, monkeypatch):
    import tools.delegate_tool as dt
    from clover_constants import get_clover_home

    (get_clover_home() / "config.yaml").write_text(
        "delegation:\n  default_tier: code\n  tiers:\n    code:\n      - anthropic/claude-sonnet-5-5\n")
    monkeypatch.setattr(dt, "_provider_authenticated_cached", lambda p: p == "anthropic")
    routes: list = []

    def fake_creds(cfg_or_match, parent):
        routes.append(dict(cfg_or_match))
        return {"provider": "custom", "model": cfg_or_match.get("model"),
                "base_url": "http://127.0.0.1:1/v1", "api_key": "local-test-credential",
                "api_mode": "chat_completions", "request_overrides": None,
                "max_output_tokens": None}

    built: list = []
    real_build = dt._build_child_preserving_parent_tools

    def spy_build(**kwargs):
        built.append(kwargs.get("model"))
        return real_build(**kwargs)

    monkeypatch.setattr(dt, "_resolve_delegation_credentials", fake_creds)
    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", spy_build)
    agent = _root()
    _handoff(agent, tmp_path, _plan(SILAS_GOALS[:2]))
    _wait_started(children, 2)

    assert all("tier" not in t for t in dispatches[0]["tasks"]), "the runtime names no tier; the default applies"
    assert {"provider": "anthropic", "model": "claude-sonnet-5-5"} in routes
    assert built == ["claude-sonnet-5-5", "claude-sonnet-5-5"]
