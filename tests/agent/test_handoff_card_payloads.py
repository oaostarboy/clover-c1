"""Regression: the handoff card must show the model's own handoff summary.

The four payloads below are the exact ``delegate_task(handoff=...)`` values
observed on the live trial gateway. Two of them (the ones containing ``;`` or
``/``) were rejected by an allow-list validator and the card fell back to
"task details are unavailable in this summary". Any reasonable model-written
handoff must be shown, lightly normalized at most.
"""

from types import SimpleNamespace

import pytest

from agent import delegation_checkpoint as dc

OBSERVED = [
    pytest.param(
        {
            "work": "fixing the file-mutation verifier",
            "outcome": "a tested fix on a local branch that stops the false 'NOT modified' warning when a file changed through another route; not pushed, no restart",
        },
        "**delegated:** fixing the file-mutation verifier.\n\n"
        "**goal:** a tested fix on a local branch that stops the false 'NOT modified' warning when a file changed through another route; not pushed, no restart.",
        id="verifier-semicolon-fell-back",
    ),
    pytest.param(
        {
            "work": "Fixing the subagent model config",
            "outcome": "the code and read tiers use Sonnet 5.5, with a clean, checked config diff",
        },
        "**delegated:** Fixing the subagent model config.\n\n"
        "**goal:** the code and read tiers use Sonnet 5.5, with a clean, checked config diff.",
        id="model-config-worked",
    ),
    pytest.param(
        {
            "work": "Fixing the Windows /update crash for Troy-style setups",
            "outcome": "a PR with the fix, a test that fails on main and passes on the branch, and green real-Windows update runs for both Troy-style and Zyra-style setups; not merged",
        },
        "**delegated:** Fixing the Windows /update crash for Troy-style setups.\n\n"
        "**goal:** a PR with the fix, a test that fails on main and passes on the branch, and green real-Windows update runs for both Troy-style and Zyra-style setups; not merged.",
        id="windows-slash-update-fell-back",
    ),
    pytest.param(
        {
            "work": "Adding a spam filter to the Nice & Tidy quote form",
            "outcome": "a spam filter that real customers won't notice, tested on a preview link and waiting for your OK before it goes live",
        },
        "**delegated:** Adding a spam filter to the Nice & Tidy quote form.\n\n"
        "**goal:** a spam filter that real customers won't notice, tested on a preview link and waiting for your OK before it goes live.",
        id="spam-filter-worked",
    ),
]

_GENERIC = (
    "task details are unavailable",
    "will return to this conversation",
    "working on the delegated task",
    "the result described in the task",
)


def _card(goals, handoff=None):
    root = SimpleNamespace(
        valid_tool_names={"todo", "delegate_task"}, platform="telegram",
        _delegate_depth=0, _subagent_id=None, session_id="s",
        _interrupt_requested=False, _active_children=[],
        _active_children_lock=None,
        _delegation_checkpoint=dc.DelegationCheckpoint(),
    )
    checkpoint = root._delegation_checkpoint
    checkpoint.declare("delegate", "Long, independent work.")
    assert checkpoint.ticket().accept_handoff(
        delegation_id="deleg_card_regression",
        goals=goals,
        subagent_ids=["subagent_private"],
        handoff=handoff,
    )
    directive = dc.completion_directive(root)
    assert directive is not None and directive.reason == "delegation_handoff"
    return directive.text


@pytest.mark.parametrize("handoff, _expected", OBSERVED)
def test_observed_handoff_work_and_outcome_reach_the_card(handoff, _expected):
    """Format-independent: the model's own words are on the card."""
    text = _card(["Do the delegated job and report back."], handoff)
    assert handoff["work"] in text
    assert handoff["outcome"] in text
    for generic in _GENERIC:
        assert generic not in text


@pytest.mark.parametrize("handoff, expected", OBSERVED)
def test_observed_handoff_card_text_is_exact(handoff, expected):
    assert _card(["Do the delegated job and report back."], handoff) == expected


def test_missing_handoff_falls_back_to_the_task_goal_not_generic_copy():
    text = _card(["Fix the Windows /update crash for Troy-style setups. Open a PR and run CI."])
    assert text == (
        "**delegated:** Fix the Windows /update crash for Troy-style setups.\n\n"
        "**goal:** a completed result for Fix the Windows /update crash for Troy-style setups, returned here."
    )
    for generic in _GENERIC:
        assert generic not in text


def test_long_missing_handoff_goal_is_truncated_cleanly():
    goal = (
        "Rebuild the whole quote form pipeline including validation, spam scoring, "
        "email routing, analytics events, preview deploys and the admin dashboard "
        "so that every path is covered by tests"
    )
    text = _card([goal])
    work = text.split("\n\n")[0][len("**delegated:** "):]
    assert work.startswith("Rebuild the whole quote form pipeline")
    assert work.endswith("…") and len(work) <= 120
    assert not work[:-1].endswith(" ")
    for generic in _GENERIC:
        assert generic not in text


def test_handoff_with_only_outcome_uses_goal_for_work():
    text = _card(["Audit the ads account."], {"work": "  ", "outcome": "a short audit report"})
    assert text == (
        "**delegated:** Audit the ads account.\n\n"
        "**goal:** a short audit report."
    )


def test_markup_newlines_and_overlong_handoff_are_lightly_normalized():
    text = _card(
        ["Fix it."],
        {
            "work": "**fixing** the `gateway` crash\nsecond line ignored",
            "outcome": "a fix " + "with careful checks " * 15,
        },
    )
    first, second = text.split("\n\n")
    assert first == "**delegated:** fixing the gateway crash."
    assert second.startswith("**goal:** a fix with careful checks")
    assert second.endswith("…") and len(second) <= len("**goal:** ") + 180
