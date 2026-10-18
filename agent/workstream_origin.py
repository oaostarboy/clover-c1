"""Trusted human-origin ingress capability (provenance only).

A *capability* is minted by exactly one kind of caller: a platform adapter
that has just authenticated and validated an inbound human message on its own
transport.  It travels, by identity, with that one event object through
gateway dispatch and ``AIAgent.run_conversation`` into message persistence,
where ``SessionDB`` writes a receipt bound to the row actually inserted.

Nothing here is read from an event field, a role, a TTY, a user-supplied id,
or model/tool text.  The registry lives in this module's private state, so a
``MessageEvent`` built, copied, ``dataclasses.replace``-d, restored,
scheduled, delegated or injected by maintenance code has no entry and carries
no provenance. A trusted input owner may explicitly move (not copy) its
existing issue to its validated dispatch or queue clone. Python is not an
in-process sandbox; this is the app's trusted-owner contract, not isolation
from arbitrary code in the same interpreter.

Origin confirms *who typed the message*.  It grants no execution permission,
no continuation authority and no approval.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import uuid
import weakref
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any, Callable, Dict, Optional

HUMAN = "human"

#: A receipt stops being readable this long after it was written.
HUMAN_ORIGIN_TTL_SECONDS = 24 * 3600

_SECRET = secrets.token_bytes(32)
_LOCK = threading.Lock()


@dataclass(frozen=True)
class _Issue:
    event_ref: "weakref.ref[Any]"
    platform: str
    event_id: str
    scope: tuple = ()
    content_digest: str = ""
    source_current: Optional[Callable[[], bool]] = None
    presence_generation: int = -1


# id(event) -> issue, for events an authenticated adapter minted and the
# gateway has not redeemed yet. Entries vanish with their event object.
_ISSUED: Dict[int, _Issue] = {}
# event_id -> callable reporting whether the redeemed turn is still current.
_LIVE: Dict[str, Callable[[], bool]] = {}


@dataclass(frozen=True)
class HumanIngress:
    """Host-minted, sealed invocation capability for one human turn.

    Only :func:`redeem_human_ingress` constructs a valid instance.  The seal
    is an HMAC under a per-process secret over every binding field, so a
    hand-built or altered copy fails :func:`ingress_is_valid`.
    """

    event_id: str
    platform: str
    profile: str
    session_id: str
    session_key: str
    run_generation: int
    seal: str = ""
    scope: tuple = ()
    content_digest: str = ""
    presence_generation: int = -1

    def _payload(self) -> bytes:
        return json.dumps((
            self.event_id, self.platform, self.profile, self.session_id,
            self.session_key, str(int(self.run_generation)),
            self.scope, self.content_digest, self.presence_generation,
        ), ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _seal(ingress: HumanIngress) -> str:
    return hmac.new(_SECRET, ingress._payload(), hashlib.sha256).hexdigest()


def mint_human_ingress(event: Any, *, platform: str, scope: Any = None,
                       original_text: Optional[str] = None,
                       is_current: Optional[Callable[[], bool]] = None) -> bool:
    """Adapter-side producer. Call only after the adapter authenticated the
    sender on its own transport and built ``event`` from that inbound message.

    Refuses synthetic events (``internal``) and a second mint for the same
    event object. Returns whether a capability is now registered for it.
    """
    if event is None or getattr(event, "internal", False) or not str(platform or ""):
        return False
    key = id(event)

    def _forget(_ref, _key=key):
        with _LOCK:
            _ISSUED.pop(_key, None)

    try:
        ref = weakref.ref(event, _forget)
    except TypeError:
        return False
    with _LOCK:
        if key in _ISSUED:
            return False
        binding = scope_binding(scope) if scope is not None else ()
        digest = text_digest(original_text) if original_text is not None else ""
        generation = -1
        source = getattr(event, 'source', None)
        private = (getattr(scope, 'private', False) if scope is not None else
                   getattr(source, 'chat_type', None) in {'dm', 'private'})
        if private and platform in {'telegram', 'desktop'}:
            try:
                from agent.presence_lifecycle import note_human_input
                from agent.workstreams import current_profile
                generation = note_human_input(scope.profile if scope is not None else current_profile())
            except Exception:
                pass  # Ordinary input is unaffected; optional presence holds.
        _ISSUED[key] = _Issue(ref, str(platform), uuid.uuid4().hex, binding, digest, is_current, generation)
    return True


def transfer_human_ingress(source: Any, target: Any, *, platform: str, original_text: str) -> bool:
    """Move an input owner's existing issue onto its exact dispatch event.

    This never mints. Imported/recovered events have no source issue. Binding
    the original digest also makes split batches or middleware rewrites fail
    closed at persistence rather than attributing generated bytes to a human.
    """
    if source is target or target is None or getattr(target, "internal", False):
        return False
    key = id(target)
    try:
        def forget(_ref):
            with _LOCK:
                _ISSUED.pop(key, None)
        ref = weakref.ref(target, forget)
    except TypeError:
        return False
    with _LOCK:
        issue = _ISSUED.get(id(source))
        if (issue is None or issue.event_ref() is not source or issue.platform != platform
                or issue.content_digest != text_digest(original_text) or key in _ISSUED):
            return False
        del _ISSUED[id(source)]
        _ISSUED[key] = replace(issue, event_ref=ref)
    return True


def transfer_queued_human_ingress(source: Any, target: Any) -> bool:
    """Move an existing issue to the host's exact /queue payload clone.

    The only permitted rewrite is removing the parsed command prefix. All
    other event fields must survive unchanged. This moves, never copies or
    mints, and retains the original scope, digest and liveness callback. In
    particular, a bound original digest does NOT attest to rewritten bytes.
    Legacy adapter issues remain unbound; metadata cannot create an issue.
    """
    if (source is target or not is_dataclass(source) or type(source) is not type(target)
            or getattr(source, "internal", False) or getattr(source, "synthetic", False)
            or not getattr(source, "allow_gateway_control", False)
            or source.get_command() != "queue"
            or target.text != source.get_command_args().strip()):
        return False
    for field in fields(source):
        if field.name == "text":
            continue
        expected = getattr(source, field.name)
        if field.name == "message_type" and not source.media_urls:
            expected = type(source.message_type).TEXT
        if getattr(target, field.name) != expected:
            return False
    key = id(target)
    def forget(_ref):
        with _LOCK:
            _ISSUED.pop(key, None)
    try:
        ref = weakref.ref(target, forget)
    except TypeError:
        return False
    with _LOCK:
        issue = _ISSUED.get(id(source))
        platform = getattr(getattr(source, "source", None), "platform", None)
        platform = getattr(platform, "value", platform)
        if (issue is None or issue.event_ref() is not source or key in _ISSUED
                or issue.platform != platform
                or (issue.content_digest and issue.content_digest != text_digest(source.text))):
            return False
        del _ISSUED[id(source)]
        _ISSUED[key] = replace(issue, event_ref=ref)
    return True


def redeem_human_ingress(
    event: Any, *, platform: str, profile: str, session_id: str, session_key: str,
    run_generation: int, is_current: Callable[[], bool],
) -> Optional[HumanIngress]:
    """Gateway-side consumer. Single use: the registry entry is removed, so
    replaying the same event object yields ``None``.

    ``platform`` must equal the minting adapter's, and ``event`` must be the
    very object the adapter minted (identity, not equality). Anything else,
    including synthetic events, returns ``None``.
    """
    if event is None or getattr(event, "internal", False):
        return None
    with _LOCK:
        issue = _ISSUED.get(id(event))
        if issue is None or issue.event_ref() is not event or issue.platform != str(platform):
            return None
        del _ISSUED[id(event)]
        source_current = issue.source_current
        _LIVE[issue.event_id] = lambda: (
            (source_current is None or source_current()) and is_current()
        )
    ingress = HumanIngress(
        event_id=issue.event_id, platform=issue.platform, profile=str(profile),
        session_id=str(session_id), session_key=str(session_key),
        run_generation=int(run_generation),
        scope=issue.scope, content_digest=issue.content_digest,
        presence_generation=issue.presence_generation,
    )
    return replace(ingress, seal=_seal(ingress))


def ingress_is_valid(ingress: Any, *, profile: str) -> bool:
    """Sealed, issued by this process, same profile, and its gateway turn is
    still the current, un-cancelled generation for its session."""
    if not isinstance(ingress, HumanIngress) or not ingress.seal:
        return False
    if not hmac.compare_digest(ingress.seal, _seal(ingress)):
        return False
    if ingress.profile != str(profile):
        return False
    with _LOCK:
        current = _LIVE.get(ingress.event_id)
    if current is None:
        return False
    try:
        return bool(current())
    except Exception:
        return False


def retire_human_ingress(ingress: Any) -> None:
    """Drop the live entry once the turn's row is durable (or abandoned)."""
    event_id = getattr(ingress, "event_id", None)
    if event_id:
        with _LOCK:
            _LIVE.pop(event_id, None)


