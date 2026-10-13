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

    def test_a_question_never_becomes_an_answering_title(self):
        title, _ = title_and_goal("why does this crash on startup?")
        assert title == FALLBACK_TITLE and "answering" not in title

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


class TestTheGoalReadsNaturally:
    """The goal never has the verb moved to the end ("..., run")."""

    @pytest.mark.parametrize("message, goal", [
        ("Run a diagnostic on why that fuse tripped.", "diagnostic on why that fuse tripped"),
        ("Can you also send the AutoHDR skill?", "send the AutoHDR skill"),
        ("research the best 2 TB NVMe for local AI models in Canada and write me a summary",
         "research on the best 2 TB NVMe for local AI models in Canada"),
        ("fix the login bug", "fix the login bug"),
        ("check my PSU and case fit", "check my PSU and case fit"),
    ])
    def test_real_messages(self, message, goal):
        assert title_and_goal(message)[1] == goal

    def test_the_gpu_comparison_is_a_noun_phrase(self):
        _, goal = title_and_goal(GPU)
        assert goal == "comparison of 3 options for adding a second GPU for local AI"

    @pytest.mark.parametrize("message", [
        "Run a diagnostic on why that fuse tripped.", GPU, "Can you also send the AutoHDR skill?",
        "research the best 2 TB NVMe for local AI models in Canada", "fix the login bug",
        "build the release then run the tests", "find out why the build is slow",
        "Use 3 subagents at once to do a full PC diagnostic and make upgrade recommendations.",
    ])
    def test_no_trailing_verb_and_no_participle(self, message):
        import re

        _, goal = title_and_goal(message)
        assert not re.search(r",\s+\w+$", goal), goal  # never "<phrase>, <verb>"
        verb = message.lower().split()[0]
        assert not goal.lower().endswith(f" {verb}"), goal

    def test_the_title_is_unchanged(self):
        assert title_and_goal("Run a diagnostic on why that fuse tripped.")[0] == (
            "running a diagnostic on why that fuse tripped")

    def test_the_routers_reason_still_wins_when_it_names_the_job(self):
        _, goal = title_and_goal("Run a diagnostic on why that fuse tripped.",
                                 "Diagnose why the fuse tripped.")
        assert goal == "Diagnose why the fuse tripped"


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
        assert goal.startswith("comparison of 3 options for adding a second GPU")

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
        assert rb._runtime_handoff(verdict, GPU)["work"] == "finishing the check on the subagent results"

    def test_the_summary_survives_the_cards_own_sanitiser(self):
        from agent.delegation_checkpoint import ack_summary_line

        line = ack_summary_line(handoff_summary(GPU, "Compare three GPUs against the user's setup."))
        assert line.startswith("\U0001f504 comparing 3 options for adding a second GPU")
        assert GPU not in line


# ── the title names the WORK, never the user's latest message ─────────────────

from agent import handoff_notes as hn  # noqa: E402
from agent.handoff_title import clean_model_goal, clean_model_title, resolve, strip_profanity  # noqa: E402

COMPLAINT = "Why the fuck do you keep stopping"
PUSH = "Push it to the domain. dndsecurity.ca"


