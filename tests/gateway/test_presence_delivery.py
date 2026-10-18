"""Synthetic W3 boundary fixtures; not live or gateway-integrated delivery."""
from dataclasses import replace

import pytest
import importlib
import importlib.util


def boundary():
    assert importlib.util.find_spec("gateway.presence_delivery") is not None, "W3 delivery boundary is missing"
    return importlib.import_module("gateway.presence_delivery")


def synthetic_offer_and_context():
    pd = boundary()
    scope = pd.DeliveryScope("profile-a", "project-a", "private:person-a", "person-a", "conversation-a")
    binding = pd.SurfaceBinding("telegram", "adapter-a", "credential-instance-a", "person-a", "chat-a", "private")
    offer = pd.DeliveryOffer(scope, "thought-a", 1, "event-a", "telegram", "A useful connection.", 200.0, 3, 7)
    context = pd.PublicationContext(scope, binding, 100.0, 3, 7)
    return pd, offer, context


def enabled_context(context):
    return replace(context, presence_enabled=True, proactive_enabled=True, suggestions_enabled=True,
                   managed_allowed=True, source_authorized=True, evidence_current=True,
                   quiet=False, busy=False)


def test_default_off_prepares_nothing_and_changes_no_input():
    pd, offer, context = synthetic_offer_and_context()
    result = pd.prepare_publication(offer, context)
    assert result.publication is None
    assert result.reason == "disabled"
    assert context.presence_enabled is False
    assert offer.text == "A useful connection."


def test_prepared_native_initiative_is_not_a_fake_user_turn_or_delivery_receipt():
    pd, offer, context = synthetic_offer_and_context()
    result = pd.prepare_publication(offer, enabled_context(context))
    assert result.reason == "prepared"
    publication = result.publication
    assert publication is not None
    assert publication.offer == offer
    assert publication.binding == context.binding
    assert publication.kind == "presence_initiative"
    assert publication.state == "proposed"
    assert not hasattr(publication, "messages")
    assert not hasattr(publication, "delivered")



@pytest.mark.parametrize("field,value,reason", [
    ("proactive_enabled", False, "disabled"),
    ("suggestions_enabled", False, "disabled"),
    ("managed_allowed", False, "managed_denied"),
    ("source_authorized", False, "unauthorized"),
    ("evidence_current", False, "stale_evidence"),
    ("quiet", True, "quiet"),
    ("busy", True, "busy"),
    ("presence_enabled", "false", "disabled"),
    ("proactive_enabled", 1, "disabled"),
    ("managed_allowed", "true", "managed_denied"),
    ("source_authorized", 1, "unauthorized"),
    ("evidence_current", "yes", "stale_evidence"),
    ("quiet", None, "quiet"),
    ("busy", 0, "busy"),
])
def test_effective_host_policy_holds_fail_closed(field, value, reason):
    pd, offer, context = synthetic_offer_and_context()
    context = replace(enabled_context(context), **{field: value})
    result = pd.prepare_publication(offer, context)
    assert result.publication is None
    assert result.reason == reason


@pytest.mark.parametrize("field", ["profile", "project", "audience", "owner_root", "conversation_root"])
def test_every_exact_scope_field_must_match(field):
    pd, offer, context = synthetic_offer_and_context()
    foreign_scope = replace(context.scope, **{field: getattr(context.scope, field) + "-foreign"})
    result = pd.prepare_publication(offer, replace(enabled_context(context), scope=foreign_scope))
    assert result.publication is None
    assert result.reason == "scope_mismatch"


@pytest.mark.parametrize("audience", ["shared:group:chat-a", "group", "public", "private:", "private"])
def test_ambiguous_or_shared_scope_never_prepares(audience):
    pd, offer, context = synthetic_offer_and_context()
    scope = replace(offer.scope, audience=audience)
    result = pd.prepare_publication(replace(offer, scope=scope), replace(enabled_context(context), scope=scope))
    assert result.publication is None
    assert result.reason == "private_only"


@pytest.mark.parametrize("field,value,reason", [
    ("epoch", 4, "stale_epoch"), ("generation", 8, "stale_generation"),
    ("now", 200.0, "expired"), ("now", 201.0, "expired"),
])
def test_stop_new_input_and_expiry_invalidate_preparation(field, value, reason):
    pd, offer, context = synthetic_offer_and_context()
    result = pd.prepare_publication(offer, replace(enabled_context(context), **{field: value}))
    assert result.publication is None
    assert result.reason == reason


@pytest.mark.parametrize("owner,field,value", [
    ("offer", "revision", True), ("offer", "revision", 0),
    ("offer", "epoch", True), ("offer", "generation", -1),
    ("offer", "expires_at", float("nan")), ("offer", "expires_at", float("inf")),
    ("offer", "expires_at", True), ("offer", "thought_id", " thought-a"),
    ("offer", "event_id", ""), ("offer", "text", ""),
    ("offer", "text", "界" * 1000), ("offer", "text", "bad\x00text"),
    ("context", "now", float("nan")), ("context", "now", False),
    ("context", "epoch", 3.0), ("context", "generation", True),
    ("binding", "adapter_id", ""), ("binding", "credential_instance", ""),
    ("binding", "principal_id", " person-a"), ("binding", "conversation_id", ""),
    ("scope", "profile", ""), ("scope", "owner_root", "person-a "),
    ("scope", "conversation_root", None),
])
def test_malformed_contract_values_hold_without_repair(owner, field, value):
    pd, offer, context = synthetic_offer_and_context()
    context = enabled_context(context)
    if owner == "offer": offer = replace(offer, **{field: value})
    if owner == "context": context = replace(context, **{field: value})
    if owner == "binding": context = replace(context, binding=replace(context.binding, **{field: value}))
    if owner == "scope":
        scope = replace(offer.scope, **{field: value})
        offer, context = replace(offer, scope=scope), replace(context, scope=scope)
    result = pd.prepare_publication(offer, context)
    assert result.publication is None
    assert result.reason == "invalid_contract"


