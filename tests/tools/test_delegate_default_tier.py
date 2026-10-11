"""``delegation.default_tier``: the worker model a task gets when it names none.

Drives the real ``delegate_task`` dispatcher against the real config loader
(``config.yaml`` written into the isolated CLOVER_HOME). Only the provider /
network boundary is faked: local credential probing, credential resolution and
child construction.

Contracts:
- the default is applied at dispatch (single goal and every batch task);
- an explicit ``tier`` always wins, resolvable or not;
- an unresolvable default falls back to today's pin/inherit path and says
  nothing in the tool result (log line only);
- ``""`` / absent key behaves exactly like a build without the setting;
- the tool schema is byte-identical with and without the setting.
"""

import json
import logging
import re
import threading
from unittest.mock import MagicMock, patch

import pytest

import tools.delegate_tool as dt
from clover_constants import get_clover_home
from tools.delegate_tool import _build_dynamic_schema_overrides, delegate_task


def _make_mock_parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    return parent


class _StubChild:
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

    def get_activity_summary(self):
        return {"api_call_count": 1, "max_iterations": 5, "current_tool": None}

    def run_conversation(self, user_message, task_id=None, **_kwargs):
        return {
            "final_response": "done",
            "completed": True,
            "api_calls": 1,
            "messages": [],
        }

    def close(self):
        return None


def _fake_resolve_delegation_credentials(cfg_or_match, _parent_agent):
    """A bare tier match (``{"provider", "model"}`` only) vs the full
    delegation config dict -- same stand-in the tier precedence tests use."""
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


def _write_delegation_config(lines: str) -> None:
    (get_clover_home() / "config.yaml").write_text("delegation:\n" + lines)


def _dispatch(monkeypatch, *, authenticated=("anthropic", "gemini"), **call):
    """Run the real dispatcher; return (payload, raw_result, build_kwargs_by_index)."""
    captured = {}

    def fake_build(**kwargs):
        captured[kwargs["task_index"]] = kwargs
        child = _StubChild()
        child.model = kwargs.get("model")
        return child

    monkeypatch.setattr(
        dt, "_provider_authenticated_cached", lambda p: p in authenticated
    )
    with (
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            side_effect=_fake_resolve_delegation_credentials,
        ),
        patch(
            "tools.delegate_tool._build_child_preserving_parent_tools",
            side_effect=fake_build,
        ),
    ):
        raw = delegate_task(parent_agent=_make_mock_parent(), **call)
    return json.loads(raw), raw, captured


@pytest.fixture(autouse=True)
def _fresh_tier_auth_cache():
    with dt._tier_auth_cache_lock:
        dt._tier_auth_cache.clear()
    yield
    with dt._tier_auth_cache_lock:
        dt._tier_auth_cache.clear()


