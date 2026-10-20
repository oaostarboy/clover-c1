"""Opt-in continuity contracts; affect is tentative, ephemeral and non-authoritative.

No provider calls, prompt mutations, source discovery, timers or delivery hooks.
The authenticated host supplies the exact context and an evidence-owner reader.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from datetime import datetime, timedelta
from uuid import uuid4
from typing import Callable, Mapping, Protocol

from agent.workstreams import Scope


def _current(enabled: bool, fence: Callable[[], bool] | None) -> bool:
    """Host-owned effective-policy/epoch fence, never inferred from model text."""
    if enabled is not True:
        return False
    try:
        return fence is None or fence() is True
    except Exception:
        return False


def _identifier(value: object, label: str, limit: int = 512) -> None:
    if (not isinstance(value, str) or not value.strip()
            or len(value.encode("utf-8")) > limit or any(ord(c) < 32 for c in value)):
        raise ValueError(f"invalid {label}")


@dataclass(frozen=True)
class ContextKey:
    scope: Scope
    conversation_root: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            raise ValueError("invalid scope")
        for field in ("profile", "project", "audience", "root"):
            _identifier(getattr(self.scope, field), field, 4096 if field == "project" else 512)
        _identifier(self.conversation_root, "conversation root")


@dataclass(frozen=True)
class EvidenceRef:
    owner: str
    source_id: str
    revision: int
    digest: str

    def __post_init__(self) -> None:
        _identifier(self.owner, "source owner")
        _identifier(self.source_id, "source id")
        if type(self.revision) is not int or not 0 < self.revision <= 2**63 - 1:
            raise ValueError("invalid evidence revision")
        if not isinstance(self.digest, str) or re.fullmatch(r"[0-9a-f]{64}", self.digest) is None:
            raise ValueError("digest must be exact lowercase SHA-256")


@dataclass(frozen=True)
class EvidenceSnapshot:
    key: ContextKey
    ref: EvidenceRef
    trusted: bool


@dataclass(frozen=True)
class Thought:
    key: ContextKey
    source: EvidenceRef
    thought_id: str
    revision: int
    purpose: str
    summary: str
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.key, ContextKey) or not isinstance(self.source, EvidenceRef):
            raise ValueError("invalid thought provenance")
        _identifier(self.thought_id, "thought id", 128)
        if type(self.revision) is not int or not 0 < self.revision <= 2**63 - 1:
            raise ValueError("invalid thought revision")
        if self.purpose not in {"follow_up", "project_connection", "shared_language"}:
            raise ValueError("unsupported thought purpose")
        if self.status not in {"pending", "expired", "corrected", "deleted", "invalidated"}:
            raise ValueError("unsupported thought status")
        if not isinstance(self.summary, str) or len(self.summary.encode("utf-8")) > 2048:
            raise ValueError("invalid summary")
        if (self.status == "pending" and not self.summary.strip()) or (self.status != "pending" and self.summary):
            raise ValueError("invalid pending text or nonempty tombstone")
        for value in (self.created_at, self.updated_at, self.expires_at):
            if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
                raise ValueError("thought timestamps must be aware UTC")
        if not self.created_at <= self.updated_at or not timedelta(0) < self.expires_at - self.created_at <= timedelta(days=7):
            raise ValueError("invalid thought lifetime")


class PresenceStoreError(Exception):
    """Unavailable, corrupt, unsupported or over-cap continuity storage."""


class ThoughtRepository(Protocol):
    def put(self, thought: Thought) -> Thought | None: ...
    def list(self, key: ContextKey) -> tuple[Thought, ...]: ...
    def source_is_current(self, key: ContextKey, source: EvidenceRef) -> bool: ...
    def invalidate(self, thought: Thought, status: str, now: datetime) -> bool: ...
    def invalidate_source(self, key: ContextKey, source: EvidenceRef, status: str, now: datetime) -> int: ...


class EvidenceReader(Protocol):
    """Owner must authenticate scope, exact revision/digest and human provenance.

    Return None for missing, deleted, untrusted or unreadable source. This is
    a trusted host boundary, never a model-supplied callback or URL checker.
    """

    def __call__(self, key: ContextKey, ref: EvidenceRef) -> EvidenceSnapshot | None: ...


# Adapted representation/latest-reading mechanism from Project N.E.K.O.
# main_logic/activity/master_emotion.py:62-102,190-281 at
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
# Modified for Clover: injected UTC clock/owner reader, exact scoped evidence,
# no globals, diagnosis, aggregation, model loop or external-intent authority.
@dataclass(frozen=True)
class AffectReading:
    valence: float
    arousal: float
    confidence: float
    observed_at: datetime
    source: EvidenceRef
    posture: str = ''  # Empty only for legacy trusted internal VA samples.


def reading_posture(reading: AffectReading | None) -> str:
    """Pure projection also used by read-only status; no state mutation."""
    if reading is None or reading.confidence < 0.7:
        return 'ordinary'
    if reading.posture:
        return reading.posture
    return 'careful_brief' if reading.valence < -0.3 else 'ordinary'


@dataclass(frozen=True)
class AssessmentTicket:
    key: ContextKey
    source: EvidenceRef
    generation: int
    observed_at: datetime


def evidence_matches(reader: EvidenceReader, key: ContextKey, ref: EvidenceRef) -> bool:
    """Unavailable or changed evidence never becomes permission or posture."""
    if not key.scope.private:
        return False
    try:
        snapshot = reader(key, ref)
        return (isinstance(snapshot, EvidenceSnapshot) and snapshot.trusted is True
                and snapshot.key == key and snapshot.ref == ref)
    except Exception:
        # Evidence owners can fail due to retention, transport or storage errors.
        return False


class ThoughtState:
    """Trusted host admission/read API. Inert unless explicitly enabled.

    Construct per exact context. Callers must enforce effective managed policy
    and authenticated ingress before passing key/reader or explicit controls.
    """

    def __init__(self, key: ContextKey, reader: EvidenceReader, store: ThoughtRepository,
                 *, clock: Callable[[], datetime], enabled: bool = False,
                 is_current: Callable[[], bool] | None = None):
        self.key, self.reader, self.store, self.clock = key, reader, store, clock
        self.enabled = enabled is True
        self._is_current = is_current
        self.affect = AffectState(key, self._affect_source_reader, clock=clock,
                                  enabled=enabled, is_current=self._active)
        self.last_error: str | None = None

    def _active(self) -> bool:
        return _current(self.enabled, self._is_current)

    def _affect_source_reader(self, key: ContextKey, source: EvidenceRef) -> EvidenceSnapshot | None:
        if not self._active() or key != self.key:
            return None
        snapshot = self.reader(key, source)
        try:
            # Revocation can come from another context owner/process even when
            # the original message itself remains unchanged after feedback.
            return snapshot if self.store.source_is_current(key, source) and self._active() else None
        except PresenceStoreError:
            self.last_error = "store_unavailable"
            return None

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled is True
        self.affect.set_enabled(self.enabled)

    def admit(self, source: EvidenceRef, summary: str, *, purpose: str = "follow_up",
              ttl: timedelta = timedelta(hours=1)) -> Thought | None:
        if (not self._active() or not evidence_matches(self.reader, self.key, source)
                or not self._active()):
            return None
        if not isinstance(summary, str) or not summary.strip() or len(summary.encode("utf-8")) > 2048:
            raise ValueError("summary must contain 1..2048 UTF-8 bytes")
        if purpose not in {"follow_up", "project_connection", "shared_language"}:
            raise ValueError("unsupported thought purpose")
        if not isinstance(ttl, timedelta) or not timedelta(0) < ttl <= timedelta(days=7):
            raise ValueError("thought TTL must be positive and at most seven days")
        now = self.clock()
        thought = Thought(self.key, source, uuid4().hex, 1, purpose, summary,
                          "pending", now, now, now + ttl)
        self.last_error = None
        try:
            if not self._active():
                return None
            admitted = self.store.put(thought)
            if admitted is not None and (not self._active()
                    or not evidence_matches(self.reader, self.key, source)
                    or not self._active()):
                # Owner operations are optimistic across distinct stores. A late
                # Stop/correction must leave no pending text, even after put.
                self.store.invalidate(admitted, 'invalidated', self.clock())
                self.affect.correct()
                return None
            return admitted
        except PresenceStoreError:
            self.last_error = "store_unavailable"
            return None

    def is_current(self, thought: Thought) -> bool:
        """Final-revalidation predicate, NOT a claim, permission or send receipt.

        A delivery owner must also check its own epoch/lease immediately before
        publication. There is no atomic transaction with the source-owner DB.
        """
        return (self._active() and isinstance(thought, Thought) and thought.key == self.key
                and thought in self.list_pending() and thought in self.inspect() and self._active())

    def invalidate_source(self, source: EvidenceRef, *, status: str = "corrected") -> int:
        """Authenticated source-owner correction/delete; revokes in-flight affect."""
        if status not in {"corrected", "deleted", "invalidated"}:
            raise ValueError("unsupported source invalidation")
        self.affect.correct()
        if not self.enabled and not self.inspect():
            return 0
        self.last_error = None
        try:
            return self.store.invalidate_source(self.key, source, status, self.clock())
        except PresenceStoreError:
            self.last_error = "store_unavailable"
            return 0

    def inspect(self) -> tuple[Thought, ...]:
        """Exact-scope dormant metadata; host must authorize user-facing access."""
        self.last_error = None
        try:
            return self.store.list(self.key)
        except PresenceStoreError:
            self.last_error = "store_unavailable"
            return ()

    def list_pending(self) -> tuple[Thought, ...]:
        if not self._active():
            return ()
        pending = []
        now = self.clock()
        try:
            for thought in self.inspect():
                if thought.status != "pending":
                    continue
                if not thought.created_at <= now < thought.expires_at:
                    self.store.invalidate(thought, "expired", now)
                    continue
                if not evidence_matches(self.reader, self.key, thought.source):
                    self.store.invalidate(thought, "invalidated", now)
                    self.affect.correct()
                    continue
                pending.append(thought)
        except PresenceStoreError:
            self.last_error = "store_unavailable"
            self.affect.correct()
            return ()
        # Another owner/connection may have corrected a record during source
        # readback. Recheck the durable revision before returning a candidate.
        final = self.inspect()
        return tuple(thought for thought in pending if self._active() and thought in final)


class AffectState:
    """One context's latest synthetic/host assessment; never persisted."""

    def __init__(self, key: ContextKey, reader: EvidenceReader, *,
                 clock: Callable[[], datetime], enabled: bool = False,
                 is_current: Callable[[], bool] | None = None):
        self._is_current = is_current
        self.key = key
        self.reader = reader
        self.clock = clock
        self.enabled = enabled is True
        self._generation = 0
        self._latest: AffectReading | None = None
        self._ticket: AssessmentTicket | None = None

    def _active(self) -> bool:
        return _current(self.enabled, self._is_current)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled is True
        self.correct()

    def correct(self) -> None:
        """Explicit user correction/new input/reset cancels all captured work."""
        self._generation += 1
        self._ticket = None
        self._latest = None

    def begin(self, source: EvidenceRef) -> AssessmentTicket | None:
        self.correct()
        generation = self._generation
        if (not self._active() or not evidence_matches(self.reader, self.key, source)
                or not self._active() or generation != self._generation):
            return None
        self._ticket = AssessmentTicket(self.key, source, self._generation, self.clock())
        return self._ticket

    def commit(self, ticket: AssessmentTicket | None, sample: object) -> bool:
        if (not isinstance(ticket, AssessmentTicket) or ticket is not self._ticket
                or ticket.generation != self._generation):
            return False
        if (not self._active()
                or not timedelta(0) <= self.clock() - ticket.observed_at < timedelta(seconds=120)
                or not evidence_matches(self.reader, self.key, ticket.source)
                or not self._active() or ticket is not self._ticket
                or ticket.generation != self._generation):
            if ticket is self._ticket:
                self.correct()
            return False
        self._ticket = None
        self._latest = None
        if not isinstance(sample, Mapping) or set(sample) not in (
                {'valence', 'arousal', 'confidence'}, {'valence', 'arousal', 'confidence', 'posture'}):
            return False
        posture = sample.get('posture', '')
        if posture:
            from agent.presence_assessment import POSTURES
            if not isinstance(posture, str) or posture not in POSTURES:
                return False
        elif 'posture' in sample:
            return False
        axes = []
        for name, lo, hi in (("valence", -1, 1), ("arousal", 0, 1), ("confidence", 0, 1)):
            value = sample[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                return False
            value = float(value)
            if not math.isfinite(value):
                return False
            axes.append(value)
        self._latest = AffectReading(axes[0], axes[1], axes[2], ticket.observed_at, ticket.source, posture)
        return True

    @property
    def latest(self) -> AffectReading | None:
        if not self._active():
            self.correct()
            return None
        reading = self._latest
        if reading and (not evidence_matches(self.reader, self.key, reading.source)
                or reading is not self._latest or not self._active()
                or not timedelta(0) <= self.clock() - reading.observed_at < timedelta(seconds=120)):
            self.correct()
        return self._latest

    @property
    def posture(self) -> str:
        return reading_posture(self.latest)