class TestNeverEchoesTheMessage:
    def test_the_complaint_with_a_todo_is_titled_from_the_todo(self):
        found = resolve(COMPLAINT, todo="add images to DND website")
        assert found[0] == "adding images to DND website" and found.source == "todo"
        blob = " ".join(found).lower()
        assert "fuck" not in blob and "answering" not in blob and "stopping" not in blob

    def test_order_of_sources(self):
        assert resolve(COMPLAINT, todo="add images to DND website", last_title="fixing the login bug",
                       earlier=["finish the DND Security website"]).source == "todo"
        assert resolve(COMPLAINT, fallback_goal="Audit the Google Ads account for Montreal.",
                       last_title="fixing the login bug").source == "plan"
        found = resolve(COMPLAINT, last_title="fixing the login bug",
                        earlier=["finish the DND Security website"])
        assert (found[0], found.source) == ("fixing the login bug", "delegation")
        found = resolve(COMPLAINT, earlier=["finish the DND Security website"])
        assert (found[0], found.source) == ("finishing the DND Security website", "earlier")
        found = resolve(COMPLAINT)
        assert (found[0], found.source) == ("finishing this up", "default")

    def test_a_last_title_that_echoed_a_message_is_not_reused(self):
        assert resolve(COMPLAINT, last_title="answering: why the fuck do you keep stopping")[0] == "finishing this up"

    @pytest.mark.parametrize("message", [
        COMPLAINT, "why did you stop?", "what's going on", "how is it going", "are you done",
        "did you finish", "is it live yet", "this is shit, fix it", "wtf", "thanks", "ok cool", "hmm",
        "you keep stopping", "stop stopping",
    ])
    def test_chitchat_complaints_and_questions_never_become_a_title(self, message):
        title, goal = title_and_goal(message, todo="add images to DND website")
        assert title == "adding images to DND website"
        for text in (title, goal):
            assert message.lower() not in text.lower() and "answering" not in text

    @pytest.mark.parametrize("message", [COMPLAINT, "shit", "fuckin stopping again", "wtf"])
    def test_profanity_is_never_shown_even_with_nothing_else_to_go_on(self, message):
        title, goal = title_and_goal(message)
        assert (title, goal) == ("finishing this up", "the finished result")

    def test_innocent_words_are_not_profanity(self):
        assert strip_profanity("review the Arsenal and Dickens pages, assess the class") == (
            "review the Arsenal and Dickens pages, assess the class")

    def test_profanity_inside_a_todo_or_earlier_request_is_stripped_or_skipped(self):
        assert "shit" not in " ".join(resolve(COMPLAINT, todo="fix the shit build", earlier=["fix the login bug"]))
        assert strip_profanity("fix the fucking build, damn it") == "fix the build, it"

    def test_push_it_to_the_domain_is_not_working_on_push_it_to_the_domain(self):
        title, goal = title_and_goal(PUSH)
        assert "push it to the domain" not in (title + goal).lower()
        assert title == "finishing this up"
        assert resolve(PUSH, earlier=["deploy the DND site"])[0] == "deploying the DND site"

    def test_a_plain_task_still_gets_a_sensible_title(self):
        assert title_and_goal("Run a diagnostic on why that fuse tripped.")[0] == (
            "running a diagnostic on why that fuse tripped")
        assert resolve("Run a diagnostic on why that fuse tripped.", todo="x y z").source == "message"

    def test_a_polite_order_is_still_an_order(self):
        assert title_and_goal("Could you push the DND site live")[0] == "pushing the DND site live"

    def test_the_answering_prefix_pattern_is_gone(self):
        import inspect

        import agent.handoff_title as ht

        assert "answering: " not in inspect.getsource(ht).replace('"answering', "").replace("(?:answering", "")


class TestModelTitle:
    @pytest.mark.parametrize("raw, ok", [
        ("finishing the DND website", "finishing the DND website"),
        ("Deploying the DND site to dndsecurity.ca.", "deploying the DND site to dndsecurity.ca"),
        ('"fixing the login bug"', "fixing the login bug"),
    ])
    def test_a_good_title_is_kept(self, raw, ok):
        assert clean_model_title(raw, COMPLAINT) == ok

    @pytest.mark.parametrize("raw", [
        "answering why you keep stopping", "why do you keep stopping", "stopping", "fixing the fucking site",
        "the DND website", "", "replying to your message about the site",
    ])
    def test_a_bad_title_is_dropped(self, raw):
        assert clean_model_title(raw, COMPLAINT) == ""

    def test_a_title_that_quotes_the_message_is_dropped(self):
        assert clean_model_title("deploying the site now", "deploying the site now") == ""

    def test_the_goal_is_checked_too(self):
        assert clean_model_goal("site live on dndsecurity.ca", COMPLAINT) == "site live on dndsecurity.ca"
        assert clean_model_goal("fuck this site", COMPLAINT) == ""
        assert clean_model_goal("w " * 30, COMPLAINT) == ""