class TestDefaultTierApplied:
    def test_shipped_default_is_empty_and_loads_through_real_loader(self):
        cfg = dt._load_config()
        assert cfg.get("default_tier") == ""

    def test_single_goal_gets_the_default(self, monkeypatch):
        _write_delegation_config("  default_tier: code\n")
        payload, _raw, captured = _dispatch(
            monkeypatch, goal="refactor the parser module"
        )
        assert not payload.get("error"), payload
        assert captured[0]["model"] == "claude-sonnet-5"
        assert captured[0]["override_provider"] == "anthropic"
        assert payload["results"][0]["model"] == "claude-sonnet-5"

    def test_every_batch_task_without_a_tier_gets_the_default(self, monkeypatch):
        _write_delegation_config("  default_tier: code\n")
        payload, _raw, captured = _dispatch(
            monkeypatch,
            tasks=[
                {"goal": "write the first module"},
                {"goal": "write the second module"},
                {"goal": "write the third module"},
            ],
        )
        assert not payload.get("error"), payload
        for i in range(3):
            assert captured[i]["model"] == "claude-sonnet-5"
            assert captured[i]["override_provider"] == "anthropic"
        assert all("tier_fallback" not in r for r in payload["results"])

    def test_no_pin_uses_the_default_route(self, monkeypatch):
        _write_delegation_config("  default_tier: code\n")
        _payload, _raw, captured = _dispatch(monkeypatch, goal="a big single job")
        assert captured[0]["model"] == "claude-sonnet-5"
        assert captured[0]["override_provider"] == "anthropic"

    def test_request_overrides_reach_the_default_route(self, monkeypatch):
        """A configured request_overrides (service_tier, extra_body) is merged
        exactly the way the explicit-override path merges it today; changing
        the worker model must not discard it."""
        _write_delegation_config(
            "  default_tier: code\n"
            "  request_overrides:\n"
            "    service_tier: flex\n"
            "    extra_body:\n"
            "      provider:\n"
            "        sort: throughput\n"
        )
        runtime_overrides = {"extra_body": {"thinking": {"type": "enabled"}}}
        monkeypatch.setattr(dt, "_provider_authenticated_cached", lambda p: True)
        monkeypatch.setattr(
            "clover_cli.runtime_provider.resolve_runtime_provider",
            lambda requested, target_model=None: {
                "provider": requested,
                "model": target_model,
                "api_key": "synthetic-key",
                "base_url": "https://provider.invalid",
                "api_mode": "chat_completions",
                "request_overrides": dict(runtime_overrides),
            },
        )
        captured = {}

        def fake_build(**kwargs):
            captured[kwargs["task_index"]] = kwargs
            child = _StubChild()
            child.model = kwargs.get("model")
            return child

        parent = _make_mock_parent()
        parent.request_overrides = None
        with patch(
            "tools.delegate_tool._build_child_preserving_parent_tools",
            side_effect=fake_build,
        ):
            raw = delegate_task(goal="a big single job", parent_agent=parent)
        assert not json.loads(raw).get("error"), raw
        assert captured[0]["model"] == "claude-sonnet-5"
        expected = dt._merge_request_overrides(
            runtime_overrides,
            {"service_tier": "flex", "extra_body": {"provider": {"sort": "throughput"}}},
        )
        assert captured[0]["override_request_overrides"] == expected
        assert captured[0]["override_request_overrides"]["service_tier"] == "flex"

    def test_internal_credentials_cfg_route_is_not_overridden(self, monkeypatch):
        _write_delegation_config("  default_tier: code\n")
        _payload, _raw, captured = _dispatch(
            monkeypatch,
            goal="review this diff",
            credentials_cfg={"provider": "review-provider", "model": "review-model"},
        )
        assert captured[0]["model"] == "review-model"
        assert captured[0]["override_provider"] == "review-provider"

    def test_internal_opt_out_flag_skips_the_default(self, monkeypatch):
        """The flag alone opts out; no credentials_cfg needed."""
        _write_delegation_config("  default_tier: code\n")
        _payload, _raw, captured = _dispatch(
            monkeypatch, goal="review this diff", apply_default_tier=False
        )
        assert captured[0]["model"] is None
        assert captured[0]["override_provider"] is None


class TestPinWins:
    @pytest.mark.parametrize(
        "pin",
        [
            "  model: pin-model\n  provider: pin-provider\n",
            "  model: pin-model\n",
            "  provider: pin-provider\n",
            "  base_url: https://pin.invalid/v1\n",
        ],
        ids=["provider+model", "model-only", "provider-only", "base-url"],
    )
    def test_pin_beats_default_for_untiered_tasks(self, monkeypatch, pin):
        _write_delegation_config("  default_tier: code\n" + pin)
        _p, _r, with_default = _dispatch(
            monkeypatch,
            tasks=[{"goal": "untiered task one"}, {"goal": "untiered task two"}],
        )
        _write_delegation_config(pin)
        _p, _r, without_default = _dispatch(
            monkeypatch,
            tasks=[{"goal": "untiered task one"}, {"goal": "untiered task two"}],
        )
        for i in (0, 1):
            assert with_default[i]["model"] != "claude-sonnet-5"
            assert with_default[i]["override_provider"] != "anthropic"
            for key in ("model", "override_provider", "override_base_url"):
                assert with_default[i][key] == without_default[i][key]

    def test_pinned_route_is_the_pin(self, monkeypatch):
        _write_delegation_config(
            "  default_tier: code\n  model: pin-model\n  provider: pin-provider\n"
        )
        _p, _r, captured = _dispatch(monkeypatch, goal="a single job here")
        assert captured[0]["model"] == "pin-model"
        assert captured[0]["override_provider"] == "pin-provider"

    def test_explicit_tier_still_beats_the_pin(self, monkeypatch):
        _write_delegation_config(
            "  default_tier: code\n  model: pin-model\n  provider: pin-provider\n"
        )
        _p, _r, captured = _dispatch(
            monkeypatch,
            tasks=[
                {"goal": "untiered task takes the pin"},
                {"goal": "explicit read tier beats the pin", "tier": "read"},
            ],
        )
        assert captured[0]["model"] == "pin-model"
        assert captured[1]["model"] == "gemini-3.8-pro"
        assert captured[1]["override_provider"] == "gemini"

    def test_one_info_line_per_dispatch_when_ignored_for_a_pin(
        self, monkeypatch, caplog
    ):
        _write_delegation_config(
            "  default_tier: code\n  model: pin-model\n  provider: pin-provider\n"
        )
        with caplog.at_level(logging.INFO, logger="tools.delegate_tool"):
            payload, raw, _c = _dispatch(
                monkeypatch,
                tasks=[
                    {"goal": "untiered task one"},
                    {"goal": "untiered task two"},
                    {"goal": "untiered task three"},
                ],
            )
        lines = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "default_tier" in r.getMessage()
        ]
        assert len(lines) == 1, [r.getMessage() for r in lines]
        assert "pin" in lines[0].getMessage()
        _assert_silent(raw, payload)


