"""Router calibration v2: the flag, the v2 rubric, the wider override wording and
the eval fixtures.  No model is called."""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

from agent import delegation_router as dr

pytestmark = pytest.mark.real_delegation_router

REPO_ROOT = Path(__file__).resolve().parents[2]
HELDOUT = REPO_ROOT / "tests" / "eval" / "delegation_router_heldout.jsonl"
MAIN_CASES = REPO_ROOT / "tests" / "eval" / "delegation_modes.jsonl"

# sha256 of ROUTER_SYSTEM_PROMPT as shipped before calibration v2.  The flag's
# contract is "off restores the old rubric byte for byte".
V1_SHA256 = "150c0c691dc622bf0d54303d7e5b88c9ce43e3915e8b83702e91b34a3e4d2981"


def _set_flag(monkeypatch, value):
    import clover_cli.config as cfg

    router = {} if value is None else {"calibration_v2": value}
    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {"delegation": {"router": router}})


# ── the flag ───────────────────────────────────────────────────────────────

def test_the_old_rubric_is_unchanged():
    assert hashlib.sha256(dr.ROUTER_SYSTEM_PROMPT.encode()).hexdigest() == V1_SHA256


@pytest.mark.parametrize("value", [False, "false", "off", 0])
def test_flag_off_sends_the_old_prompt_bytes(monkeypatch, value):
    _set_flag(monkeypatch, value)
    system = dr._build_messages("email Julia the extension", [], 3)[0]["content"]
    assert system == dr.ROUTER_SYSTEM_PROMPT.format(max_tasks=3)
    assert hashlib.sha256(dr.router_system_prompt().encode()).hexdigest() == V1_SHA256


@pytest.mark.parametrize("value", [None, True, "yes", "garbage"])
def test_flag_on_or_unset_sends_the_v2_prompt(monkeypatch, value):
    _set_flag(monkeypatch, value)
    system = dr._build_messages("email Julia the extension", [], 3)[0]["content"]
    assert system == dr.ROUTER_SYSTEM_PROMPT_V2.format(max_tasks=3)
    assert system != dr.ROUTER_SYSTEM_PROMPT.format(max_tasks=3)


def test_an_unreadable_config_means_v2(monkeypatch):
    import clover_cli.config as cfg

    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(cfg, "load_config_readonly", boom)
    assert dr.calibration_v2_enabled() is True


def test_the_flag_is_shipped_default_on_and_documented():
    from clover_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["delegation"]["router"]["calibration_v2"] is True
    assert "calibration_v2" in (REPO_ROOT / "cli-config.yaml.example").read_text()


def test_the_flag_does_not_touch_router_settings():
    assert "calibration_v2" not in dr.RouterSettings.__dataclass_fields__


# ── the v2 rubric ──────────────────────────────────────────────────────────

def test_v2_rubric_is_a_small_addition_with_the_same_structure():
    v1, v2 = dr.ROUTER_SYSTEM_PROMPT, dr.ROUTER_SYSTEM_PROMPT_V2
    assert 0 < len(v2) - len(v1) < 700
    for heading in ('1. "direct"', '2. "parallel"', '3. "staged"', '4. "single"',
                    "**Task rules:**", "**Reply:**"):
        assert heading in v1 and heading in v2
    assert "{max_tasks}" in v2 and "{{" in v2  # still a .format() template


def test_v2_rubric_names_preparation_and_keeps_the_quick_cases_direct():
    v2 = dr.ROUTER_SYSTEM_PROMPT_V2
    assert "preparing something to send to or write for a person" in v2
    assert "finding and reading files, code or past sessions" in v2
    assert "Sending a skill or project" in v2
    assert "sending a link or text from the chat" in v2
    for kept in ("a quick answer", "reading or editing known files", "one small fix",
                 "a diagnostic or health check made of quick commands"):
        assert kept in v2
    assert "send subagents" in v2


def test_v2_prompt_is_byte_stable_across_messages():
    a = dr._build_messages("fix the cart bug", [], 3)[0]
    b = dr._build_messages("send the skill", [{"role": "user", "content": "x"}], 3)[0]
    assert a == b


# ── overrides ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Send subagents to do a full PC diagnostic. I want to upgrade it.",
    "send a subagent to check the logs",
    "Send 3 helpers to go through these repos",
    "spin up a subagent for this",
    "spin up some helpers",
    "Do the audit with subagents",
    "Check every service using helpers",
    "Run the audit in the background",
    "In the background, check the logs and report",
])
def test_send_or_use_subagents_is_a_helper_request(text):
    assert dr.explicit_override(text) == "helpers"


@pytest.mark.parametrize("text", [
    "do it yourself",
    "Do this here please",
    "just handle it directly",
    "Do it yourself, no helpers",
    "Send subagents? No, do it yourself.",
])
def test_do_it_here_stays_self(text):
    assert dr.explicit_override(text) == "self"


