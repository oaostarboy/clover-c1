"""Revocable main-selected owner handoff, not another credential resolver.

Only weak owner references and opaque digests survive the worker. No secret or
copied Context is retained here. A callback reconstructs its exact live owner in
a task-local scope and always resets it, including cancellation/failure.
"""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field, replace
import time
import weakref
from typing import Any

from agent.bounded_auth import _fingerprint, _held, _pool_disk_state, _reconcile_persisted_owner

_FIELDS = ('provider', 'requested_provider', 'model', 'base_url', 'api_key', 'api_mode', 'auth_mode')
_ACTIVE: ContextVar[Any] = ContextVar('presence_selected_callback_owner', default=None)


def _agent_runtime(agent):
    from agent.auxiliary_client import _normalize_main_runtime
    return _normalize_main_runtime({name: getattr(agent, name, '') for name in _FIELDS})


def _identity(runtime):
    return _fingerprint(tuple(runtime.get(name, '') for name in _FIELDS))


def _source_state(pool, selected):
    return getattr(pool, '_credential_source', None), _pool_disk_state(pool.provider, selected.id)


@dataclass(frozen=True)
class SelectedOwner:
    agent: Any = field(repr=False)
    pool: Any = field(repr=False)
    selected: Any = field(repr=False)
    runtime_digest: bytes = field(repr=False)
    selected_digest: bytes = field(repr=False)
    source_digest: bytes = field(repr=False)
    profile: str
    expires: float | None = None

    def bounded(self):
        # Ordinary requested replies may take arbitrarily long. The optional
        # callback starts its short lease only at the actual delivery handoff.
        return replace(self, expires=min(self.expires if self.expires is not None else float('inf'),
                                         time.monotonic() + 30))

    def runtime(self):
        from agent.workstreams import current_profile
        agent = self.agent()
        if (agent is None or current_profile() != self.profile or self.expires is None
                or time.monotonic() >= self.expires):
            _held('selected_authority_revoked')
        runtime = _agent_runtime(agent)
        if _identity(runtime) != self.runtime_digest:
            _held('credential_authority_changed')
        pool = self.pool() if self.pool is not None else None
        if self.pool is not None and pool is None:
            # A collected pooled owner is revoked, not a poolless token route.
            _held('selected_authority_revoked')
        if getattr(agent, '_credential_pool', None) is not pool:
            _held('credential_authority_changed')
        if pool is not None:
            selected = self.selected()
            if (selected is None or pool.current() is not selected
                    or selected.provider != pool.provider
                    or _fingerprint(selected.to_dict()) != self.selected_digest):
                _held('credential_authority_changed')
            source = _source_state(pool, selected)
            if _fingerprint(source) != self.source_digest:
                _held('credential_authority_changed')
            # Reuse the bounded owner's actual persisted-schema/expiry checks,
            # including time-based expiry after inference and before publishing.
            rows = source[1][1]
            _reconcile_persisted_owner(pool, selected, rows[0] if rows else selected.to_dict())
            runtime['credential_pool'] = pool
        # Synchronous owner/source readback cannot be preempted. Do not admit
        # a physical dispatch if that read crossed this capability's lease.
        if time.monotonic() >= self.expires:
            _held('selected_authority_revoked')
        return runtime


def capture(agent):
    """Capture only this worker's already-selected connection; never select."""
    from agent import auxiliary_client as aux
    from agent.workstreams import current_profile
    runtime = aux._RUNTIME_MAIN_CONTEXT.get()
    if not isinstance(runtime, dict) or not runtime.get('provider'):
        return None
    normalized = aux._normalize_main_runtime(runtime)
    if _identity(normalized) != _identity(_agent_runtime(agent)):
        _held('credential_authority_changed')
    pool = runtime.get('credential_pool')
    if pool is not getattr(agent, '_credential_pool', None):
        _held('credential_authority_changed')
    selected = pool.current() if pool is not None else None
    if pool is not None and selected is None:
        _held('selected_authority_missing')
    return SelectedOwner(weakref.ref(agent), weakref.ref(pool) if pool is not None else None,
        weakref.ref(selected) if selected is not None else None, _identity(normalized),
        _fingerprint(selected.to_dict()) if selected is not None else b'',
        _fingerprint(_source_state(pool, selected)) if selected is not None and pool is not None else b'',
        current_profile())


def validate_active():
    active = _ACTIVE.get()
    if active is not None:
        owner, current, deadline = active
        from agent.bounded_completion import remaining_budget
        if deadline is not None:
            remaining_budget(deadline)
        if not current():
            _held('selected_authority_revoked')
        if owner is not None:
            owner.runtime()
        if deadline is not None:
            remaining_budget(deadline)


def active_deadline():
    active = _ACTIVE.get()
    return active[2] if active is not None else None


def callback_context():
    """Keep existing profile/transport context, not main credential material."""
    from agent.auxiliary_client import _RUNTIME_MAIN_CONTEXT
    context = copy_context()
    context.run(_RUNTIME_MAIN_CONTEXT.set, None)
    context.run(_ACTIVE.set, None)
    return context


@contextmanager
def scoped(owner, current, *, deadline=None):
    from agent.auxiliary_client import scoped_runtime_main
    # An empty explicit dict prevents ambient/compatibility state from being
    # consulted. Configured task credentials and legitimate keyless routes
    # remain available to the normal bounded resolver without a main owner.
    runtime = owner.runtime() if owner is not None else {}
    token = _ACTIVE.set((owner, current, deadline))
    try:
        with scoped_runtime_main(runtime):
            validate_active()
            yield
    finally:
        _ACTIVE.reset(token)
