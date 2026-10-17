"""Fresh policy validation at proactive writers and the real offline PTB boundary.

All policy, preferences and Telegram identities are synthetic. Only the network
endpoint is replaced: the native callbacks and atomic preference writer are real.
"""
import os
from pathlib import Path

import pytest

from clover_cli import config, managed_scope
from clover_cli.proactive import ProactiveDraft, proactive_command
from tests.gateway.test_proactive_command import home  # noqa: F401
from tests.test_proactive_command_access_wire import callback, command, dispatch, wire
from tests.test_proactive_customize_wire import token


INVALID = ["proactive: [\n", "- invalid-policy-root\n", "[]\n", "false\n", "0\n", "policy\n"]
ACTIONS = ["on", "off", "enable", "disable", "recommended", "recall on",
           "suggestions on", "timing never", "frequency daily", "audit on", "skill-drafts on"]


@pytest.fixture
def policy(tmp_path, monkeypatch):
    directory = tmp_path / "managed"
    directory.mkdir()
    monkeypatch.setenv("CLOVER_MANAGED_DIR", str(directory))
    managed_scope.invalidate_managed_cache()
    yield directory / "config.yaml"
    managed_scope.invalidate_managed_cache()


def seed(home):
    path = home / "config.yaml"
    path.write_text("# keep raw preferences and comments\nproactive:\n  enabled: true\n  mode: active\n"
                    "  suggestions:\n    enabled: false\n    preferred_timing: never\n"
                    "  aux:\n    max_calls_per_day: 0\nuntouched: preserve\n")
    return path, path.read_bytes()


@pytest.mark.parametrize("body", INVALID)
@pytest.mark.parametrize("action", ACTIONS)
def test_invalid_policy_text_choices_never_write(home, policy, body, action, monkeypatch):
    path, before = seed(home)
    policy.write_text(body)
    calls = []
    save = config.save_config

    def counted(*args, **kwargs):
        calls.append(True)
        return save(*args, **kwargs)

    monkeypatch.setattr(config, "save_config", counted)
    result = proactive_command(action, can_change=True)
    assert "Not saved: administrator settings" in result
    assert "fix them, then try again" in result
    assert "Saved" not in result
    assert not calls
    assert path.read_bytes() == before


@pytest.mark.parametrize("body", INVALID)
def test_open_draft_save_rechecks_current_invalid_policy(home, policy, body):
    path, before = seed(home)
    policy.write_text("{}\n")
    draft = ProactiveDraft.open()
    draft.recommended()
    policy.write_text(body)  # no invalidation: the write must reread policy
    result = draft.save()
    assert "Not saved: administrator settings" in result
    assert path.read_bytes() == before


@pytest.mark.parametrize("action", ["on", "off", "recall on", "draft"])
def test_unreadable_existing_policy_never_writes(home, policy, monkeypatch, action):
    path, before = seed(home)
    policy.write_text("{}\n")
    draft = ProactiveDraft.open()
    draft.recommended()
    original = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == policy:
            raise PermissionError("synthetic unreadable administrator file")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    result = draft.save() if action == "draft" else proactive_command(action, can_change=True)
    assert "Not saved: administrator settings" in result
    assert path.read_bytes() == before


@pytest.mark.parametrize("body", [None, "", "# no pins\n", "null\n", "{}\n"])
def test_absent_or_empty_policy_keeps_admitted_control_without_owner(home, policy, body):
    if body is not None:
        policy.write_text(body)
    assert managed_scope.load_managed_config(strict=True) == {}
    assert "Saved" in proactive_command("on", can_change=True)
    assert config.require_readable_config_before_write()["proactive"]["enabled"] is True


