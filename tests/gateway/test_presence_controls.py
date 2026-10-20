"""Synthetic UI/control contract tests, not live authorization or persistence."""
from dataclasses import replace

import pytest

from tests.gateway.test_presence_delivery import synthetic_offer_and_context


def synthetic_controls():
    pd, offer, publication_context = synthetic_offer_and_context()
    assert hasattr(pd, "PresenceControls"), "presence control boundary is missing"
    controls = pd.PresenceControls()
    context = pd.ControlContext(
        scope=offer.scope, binding=publication_context.binding,
        actor_id="person-a", message_id="message-ack-a", thread_id="",
        epoch=offer.epoch, now=100.0, thought_id=offer.thought_id, revision=offer.revision,
        can_inspect=True, can_change=True, managed_readable=True, managed_allowed=True,
    )
    return pd, controls, context


def test_menu_renders_bounded_opaque_callbacks_for_both_surface_consumers():
    import json
    pd, controls, context = synthetic_controls()
    menu = controls.open_menu(context)
    assert menu["type"] == "presence.controls"
    assert menu["schema_version"] == 1
    assert [b["label"] for b in menu["buttons"]] == ["Inspect", "Correct", "Forget", "Stop everywhere"]
    tokens = [b["token"] for b in menu["buttons"]]
    assert len(set(tokens)) == 4
    assert all(len(t.encode("utf-8")) <= 64 for t in tokens)
    assert all(context.thought_id not in t and context.actor_id not in t for t in tokens)
    assert "Saved" not in menu["status"] and "Stopped" not in menu["status"]
    json.dumps(menu)



@pytest.mark.parametrize("changes,labels", [
    ({"can_inspect": False}, []), ({"can_inspect": "true"}, []),
    ({"can_change": False}, ["Inspect"]), ({"can_change": 1}, ["Inspect"]),
    ({"managed_allowed": False}, ["Inspect"]), ({"managed_allowed": "true"}, ["Inspect"]),
    ({"managed_readable": False}, []), ({"managed_readable": None}, []),
    ({"message_id": ""}, []),
])
def test_menu_has_no_fake_or_unauthorized_mutating_controls(changes, labels):
    pd, controls, context = synthetic_controls()
    menu = controls.open_menu(replace(context, **changes))
    assert [b["label"] for b in menu["buttons"]] == labels
    assert "On" not in [b["label"] for b in menu["buttons"]]


@pytest.mark.parametrize("surface,chat_type", [("telegram", "group"), ("desktop", "shared"), ("tui", "private")])
def test_unsupported_shared_menu_has_no_controls(surface, chat_type):
    pd, controls, context = synthetic_controls()
    context = replace(context, binding=replace(context.binding, surface=surface, chat_type=chat_type))
    assert controls.open_menu(context)["buttons"] == []


@pytest.mark.parametrize("label,action", [("Inspect", "inspect"), ("Correct", "begin_correction"), ("Forget", "forget"), ("Stop everywhere", "stop")])
def test_control_yields_one_scoped_request_not_an_applied_change(label, action):
    pd, controls, context = synthetic_controls()
    menu = controls.open_menu(context)
    token = next(b["token"] for b in menu["buttons"] if b["label"] == label)
    assert hasattr(controls, "request"), "callback request boundary is missing"
    request = controls.request(token, context)
    assert request.action == action
    assert request.profile == context.scope.profile
    assert request.expected_epoch == context.epoch
    assert not hasattr(request, "saved") and not hasattr(request, "delivered")
    if action == "stop":
        assert request.scope is None and request.thought_id is None and request.revision is None
    else:
        assert request.scope == context.scope
        assert request.thought_id == context.thought_id and request.revision == context.revision
    assert controls.request(token, context) is None, "same native tap must not produce a second request"


@pytest.mark.parametrize("field,value", [
    ("actor_id", "other-person"), ("message_id", "message-foreign"), ("thread_id", "thread-foreign"),
    ("epoch", 4), ("thought_id", "thought-foreign"), ("revision", 2),
    ("can_inspect", False), ("can_change", False), ("managed_allowed", False),
    ("managed_readable", False), ("can_change", "true"), ("managed_allowed", 1),
    ("now", 400.0), ("now", 99.0),
])
def test_changed_context_or_revoked_authority_never_requests_or_consumes_menu(field, value):
    pd, controls, context = synthetic_controls()
    token = controls.open_menu(context)["buttons"][-1]["token"]
    assert controls.request(token, replace(context, **{field: value})) is None
    assert controls.request(token, context) is not None


