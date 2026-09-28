"""Value-routed delegation tiers (task.tier -> ordered provider/model
candidates) and the plan-first delegation prompts that ship alongside them.

Covers: tier resolution against local (no-network) credential state, the
delegation.tiers config override/disable semantics, the dynamic schema's
optional per-task ``tier`` property, precedence against a
delegation.model/provider pin and plain parent inheritance, tier_fallback
bookkeeping when nothing authenticates, the plan-first system-prompt
additions, and that children still inherit the parent's skills toolset with
tiers in play (the standing requirement -- no code change expected there).
"""

import json
import threading
from unittest.mock import MagicMock, patch

import tools.delegate_tool as dt
from tools.delegate_tool import (
    _build_child_agent,
    _build_child_system_prompt,
    _build_dynamic_schema_overrides,
    _effective_tiers,
    _resolve_tier,
    _run_single_child,
    delegate_task,
)


def _make_mock_parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    return parent


class _StubChild:
    """Minimal child agent double (mirrors test_delegate_output_schema.py)."""

    tool_progress_callback = None
    _delegate_saved_tool_names: list = []
    _credential_pool = None
    _subagent_id = None
    _delegate_depth = 1
    _parent_subagent_id = None
    _delegate_output_schema = None
    model = "test-model"
    session_prompt_tokens = 0
    session_completion_tokens = 0
    session_estimated_cost_usd = 0.0
    session_reasoning_tokens = 0

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []

    def get_activity_summary(self):
        return {"api_call_count": 1, "max_iterations": 5, "current_tool": None}

    def run_conversation(self, user_message, task_id=None, **_kwargs):
        self.calls.append(user_message)
        text = self.responses.pop(0)
        return {
            "final_response": text,
            "completed": True,
            "api_calls": 1,
            "messages": [],
        }

    def close(self):
        return None


class _StubParent:
    _current_task_id = None
    _delegate_depth = 0

    def _touch_activity(self, _desc):
        return None


def _run_child(child):
    return _run_single_child(0, "do the thing", child, _StubParent())


def _fake_resolve_delegation_credentials(cfg_or_match, _parent_agent):
    """Stands in for the real resolver: distinguishes a bare tier-match dict
    (``{"provider", "model"}`` only) from the full delegation config dict."""
    base = {
        "provider": None,
        "model": None,
        "base_url": None,
        "api_key": None,
        "api_mode": None,
        "request_overrides": None,
        "max_output_tokens": None,
    }
    if set(cfg_or_match.keys()) == {"provider", "model"}:
        base.update(cfg_or_match)
        return base
    base["model"] = cfg_or_match.get("model") or None
    base["provider"] = cfg_or_match.get("provider") or None
    return base


# ---------------------------------------------------------------------------
# _resolve_tier / _effective_tiers -- pure resolution behavior
# ---------------------------------------------------------------------------


class TestResolveTier:
    def test_resolves_to_first_authenticated_candidate(self, monkeypatch):
        """Second candidate wins when the first candidate's provider has no
        local credentials -- a walk, not a straight pick of index 0."""
        cfg = {"tiers": {"code": ["anthropic/claude-sonnet-5", "openai-codex/gpt-6-luna"]}}
        monkeypatch.setattr(
            dt, "_provider_authenticated_cached", lambda p: p == "openai-codex"
        )
        assert _resolve_tier("code", cfg) == {
            "provider": "openai-codex",
            "model": "gpt-6-luna",
        }

    def test_all_candidates_unauthenticated_resolves_to_none(self, monkeypatch):
        cfg = {"tiers": {"code": ["anthropic/claude-sonnet-5", "openai-codex/gpt-6-luna"]}}
        monkeypatch.setattr(dt, "_provider_authenticated_cached", lambda p: False)
        assert _resolve_tier("code", cfg) is None

    def test_unknown_tier_name_resolves_to_none(self, monkeypatch):
        cfg = {"tiers": {"code": ["anthropic/claude-sonnet-5"]}}
        monkeypatch.setattr(dt, "_provider_authenticated_cached", lambda p: True)
        assert _resolve_tier("does-not-exist", cfg) is None

    def test_user_override_replaces_default_list_entirely(self, monkeypatch):
        """A user tier REPLACES the built-in list of the same name -- it does
        not merge with it. anthropic is authenticated but is not a candidate
        of this (user-supplied) list, so resolution must fail rather than
        silently falling through to a default candidate that isn't there."""
        cfg = {"tiers": {"code": ["gemini/custom-model"]}}
        monkeypatch.setattr(
            dt, "_provider_authenticated_cached", lambda p: p == "anthropic"
        )
        assert _resolve_tier("code", cfg) is None
        monkeypatch.setattr(
            dt, "_provider_authenticated_cached", lambda p: p == "gemini"
        )
        assert _resolve_tier("code", cfg) == {
            "provider": "gemini",
            "model": "custom-model",
        }