@pytest.mark.parametrize("text", [
    "send helpers.py to Bob",
    "Fix it with the helper function",
    "Send me the link you just made",
    "What's the weather",
    "Email Julia: running late",
    "The man in the background of the photo is wearing a red coat; who is he?",
    'Explain this log:\n```\nsend subagents now\n```',
    'Review this quote: "send subagents" and tell me who said it',
])
def test_ordinary_text_is_not_an_override(text):
    assert dr.explicit_override(text) is None


def test_new_wording_is_ignored_when_the_flag_is_off(monkeypatch):
    _set_flag(monkeypatch, False)
    assert dr.explicit_override("Send subagents to do a full PC diagnostic.") is None
    assert dr.explicit_override("Do this here please") is None
    # the earlier wording still works
    assert dr.explicit_override("Use helpers for this") == "helpers"
    assert dr.explicit_override("do it yourself") == "self"


def test_send_subagents_upgrades_a_direct_verdict(monkeypatch):
    """The override reaches the verdict: a model answer of direct becomes single."""
    monkeypatch.setattr(dr, "_call_model",
                        lambda messages, timeout: '{"mode":"direct","reason":"quick","tasks":[]}')
    verdict = dr.route("Send subagents to do a full PC diagnostic. I want to upgrade it.", [],
                       settings=dr.RouterSettings(), max_tasks=3)
    assert verdict.mode == "single" and verdict.source == "override"


# ── the eval fixtures ──────────────────────────────────────────────────────

def _eval_module():
    path = REPO_ROOT / "scripts" / "eval" / "delegation_router.py"
    spec = importlib.util.spec_from_file_location("delegation_router_eval_v2", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["delegation_router_eval_v2"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_octavia_fixtures_load_with_their_context():
    rt = _eval_module()
    cases = {c["id"]: c for c in rt.load_cases(HELDOUT)}
    email = cases["h-octavia-email-extension"]
    skill = cases["h-octavia-send-skill"]
    assert email["expected_mode"] == skill["expected_mode"] == "single"
    assert email["prompt"].startswith("Hey, can you email to Julia")
    assert "Be VERY specific" in email["prompt"]
    assert skill["prompt"].endswith("Can you also send the AutoHDR skill?")
    assert "Sent to levesquejulia475@gmail.com" in skill["prompt"]
    assert [t["role"] for t in skill["recent_turns"]] == ["user", "assistant", "user", "assistant"]
    assert email["reps"] == skill["reps"] == 5


def test_guard_and_family_cases_are_present():
    rt = _eval_module()
    cases = {c["id"]: c for c in rt.load_cases(HELDOUT)}
    guards = [c for i, c in cases.items() if i.startswith("h-guard-")]
    prep = [c for i, c in cases.items() if i.startswith("h-prep-")]
    assert len(guards) >= 3 and all(c["expected_mode"] == "direct" for c in guards)
    assert len(prep) >= 3 and all(c["expected_mode"] == "single" for c in prep)


def test_recent_turns_reach_the_router_and_case_reps_apply():
    rt = _eval_module()
    seen = []

    def route(message, recent_turns=()):
        seen.append((message, list(recent_turns)))

        class V:
            mode, source, reason = "single", "router", ""

        return V()

    cases = [
        {"id": "a", "prompt": "p", "expected_mode": "single", "reps": 3,
         "recent_turns": [{"role": "user", "content": "earlier"}]},
        {"id": "b", "prompt": "q", "expected_mode": "single"},
    ]
    runs = rt.run_router_eval(cases, route, reps=1, case_reps=True)
    assert [r["case_id"] for r in runs] == ["a", "a", "a", "b"]
    assert seen[0][1] == [{"role": "user", "content": "earlier"}] and seen[3][1] == []
    assert len(rt.run_router_eval(cases, route, reps=1)) == 2  # case_reps off: --reps rules


@pytest.mark.parametrize("bad", [
    '{"id":"x","prompt":"p","expected_mode":"direct","recent_turns":"nope"}',
    '{"id":"x","prompt":"p","expected_mode":"direct","recent_turns":[{"role":"tool","content":"c"}]}',
    '{"id":"x","prompt":"p","expected_mode":"direct","reps":0}',
])
def test_bad_fixtures_are_rejected(tmp_path, bad):
    rt = _eval_module()
    path = tmp_path / "c.jsonl"
    path.write_text(bad + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        rt.load_cases(path)


def test_the_existing_anchors_are_still_direct():
    rt = _eval_module()
    main = {c["id"]: c for c in rt.load_cases(MAIN_CASES)}
    assert main["monitors-1440p"]["expected_mode"] == "direct"
    assert main["pc-diagnostic"]["expected_mode"] == "direct"
