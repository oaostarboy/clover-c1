"""Bounded local suggestions, presented by the existing gateway response owner.

No model calls, scheduler, delivery route, or execution authority. The optional
footer is a presentation artifact, not model output: persisted provider history
and api_content remain untouched. The existing notepad owner stores a bounded
proposed receipt containing the local artifact; it is NOT a delivery or saved
file proof. Only the delivery owner can establish delivery later.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from agent import workstream_origin as origin
from agent import workstreams as ws
from cron import notepad as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfferTurn:
    scope: ws.Scope
    session_id: str
    conversation_root: str
    row_id: int
    event_id: str
    query: str
    ingress: Any


def _now() -> datetime:
    from clover_time import now
    return now()


def _timing(config: Mapping[str, Any]) -> Optional[bool]:
    section = config.get("proactive", {})
    suggestions = section.get("suggestions", {}) if isinstance(section, Mapping) else {}
    value = suggestions.get("preferred_timing") if isinstance(suggestions, Mapping) else None
    # Explicit user-selected inline-after-reply preference; no inferred quiet
    # hours and no clock/scheduler wakeups. Invalid/missing means defer.
    if value == "after_reply":
        return True
    if value == "never":
        return False
    return None


def prepare_offer_turn(agent: Any, row: Any) -> None:
    """Stage a local-only input after the real user row + receipt commit.

    Not included in TurnContext, messages, prompts, tools, or provider payloads.
    The gateway takes it once after a successful ordinary reply. Corrections and
    Feedback events themselves are never opportunities for unsolicited offers.
    """
    if hasattr(agent, '_presence_turn'):
        agent._presence_turn = None
    if hasattr(agent, "_presence_shadow"):
        agent._presence_shadow = None
    settings = ws.load_settings()
    if not settings.enabled or not settings.suggestions_enabled:
        db = getattr(agent, "_session_db", None)
        if hasattr(db, "_presence_pending"):
            db._presence_pending = None
        return
    agent._workstream_suggestion_turn = None
    scope = ws.scope_for_agent(agent)
    if not scope.private or scope.profile != ws.current_profile():
        return
    if not isinstance(row, dict) or row.get("role") != "user":
        return
    text = row.get("content")
    row_id = row.get("_row_id")
    if not isinstance(text, str) or not text.strip() or not isinstance(row_id, int):
        return
    if row.get("display_kind") not in (None, "user") or row.get("tool_call_id"):
        return
    from agent.workstream_human_hooks import parse_operations
    if parse_operations(text):
        return
    ingress = getattr(agent, "_trusted_human_ingress", None)
    if ingress is None or not origin.ingress_is_valid(ingress, profile=scope.profile):
        return
    db = getattr(agent, "_session_db", None)
    if db is None:
        return
    receipt = origin.read_human_origin(db, agent.session_id, row_id)
    if receipt is None or receipt.consumed or receipt.ingress_event_id != ingress.event_id:
        return
    root = db.get_conversation_root(agent.session_id)
    if not root or root != db.get_conversation_root(ingress.session_id):
        return
    agent._workstream_suggestion_turn = OfferTurn(
        scope, agent.session_id, root, row_id, ingress.event_id, text, ingress)
    from clover_cli.config import load_config
    config = load_config()
    presence = config.get("presence", {})
    if isinstance(presence, Mapping) and presence.get("enabled") is True:
        try:
            from agent.presence_shadow import prepare_shadow
            prepare_shadow(agent, agent._workstream_suggestion_turn, now=_now())
            from agent.presence_runtime import prepare_presence
            prepare_presence(agent, agent._workstream_suggestion_turn)
        except Exception:
            logger.debug("optional presence admission unavailable", exc_info=True)
    elif hasattr(db, "_presence_pending"):
        db._presence_pending = None


def _artifact_detail(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        value = json.loads(receipt.get("detail") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def gate_candidate(candidate: ws.Candidate, scope: ws.Scope,
                   settings: ws.ProactiveSettings, *, session_id: str,
                   timing: Optional[bool], now: datetime,
                   judge_verdict: Optional[str] = None) -> ws.GateDecision:
    """Canonical policy plus durable session and bounded scope artifact caps."""
    offers = [r for r in np.list_receipts(scope, kinds=["proposed"])
              if str(r["source_id"]).startswith("offer:")]
    count = sum(_artifact_detail(r).get("session_id") == session_id
                or r.get("detail") == f"session={session_id}" for r in offers)
    decision = ws.evaluate_suggestion(
        candidate, scope, settings, session_id=session_id, query_scope=scope,
        user_timing_ok=timing, offered_this_session=count, now=now)
    if decision.action == "offer" and len(offers) >= settings.threads_max_items:
        decision = ws.GateDecision("defer", "scope_cap", False)
    return ws.apply_judge(decision, judge_verdict)


def _candidate(row: Mapping[str, Any], query: str) -> ws.Candidate:
    # Redact even pre-existing imported records; never surface secrets from an
    # older writer. All text is a proposal to check, not permission to execute.
    text = ws._clip(ws._redact(str(row.get("summary") or row.get("title") or "")), 180)
    return ws.Candidate(
        workstream_id=row["workstream_id"], text=text,
        relevance=ws.relevance(query, text),
        fingerprint=ws._fp("check", text),
    )


def present_offer(agent: Any, result: Any, *, is_current) -> Optional[dict]:
    """Return an inline local artifact only after commit + exact readback.

    The caller is the current-response presentation owner in gateway/run.py.
    It adds the footer to final_response before streaming finalization, without
    altering model messages. A crash after the proposal commit is conservative:
    it stays proposed/unknown and deduped, never falsely marked delivered.
    """
    if hasattr(agent, "_presence_shadow"):
        agent._presence_shadow = None
    turn = getattr(agent, "_workstream_suggestion_turn", None)
    if not isinstance(turn, OfferTurn) or not isinstance(result, dict):
        return None
    agent._workstream_suggestion_turn = None
    if (not isinstance(result.get("final_response"), str)
            or not result["final_response"].strip()
            or any(result.get(k) for k in ("failed", "interrupted", "partial", "error"))
            or result.get("completed") is False or not is_current()):
        return None
    settings = ws.load_settings()
    if not settings.enabled or not settings.suggestions_enabled:
        return None
    if ws.scope_for_agent(agent) != turn.scope or not turn.scope.private:
        return None
    db = getattr(agent, "_session_db", None)
    if db is None or db.get_conversation_root(agent.session_id) != turn.conversation_root:
        return None
    receipt = origin.read_human_origin(db, turn.session_id, turn.row_id)
    if (receipt is None or receipt.consumed or receipt.ingress_event_id != turn.event_id
            or receipt.profile != turn.scope.profile):
        return None
    current_row = next((r for r in db.get_messages(turn.session_id, limit=20, latest=True)
                        if r.get("id") == turn.row_id), None)
    if current_row is None or current_row.get("content") != turn.query:
        return None
    from clover_cli.config import load_config
    config = load_config()
    timing = _timing(config)
    moment = _now()
    presence = config.get("presence", {})
    if isinstance(presence, Mapping) and presence.get("enabled") is True:
        try:
            from agent.presence_shadow import evaluate_shadow
            agent._presence_shadow = evaluate_shadow(
                agent, turn, settings=settings, timing=timing, now=moment, is_current=is_current)
        except Exception:
            logger.debug("optional presence shadow unavailable", exc_info=True)
    elif hasattr(db, "_presence_pending"):
        db._presence_pending = None
    # Public write_batch serializes the policy read + proposal claim across
    # processes; a second response cannot race past caps/cooldown/dedupe.
    offered = None
    with np.write_batch():
        rows = np.list_workstreams(turn.scope, statuses=["open", "deferred"],
                                  limit=settings.threads_max_items)
        candidates = []
        for row in rows:
            if not row.get("approved"):
                continue
            expiry = ws._parse(row.get("expires_at"))
            if expiry is not None and moment >= expiry:
                continue
            if row.get("kind") not in {"correction", "constraint", "decision", "obligation", "blocker"}:
                continue
            candidates.append(_candidate(row, turn.query))
        candidates.sort(key=lambda c: (-c.relevance, c.workstream_id))
        for candidate in candidates:
            # Message-global attachment/audio directives cannot be made inert
            # by quoting. Defer these records, never reinterpret them as sends.
            # Also avoid an unhelpful footer duplicating the requested answer.
            if "[[" in candidate.text or candidate.text in result["final_response"]:
                continue
            decision = gate_candidate(candidate, turn.scope, settings,
                                      session_id=turn.conversation_root,
                                      timing=timing, now=moment)
            if decision.action != "offer":
                continue
            if not is_current() or getattr(agent, "_interrupt_requested", False):
                return None
            state = "offered_local" if decision.surface else "candidate"
            artifact = {
                "state": state, "session_id": turn.conversation_root,
                "workstream_id": candidate.workstream_id,
                "text": candidate.text, "delivery": "unknown",
            }
            detail = json.dumps(artifact, ensure_ascii=False, separators=(",", ":"))
            # add_receipt has a 512-character bound. Never persist truncated
            # JSON and then pretend the visible artifact was saved intact.
            if len(detail) > 512:
                continue
            prefix = "offer:" if decision.surface else "candidate:"
            saved = np.add_receipt(
                turn.scope, candidate.workstream_id, kind="proposed",
                owner="gateway_response", source_id=prefix + candidate.fp,
                detail=detail, now=moment)
            if saved["detail"] != detail:
                continue
            if decision.surface:
                offered = (artifact, saved["receipt_id"])
            break
    if offered is None:
        return None
    artifact, receipt_id = offered
    committed = next((r for r in np.list_receipts(turn.scope, artifact["workstream_id"],
                                                kinds=["proposed"])
                      if r["receipt_id"] == receipt_id), None)
    if committed is None or _artifact_detail(committed) != artifact or not is_current():
        return None
    artifact["receipt_id"] = receipt_id
    from agent.actionable_suggestions import propose

    action = propose(agent, turn, artifact, is_current=is_current)
    if action is not None:
        artifact["action"] = action
    # The existing response adapter treats bare paths and MEDIA: tags as
    # attachment directives. Its blockquote mask is the supported literal
    # presentation surface. Prefix EVERY line (including CR/Unicode newlines)
    # so a stored identifier stays exact without becoming an upload command.
    quoted = "\n".join("> " + line for line in artifact["text"].splitlines())
    artifact["footer"] = (
        f"Optional workstream check [ws:{artifact['workstream_id']}]:\n"
        f"{quoted}\n"
        f"To defer: Feedback: {artifact['workstream_id']} defer: not now"
    )
    return artifact
