"""Focused workstream compaction-audit behavior tests."""

from unittest.mock import MagicMock, patch

from agent import workstreams
from agent.context_compressor import ContextCompressor, _audit_workstream_summary


def _compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=0,
            protect_last_n=0,
            quiet_mode=True,
            tail_mode="legacy",
        )


def _response(content):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    return response


def test_compaction_audit_splices_and_redacts_before_previous_summary(monkeypatch):
    scope = object()
    settings = type(
        "Settings", (), {"enabled": True, "compaction_audit_enabled": True}
    )()
    capsule = "Open obligation: verify api_key=sk-abcdefghijklmnopqrstuvwxyz123456"
    seen = []
    monkeypatch.setattr(workstreams, "load_settings", lambda: settings, raising=False)
    monkeypatch.setattr(
        workstreams,
        "thread_context",
        lambda got_scope, got_settings: seen.append((got_scope, got_settings)) or capsule,
        raising=False,
    )
    compressor = _compressor()
    compressor._workstream_scope = scope

    with patch(
        "agent.context_compressor.call_llm", return_value=_response("summary omits ledger")
    ):
        result = compressor._generate_summary(
            [{"role": "user", "content": "Continue the task"}],
        )

    assert seen == [(scope, settings)]
    summary = compressor._previous_summary
    assert isinstance(summary, str)
    assert isinstance(result, str)
    assert "summary omits ledger" in summary
    assert "Open obligation:" in summary
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in summary
    assert "***" in summary
    assert summary in result


def test_compaction_audit_is_inert_when_disabled(monkeypatch):
    summary = "summary remains exactly this"
    settings = type(
        "Settings", (), {"enabled": True, "compaction_audit_enabled": False}
    )()
    monkeypatch.setattr(workstreams, "load_settings", lambda: settings, raising=False)
    monkeypatch.setattr(
        workstreams,
        "thread_context",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must stay off")),
        raising=False,
    )

    assert _audit_workstream_summary(summary, object()) == summary


def test_compaction_audit_does_not_duplicate_existing_capsule(monkeypatch):
    settings = type(
        "Settings", (), {"enabled": True, "compaction_audit_enabled": True}
    )()
    capsule = "Open blocker: release gate"
    monkeypatch.setattr(workstreams, "load_settings", lambda: settings, raising=False)
    monkeypatch.setattr(
        workstreams, "thread_context", lambda *_args: capsule, raising=False
    )

    result = _audit_workstream_summary(f"summary\n\n{capsule}", object())

    assert result.count(capsule) == 1