class TestExplicitTierWins:
    def test_explicit_tier_beats_the_default_per_task(self, monkeypatch):
        _write_delegation_config("  default_tier: code\n")
        payload, _raw, captured = _dispatch(
            monkeypatch,
            tasks=[
                {"goal": "untiered task takes the default"},
                {"goal": "explicitly read-tier task", "tier": "read"},
            ],
        )
        assert not payload.get("error"), payload
        assert captured[0]["model"] == "claude-sonnet-5"
        assert captured[1]["model"] == "gemini-3.8-pro"
        assert captured[1]["override_provider"] == "gemini"

    def test_unresolvable_explicit_tier_does_not_fall_through_to_default(
        self, monkeypatch
    ):
        """Today's explicit-tier fallback is unchanged: batch-level creds and a
        tier_fallback note. The default is only for tasks that named nothing."""
        _write_delegation_config(
            "  default_tier: code\n  model: pin-model\n  provider: pin-provider\n"
        )
        payload, _raw, captured = _dispatch(
            monkeypatch,
            tasks=[{"goal": "asks for a tier nobody has", "tier": "no-such-tier"}],
        )
        assert captured[0]["model"] == "pin-model"
        assert captured[0]["override_provider"] == "pin-provider"
        assert "no-such-tier" in payload["results"][0]["tier_fallback"]


_FALLBACK_WORDING = (
    "tier_fallback",
    "default_tier",
    "default tier",
    "authenticated",
    "no authenticated candidate",
    "credential resolution",
    "fall back",
    "falling back",
    "fallback",
    "unresolv",
    "could not",
    "error",
)


def _assert_silent(raw: str, payload: dict) -> None:
    assert not payload.get("error"), payload
    # Live-transcript paths embed the pytest tmp dir (and so the test's name).
    lowered = re.sub(r'"[^"]*/delegation/live/[^"]*"', '""', raw).lower()
    for word in _FALLBACK_WORDING:
        assert word not in lowered, f"{word!r} leaked into the tool result: {raw}"


