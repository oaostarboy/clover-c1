"""Explicit, ephemeral presence at the existing after-reply seam.

This experimental path only writes an internal shadow result. No provider,
background task, prompt mutation, delivery or execution authority is added.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import math
from typing import Any

from agent import workstreams as ws
from agent import workstream_origin as origin


# The reading representation and _clamp below are adapted from Project N.E.K.O.
# main_logic/activity/master_emotion.py:62-102 at
# 8d767935f99c31b9b62b9c360d5c181bd48a7d16.
# Copyright 2025-2026 Project N.E.K.O. Team
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License. See third_party/neko/{LICENSE,NOTICE}.
# Modified for Clover: remove model/Focus globals, bind reading to a thought,
# reject bool/non-finite input and use injected UTC time.
# Other parts of this module are new Clover design.
@dataclass(frozen=True)
class AffectReading:
    valence: float
    arousal: float
    confidence: float
    updated_at: datetime


def _clamp(value, lo: float, hi: float, default: float) -> float:
    """NEKO coercion primitive, used only on structured host assessment input."""
    if isinstance(value, bool):
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(lo, min(hi, v))


@dataclass(frozen=True)
class PendingThought:
    scope: ws.Scope
    conversation_root: str
    session_id: str
    source_row_id: int
    text: str
    event_id: str
    expires_at: datetime
    source_digest: str
    reading: AffectReading | None = None
    offered_event_id: str = ""


@dataclass(frozen=True)
class ShadowProposal:
    action: str
    reason: str
    source_row_id: int
    posture: str = "ordinary"
    surface: bool = False


def prepare_shadow(agent: Any, turn: Any, *, now: datetime) -> None:
    """Admit only an explicit command at the receipt-backed prepare seam."""
    agent._presence_shadow = None
    agent._presence_turn_stop = (turn.event_id, int(getattr(agent, "_human_ingress_stop_generation", 0)))
    prefix = "Carry thought: "
    if not turn.query.startswith(prefix):
        return
    prior = getattr(agent._session_db, "_presence_pending", None)
    if isinstance(prior, PendingThought) and prior.event_id == turn.event_id:
        return
    text = ws._clip(ws._redact(turn.query[len(prefix):].strip()), 180)
    if text:
        agent._session_db._presence_pending = PendingThought(
            turn.scope, turn.conversation_root, turn.session_id,
            turn.row_id, text, turn.event_id, now + timedelta(hours=1),
            origin.text_digest(turn.query))


def admit_affect(agent: Any, turn: Any, sample: dict, *, now: datetime) -> bool:
    """Trusted assessment-owner API, not automatic inference or consent.

    The initial runtime has no assessment producer; tests supply synthetic
    structured readings. Only the exact admitted source turn can update it.
    """
    thought = getattr(agent._session_db, "_presence_pending", None)
    if (not isinstance(thought, PendingThought) or thought.event_id != turn.event_id
            or thought.scope != turn.scope or thought.session_id != turn.session_id
            or thought.conversation_root != turn.conversation_root
            or thought.source_row_id != turn.row_id
            or thought.source_digest != origin.text_digest(turn.query)
            or ws.scope_for_agent(agent) != thought.scope
            or not origin.ingress_is_valid(turn.ingress, profile=thought.scope.profile)):
        return False
    from clover_cli.config import load_config
    config = load_config()
    section = config.get("presence", {})
    settings = ws.load_settings(config)
    if (not isinstance(section, dict) or section.get("enabled") is not True
            or not settings.enabled or not settings.suggestions_enabled):
        return False
    reading = AffectReading(
        _clamp(sample.get("valence"), -1, 1, 0),
        _clamp(sample.get("arousal"), 0, 1, 0),
        _clamp(sample.get("confidence"), 0, 1, 0), now)
    agent._session_db._presence_pending = replace(thought, reading=reading)
    return True


def evaluate_shadow(agent: Any, turn: Any, *, settings: ws.ProactiveSettings,
                    timing: bool | None, now: datetime, is_current) -> ShadowProposal | None:
    """Evaluate without surfacing: an offer here is not a delivery decision."""
    thought = getattr(agent._session_db, "_presence_pending", None)
    if not isinstance(thought, PendingThought) or thought.event_id == turn.event_id:
        return None
    if (thought.scope != turn.scope or thought.conversation_root != turn.conversation_root
            or turn.scope.profile != ws.current_profile() or not turn.scope.private):
        return None

    def current() -> bool:
        # run_conversation retires ingress in its finally block before gateway
        # presentation (run_agent.py). The gateway owns post-turn generation;
        # while a capability is still attached, also enforce its live binding.
        attached = getattr(agent, "_trusted_human_ingress", None)
        return (is_current()
                and getattr(agent, "_presence_turn_stop", None) == (
                    turn.event_id, int(getattr(agent, "_human_ingress_stop_generation", 0)))
                and (attached is None or (attached is turn.ingress
                     and origin.ingress_is_valid(turn.ingress, profile=turn.scope.profile))))

    if not current():
        return None
    source = agent._session_db.get_messages_around(
        thought.session_id, thought.source_row_id, window=0)["window"]
    if not source or origin.text_digest(source[0].get("content")) != thought.source_digest:
        agent._session_db._presence_pending = None
        return ShadowProposal("hold", "source_changed", thought.source_row_id)
    if now >= thought.expires_at:
        return ShadowProposal("hold", "expired", thought.source_row_id)
    if thought.offered_event_id:
        return ShadowProposal("hold", "duplicate", thought.source_row_id)
    from agent.workstream_suggestions import gate_candidate
    candidate = ws.Candidate("", thought.text, ws.relevance(turn.query, thought.text))
    decision = gate_candidate(candidate, turn.scope, settings,
                              session_id=turn.conversation_root, timing=timing, now=now)
    if not current():
        return None
    if decision.action == "offer":
        agent._session_db._presence_pending = replace(thought, offered_event_id=turn.event_id)
    return ShadowProposal(
        "offer" if decision.action == "offer" else "hold",
        "shadow" if decision.action == "offer" else decision.reason,
        thought.source_row_id,
        "careful_brief" if thought.reading and thought.reading.valence < -0.3
        and thought.reading.confidence >= 0.7
        and timedelta(0) <= now - thought.reading.updated_at < timedelta(seconds=120)
        else "ordinary")
