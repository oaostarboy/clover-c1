"""Latency-critical side tasks default to a small fast model.

Covers the ladder in ``agent.auxiliary_client._resolve_fast_pick`` and the pure
pieces in ``agent.aux_fast_model``: default-on, explicit override wins,
``prefer_fast_model: false``, the codex profile pick, rung order and excludes,
the price pick, the speed probe (+ cache + TTL + never inline), borrowing, the
slow-model timeout and the small-talk skip.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import aux_fast_model as afm
from agent import auxiliary_client as ac
from agent import delegation_router as dr

pytestmark = pytest.mark.real_fast_model


@pytest.fixture(autouse=True)
def _clean_state():
    ac.reset_fast_pick_state()
    yield
    ac.reset_fast_pick_state()


def _cfg(**task_cfg):
    return patch.object(ac, "_get_auxiliary_task_config", return_value=task_cfg)


# ── default-on / overrides ─────────────────────────────────────────────────

class TestDefaultOn:
    def test_both_tasks_prefer_fast_with_no_config(self):
        with _cfg():
            assert ac._task_prefers_fast_model("delegation_router")
            assert ac._task_prefers_fast_model("title_generation")

    def test_other_tasks_never(self):
        with _cfg():
            for task in ("compression", "vision", "session_search", None):
                assert not ac._task_prefers_fast_model(task)

    def test_prefer_fast_false_keeps_main(self):
        with _cfg(prefer_fast_model=False):
            assert not ac._task_prefers_fast_model("delegation_router")
        with _cfg(prefer_fast_model="false"):
            assert not ac._task_prefers_fast_model("title_generation")

    @pytest.mark.parametrize("pin", [{"provider": "anthropic"}, {"model": "gpt-6-luna"},
                                     {"provider": "openai-codex", "model": "gpt-6-luna"}])
    def test_explicit_provider_or_model_wins(self, pin):
        with _cfg(prefer_fast_model=True, **pin):
            assert not ac._task_prefers_fast_model("delegation_router")

    def test_auto_provider_and_model_are_not_a_pin(self):
        with _cfg(provider="auto", model="auto"):
            assert ac._task_prefers_fast_model("delegation_router")

    def test_resolve_auto_swaps_to_fast_model_and_off_switch_does_not(self):
        client = MagicMock()
        seen = []

        def resolve(provider, model, **kw):
            seen.append((provider, model))
            return client, model

        runtime = {"provider": "openai-codex", "model": "gpt-6.1-sol"}
        with _cfg(), patch.object(ac, "resolve_provider_client", side_effect=resolve), \
                patch.object(ac, "_is_provider_unhealthy", return_value=False):
            ac._resolve_auto(main_runtime=runtime, task="delegation_router")
        with _cfg(prefer_fast_model=False), patch.object(ac, "resolve_provider_client", side_effect=resolve), \
                patch.object(ac, "_is_provider_unhealthy", return_value=False):
            ac._resolve_auto(main_runtime=runtime, task="delegation_router")
        assert seen == [("openai-codex", "gpt-6-luna"), ("openai-codex", "gpt-6.1-sol")]


# ── ladder: profile, family, price ─────────────────────────────────────────

class TestProfileRung:
    def test_codex_and_openai_resolve_to_luna_without_network(self):
        with patch.object(ac, "_load_fast_catalog", side_effect=AssertionError("no network")):
            for provider in ("openai-codex",):
                pick = ac._resolve_fast_pick("delegation_router", provider, "gpt-6.1-sol")
                assert (pick.model, pick.how) == ("gpt-6-luna", "profile")
            pick = ac._resolve_fast_pick(
                "delegation_router", "custom", "gpt-6.1-sol", "https://api.openai.com/v1")
            assert (pick.model, pick.how) == ("gpt-6-luna", "profile")

    def test_codex_profile_declares_luna(self):
        from providers import get_provider_profile

        assert get_provider_profile("openai-codex").fast_aux_model == "gpt-6-luna"

    def test_main_model_already_small_is_kept(self):
        pick = ac._resolve_fast_pick("title_generation", "anthropic", "claude-haiku-4-5")
        assert pick.model == "claude-haiku-4-5"


class TestFamilyRungs:
    def test_rung_order_is_luna_before_flash_lite_before_flash_before_haiku(self):
        order = ["-luna", "flash-lite", "-flash", "haiku", "-nano", "-lite",
                 "-mini", "-turbo", "-small", "-fast"]
        assert afm.FAST_RUNGS.index("-small") < afm.FAST_RUNGS.index("-fast")
        rungs = list(afm.FAST_RUNGS)
        assert [r for r in rungs if r in order] == order

    def test_latest_alias_beats_pinned_family(self):
        ids = ["gpt-6-luna", "~openai/gpt-mini-latest", "gemini-3.8-flash"]
        assert afm.pick_family(ids) == "~openai/gpt-mini-latest"

    @pytest.mark.parametrize("ids,expected", [
        (["gemini-3.8-flash", "gemini-3.1-flash-lite", "gemini-3.8-pro"], "gemini-3.1-flash-lite"),
        (["gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol"], "gpt-6-luna"),
        (["claude-opus-5", "claude-haiku-5-5", "claude-haiku-4-5"], "claude-haiku-5-5"),
        (["big-1", "foo-nano", "foo-mini", "foo-turbo"], "foo-nano"),
        (["a-small", "a-fast", "a-turbo"], "a-turbo"),
        (["a-fast", "a-small"], "a-small"),
        (["frontier-1", "frontier-2"], ""),
    ])
    def test_rung_priority(self, ids, expected):
        assert afm.pick_family(ids) == expected

    def test_excludes_non_chat_siblings(self):
        ids = ["gpt-4o-mini-tts", "gpt-4o-mini-transcribe", "gpt-4o-mini-realtime-preview",
               "gpt-image-1-mini", "gpt-4o-mini-search-preview", "omni-moderation-mini",
               "text-embedding-3-small", "o3-mini", "gpt-5-mini-thinking", "gpt-4o-mini"]
        assert afm.pick_family(ids) == "gpt-4o-mini"

    def test_non_reasoning_variant_is_not_excluded(self):
        assert afm.pick_family(["grok-4-fast-non-reasoning", "grok-4-fast-reasoning"]) \
            == "grok-4-fast-non-reasoning"

    def test_pro_sibling_of_a_small_model_is_not_fast(self):
        assert afm.pick_family(["openai/gpt-5.6-luna-pro", "openai/gpt-5.6-luna"]) \
            == "openai/gpt-5.6-luna"
        assert afm.pick_family(["openai/gpt-5.6-luna-pro"]) == ""

    def test_xai_catalog_picks_the_non_reasoning_grok_not_media_models(self):
        ids = ["grok-4.6", "grok-4.7", "grok-4.20-0309-reasoning",
               "grok-4.20-0309-non-reasoning", "grok-imagine-image",
               "grok-imagine-video-1.5-lite", "grok-4.20-multi-agent-0309"]
        assert afm.pick_family(ids) == "grok-4.20-0309-non-reasoning"

    def test_newest_generation_wins_within_a_rung(self):
        assert afm.pick_family(["x-9-mini", "x-10-mini", "x-3.5-mini"]) == "x-10-mini"

    def test_catalog_family_pick_through_the_ladder(self):
        catalog = {"kimi-k3": {}, "kimi-k3-turbo": {}, "kimi-k2": {}}
        with patch.object(ac, "_load_fast_catalog", return_value=catalog):
            pick = ac._resolve_fast_pick("delegation_router", "someprov", "kimi-k3",
                                         "https://api.example.com/v1")
        assert (pick.model, pick.how) == ("kimi-k3-turbo", "family")


class TestPriceRung:
    def test_cheapest_chat_model_when_no_family_matches(self):
        catalog = {
            "frontier-big": {"prompt": "0.00001", "completion": "0.00004"},
            "frontier-mid": {"prompt": "0.000002", "completion": "0.000008"},
            "frontier-tts": {"prompt": "0.0000001", "completion": "0.0000001"},
            "frontier-embed": {"prompt": "0.00000001", "completion": "0"},
            "frontier-unpriced": {},
        }
        assert afm.pick_cheapest(catalog) == "frontier-mid"

    def test_unpriced_catalog_has_no_price_pick(self):
        assert afm.pick_cheapest({"a": {}, "b": {"prompt": "", "completion": ""}}) == ""

    def test_price_rung_in_the_ladder(self):
        catalog = {"alpha": {"prompt": "0.00001", "completion": "0.00002"},
                   "beta": {"prompt": "0.000001", "completion": "0.000002"}}
        with patch.object(ac, "_load_fast_catalog", return_value=catalog):
            pick = ac._resolve_fast_pick("delegation_router", "someprov", "alpha",
                                         "https://api.example.com/v1")
        assert (pick.model, pick.how) == ("beta", "price")


# ── speed probe ────────────────────────────────────────────────────────────

class TestSpeedProbe:
    def test_picks_fastest_valid_candidate_and_drops_bad_ones(self):
        times = {"m1": ('{"mode":"direct"}', 3.0), "m2": ('{"mode":"direct"}', 0.8),
                 "m3": ("sorry, I cannot", 0.1)}
        model, seconds = afm.run_probe("p", "u", ["m1", "m2", "m3"], lambda m: times[m])
        assert (model, seconds) == ("m2", 0.8)

    def test_a_raising_candidate_is_skipped(self):
        def call(m):
            if m == "bad":
                raise TimeoutError("slow")
            return '{"mode":"direct"}', 1.0
        assert afm.run_probe("p", "u", ["bad", "ok"], call)[0] == "ok"

    def test_at_most_three_candidates(self):
        assert len(afm.probe_candidates([f"model-{i}" for i in range(20)])) == 3
        seen = []

        def call(m):
            seen.append(m)
            return '{"mode":"direct"}', 1.0

        afm.run_probe("p", "u", [f"m{i}" for i in range(9)], call)
        assert len(seen) == 3

    def test_probe_candidates_are_chat_only_and_newest(self):
        cands = afm.probe_candidates(["k-2", "k-3", "k-3-tts", "k-embed", "k-1"])
        assert "k-3-tts" not in cands and "k-embed" not in cands
        assert cands[0] == "k-3"

    def test_probe_prompt_is_tiny_and_bounded(self):
        assert afm.PROBE_MAX_TOKENS <= 40
        assert afm.PROBE_TIMEOUT_SECONDS <= 6.5
        assert sum(len(m["content"]) for m in afm.PROBE_MESSAGES) < 250

    def test_cache_roundtrip_and_ttl(self):
        afm.store_probe("kimi", "https://k/v1/", "kimi-k3-fast", 0.7, now=1000.0)
        assert afm.cached_probe("kimi", "https://k/v1", now=1000.0 + 6 * 86400)["model"] == "kimi-k3-fast"
        assert afm.cached_probe("kimi", "https://k/v1", now=1000.0 + 8 * 86400) is None
        assert afm.cached_probe("other", "https://k/v1", now=1000.0) is None

    def test_cache_is_written_under_clover_home_cache(self):
        from clover_cli.config import get_clover_home

        afm.store_probe("kimi", "https://k/v1", "m", 0.5)
        path = get_clover_home() / "cache" / "aux_fast_model.json"
        assert path.exists()
        assert json.loads(path.read_text())["kimi|https://k/v1"]["model"] == "m"

    def test_failed_probe_is_remembered_for_a_shorter_time(self):
        afm.store_probe("kimi", "https://k/v1", "", None, now=1000.0)
        assert afm.cached_probe("kimi", "https://k/v1", now=1000.0 + 3600) is not None
        assert afm.cached_probe("kimi", "https://k/v1", now=1000.0 + 2 * 86400) is None

    def test_never_inline_first_call_uses_main_and_starts_a_thread(self):
        catalog = {"kimi-k3": {}, "kimi-k2": {}}
        started = threading.Event()
        release = threading.Event()

        def slow_call(model):
            started.set()
            release.wait(5)
            return '{"mode":"direct"}', 0.5

        with patch.object(ac, "_load_fast_catalog", return_value=catalog), \
                patch.object(ac, "_probe_call_factory", return_value=slow_call), \
                patch.object(ac, "_borrow_fast_pick", return_value=None):
            t0 = time.monotonic()
            pick = ac._resolve_fast_pick("delegation_router", "someprov", "kimi-k3",
                                         "https://api.example.com/v1")
            elapsed = time.monotonic() - t0
            assert pick.is_main and pick.model == "kimi-k3"
            assert elapsed < 1.0  # the probe call is blocked on `release`
            assert started.wait(2)  # ...and it did start, in the background
            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if afm.cached_probe("someprov", "https://api.example.com/v1"):
                    break
                time.sleep(0.05)
            landed = ac._resolve_fast_pick("delegation_router", "someprov", "kimi-k3",
                                           "https://api.example.com/v1")
        assert (landed.model, landed.how) in {("kimi-k3", "probe"), ("kimi-k3", "main")}
        cached = afm.cached_probe("someprov", "https://api.example.com/v1")
        assert cached and cached["model"] in {"kimi-k3", "kimi-k2"}

    def test_cached_probe_winner_is_used_without_probing(self):
        afm.store_probe("someprov", "https://api.example.com/v1", "kimi-k3-fast", 0.6)
        with patch.object(ac, "_load_fast_catalog", return_value={"kimi-k3": {}}), \
                patch.object(afm, "start_background_probe", side_effect=AssertionError("re-probed")):
            pick = ac._resolve_fast_pick("delegation_router", "someprov", "kimi-k3",
                                         "https://api.example.com/v1")
        assert (pick.model, pick.how) == ("kimi-k3-fast", "probe")

    def test_one_probe_per_process_per_provider(self):
        gate = threading.Event()
        calls = []

        def lister():
            calls.append(1)
            gate.wait(2)
            return []

        assert afm.start_background_probe("pp", "u", lister, lambda: (lambda m: ("{}", 1.0)))
        assert not afm.start_background_probe("pp", "u", lister, lambda: (lambda m: ("{}", 1.0)))
        gate.set()


# ── borrowing ──────────────────────────────────────────────────────────────

class TestBorrow:
    def _chain(self):
        return [{"provider": "xai-oauth", "model": "grok-4.6"},
                {"provider": "anthropic", "model": "claude-opus-5-5"}]

    def test_borrows_haiku_when_credentials_resolve(self):
        client = MagicMock()
        with patch("clover_cli.fallback_config.get_fallback_chain", return_value=self._chain()), \
                _cfg(), patch.object(ac, "_is_provider_unhealthy", return_value=False), \
                patch.object(ac, "_curated_fast_model", side_effect=lambda p: "claude-haiku-4-5" if p == "anthropic" else ""), \
                patch.object(ac, "_static_fast_model", return_value=""), \
                patch.object(ac, "resolve_provider_client", return_value=(client, "claude-haiku-4-5")):
            pick = ac._borrow_fast_pick("delegation_router", "xai-oauth")
        assert pick == afm.FastPick("anthropic", "claude-haiku-4-5", "borrowed")

    def test_no_borrow_when_credentials_do_not_resolve(self):
        with patch("clover_cli.fallback_config.get_fallback_chain", return_value=self._chain()), \
                _cfg(), patch.object(ac, "_is_provider_unhealthy", return_value=False), \
                patch.object(ac, "_static_fast_model", return_value=""), \
                patch.object(ac, "_curated_fast_model", return_value="claude-haiku-4-5"), \
                patch.object(ac, "resolve_provider_client", return_value=(None, None)):
            assert ac._borrow_fast_pick("delegation_router", "xai-oauth") is None

    def test_never_borrows_the_main_provider_itself(self):
        with patch("clover_cli.fallback_config.get_fallback_chain", return_value=self._chain()), \
                _cfg(), patch.object(ac, "_is_provider_unhealthy", return_value=False), \
                patch.object(ac, "_static_fast_model", return_value="m"), \
                patch.object(ac, "resolve_provider_client", side_effect=AssertionError("asked")):
            assert ac._borrow_fast_pick("delegation_router", "anthropic") is None

    def test_borrow_is_a_ladder_rung_before_main(self):
        client = MagicMock()
        borrowed = afm.FastPick("anthropic", "claude-haiku-4-5", "borrowed")
        with patch.object(ac, "_load_fast_catalog", return_value={}), \
                patch.object(ac, "_fast_catalog_ids", return_value=[]), \
                patch.object(ac, "_borrow_fast_pick", return_value=borrowed):
            pick = ac._resolve_fast_pick("delegation_router", "xai-oauth", "grok-4.6")
        assert pick == borrowed

    def test_ends_on_main_when_nothing_works(self):
        with patch.object(ac, "_load_fast_catalog", return_value={}), \
                patch.object(ac, "_fast_catalog_ids", return_value=["grok-4.6", "grok-4.7"]), \
                patch.object(ac, "_borrow_fast_pick", return_value=None):
            pick = ac._resolve_fast_pick("delegation_router", "xai-oauth", "grok-4.6")
        assert (pick.provider, pick.model, pick.how) == ("xai-oauth", "grok-4.6", "main")


# ── one log line per task per process ──────────────────────────────────────

def test_fast_model_is_logged_once_per_task(caplog):
    import logging

    pick = afm.FastPick("openai-codex", "gpt-6-luna", "profile")
    with caplog.at_level(logging.INFO, logger="agent.auxiliary_client"):
        ac._log_fast_pick_once("delegation_router", pick)
        ac._log_fast_pick_once("delegation_router", pick)
        ac._log_fast_pick_once("title_generation", pick)
    lines = [r.getMessage() for r in caplog.records if "fast model" in r.getMessage()]
    assert lines == [
        "Auxiliary delegation_router: fast model openai-codex/gpt-6-luna (profile)",
        "Auxiliary title_generation: fast model openai-codex/gpt-6-luna (profile)",
    ]


# ── small call: no reasoning ───────────────────────────────────────────────

class TestNoReasoningOnSmallCall:
    def _prime(self, provider="anthropic", model="claude-opus-5-5", how="family", fast="claude-haiku-5-5"):
        with ac._fast_pick_lock:
            ac._fast_pick_memo[ac._fast_memo_key("delegation_router", provider, model, "")] = (
                time.monotonic(), afm.FastPick(provider, fast, how))

    def test_anthropic_gets_thinking_off(self):
        self._prime()
        rc, body = ac._small_call_reasoning(
            "delegation_router", "anthropic", {}, None,
            main_runtime={"provider": "anthropic", "model": "claude-opus-5-5"})
        assert rc == {"enabled": False}

    def test_codex_gets_the_lowest_effort(self):
        self._prime("openai-codex", "gpt-6.1-sol", "profile", "gpt-6-luna")
        rc, body = ac._small_call_reasoning(
            "delegation_router", "openai-codex", {}, None,
            main_runtime={"provider": "openai-codex", "model": "gpt-6.1-sol"})
        assert rc is None and body["reasoning"]["effort"] == "low"

    def test_user_setting_is_never_overridden(self):
        self._prime()
        rc, body = ac._small_call_reasoning(
            "delegation_router", "anthropic", {"reasoning": {"effort": "high"}}, None,
            main_runtime={"provider": "anthropic", "model": "claude-opus-5-5"})
        assert rc is None and body["reasoning"]["effort"] == "high"

    def test_main_model_call_is_untouched(self):
        self._prime(how="main", fast="claude-opus-5-5")
        rc, body = ac._small_call_reasoning(
            "delegation_router", "anthropic", {}, None,
            main_runtime={"provider": "anthropic", "model": "claude-opus-5-5"})
        assert rc is None and "reasoning" not in body


# ── router: slow-model timeout + small-talk skip ───────────────────────────

SETTINGS = dr.RouterSettings(enabled=True, timeout_seconds=8.0, backstop_calls=2,
                             backstop_seconds=60.0)


class TestSlowModelTimeout:
    def test_main_model_gets_the_longer_wait(self, caplog):
        import logging

        route = {"provider": "xai-oauth", "model": "grok-4.6", "how": "main", "slow": True}
        with patch.object(dr, "_model_route", return_value=route), \
                patch.object(dr, "_router_config", return_value={}):
            seconds, rule, _ = dr.effective_timeout(SETTINGS)
        assert (seconds, rule) == (20.0, "slow_model")

    def test_fast_model_keeps_the_normal_wait(self):
        route = {"provider": "openai-codex", "model": "gpt-6-luna", "how": "profile", "slow": False}
        with patch.object(dr, "_model_route", return_value=route), \
                patch.object(dr, "_router_config", return_value={}):
            assert dr.effective_timeout(SETTINGS) == (8.0, "fast", route)

    def test_unknown_route_keeps_the_normal_wait(self):
        with patch.object(dr, "_model_route", return_value=None):
            assert dr.effective_timeout(SETTINGS)[:2] == (8.0, "fast")

    def test_configurable(self):
        route = {"provider": "p", "model": "m", "how": "main", "slow": True}
        with patch.object(dr, "_model_route", return_value=route), \
                patch.object(dr, "_router_config", return_value={"slow_model_timeout_seconds": 35}):
            assert dr.effective_timeout(SETTINGS)[:2] == (35.0, "slow_model")

    def test_never_shorter_than_the_configured_timeout(self):
        route = {"provider": "p", "model": "m", "how": "main", "slow": True}
        long_settings = dr.RouterSettings(timeout_seconds=30.0)
        with patch.object(dr, "_model_route", return_value=route), \
                patch.object(dr, "_router_config", return_value={}):
            assert dr.effective_timeout(long_settings)[0] == 30.0

    def test_route_detail_uses_and_logs_the_slow_wait(self, caplog):
        import logging

        route = {"provider": "xai-oauth", "model": "grok-4.6", "how": "main", "slow": True}
        seen = {}

        def fake_call(messages, timeout, hedge_after=None):
            seen["timeout"], seen["hedge"] = timeout, hedge_after
            return json.dumps({"mode": "direct", "tasks": [], "reason": "x"})

        with patch.object(dr, "_model_route", return_value=route), \
                patch.object(dr, "_router_config", return_value={}), \
                patch.object(dr, "_call_model", side_effect=fake_call), \
                caplog.at_level(logging.INFO, logger="agent.delegation_router"):
            verdict, stats = dr._route_detail("sum up the report in /tmp/r.md", settings=SETTINGS, max_tasks=3)
        assert verdict is not None
        assert 19.0 < seen["timeout"] <= 20.0
        assert seen["hedge"] == dr.SLOW_HEDGE_AFTER_SECONDS
        assert stats["timeout_rule"] == "slow_model"
        assert any("waiting up to 20s instead of 8s" in r.getMessage() for r in caplog.records)

    def test_describe_fast_route_flags_big_main_model_as_slow(self):
        with _cfg(), patch.object(ac, "_load_fast_catalog", return_value={}), \
                patch.object(ac, "_fast_catalog_ids", return_value=["grok-4.6"]), \
                patch.object(ac, "_borrow_fast_pick", return_value=None), \
                patch.object(ac, "_fast_main_identity",
                             return_value=("xai-oauth", "grok-4.6", "", "")):
            route = ac.describe_fast_route("delegation_router")
        assert route["slow"] is True and route["how"] == "main"

    def test_describe_with_no_network_answers_from_the_profile(self):
        with _cfg(), patch.object(ac, "_fast_main_identity",
                                  return_value=("openai-codex", "gpt-6.1-sol", "", "")), \
                patch.object(ac, "_load_fast_catalog", side_effect=AssertionError("network")):
            route = ac.describe_fast_route("delegation_router", allow_network=False)
        assert route["model"] == "gpt-6-luna" and route["slow"] is False

    def test_pinned_router_model_gets_no_extra_wait(self):
        with _cfg(provider="openai-codex", model="gpt-6.1-sol"), \
                patch.object(ac, "_fast_main_identity",
                             return_value=("openai-codex", "gpt-6.1-sol", "", "")):
            assert ac.describe_fast_route("delegation_router")["slow"] is False


class TestSmallTalkSkip:
    @pytest.mark.parametrize("message", [
        "ok go", "thanks", "how are you", "👍", "restart done", "nice", "lol", "yes",
        "Thank you so much!", "hey there", "ok thanks", "go ahead", "good morning",
        "sounds good", "no", "😂😂", "how are you?", "what's up",
    ])
    def test_small_talk_is_detected(self, message):
        assert afm.is_small_talk(message), message

    @pytest.mark.parametrize("message", [
        "use a helper to count files", "how is the build going", "run the tests",
        "fix the login bug", "restart the gateway", "ok go fix it", "go",
        "yes please do it", "thanks, now deploy it", "check https://example.com",
        "look at ~/notes.md", "what is the status of PR 98", "continue", "do it",
        "can you summarize this", "ok but why did it fail", "thanks for 3 things: a, b, c",
        "", "   ", "x" * 200,
    ])
    def test_work_is_not_small_talk(self, message):
        assert not afm.is_small_talk(message), message

    def test_never_skips_on_length_alone(self):
        assert not afm.is_small_talk("Please look at the database schema again")
        assert afm.is_small_talk("ok")  # an ack is small talk whatever its length

    def _agent(self):
        return SimpleNamespace(valid_tool_names={"delegate_task"})

    def test_skip_reason_small_talk_both_sides(self):
        settings = SETTINGS
        with patch.object(dr, "_router_config", return_value={}):
            assert dr.skip_reason(self._agent(), "ok go", None, settings=settings) == "small_talk"
            assert dr.skip_reason(self._agent(), "how are you", None, settings=settings) == "small_talk"
            assert dr.skip_reason(self._agent(), "use a helper to count files", None, settings=settings) != "small_talk"
            assert dr.skip_reason(self._agent(), "how is the build going", None, settings=settings) != "small_talk"

    def test_config_switch_turns_the_skip_off(self):
        with patch.object(dr, "_router_config", return_value={"skip_small_talk": False}):
            assert dr.skip_reason(self._agent(), "thanks", None, settings=SETTINGS) != "small_talk"

    def test_no_router_call_for_small_talk(self):
        called = []
        agent = self._agent()
        with patch.object(dr, "_router_config", return_value={}), \
                patch.object(dr, "_route_detail", side_effect=lambda *a, **k: called.append(1)):
            assert dr.plan_for_turn(agent, "thanks") == ""
        assert not called
