"""The hand-off card names the job (title + goal) without a model call."""
from __future__ import annotations

import pytest

from agent import delegation_router as dr
from agent import router_backstop as rb
from agent.handoff_title import (
    FALLBACK_GOAL, FALLBACK_TITLE, GOAL_CHARS, TITLE_CHARS, handoff_summary, title_and_goal,
)

GPU = ("compare 3 options for adding a second GPU for local AI (used 3090, 5090, R9700): "
       "check if my PSU and case fit, real Canadian prices, and what models each unlocks. "
       "short report at the end")


class TestTitleFromTheMessage:
    def test_the_gpu_request(self):
        title, goal = title_and_goal(GPU)
        assert title == "comparing 3 options for adding a second GPU"
        assert goal and goal != FALLBACK_GOAL and len(goal) <= GOAL_CHARS

    @pytest.mark.parametrize("message, title", [
        ("Can you also send the AutoHDR skill?", "sending the AutoHDR skill"),
        ("please fix the login bug then run the tests then cut a release", "fixing the login bug"),
        ("Use 3 subagents at once to do a full PC diagnostic and make upgrade recommendations for local AI use.",
         "doing a full PC diagnostic"),
        ("Hey, could you please research the best 2 TB NVMe for local AI models in Canada and write me a summary",
         "researching the best 2 TB NVMe for local AI models"),
    ])
    def test_filler_is_stripped_and_the_title_is_a_gerund(self, message, title):
        assert title_and_goal(message)[0] == title

    def test_a_question_gets_a_plain_title(self):
        title, _ = title_and_goal("why does this crash on startup?")
        assert title == "answering: why does this crash on startup"

    def test_limits_hold_for_any_input(self):
        for message in (GPU * 20, "x" * 5000, "a " * 3000, "compare " + "word " * 400):
            title, goal = title_and_goal(message)
            assert 0 < len(title) <= TITLE_CHARS and 0 < len(goal) <= GOAL_CHARS
            assert not title.endswith((" a", " the", " of", " and", " for", " to"))

    def test_a_long_message_is_cut_on_a_word_never_pasted(self):
        message = "compare " + " ".join(f"option{i}" for i in range(300))
        title, goal = title_and_goal(message)
        assert len(title) <= TITLE_CHARS and len(goal) <= GOAL_CHARS
        assert message not in title and message not in goal
        assert title.split()[-1].startswith("option") and title.split()[-1][-1].isdigit()

    def test_a_quoted_block_and_fence_are_not_shown(self):
        message = ('Please send Sam a reply to this:\n> Dear Sam, thanks for dinner, the pasta was lovely\n'
                   '> see you soon\n```\nsecret = abc123\n```\n"Dear Sam, thanks for dinner last night, we loved it"')
        title, goal = title_and_goal(message)
        for text in (title, goal):
            assert "Dear Sam" not in text and "secret" not in text and "abc123" not in text
        assert title.startswith("sending Sam a reply")

    def test_only_a_quote_falls_back(self):
        assert title_and_goal('> forwarded text that is long enough to be a quote\n> more') == (
            FALLBACK_TITLE, FALLBACK_GOAL)

    def test_a_link_is_not_shown(self):
        title, goal = title_and_goal("summarize https://example.com/a/very/long/path?q=1 for me")
        assert "http" not in title + goal

    @pytest.mark.parametrize("message", ["", "   ", "ok", "do it", "this", "x" * 500, None])
    def test_nothing_usable_falls_back_to_the_old_wording(self, message):
        title, goal = title_and_goal(message)
        assert title == FALLBACK_TITLE and goal in (FALLBACK_GOAL, "x" * 60 if message == "x" * 500 else FALLBACK_GOAL)

    def test_no_model_call(self, monkeypatch):
        from agent import auxiliary_client

        def boom(*a, **k):
            raise AssertionError("the title must not call a model")

        monkeypatch.setattr(auxiliary_client, "call_llm", boom)
        assert title_and_goal(GPU)[0]


class TestGoalFromTheRoutersReason:
    def test_a_reason_that_describes_the_job_is_the_goal(self):
        _, goal = title_and_goal(GPU, "Compare three GPUs against the user's setup, current Canadian pricing.")
        assert goal.startswith("Compare three GPUs against your setup")
        assert len(goal) <= GOAL_CHARS and "user's" not in goal

    @pytest.mark.parametrize("reason", [
        "Quick lookup, a few tool calls.", "Requires several steps and many lookups.",
        "Simple question.", "", "ok",
    ])
    def test_a_reason_about_the_sizing_is_not_used(self, reason):
        _, goal = title_and_goal(GPU, reason)
        assert goal.startswith("3 options for adding a second GPU")

    def test_a_long_reason_is_cut_not_pasted(self):
        reason = "Compare three GPUs against the user's setup, current Canadian pricing, and model capacity for local AI."
        _, goal = title_and_goal(GPU, reason)
        assert len(goal) <= GOAL_CHARS and goal in reason.replace("the user's", "your")


def _state(message, verdict):
    return type("S", (), {"user_message": message, "verdict": verdict})()


class TestBothBackstopPaths:
    def test_planned_path_uses_the_users_message_and_reason(self):
        verdict = dr.Verdict(
            mode="single", reason="Compare three GPUs against the user's setup and prices.",
            tasks=(dr.PlanTask(goal="Compare the GPUs and report."),))
        summary = rb._runtime_handoff(verdict, GPU)
        assert summary["work"] == "comparing 3 options for adding a second GPU"
        assert summary["outcome"].startswith("Compare three GPUs against your setup")

    def test_direct_path_uses_the_users_message_not_the_generic_task_goal(self):
        base = dr.Verdict(mode="direct", reason="Quick lookup.")
        verdict = rb._direct_plan(base, GPU)
        assert verdict.source == "direct_backstop"
        summary = rb._runtime_handoff(verdict, GPU)
        assert summary["work"] == "comparing 3 options for adding a second GPU"
        assert "Finish the user's request" not in summary["work"] + summary["outcome"]

    def test_direct_path_with_a_job_describing_reason(self):
        base = dr.Verdict(mode="direct", reason="Research and summarize NVMe prices in Canada.")
        summary = rb._runtime_handoff(rb._direct_plan(base, "nvme?"), "nvme?")
        assert summary["outcome"] == "Research and summarize NVMe prices in Canada"

    def test_planned_path_without_a_usable_message_uses_the_single_task_goal(self):
        verdict = dr.Verdict(mode="single", tasks=(dr.PlanTask(goal="Audit the Google Ads account for Montreal."),))
        assert rb._runtime_handoff(verdict, "do it")["work"] == "auditing the Google Ads account for Montreal"

    def test_staged_continuation_without_a_message_keeps_a_valid_summary(self):
        verdict = dr.Verdict(mode="staged", tasks=(dr.PlanTask(goal="a" * 12), dr.PlanTask(goal="b" * 12)))
        summary = rb._runtime_handoff(verdict)
        assert summary["work"] and summary["outcome"]

    def test_lifecycle_handoff_is_unchanged(self):
        verdict = dr.Verdict(mode="single", source="lifecycle", tasks=(dr.PlanTask(goal="x" * 12),))
        assert rb._runtime_handoff(verdict, GPU)["work"] == "finishing the check on the helper results"

    def test_the_summary_survives_the_cards_own_sanitiser(self):
        from agent.delegation_checkpoint import ack_summary_line

        line = ack_summary_line(handoff_summary(GPU, "Compare three GPUs against the user's setup."))
        assert line.startswith("\U0001f504 comparing 3 options for adding a second GPU")
        assert GPU not in line