def scope_binding(scope: Any) -> tuple:
    """Literal canonical scope, never an inferred private audience."""
    return tuple(str(getattr(scope, k)) for k in ("profile", "project", "audience", "root"))


def text_digest(text: Any) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if isinstance(text, str) else ""


def ingress_matches_content(ingress: Any, content: Any) -> bool:
    """New input owners bind exact original text; legacy adapters unchanged."""
    digest = getattr(ingress, "content_digest", "")
    return not digest or hmac.compare_digest(digest, text_digest(content))


def ingress_matches_turn(ingress: Any, agent: Any, content: Any) -> bool:
    from agent.workstreams import scope_for_agent
    binding = getattr(ingress, "scope", ())
    return (not binding or binding == scope_binding(scope_for_agent(agent))) and ingress_matches_content(ingress, content)


def bind_human_ingress_to_turn(ingress: Any, agent: Any, content: Any):
    """Tighten new scoped input capabilities at the real agent invocation.

    Input owners know the checked sender; the agent owner also knows the
    actual turn scope and Stop generation. Every later consumer must retain
    both, including after row persistence. Legacy adapter capabilities keep
    their existing gateway generation/readback contract.
    """
    if not ingress_matches_turn(ingress, agent, content):
        return None
    if not getattr(ingress, "scope", ()):
        return ingress
    from agent.workstreams import scope_for_agent, current_profile
    if not ingress_is_valid(ingress, profile=current_profile()):
        return None
    stop = int(getattr(agent, "_human_ingress_stop_generation", 0))
    with _LOCK:
        prior = _LIVE.get(ingress.event_id)
        if prior is None:
            return None
        _LIVE[ingress.event_id] = lambda: (
            prior() and getattr(agent, "_trusted_human_ingress", None) is ingress
            and int(getattr(agent, "_human_ingress_stop_generation", 0)) == stop
            and scope_binding(scope_for_agent(agent)) == ingress.scope
        )
    return ingress


class ComposerSubmission(str):
    """Queue-compatible input identity; constructing/copying text is not minting.

    Only the actual composer input owner mints on this object. String
    operations deliberately lose its registry identity.
    """


def read_human_origin(db: Any, session_id: str, message_row_id: Any):
    """``agent.workstreams.HumanOriginReader`` backed by the SessionDB receipt.

    Pass as ``human_reader=`` to the workstream consumers. It is deliberately
    *not* installed as ``workstreams._HUMAN_ORIGIN_READER``: wiring the host
    consumers is a separate, explicitly gated step.
    """
    getter = getattr(db, "get_human_origin", None)
    if getter is None:
        return None
    return getter(session_id, message_row_id)