class TestBriefParsing:
    def test_json_gives_title_goal_and_known(self):
        brief = hn.parse_brief(
            '{"title": "finishing the DND website", "goal": "site live on dndsecurity.ca", '
            '"known": ["Pages are built.", "Images are missing."]}', COMPLAINT)
        assert (brief.title, brief.goal) == ("finishing the DND website", "site live on dndsecurity.ca")
        assert brief.known == ["Pages are built.", "Images are missing."]

    def test_fenced_json_is_accepted(self):
        brief = hn.parse_brief('```json\n{"title": "fixing the login bug", "goal": "bug gone", "known": []}\n```')
        assert brief.title == "fixing the login bug"

    def test_the_old_bullet_reply_still_gives_bullets_and_no_title(self):
        brief = hn.parse_brief("- one\n- two")
        assert brief.known == ["one", "two"] and brief.title == "" and brief.goal == ""

    def test_a_profane_or_echoing_title_is_dropped_but_known_is_kept(self):
        brief = hn.parse_brief('{"title": "answering why the fuck", "goal": "ok ok", "known": ["Fucking pages done."]}',
                               COMPLAINT)
        assert brief.title == "" and "fuck" not in brief.known[0].lower()


class TestBriefCall:
    @pytest.fixture
    def aux(self, monkeypatch):
        import time
        from types import SimpleNamespace

        import agent.auxiliary_client as ac

        box = SimpleNamespace(calls=[], reply='', error=None, delay=0.0)

        def fake(task=None, *, messages=None, **kw):
            box.calls.append({"task": task, "messages": messages, **kw})
            if box.delay:
                time.sleep(box.delay)
            if box.error:
                raise box.error
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=box.reply))])

        monkeypatch.setattr(ac, "call_llm", fake)
        return box

    def test_one_tiny_call_on_the_router_model_inside_the_dispatch_budget(self, aux):
        aux.reply = '{"title": "finishing the DND website", "goal": "site live", "known": ["a"]}'
        ctx = hn.title_context(message=COMPLAINT, history=[("user", "x"), ("assistant", "y")])
        brief = hn.handoff_title(ctx, message=COMPLAINT)
        (call,) = aux.calls
        assert call["task"] == "delegation_router" and call["timeout"] == hn.TITLE_TIMEOUT_SECONDS
        assert hn.TITLE_TIMEOUT_SECONDS <= 6.0 and call["max_tokens"] == hn.TITLE_MAX_TOKENS <= 200
        system = call["messages"][0]["content"]
        assert "NEVER quote" in system and "profanity" in system and "title" in system and "goal" in system
        assert "known" not in system, "the slow bullets are not part of the title call"
        assert brief.title == "finishing the DND website" and brief.known == []

    def test_the_title_input_is_short_even_for_a_huge_turn(self):
        log = hn.WorkLog()
        for i in range(30):
            log.add("terminal", "{" + "a" * 900 + "}", "r" * 20_000)
        history = [("user", "q" * 5000), ("assistant", "start " + "z" * 5000 + " want me to deploy the fix?")] * 6
        ctx = hn.title_context(message="ok, do it", history=history, todos=["[>] fixing it"], log=log)
        assert len(ctx) < 4_000
        assert "want me to deploy the fix?" in ctx, "the END of a long assistant turn (its offer) is kept"

    def test_a_timeout_gives_an_empty_brief(self, aux):
        aux.delay = 1.0
        assert hn.handoff_title("n", timeout=0.1) == hn.Brief()

    def test_an_error_gives_an_empty_brief(self, aux):
        aux.error = RuntimeError("down")
        assert hn.handoff_title("n") == hn.Brief()