def test_nonexistent_managed_directory_is_unconfigured(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_MANAGED_DIR", str(tmp_path / "not-installed"))
    assert "Saved" in proactive_command("on", can_change=True)


def test_current_pins_bypass_startup_same_size_mtime_cache(home, policy):
    path, before = seed(home)
    policy.write_text("proactive:\n  repair:\n    enabled: true\n")
    original = policy.stat()
    assert managed_scope.load_managed_config()["proactive"]["repair"]["enabled"] is True
    draft = ProactiveDraft.open()
    draft.set("recall", "on")
    policy.write_text("proactive:\n  recall:\n    enabled: true\n")
    os.utime(policy, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert policy.stat().st_size == original.st_size
    assert managed_scope.load_managed_config(strict=True)["proactive"]["recall"]["enabled"] is True
    assert "managed by your administrator" in draft.save()
    assert path.read_bytes() == before


@pytest.mark.parametrize("body", INVALID)
def test_strict_validation_does_not_change_forgiving_startup(policy, body):
    policy.write_text(body)
    assert managed_scope.load_managed_config() == {}
    with pytest.raises(managed_scope.ManagedPolicyError, match="administrator settings"):
        managed_scope.load_managed_config(strict=True)


@pytest.mark.parametrize("failure", ["directory", "encoding", "directory-permission"])
def test_established_policy_io_failure_is_not_absence(home, policy, monkeypatch, failure):
    path, before = seed(home)
    if failure == "directory":
        policy.mkdir()
    elif failure == "encoding":
        policy.write_bytes(b"\xff\xfe\x00\x81")
    else:
        original = Path.is_dir

        def denied(self):
            if self == policy.parent:
                raise PermissionError("synthetic managed directory permission denial")
            return original(self)

        monkeypatch.setattr(Path, "is_dir", denied)
    assert "Not saved: administrator settings" in proactive_command("on", can_change=True)
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("body", INVALID[:2])
@pytest.mark.parametrize("entry", ["/proactive", "/proactive customize", "/proactive on", "/proactive off", "/proactive recall on"])
async def test_real_ptb_invalid_policy_menu_and_text_explain_refusal(home, policy, monkeypatch, body, entry):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    before = (home / "config.yaml").read_bytes()
    policy.write_text(body)
    async with app:
        await dispatch(app, a, command(app, entry))
        reply = [p for method, p in endpoint.calls if method == "sendMessage"][-1]
        assert "administrator settings" in reply["text"]
        assert "fix them, then try again" in reply["text"]
        assert "reply_markup" not in reply
    assert (home / "config.yaml").read_bytes() == before
    errors.assert_not_called()
    r._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", INVALID[:2])
@pytest.mark.parametrize("selection", ["On", "Off", "💾 Save"])
async def test_real_ptb_open_callback_rechecks_invalid_policy_before_any_page_or_save(home, policy, monkeypatch, body, selection):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    policy.write_text("{}\n")
    before = (home / "config.yaml").read_bytes()
    async with app:
        await dispatch(app, a, command(app, "/proactive customize" if selection == "💾 Save" else "/proactive"))
        menu = [p for method, p in endpoint.calls if method == "sendMessage"][-1]
        if selection == "💾 Save":
            await app.process_update(callback(app, token(menu, "✨ Use recommended")))
            menu = [p for method, p in endpoint.calls if method == "editMessageText"][-1]
        edits_before = len([1 for method, _ in endpoint.calls if method == "editMessageText"])
        policy.write_text(body)  # no cache invalidation or gateway reload
        await app.process_update(callback(app, token(menu, selection)))
        assert len([1 for method, _ in endpoint.calls if method == "editMessageText"]) == edits_before
        answers = [p for method, p in endpoint.calls if method == "answerCallbackQuery"]
        assert answers and "Saved" not in answers[-1].get("text", "")
    assert (home / "config.yaml").read_bytes() == before
    errors.assert_not_called()
    r._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["On", "Off", "💾 Save"])
async def test_real_ptb_unreadable_policy_revokes_open_controls_and_explains_text(home, policy, monkeypatch, selection):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    policy.write_text("{}\n")
    before = (home / "config.yaml").read_bytes()
    async with app:
        await dispatch(app, a, command(app, "/proactive customize" if selection == "💾 Save" else "/proactive"))
        menu = [p for method, p in endpoint.calls if method == "sendMessage"][-1]
        if selection == "💾 Save":
            await app.process_update(callback(app, token(menu, "✨ Use recommended")))
            menu = [p for method, p in endpoint.calls if method == "editMessageText"][-1]
        edits_before = len([1 for method, _ in endpoint.calls if method == "editMessageText"])
        original = Path.read_text

        def denied(self, *args, **kwargs):
            if self == policy:
                raise PermissionError("synthetic managed file read denial")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
        await app.process_update(callback(app, token(menu, selection)))
        assert len([1 for method, _ in endpoint.calls if method == "editMessageText"]) == edits_before
        for entry in ("/proactive", "/proactive on", "/proactive off", "/proactive recall on"):
            await dispatch(app, a, command(app, entry))
            reply = [p for method, p in endpoint.calls if method == "sendMessage"][-1]
            assert "administrator settings" in reply["text"]
            assert "reply_markup" not in reply
    assert (home / "config.yaml").read_bytes() == before
    errors.assert_not_called()
    r._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["feature", "admin"])
async def test_real_ptb_save_honors_new_valid_policy_without_reload(home, policy, monkeypatch, restriction):
    r, a, app, endpoint, errors = wire(home, monkeypatch)
    policy.write_text("{}\n")
    before = (home / "config.yaml").read_bytes()
    async with app:
        await dispatch(app, a, command(app, "/proactive customize"))
        menu = [p for method, p in endpoint.calls if method == "sendMessage"][-1]
        await app.process_update(callback(app, token(menu, "✨ Use recommended")))
        menu = [p for method, p in endpoint.calls if method == "editMessageText"][-1]
        edits_before = len([1 for method, _ in endpoint.calls if method == "editMessageText"])
        policy.write_text("proactive:\n  suggestions:\n    enabled: false\n" if restriction == "feature"
                          else "platforms:\n  telegram:\n    allow_admin_from: ['87654321']\n")
        await app.process_update(callback(app, token(menu, "💾 Save")))
        edits = [p for method, p in endpoint.calls if method == "editMessageText"]
        if restriction == "feature":
            assert len(edits) == edits_before + 1
            assert "Not saved" in edits[-1]["text"] and "Saved; applies" not in edits[-1]["text"]
        else:
            assert len(edits) == edits_before
    assert (home / "config.yaml").read_bytes() == before
    errors.assert_not_called()
    r._handle_message_with_agent.assert_not_called()