class TestUnresolvableDefaultFallsBackSilently:
    def test_unknown_tier_name(self, monkeypatch, caplog):
        _write_delegation_config("  default_tier: nonexistent\n")
        with caplog.at_level(logging.INFO, logger="tools.delegate_tool"):
            payload, raw, captured = _dispatch(monkeypatch, goal="a single job here")
        # Today's path: inherit the parent.
        assert captured[0]["model"] is None
        assert captured[0]["override_provider"] is None
        _assert_silent(raw, payload)
        assert any(
            "default_tier" in r.getMessage() and "nonexistent" in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_empty_candidate_list(self, monkeypatch, caplog):
        _write_delegation_config(
            "  default_tier: scratch\n  tiers:\n    scratch: []\n"
        )
        with caplog.at_level(logging.INFO, logger="tools.delegate_tool"):
            payload, raw, captured = _dispatch(
                monkeypatch, tasks=[{"goal": "first independent job"}]
            )
        assert captured[0]["model"] is None
        assert captured[0]["override_provider"] is None
        _assert_silent(raw, payload)
        assert any("default_tier" in r.getMessage() for r in caplog.records)

    def test_no_authenticated_route(self, monkeypatch, caplog):
        _write_delegation_config("  default_tier: code\n")
        with caplog.at_level(logging.INFO, logger="tools.delegate_tool"):
            payload, raw, captured = _dispatch(
                monkeypatch, authenticated=(), goal="a single job here"
            )
        assert captured[0]["model"] is None
        assert captured[0]["override_provider"] is None
        _assert_silent(raw, payload)
        assert any("default_tier" in r.getMessage() for r in caplog.records)

    def test_credential_resolution_failure(self, monkeypatch, caplog):
        _write_delegation_config("  default_tier: code\n")
        captured = {}

        def fake_build(**kwargs):
            captured[kwargs["task_index"]] = kwargs
            child = _StubChild()
            child.model = kwargs.get("model")
            return child

        def resolver(cfg_or_match, parent):
            if set(cfg_or_match.keys()) == {"provider", "model"}:
                raise ValueError("no key for anthropic")
            return _fake_resolve_delegation_credentials(cfg_or_match, parent)

        monkeypatch.setattr(dt, "_provider_authenticated_cached", lambda p: True)
        with (
            caplog.at_level(logging.INFO, logger="tools.delegate_tool"),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                side_effect=resolver,
            ),
            patch(
                "tools.delegate_tool._build_child_preserving_parent_tools",
                side_effect=fake_build,
            ),
        ):
            raw = delegate_task(
                goal="a single job here", parent_agent=_make_mock_parent()
            )
        payload = json.loads(raw)
        assert captured[0]["model"] is None
        _assert_silent(raw, payload)
        assert any("default_tier" in r.getMessage() for r in caplog.records)

    def test_tiers_disabled_with_a_default_set(self, monkeypatch, caplog):
        _write_delegation_config("  default_tier: code\n  tiers: {}\n")
        with caplog.at_level(logging.INFO, logger="tools.delegate_tool"):
            payload, raw, captured = _dispatch(monkeypatch, goal="a single job here")
        assert captured[0]["model"] is None
        _assert_silent(raw, payload)
        messages = [r.getMessage() for r in caplog.records if "default_tier" in r.getMessage()]
        assert len(messages) == 1, messages
        # Accurate about the cause: tiers are off, not "unauthenticated".
        assert "no authenticated candidate" not in messages[0]

    def test_all_tasks_explicitly_tiered_never_resolves_the_default(
        self, monkeypatch, caplog
    ):
        """Nothing needs the default, so a bad one must not even be probed or
        logged about."""
        _write_delegation_config("  default_tier: nonexistent\n")
        with caplog.at_level(logging.INFO, logger="tools.delegate_tool"):
            payload, raw, captured = _dispatch(
                monkeypatch,
                tasks=[
                    {"goal": "explicit read task", "tier": "read"},
                    {"goal": "explicit code task", "tier": "code"},
                ],
            )
        assert captured[0]["model"] == "gemini-3.8-pro"
        assert captured[1]["model"] == "claude-sonnet-5"
        assert not [r for r in caplog.records if "default_tier" in r.getMessage()]

    @pytest.mark.parametrize("value", ["  default_tier: 7\n", "  default_tier: [code]\n", "  default_tier: '   '\n"])
    def test_non_string_or_blank_value_is_ignored(self, monkeypatch, value):
        _write_delegation_config(value)
        payload, raw, captured = _dispatch(monkeypatch, goal="a single job here")
        assert captured[0]["model"] is None
        _assert_silent(raw, payload)


class TestEmptyDefaultIsTodaysBehaviour:
    def _run(self, monkeypatch, config_lines):
        _write_delegation_config(config_lines)
        payload, raw, captured = _dispatch(
            monkeypatch,
            tasks=[
                {"goal": "untiered task one"},
                {"goal": "tiered task two", "tier": "read"},
            ],
        )
        return payload, raw, captured

    def test_empty_string_matches_key_absent_and_the_pin(self, monkeypatch):
        pin = "  model: pin-model\n  provider: pin-provider\n"
        _p1, raw_absent, absent = self._run(monkeypatch, pin)
        _p2, raw_empty, empty = self._run(monkeypatch, pin + '  default_tier: ""\n')

        def strip_volatile(raw):
            # Delegation ids (live-transcript paths) and wall-clock durations
            # differ per run; everything else must be identical.
            def scrub(node):
                if isinstance(node, dict):
                    return {
                        k: scrub(v)
                        for k, v in node.items()
                        if k not in ("total_duration_seconds", "duration_seconds")
                    }
                if isinstance(node, list):
                    return [scrub(v) for v in node]
                if isinstance(node, str) and "/delegation/live/" in node:
                    return "<live-transcript>"
                return node

            return scrub(json.loads(raw))

        def comparable(captured):
            # parent_agent is a fresh mock per dispatch; everything else the
            # dispatcher hands the child builder must be identical.
            return {
                i: {k: v for k, v in kw.items() if k != "parent_agent"}
                for i, kw in captured.items()
            }

        assert comparable(empty) == comparable(absent)
        assert strip_volatile(raw_empty) == strip_volatile(raw_absent)
        # And that "today" is the pin for untiered tasks, the tier for tiered.
        assert empty[0]["model"] == "pin-model"
        assert empty[0]["override_provider"] == "pin-provider"
        assert empty[1]["model"] == "gemini-3.8-pro"