class TestTiersDisableEscapeHatch:
    def test_empty_dict_in_real_config_file_disables_tiers(self, tmp_path, monkeypatch):
        """``delegation.tiers: {}`` in the user's own config.yaml disables
        tiering end-to-end, even though the generic deep-merge folds an
        empty-dict override to a no-op against DEFAULT_CONFIG's tier
        defaults (see _tiers_explicitly_disabled's docstring) -- this proves
        the escape hatch through the real file + real merge, not a mock."""
        home = tmp_path / "clover_test"
        (home / "config.yaml").write_text("delegation:\n  tiers: {}\n")

        cfg = dt._load_config()
        # The deep-merge alone still shows the built-in defaults...
        assert set(cfg.get("tiers", {})) == {"deep", "code", "read", "check", "fast"}
        # ...but the explicit {} the user wrote is honored on top of it.
        assert dt._tiers_explicitly_disabled() is True
        assert _effective_tiers(cfg) == {}

        monkeypatch.setattr(dt, "_provider_authenticated_cached", lambda p: True)
        assert dt._resolved_tiers(cfg) == {}
        overrides = _build_dynamic_schema_overrides()
        tprops = overrides["parameters"]["properties"]["tasks"]["items"]["properties"]
        assert "tier" not in tprops


# ---------------------------------------------------------------------------
# Dynamic schema: only-when-resolved tier property + cache stability
# ---------------------------------------------------------------------------


class TestDynamicSchemaTierProperty:
    def test_tier_omitted_when_nothing_authenticates(self, monkeypatch):
        monkeypatch.setattr(dt, "_provider_authenticated_cached", lambda p: False)
        overrides = _build_dynamic_schema_overrides()
        tprops = overrides["parameters"]["properties"]["tasks"]["items"]["properties"]
        assert "tier" not in tprops
        # The static schema is never mutated by schema-building calls.
        assert "tier" not in dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"][
            "tasks"
        ]["items"]["properties"]

    def test_tier_present_and_lists_only_resolved_tiers(self, monkeypatch):
        monkeypatch.setattr(
            dt, "_provider_authenticated_cached", lambda p: p == "anthropic"
        )
        overrides = _build_dynamic_schema_overrides()
        tprops = overrides["parameters"]["properties"]["tasks"]["items"]["properties"]
        assert "tier" in tprops
        desc = tprops["tier"]["description"]
        # anthropic covers deep/code/read (their first-or-fallback candidate);
        # check/fast only offer xai/gemini candidates, so they stay absent.
        assert "deep" in desc and "code" in desc and "read" in desc
        assert "check ->" not in desc
        assert "fast ->" not in desc

    def test_schema_description_is_byte_stable_across_calls(self, monkeypatch):
        """Same config + same credential state must produce byte-identical
        schema text -- required for prompt caching (a changing tool schema
        invalidates the cached prefix every call)."""
        monkeypatch.setattr(
            dt, "_provider_authenticated_cached", lambda p: p == "anthropic"
        )
        first = _build_dynamic_schema_overrides()
        second = _build_dynamic_schema_overrides()
        assert first["description"] == second["description"]
        first_tier = first["parameters"]["properties"]["tasks"]["items"]["properties"]["tier"]
        second_tier = second["parameters"]["properties"]["tasks"]["items"]["properties"]["tier"]
        assert first_tier == second_tier

    def test_top_level_description_stays_under_length_ceiling_with_tiers(
        self, monkeypatch
    ):
        """Mirrors test_delegate.py's compaction ceiling: adding the
        plan-first block must not blow the existing budget even when tiers
        resolve and both plan-first sentences are appended."""
        monkeypatch.setattr(
            dt, "_provider_authenticated_cached", lambda p: p == "anthropic"
        )
        overrides = _build_dynamic_schema_overrides()
        assert len(overrides["description"]) <= 2200
        assert "PLAN FIRST" in overrides["description"]


