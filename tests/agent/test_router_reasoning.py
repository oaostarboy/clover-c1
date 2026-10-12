"""The delegation router thinks on ``low`` by default; title generation does not.

Live eval (gpt-6-luna, 12 cases): reasoning ``none`` sized a 3-part compare as a
quick answer 0/2; ``low`` scored 12/12 at p50 1.5 s.  The OpenCode-style
"small call -> reasoning off" rule is right for a title and wrong for sizing.
"""
from __future__ import annotations

import json

import pytest

from agent import auxiliary_client as ac
from agent import delegation_router as dr


def _router(provider, model="m", extra=None, rc=None):
    return ac._small_call_reasoning(
        "delegation_router", provider, dict(extra or {}), rc, model=model)


class TestRouterDefaultsToLow:
    def test_codex_asks_for_low(self):
        rc, body = _router("openai-codex", "gpt-6-luna")
        assert rc is None
        assert body["reasoning"] == {"enabled": True, "effort": "low"}

    def test_gemini_flash_gets_thinking_level_low_not_minimal(self):
        from agent.transports.chat_completions import _build_gemini_thinking_config

        rc, body = _router("gemini", "gemini-3-flash-preview")
        assert rc == {"enabled": True, "effort": "low"} and "reasoning" not in body
        cfg = _build_gemini_thinking_config("gemini-3-flash-preview", rc)
        assert cfg["thinkingLevel"] == "low"

    def test_gemma_on_gemini_provider_is_left_alone(self):
        rc, body = _router("gemini", "gemma-3-27b")
        assert rc is None and "reasoning" not in body

    def test_anthropic_adaptive_model_gets_low_effort(self):
        rc, _ = _router("anthropic", "claude-sonnet-4-6")
        assert rc == {"enabled": True, "effort": "low"}

    def test_anthropic_haiku_is_left_as_it_was(self):
        rc, body = _router("anthropic", "claude-haiku-4-5")
        assert rc is None and "reasoning" not in body

    def test_openrouter_gets_low(self):
        rc, _ = _router("openrouter", "google/gemini-3-flash")
        assert rc == {"enabled": True, "effort": "low"}

    def test_unknown_provider_untouched(self):
        rc, body = _router("custom", "whatever")
        assert rc is None and "reasoning" not in body


class TestExplicitValueWins:
    def test_user_none_is_respected_on_codex(self):
        rc, body = _router("openai-codex", "gpt-6-luna", {"reasoning": {"enabled": False}})
        assert rc is None and body["reasoning"] == {"enabled": False}

    def test_user_high_is_respected(self):
        _, body = _router("openai-codex", "gpt-6-luna", {"reasoning": {"enabled": True, "effort": "high"}})
        assert body["reasoning"]["effort"] == "high"

    def test_caller_reasoning_config_is_respected(self):
        rc, body = _router("gemini", "gemini-3-flash", rc={"enabled": False})
        assert rc == {"enabled": False} and "reasoning" not in body

    def test_config_shorthand_reaches_extra_body(self, monkeypatch):
        """auxiliary.delegation_router.reasoning_effort -> extra_body.reasoning -> wins over the default."""
        monkeypatch.setattr(
            ac, "_get_auxiliary_task_config",
            lambda task: {"reasoning_effort": "none"} if task == "delegation_router" else {})
        extra = ac._get_task_extra_body("delegation_router")
        _, body = _router("openai-codex", "gpt-6-luna", extra)
        assert body["reasoning"] == {"enabled": False}


class TestOnlyTheRouterChanged:
    def test_router_default_applies_with_or_without_fast_model_pick(self):
        # even when the router is pinned (no fast pick), the default is low
        _, body = _router("openai-codex", "gpt-6-luna")
        assert body["reasoning"]["effort"] == "low"

    def test_title_generation_still_has_no_low_default_without_a_fast_pick(self):
        rc, body = ac._small_call_reasoning("title_generation", "openai-codex", {}, None)
        assert rc is None and "reasoning" not in body

    def test_other_tasks_untouched(self):
        rc, body = ac._small_call_reasoning("compression", "openai-codex", {}, None)
        assert rc is None and "reasoning" not in body


class TestWireShape:
    def test_codex_responses_wire_carries_low(self):
        """The extra_body the router builds becomes top-level reasoning.effort=low on the Responses call."""
        from unittest.mock import MagicMock

        _, body = _router("openai-codex", "gpt-6-luna")
        captured = {}

        client = MagicMock()

        def _create(**kw):
            captured.update(kw)
            raise RuntimeError("stop after capturing the request")

        client.responses.create.side_effect = _create
        adapter = ac._CodexCompletionsAdapter(client, "gpt-6-luna")
        try:
            adapter.create(messages=[{"role": "user", "content": "hi"}], extra_body=body, max_tokens=50)
        except Exception:
            pass
        assert captured.get("reasoning", {}).get("effort") == "low"


class TestTokenBudget:
    def test_cap_fits_low_reasoning_plus_the_largest_verdict(self):
        tasks = [
            {"goal": "g" * dr.MAX_GOAL_CHARS if False else "x" * 400, "acceptance": "a" * dr.MAX_ACCEPTANCE_CHARS,
             "deps": []}
            for _ in range(dr.DEFAULT_MAX_TASKS)
        ]
        reply = json.dumps({"mode": "parallel", "reason": "r" * dr.MAX_REASON_CHARS, "tasks": tasks})
        low_reasoning_burst = 450  # tokens; observed low-effort thought stays well under this
        assert len(reply) / 3 + low_reasoning_burst < dr.ROUTER_MAX_TOKENS

    def test_cap_is_not_the_old_700(self):
        assert dr.ROUTER_MAX_TOKENS > 700