@pytest.mark.parametrize("surface,chat_type,reason", [
    ("telegram", "group", "private_only"), ("telegram", "supergroup", "private_only"),
    ("telegram", "channel", "private_only"), ("desktop", "shared", "private_only"),
    ("tui", "private", "unsupported_surface"), ("Telegram", "private", "unsupported_surface"),
])
def test_surface_capability_is_explicit_and_private_only(surface, chat_type, reason):
    pd, offer, context = synthetic_offer_and_context()
    context = replace(enabled_context(context), binding=replace(context.binding, surface=surface, chat_type=chat_type))
    result = pd.prepare_publication(offer, context)
    assert result.publication is None
    assert result.reason == reason


def test_matching_numeric_ids_are_not_an_authenticated_cross_surface_link():
    pd, offer, context = synthetic_offer_and_context()
    context = replace(enabled_context(context), binding=replace(context.binding, surface="desktop"))
    result = pd.prepare_publication(offer, context)
    assert result.publication is None
    assert result.reason == "identity_link_required"
    linked = replace(context, binding=replace(context.binding, identity_link="link-from-authenticated-owner"))
    assert pd.prepare_publication(offer, linked).publication is not None


@pytest.mark.parametrize("change", ["epoch", "generation", "evidence", "quiet", "managed", "credential", "principal", "adapter", "conversation", "link"])
def test_final_payload_rechecks_current_host_state_and_exact_adapter_binding(change):
    pd, offer, context = synthetic_offer_and_context()
    context = enabled_context(context)
    publication = pd.prepare_publication(offer, context).publication
    assert hasattr(pd, "surface_payload"), "final publication validator is missing"
    if change in ("epoch", "generation"):
        current = replace(context, **{change: getattr(context, change) + 1})
    elif change == "evidence": current = replace(context, evidence_current=False)
    elif change == "quiet": current = replace(context, quiet=True)
    elif change == "managed": current = replace(context, managed_allowed=False)
    else:
        field = {"credential": "credential_instance", "principal": "principal_id", "adapter": "adapter_id",
                 "conversation": "conversation_id", "link": "identity_link"}[change]
        current = replace(context, binding=replace(context.binding, **{field: "changed"}))
    assert pd.surface_payload(publication, current) is None


@pytest.mark.parametrize("surface", ["telegram", "desktop"])
def test_surface_payload_is_plain_native_data_not_history_or_html(surface):
    import json
    pd, offer, context = synthetic_offer_and_context()
    offer = replace(offer, text="<script>untrusted</script> **not a command**", origin_surface=surface)
    context = replace(enabled_context(context), binding=replace(context.binding, surface=surface))
    publication = pd.prepare_publication(offer, context).publication
    assert hasattr(pd, "surface_payload"), "surface payload renderer is missing"
    payload = pd.surface_payload(publication, context)
    json.dumps(payload)
    assert "messages" not in payload and "role" not in payload and "html" not in payload
    if surface == "telegram":
        assert payload["chat_id"] == "chat-a"
        assert payload["text"] == offer.text
        assert payload["parse_mode"] is None
        assert payload["disable_web_page_preview"] is True
        assert payload["disable_notification"] is True
    else:
        assert payload["type"] == "presence.initiative"
        assert payload["text"] == offer.text
        assert payload["state"] == "proposed"
        assert payload["thought_id"] == offer.thought_id
        assert payload["revision"] == offer.revision
        assert payload["epoch"] == offer.epoch
        assert payload["schema_version"] == 1
        assert payload["focus"] is False
        assert payload["navigate"] is False


@pytest.mark.parametrize("bad", ["offer", "context", "scope", "binding", "surrogate", "scope-surrogate", "boolean-equal-epoch"])
def test_malformed_nested_types_and_unencodable_text_hold_safely(bad):
    pd, offer, context = synthetic_offer_and_context()
    context = enabled_context(context)
    if bad == "offer": offer = None
    if bad == "context": context = {}
    if bad == "scope": offer = replace(offer, scope=None)
    if bad == "binding": context = replace(context, binding={})
    if bad == "surrogate": offer = replace(offer, text="\ud800")
    if bad == "scope-surrogate": offer = replace(offer, scope=replace(offer.scope, profile="\ud800"))
    if bad == "boolean-equal-epoch": offer, context = replace(offer, epoch=1), replace(context, epoch=True)
    result = pd.prepare_publication(offer, context)
    assert result.publication is None
    assert result.reason == "invalid_contract"


def test_out_of_range_time_cannot_crash_preparation():
    pd, offer, context = synthetic_offer_and_context()
    result = pd.prepare_publication(offer, replace(enabled_context(context), now=10 ** 1000))
    assert result.publication is None and result.reason == "invalid_contract"


def test_invalid_final_publication_input_holds_instead_of_raising():
    pd, offer, context = synthetic_offer_and_context()
    context = enabled_context(context)
    publication = pd.prepare_publication(offer, context).publication
    assert pd.surface_payload(None, context) is None
    assert pd.surface_payload(publication, None) is None