class TestReviewEngineNeverTakesTheDefault:
    """/review keeps its main-model-first contract: configured
    auxiliary.review route, or the pin / parent model, never default_tier."""

    def _start_review(self, monkeypatch):
        from agent.review_engine import start_review

        captured = {}

        def fake_build(**kwargs):
            captured[kwargs["task_index"]] = kwargs
            child = _StubChild()
            child.model = kwargs.get("model")
            return child

        real_delegate_task = dt.delegate_task

        def foreground_delegate_task(**kwargs):
            # Same dispatcher, same kwargs start_review chose; only the
            # background hand-off is made synchronous for the test.
            kwargs["background"] = False
            return real_delegate_task(**kwargs)

        monkeypatch.setattr(
            dt, "_provider_authenticated_cached", lambda p: p in ("anthropic", "gemini")
        )
        with (
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                side_effect=_fake_resolve_delegation_credentials,
            ),
            patch(
                "tools.delegate_tool._build_child_preserving_parent_tools",
                side_effect=fake_build,
            ),
            patch("tools.delegate_tool.delegate_task", foreground_delegate_task),
        ):
            start_review(
                _make_mock_parent(),
                [
                    {"role": "user", "content": "please build the parser"},
                    {"role": "assistant", "content": "done, parser is written"},
                ],
            )
        return captured

    def test_configured_review_route_is_used(self, monkeypatch):
        (get_clover_home() / "config.yaml").write_text(
            "delegation:\n  default_tier: code\n"
            "auxiliary:\n  review:\n    provider: review-provider\n"
            "    model: review-model\n"
        )
        captured = self._start_review(monkeypatch)
        assert captured[0]["model"] == "review-model"
        assert captured[0]["override_provider"] == "review-provider"

    def test_unconfigured_review_follows_the_pin_not_the_default(self, monkeypatch):
        (get_clover_home() / "config.yaml").write_text(
            "delegation:\n  default_tier: code\n  model: pin-model\n"
            "  provider: pin-provider\n"
        )
        captured = self._start_review(monkeypatch)
        assert captured[0]["model"] == "pin-model"
        assert captured[0]["override_provider"] == "pin-provider"

    def test_unconfigured_unpinned_review_inherits_the_parent(self, monkeypatch):
        (get_clover_home() / "config.yaml").write_text(
            "delegation:\n  default_tier: code\n"
        )
        captured = self._start_review(monkeypatch)
        assert captured[0]["model"] is None
        assert captured[0]["override_provider"] is None


class TestSchemaByteIdentity:
    def _schema_bytes(self, monkeypatch):
        monkeypatch.setattr(
            dt, "_provider_authenticated_cached", lambda p: p in ("anthropic", "gemini")
        )
        return json.dumps(_build_dynamic_schema_overrides(), sort_keys=True)

    def test_schema_is_identical_with_and_without_a_default(self, monkeypatch):
        static_before = json.dumps(dt.DELEGATE_TASK_SCHEMA, sort_keys=True)

        _write_delegation_config("  model: ''\n")
        without = self._schema_bytes(monkeypatch)

        _write_delegation_config("  default_tier: code\n")
        with_code = self._schema_bytes(monkeypatch)
        again = self._schema_bytes(monkeypatch)

        _write_delegation_config("  default_tier: nonexistent\n")
        with_bogus = self._schema_bytes(monkeypatch)

        assert with_code == without
        assert again == without
        assert with_bogus == without
        assert "default_tier" not in without
        assert json.dumps(dt.DELEGATE_TASK_SCHEMA, sort_keys=True) == static_before

    def test_schema_is_unchanged_by_dispatching_with_a_default(self, monkeypatch):
        """Applying the default at dispatch must not leak into later schemas."""
        _write_delegation_config("  default_tier: code\n")
        before = self._schema_bytes(monkeypatch)
        _dispatch(monkeypatch, goal="a single job here")
        assert self._schema_bytes(monkeypatch) == before