@pytest.mark.parametrize("field", ["profile", "project", "audience", "owner_root", "conversation_root"])
def test_control_scope_is_exact_and_not_retargeted(field):
    pd, controls, context = synthetic_controls()
    token = controls.open_menu(context)["buttons"][-1]["token"]
    scope = replace(context.scope, **{field: "foreign"})
    assert controls.request(token, replace(context, scope=scope)) is None
    assert controls.request(token, context) is not None


@pytest.mark.parametrize("field", ["surface", "adapter_id", "credential_instance", "principal_id", "conversation_id", "chat_type", "identity_link"])
def test_native_callback_is_bound_to_adapter_identity_and_conversation(field):
    pd, controls, context = synthetic_controls()
    token = controls.open_menu(context)["buttons"][-1]["token"]
    binding = replace(context.binding, **{field: "foreign"})
    assert controls.request(token, replace(context, binding=binding)) is None
    assert controls.request(token, context) is not None


@pytest.mark.parametrize("field,value", [
    ("epoch", True), ("revision", True), ("epoch", -1), ("revision", 0),
    ("now", float("inf")), ("now", float("nan")), ("now", False),
    ("actor_id", ""), ("actor_id", "person-a "), ("actor_id", "\ud800"),
    ("thought_id", ""), ("thought_id", " thought-a"), ("message_id", True),
])
def test_malformed_controls_do_not_issue_live_callbacks(field, value):
    pd, controls, context = synthetic_controls()
    assert controls.open_menu(replace(context, **{field: value}))["buttons"] == []


def test_shared_scope_controls_are_denied_even_if_surface_says_private():
    pd, controls, context = synthetic_controls()
    context = replace(context, scope=replace(context.scope, audience="shared:group:chat-a"))
    assert controls.open_menu(context)["buttons"] == []


def test_new_menu_invalidates_old_native_message_but_not_foreign_profiles():
    pd, controls, context = synthetic_controls()
    old = controls.open_menu(context)["buttons"][-1]["token"]
    foreign = replace(context, scope=replace(context.scope, profile="profile-b"))
    foreign_token = controls.open_menu(foreign)["buttons"][-1]["token"]
    newer = replace(context, message_id="message-ack-b")
    new = controls.open_menu(newer)["buttons"][-1]["token"]
    assert controls.request(old, context) is None
    assert controls.request(foreign_token, foreign) is not None
    assert controls.request(new, newer) is not None


def test_menu_cache_is_bounded_and_restart_drops_callbacks():
    pd, controls, context = synthetic_controls()
    old = controls.open_menu(context)["buttons"][-1]["token"]
    for i in range(300):
        current = replace(context, actor_id=f"actor-{i}")
        latest = controls.open_menu(current)["buttons"][-1]["token"]
    assert controls.request(old, context) is None
    assert pd.PresenceControls().request(latest, current) is None
    assert controls.request(latest, current) is not None


@pytest.mark.parametrize("changes,words", [
    ({"can_inspect": False}, "permission"),
    ({"can_change": False}, "Read-only"),
    ({"managed_allowed": False}, "Read-only"),
    ({"managed_readable": False}, "Administrator settings"),
    ({"message_id": ""}, "current private conversation"),
])
def test_unavailable_controls_explain_permission_or_policy_not_owner_enrollment(changes, words):
    pd, controls, context = synthetic_controls()
    menu = controls.open_menu(replace(context, **changes))
    assert words in menu["status"]
    assert "enroll" not in menu["status"].lower()
    assert "saved" not in menu["status"].lower()



def test_desktop_controls_expose_fixed_action_ids_for_localization():
    pd, controls, context = synthetic_controls()
    menu = controls.open_menu(context)
    assert [button.get("action") for button in menu["buttons"]] == [
        "inspect", "begin_correction", "forget", "stop",
    ]
