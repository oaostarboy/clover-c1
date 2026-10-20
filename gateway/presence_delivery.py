"""Provisional presence surface/data contract, pending W1/W2 integration.

Pure preparation only: no imports of a provider, database, platform adapter,
config reader or gateway host. Host-owned effective policy, evidence and identity
snapshots are NOT client input. A prepared publication grants no authority and
is not a claim, attempt or delivery receipt. See docs/design/presence-delivery-boundary.md.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import math
import secrets


@dataclass(frozen=True)
class DeliveryScope:
    profile: str
    project: str
    audience: str
    owner_root: str
    conversation_root: str


@dataclass(frozen=True)
class SurfaceBinding:
    surface: str
    adapter_id: str
    credential_instance: str
    principal_id: str
    conversation_id: str
    chat_type: str
    identity_link: str = ""


@dataclass(frozen=True)
class DeliveryOffer:
    scope: DeliveryScope
    thought_id: str
    revision: int
    event_id: str
    origin_surface: str
    text: str
    expires_at: float
    epoch: int
    generation: int


@dataclass(frozen=True)
class PublicationContext:
    scope: DeliveryScope
    binding: SurfaceBinding
    now: float
    epoch: int
    generation: int
    presence_enabled: bool = False
    proactive_enabled: bool = False
    suggestions_enabled: bool = False
    managed_allowed: bool = False
    source_authorized: bool = False
    evidence_current: bool = False
    quiet: bool = True
    busy: bool = True


@dataclass(frozen=True)
class PreparedPublication:
    offer: DeliveryOffer
    binding: SurfaceBinding
    kind: str = "presence_initiative"
    state: str = "proposed"


@dataclass(frozen=True)
class Preparation:
    publication: PreparedPublication | None
    reason: str


def _bounded_text(value: object, limit: int) -> bool:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeError:
        return False


def _token(value: object) -> bool:
    return (_bounded_text(value, 1024) and value == value.strip()
            and not any(ord(c) < 32 for c in value))


def _counter(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _timestamp(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def prepare_publication(offer: DeliveryOffer, context: PublicationContext) -> Preparation:
    if not isinstance(context, PublicationContext):
        return Preparation(None, "invalid_contract")
    if any(value is not True for value in (
        context.presence_enabled, context.proactive_enabled, context.suggestions_enabled,
    )):
        return Preparation(None, "disabled")
    for value, reason in (
        (context.managed_allowed is True, "managed_denied"),
        (context.source_authorized is True, "unauthorized"),
        (context.evidence_current is True, "stale_evidence"),
        (context.quiet is False, "quiet"),
        (context.busy is False, "busy"),
    ):
        if not value:
            return Preparation(None, reason)
    if (not isinstance(offer, DeliveryOffer) or not isinstance(offer.scope, DeliveryScope)
            or not isinstance(context.scope, DeliveryScope) or not isinstance(context.binding, SurfaceBinding)):
        return Preparation(None, "invalid_contract")
    binding = context.binding
    if not (
        all(_token(getattr(scope, f.name)) for scope in (offer.scope, context.scope)
            for f in fields(DeliveryScope))
        and all(_token(value) for value in (offer.thought_id, offer.event_id,
            binding.adapter_id, binding.credential_instance, binding.principal_id,
            binding.conversation_id))
        and _counter(offer.revision, 1)
        and all(_counter(value) for value in (offer.epoch, offer.generation, context.epoch, context.generation))
        and _timestamp(context.now) and _timestamp(offer.expires_at)
        and _bounded_text(offer.text, 2048)
    ):
        return Preparation(None, "invalid_contract")
    if binding.surface not in ("telegram", "desktop") or offer.origin_surface not in ("telegram", "desktop"):
        return Preparation(None, "unsupported_surface")
    if binding.chat_type != "private":
        return Preparation(None, "private_only")
    if offer.origin_surface != binding.surface and not _token(binding.identity_link):
        return Preparation(None, "identity_link_required")
    if offer.scope != context.scope:
        return Preparation(None, "scope_mismatch")
    if not offer.scope.audience.startswith("private:") or not offer.scope.audience[8:]:
        return Preparation(None, "private_only")
    if offer.epoch != context.epoch:
        return Preparation(None, "stale_epoch")
    if offer.generation != context.generation:
        return Preparation(None, "stale_generation")
    if offer.expires_at <= context.now:
        return Preparation(None, "expired")
    return Preparation(PreparedPublication(offer, context.binding), "prepared")


def surface_payload(publication: PreparedPublication, current: PublicationContext) -> dict | None:
    """Revalidate a host snapshot immediately before the future owner publishes.

    The future owner must CAS its claim/epoch and account for ACK/unknown. This
    pure payload must not be mistaken for a publication lease or an ACK.
    """
    if not isinstance(publication, PreparedPublication) or not isinstance(current, PublicationContext):
        return None
    if publication.binding != current.binding:
        return None
    if prepare_publication(publication.offer, current).publication is None:
        return None
    offer = publication.offer
    if current.binding.surface == "telegram":
        return {"chat_id": current.binding.conversation_id, "text": offer.text,
                "parse_mode": None, "disable_web_page_preview": True,
                "disable_notification": True}
    return {"type": "presence.initiative", "schema_version": 1, "text": offer.text,
            "state": "proposed", "thought_id": offer.thought_id, "revision": offer.revision,
            "epoch": offer.epoch, "event_id": offer.event_id,
            "generation": offer.generation, "focus": False, "navigate": False}


@dataclass(frozen=True)
class ControlContext:
    scope: DeliveryScope
    binding: SurfaceBinding
    actor_id: str
    message_id: str
    thread_id: str
    epoch: int
    now: float
    thought_id: str
    revision: int
    can_inspect: bool = False
    can_change: bool = False
    managed_readable: bool = False
    managed_allowed: bool = False


@dataclass(frozen=True)
class ControlRequest:
    action: str
    profile: str
    expected_epoch: int
    scope: DeliveryScope | None
    thought_id: str | None
    revision: int | None


def _valid_control_context(context: ControlContext) -> bool:
    if (not isinstance(context, ControlContext) or not isinstance(context.scope, DeliveryScope)
            or not isinstance(context.binding, SurfaceBinding)):
        return False
    binding = context.binding
    return (
        all(_token(getattr(context.scope, f.name)) for f in fields(DeliveryScope))
        and context.scope.audience.startswith("private:") and bool(context.scope.audience[8:])
        and all(_token(value) for value in (binding.adapter_id, binding.credential_instance,
            binding.principal_id, binding.conversation_id, context.actor_id,
            context.message_id, context.thought_id))
        and binding.surface in ("telegram", "desktop") and binding.chat_type == "private"
        and (context.thread_id == "" or _token(context.thread_id))
        and (binding.identity_link == "" or _token(binding.identity_link))
        and _counter(context.epoch) and _counter(context.revision, 1) and _timestamp(context.now)
    )


class PresenceControls:
    """Ephemeral UI request projection only, not an authorization/persistence owner.

    Hosts must supply trusted current contexts and apply typed requests through
    the source/epoch owner. Restart drops all menu tokens; it never replays them.
    """

    def __init__(self) -> None:
        self._tickets: dict[str, tuple[ControlContext, str]] = {}

    def open_menu(self, context: ControlContext) -> dict:
        labels = []
        valid = _valid_control_context(context)
        if valid:
            for token, (opened, _) in list(self._tickets.items()):
                if (opened.now + 300 <= context.now or (
                    opened.scope == context.scope and opened.binding == context.binding
                    and opened.actor_id == context.actor_id and opened.thread_id == context.thread_id
                )):
                    self._tickets.pop(token, None)
        if valid and context.can_inspect is True and context.managed_readable is True:
            labels.append("Inspect")
            if context.can_change is True and context.managed_allowed is True:
                labels.extend(("Correct", "Forget", "Stop everywhere"))
        actions = {"Inspect": "inspect", "Correct": "begin_correction", "Forget": "forget", "Stop everywhere": "stop"}
        buttons = [{"label": label, "action": actions[label], "token": "pc:" + secrets.token_hex(16)}
                   for label in labels]
        for button in buttons:
            self._tickets[button["token"]] = (context, actions[button["label"]])
        while len(self._tickets) > 256:
            self._tickets.pop(next(iter(self._tickets)))
        if not valid:
            status = "Controls unavailable. Reopen from a current private conversation."
        elif context.can_inspect is not True:
            status = "You do not have permission to inspect presence here."
        elif context.managed_readable is not True:
            status = "Administrator settings cannot be read. No changes made."
        elif context.can_change is not True or context.managed_allowed is not True:
            status = "Read-only. Current command or administrator policy does not allow changes."
        else:
            status = "Stop applies to proactive presence in this profile on all linked surfaces."
        return {"type": "presence.controls", "schema_version": 1, "status": status, "buttons": buttons}

    def request(self, token: str, current: ControlContext) -> ControlRequest | None:
        if not isinstance(token, str) or not _valid_control_context(current):
            return None
        ticket = self._tickets.get(token)
        if ticket is None:
            return None
        opened, action = ticket
        if (opened.scope != current.scope or opened.binding != current.binding
                or opened.actor_id != current.actor_id or opened.message_id != current.message_id
                or opened.thread_id != current.thread_id or opened.epoch != current.epoch
                or opened.thought_id != current.thought_id or opened.revision != current.revision
                or not opened.now <= current.now < opened.now + 300
                or current.can_inspect is not True or current.managed_readable is not True):
            return None
        if action != "inspect" and (current.can_change is not True or current.managed_allowed is not True):
            return None
        if self._tickets.pop(token, None) is None:
            return None
        if action == "stop":
            return ControlRequest(action, current.scope.profile, current.epoch, None, None, None)
        return ControlRequest(action, current.scope.profile, current.epoch, current.scope,
                              current.thought_id, current.revision)


def telegram_control_markup(menu: dict) -> dict:
    """Render server-issued control data; PTB remains a lazy surface dependency."""
    return {"inline_keyboard": [
        [{"text": button["label"], "callback_data": button["token"]}]
        for button in menu["buttons"]
    ]}
