"""`clover setup claude` must work for someone who knows nothing.

Users reported that wiring Claude Code was complicated. The old path asked
them to type a provider name at a blank prompt, from a list of 80 that never
says "Claude Code". These tests hold the new path to the promise: one command,
no memorised names, and a clear instruction when the login is missing.
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "scc", ROOT / "clover_cli" / "setup_claude_code.py"
)
scc = importlib.util.module_from_spec(_spec)
sys.modules["scc"] = scc
_spec.loader.exec_module(scc)


def _write_login(tmp_path, *, expires_in_hours=8, plan="max"):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "tok-abc",
            "refreshToken": "ref-def",
            "expiresAt": int((time.time() + expires_in_hours * 3600) * 1000),
            "subscriptionType": plan,
        }
    }))
    return p


class TestFindingTheLogin:
    def test_reads_a_real_claude_code_credential(self, tmp_path):
        assert scc.read_login(_write_login(tmp_path))["accessToken"] == "tok-abc"

    def test_missing_file_returns_none_rather_than_raising(self, tmp_path):
        """Not having Claude Code is a normal state, not a crash."""
        assert scc.read_login(tmp_path / "nope.json") is None

    def test_malformed_file_returns_none(self, tmp_path):
        p = tmp_path / ".credentials.json"
        p.write_text("{ this is not json")
        assert scc.read_login(p) is None

    def test_file_without_the_oauth_block_returns_none(self, tmp_path):
        """An MCP-only credentials file must not read as a Claude login."""
        p = tmp_path / ".credentials.json"
        p.write_text(json.dumps({"mcpOAuth": {"some-server": {}}}))
        assert scc.read_login(p) is None

    def test_empty_access_token_counts_as_no_login(self, tmp_path):
        p = tmp_path / ".credentials.json"
        p.write_text(json.dumps({"claudeAiOauth": {"accessToken": ""}}))
        assert scc.read_login(p) is None


class TestWhatTheUserIsTold:
    def test_reports_the_plan(self, tmp_path):
        oauth = scc.read_login(_write_login(tmp_path, plan="max"))
        assert any("max" in line for line in scc.describe(oauth))

    def test_expired_token_is_not_reported_as_a_failure(self, tmp_path):
        """Clover refreshes it. Alarming the user would be wrong."""
        oauth = scc.read_login(_write_login(tmp_path, expires_in_hours=-5))
        text = " ".join(scc.describe(oauth))
        assert "refresh" in text.lower()


class TestWhatItWrites:
    def test_sets_provider_model_and_a_fallback_chain(self, tmp_path, monkeypatch):
        """The written config must match what a working agent runs.

        Alfred and Oracle both run provider=anthropic with cheaper models
        underneath. A single dead provider leaves an agent with nothing to
        answer with, so the chain is part of the promise.
        """
        saved = {}

        import clover_cli.config as cfgmod
        monkeypatch.setattr(cfgmod, "load_config", lambda: {})
        monkeypatch.setattr(cfgmod, "save_config", lambda c, *a, **k: saved.update(c))
        monkeypatch.setattr(
            cfgmod, "use_anthropic_claude_code_credentials",
            lambda *a, **k: saved.setdefault("_creds_cleared", True),
        )

        scc.apply("claude-opus-5")

        assert saved["model"]["provider"] == "anthropic"
        assert saved["model"]["default"] == "claude-opus-5"
        assert [f["model"] for f in saved["fallback_providers"]] == scc.FALLBACKS
        assert saved["_creds_cleared"] is True

    def test_clears_the_env_token_slots(self, tmp_path, monkeypatch):
        """Clover must read Claude Code's own file, not a stale copy.

        Copying the token would freeze it: the next `claude login` would
        refresh the real file while Clover kept using the old value.
        """
        calls = []
        import clover_cli.config as cfgmod
        monkeypatch.setattr(cfgmod, "load_config", lambda: {})
        monkeypatch.setattr(cfgmod, "save_config", lambda c, *a, **k: None)
        monkeypatch.setattr(
            cfgmod, "use_anthropic_claude_code_credentials",
            lambda *a, **k: calls.append("cleared"),
        )
        scc.apply()
        assert calls == ["cleared"]

    def test_keeps_a_fallback_chain_the_user_already_set(self, tmp_path, monkeypatch):
        existing = {"fallback_providers": [{"provider": "openai", "model": "gpt-5"}]}
        saved = {}
        import clover_cli.config as cfgmod
        monkeypatch.setattr(cfgmod, "load_config", lambda: dict(existing))
        monkeypatch.setattr(cfgmod, "save_config", lambda c, *a, **k: saved.update(c))
        monkeypatch.setattr(
            cfgmod, "use_anthropic_claude_code_credentials", lambda *a, **k: None
        )
        scc.apply()
        assert saved["fallback_providers"] == existing["fallback_providers"]


class TestTheNameUsersActuallyType:
    def test_claude_and_claude_code_resolve_to_anthropic(self):
        """The provider list has 80 names and none of them says Claude Code."""
        from clover_cli.auth_commands import _normalize_provider
        for typed in ("claude", "claude-code", "Claude Code".lower().replace(" ", "-"),
                      "CLAUDE", "  claude  "):
            assert _normalize_provider(typed) == "anthropic", typed

    def test_unrelated_names_are_left_alone(self):
        from clover_cli.auth_commands import _normalize_provider
        assert _normalize_provider("openrouter") == "openrouter"
        assert _normalize_provider("gemini") == "gemini"