class TestTopLevelDescriptionPlanFirst:
    def test_plan_first_present_even_with_zero_resolved_tiers(self, monkeypatch):
        monkeypatch.setattr(dt, "_provider_authenticated_cached", lambda p: False)
        desc = dt._build_top_level_description(resolved_tiers={})
        assert "PLAN FIRST, THEN DELEGATE" in desc
        assert "Pick `tier`" not in desc

    def test_tier_pick_sentence_only_when_tiers_resolve(self):
        desc = dt._build_top_level_description(
            resolved_tiers={"code": {"provider": "anthropic", "model": "claude-sonnet-5"}}
        )
        assert "PLAN FIRST, THEN DELEGATE" in desc
        assert "Pick `tier`" in desc


# ---------------------------------------------------------------------------
# Child system prompt: plan-first paragraph gated on non-empty context
# ---------------------------------------------------------------------------


class TestChildSystemPromptPlanFirst:
    def test_no_context_no_plan_first_paragraph(self):
        prompt = _build_child_system_prompt("Fix the tests")
        assert "already planned this task" not in prompt

    def test_context_present_adds_plan_first_paragraph(self):
        prompt = _build_child_system_prompt("Fix the tests", context="Use pytest -k foo")
        assert "CONTEXT:" in prompt
        assert "already planned this task" in prompt
        assert "Do not re-investigate decisions it states as settled" in prompt


# ---------------------------------------------------------------------------
# Skills toolset inheritance (the Ant requirement) -- no code change expected,
# this locks in the existing behavior.
# ---------------------------------------------------------------------------


class TestChildKeepsSkillsToolset:
    def test_child_inherits_parent_skills_toolset(self):
        parent = MagicMock()
        parent.enabled_toolsets = ["terminal", "file", "web", "skills"]
        parent.disabled_toolsets = []
        parent._delegate_depth = 0

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="Use a skill",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
                role="leaf",
            )

        _, kwargs = MockAgent.call_args
        assert "skills" in kwargs["enabled_toolsets"]


# ---------------------------------------------------------------------------
# _run_single_child: result entry's model is the model that actually ran
# ---------------------------------------------------------------------------


class TestResultEntryModel:
    def test_entry_model_matches_child_model_not_parent(self):
        child = _StubChild(["done"])
        child.model = "claude-sonnet-5"
        entry = _run_child(child)
        assert entry["model"] == "claude-sonnet-5"
        assert "tier_fallback" not in entry

    def test_entry_carries_tier_fallback_when_set_on_child(self):
        child = _StubChild(["done"])
        child.model = "inherited-model"
        child._delegate_tier_fallback = "tier 'code' has no authenticated candidate"
        entry = _run_child(child)
        assert entry["tier_fallback"] == "tier 'code' has no authenticated candidate"


# ---------------------------------------------------------------------------
# delegate_task end-to-end: precedence + fallback-never-fails-the-batch
# ---------------------------------------------------------------------------


class TestDelegateTaskTierPrecedence:
    def test_task_tier_beats_pin_beats_inherit(self, monkeypatch):
        captured = {}

        def fake_build(**kwargs):
            captured[kwargs["task_index"]] = kwargs
            child = _StubChild(["done"])
            child.model = kwargs.get("model")
            return child

        cfg = {
            "tiers": {"code": ["anthropic/claude-sonnet-5"]},
            "model": "pin-model",
            "provider": "pin-provider",
        }
        monkeypatch.setattr(
            dt, "_provider_authenticated_cached", lambda p: p == "anthropic"
        )

        with (
            patch("tools.delegate_tool._load_config", return_value=cfg),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                side_effect=_fake_resolve_delegation_credentials,
            ),
            patch(
                "tools.delegate_tool._build_child_preserving_parent_tools",
                side_effect=fake_build,
            ),
        ):
            out = delegate_task(
                tasks=[
                    {"goal": "tiered task gets its own model", "tier": "code"},
                    {"goal": "untiered task inherits the pin"},
                ],
                parent_agent=_make_mock_parent(),
            )

        payload = json.loads(out)
        assert not payload.get("error"), payload
        # Task 0: task.tier beats the delegation.model/provider pin.
        assert captured[0]["model"] == "claude-sonnet-5"
        assert captured[0]["override_provider"] == "anthropic"
        # Task 1: no tier -> falls through to the pin (not parent inherit).
        assert captured[1]["model"] == "pin-model"
        assert captured[1]["override_provider"] == "pin-provider"
        # D: the result entry's model is the model that actually ran.
        results_by_index = {r["task_index"]: r for r in payload["results"]}
        assert results_by_index[0]["model"] == "claude-sonnet-5"
        assert "tier_fallback" not in results_by_index[0]

    def test_pin_beats_plain_parent_inherit(self, monkeypatch):
        captured = {}

        def fake_build(**kwargs):
            captured[kwargs["task_index"]] = kwargs
            child = _StubChild(["done"])
            child.model = kwargs.get("model")
            return child

        cfg = {"tiers": {}, "model": "pin-model", "provider": "pin-provider"}
        with (
            patch("tools.delegate_tool._load_config", return_value=cfg),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                side_effect=_fake_resolve_delegation_credentials,
            ),
            patch(
                "tools.delegate_tool._build_child_preserving_parent_tools",
                side_effect=fake_build,
            ),
        ):
            delegate_task(
                tasks=[{"goal": "untiered task with a pin set"}],
                parent_agent=_make_mock_parent(),
            )

        assert captured[0]["model"] == "pin-model"
        assert captured[0]["override_provider"] == "pin-provider"

    def test_unresolvable_tier_falls_back_without_failing_batch(self, monkeypatch):
        captured = {}

        def fake_build(**kwargs):
            captured[kwargs["task_index"]] = kwargs
            child = _StubChild(["done"])
            child.model = kwargs.get("model")
            return child

        cfg = {"tiers": {"code": ["anthropic/claude-sonnet-5"]}, "model": "", "provider": ""}
        monkeypatch.setattr(dt, "_provider_authenticated_cached", lambda p: False)

        with (
            patch("tools.delegate_tool._load_config", return_value=cfg),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                side_effect=_fake_resolve_delegation_credentials,
            ),
            patch(
                "tools.delegate_tool._build_child_preserving_parent_tools",
                side_effect=fake_build,
            ),
        ):
            out = delegate_task(
                tasks=[{"goal": "tiered task with no authenticated candidate", "tier": "code"}],
                parent_agent=_make_mock_parent(),
            )

        payload = json.loads(out)
        assert not payload.get("error"), payload
        # Fell back to the batch-level (pin-or-inherit) credentials, not a hard error.
        assert captured[0]["model"] is None
        entry = payload["results"][0]
        assert entry.get("tier_fallback")
        assert "code" in entry["tier_fallback"]
