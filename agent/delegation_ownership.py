"""Exact accepted-job references for the automatic router.

Scope comes from runtime profile/session/workspace binding, never model text.
A process-local live record AND its durable acceptance must agree. Thus an old
receipt, restart descriptor, foreign job or cancelled continuation cannot stand
in for a running launch owner. No goal similarity matching is used.
"""
from __future__ import annotations

import os
import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import copy
from typing import Any

_continuation_workspace: ContextVar[tuple[str, str] | None] = ContextVar('owned_continuation_workspace', default=None)
_dispatch_admission: ContextVar[Any] = ContextVar('owned_dispatch_admission', default=None)


def _task_payload(tasks: list[dict]) -> list[dict]:
    return [{k: t[k] for k in ('goal', 'context', 'route', 'account') if k in t} for t in tasks]


@dataclass(frozen=True)
class DispatchAdmission:
    parent: Any
    stop_epoch: int
    task_digest: str
    route_bindings: dict
    plan_id: str = ''
    plan_digest: str = ''

    def memory_valid(self) -> bool:
        # Called under async admission's record lock too: no DB/IO here.
        from tools import async_delegation as ad
        return (getattr(self.parent, '_interrupt_requested', False) is not True
                and int(getattr(self.parent, '_orchestration_stop_epoch', 0)) == self.stop_epoch
                and not (self.plan_id and (ad._records.get(self.plan_id) or {}).get('_ownership_revoked')))

    def validate(self, agent: Any, tasks: list[dict], cfg: dict) -> None:
        from agent.delegation_followthrough import stage_digest, plan_is_live
        from agent.delegation_routes import route_catalog
        from tools import async_delegation as ad
        if agent is not self.parent or not self.memory_valid():
            raise ValueError('The admitted dispatch was revoked by Stop; no child was started.')
        if stage_digest({'tasks': _task_payload(tasks)}) != self.task_digest:
            raise ValueError('The admitted task/route payload changed during preparation; no child was started.')
        catalog = route_catalog(cfg)
        if any(catalog.get(name) != binding for name, binding in self.route_bindings.items()):
            raise ValueError('The admitted exact named route changed during preparation; no substitute was started.')
        if self.plan_id:
            plan = ad.get_continuation_plan(self.plan_id)
            if (not isinstance(plan, dict) or not plan_is_live(plan) or stage_digest(plan) != self.plan_digest
                    or self.plan_id not in ad.owned_receipts_ready((self.plan_id,))):
                raise ValueError('The exact claimed continuation was revoked or changed; no child was started.')


@contextmanager
def admitted_dispatch(agent: Any, *, stop_epoch: int, tasks: list[dict], route_bindings: dict,
                      plan: dict | None = None):
    """Runtime-only admission; model arguments cannot manufacture or replace it."""
    from agent.delegation_followthrough import stage_digest
    claimed = {**plan, 'consumed': 1} if plan else None
    admission = DispatchAdmission(agent, stop_epoch, stage_digest({'tasks': _task_payload(tasks)}),
                                  copy.deepcopy(route_bindings), str(plan['delegation_id']) if plan else '',
                                  stage_digest(claimed) if claimed else '')
    token = _dispatch_admission.set(admission)
    try:
        yield admission
    finally:
        _dispatch_admission.reset(token)


def dispatch_admission(agent: Any) -> DispatchAdmission | None:
    admission = _dispatch_admission.get()
    return admission if isinstance(admission, DispatchAdmission) and admission.parent is agent else None


def owned_workspace(agent: Any) -> str | None:
    binding = _continuation_workspace.get()
    if binding and binding[0] == str(getattr(agent, 'session_id', '') or ''):
        return binding[1]
    return None


@contextmanager
def original_workspace(agent: Any, scope: dict | None):
    """Only the runtime's claimed stage uses its original admitted project."""
    scope = scope or {}
    from clover_constants import get_clover_home
    if scope and (scope.get('profile_home') != str(get_clover_home().resolve())
                  or scope.get('session_id') != str(getattr(agent, 'session_id', '') or '')):
        raise ValueError('Original stage workspace belongs to a different profile/session.')
    if scope.get('workspace') and not os.path.isdir(scope['workspace']):
        raise ValueError('Original stage project is no longer available; no replacement project was selected.')
    token = _continuation_workspace.set((str(getattr(agent, 'session_id', '') or ''), scope['workspace'])
                                        if scope.get('workspace') else None)
    try:
        yield
    finally:
        _continuation_workspace.reset(token)


def scope_for(agent: Any, task_id: str = '') -> dict:
    from clover_constants import get_clover_home
    from tools.delegate_tool import _capture_parent_workspace
    from tools.approval import get_current_session_key
    _, cwd = _capture_parent_workspace(agent, task_id)
    return {'profile_home': str(get_clover_home().resolve()),
            'session_id': str(getattr(agent, 'session_id', '') or ''),
            'session_key': get_current_session_key(default='') or '',
            'workspace': os.path.realpath(cwd) if cwd else ''}


def active_owned_jobs(agent: Any, task_id: str = '') -> list[dict]:
    from tools import async_delegation as ad
    from tools.delegate_tool import list_active_subagents
    scope = scope_for(agent, task_id)
    if not scope['session_id'] or not scope['workspace'] or getattr(agent, '_interrupt_requested', False) is True:
        return []
    with ad._records_lock:
        candidates = [dict(r) for r in ad._records.values()
                      if r.get('owner_scope') == scope and r.get('job_epoch')
                      and r.get('status') == 'running' and not r.get('_interrupted_at')
                      and not r.get('_ownership_revoked')]
    found = []
    children = list_active_subagents()
    for record in candidates:
        did = record['delegation_id']
        row = ad.get_durable_delegation(did)
        plan = ad.get_continuation_plan(did)
        if not row or row['state'] != 'running' or (plan and plan.get('cancelled')):
            continue
        found.append({'delegation_id': did, 'job_epoch': record['job_epoch'],
                      'goals': record.get('goals') or [record.get('goal')],
                      'requested_routes': record.get('requested_routes') or [],
                      'model': record.get('model'),
                      'runtime_models': [c['model'] for c in children if c.get('delegation_id') == did and c.get('model')],
                      'state': 'running' if any(c.get('delegation_id') == did for c in children) else 'preparing',
                      'owner_scope': scope})
    return found


def existing_owner(agent: Any, delegation_id: str, task_id: str = '') -> dict | None:
    return next((r for r in active_owned_jobs(agent, task_id)
                 if r['delegation_id'] == delegation_id), None)


def revoke_owned_job(delegation_id: str) -> None:
    """A trusted task-specific Stop revokes this batch, not sibling jobs."""
    from tools import async_delegation as ad
    with ad._records_lock:
        record = ad._records.get(delegation_id)
        if record is not None:
            record['_ownership_revoked'] = True
    ad.revoke_continuation_plan(delegation_id)
    from tools.delegation_inflight import remove_record
    remove_record(delegation_id)


def revoke_current_jobs(agent: Any) -> None:
    """Stop invalidates only this turn/claimed completion's jobs, not siblings.

    Tombstone synchronously before cleanup; a fresh agent object cannot revive
    the live record by clearing its interrupt flag. Restart descriptors are
    removed too. This does not terminate unrelated productive workers.
    """
    from tools import async_delegation as ad, delegation_inflight
    from clover_constants import get_clover_home
    session = str(getattr(agent, 'session_id', '') or '')
    profile = str(get_clover_home().resolve())
    turn = str(getattr(agent, '_current_turn_id', '') or '')
    completions = set(getattr(agent, '_router_completion_ids', ()) or ())
    with ad._records_lock:
        ids = []
        for did, record in ad._records.items():
            owner = record.get('owner_scope') or {}
            if (owner.get('profile_home') == profile and owner.get('session_id') == session
                    and ((turn and record.get('request_turn_id') == turn) or did in completions)):
                record['_ownership_revoked'] = True
                ids.append(did)
    for did in ids:
        ad.revoke_continuation_plan(did)
        delegation_inflight.remove_record(did)


def claim_dispatch(agent: Any, request_id: str, task_id: str = '') -> tuple[str, dict]:
    """Atomically own this exact runtime request. Empty token means no effect.

    A preparing claim does not assert child acceptance. It prevents a competing
    backstop from dispatching beside the original before acceptance settles.
    Retries need a new admitted request, not expiry-based duplicate launches.
    """
    from tools import async_delegation as ad
    scope = json.dumps(scope_for(agent, task_id), sort_keys=True, separators=(',', ':'))
    token = uuid.uuid4().hex
    with ad._DB_LOCK, ad._write_transaction() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS orchestration_dispatch_claims (
            scope TEXT NOT NULL, request_id TEXT NOT NULL, epoch TEXT NOT NULL,
            state TEXT NOT NULL, delegation_id TEXT, created_at REAL NOT NULL,
            PRIMARY KEY (scope, request_id))''')
        # Bounded retention; a request's claim outlives any allowed stage age.
        conn.execute('DELETE FROM orchestration_dispatch_claims WHERE created_at < ?',
                     (time.time() - 2 * 86400,))
        cursor = conn.execute('''INSERT OR IGNORE INTO orchestration_dispatch_claims
            VALUES (?, ?, ?, 'preparing', NULL, ?)''', (scope, request_id, token, time.time()))
        if cursor.rowcount == 1:
            return token, {'scope': scope, 'request_id': request_id}
        row = conn.execute('''SELECT state, delegation_id, epoch FROM orchestration_dispatch_claims
            WHERE scope=? AND request_id=?''', (scope, request_id)).fetchone()
        return '', {'state': row[0], 'delegation_id': row[1], 'job_epoch': row[2]}


def settle_dispatch(claim: dict, token: str, result: dict) -> None:
    from tools import async_delegation as ad
    with ad._DB_LOCK, ad._write_transaction() as conn:
        conn.execute('''UPDATE orchestration_dispatch_claims SET state=?, delegation_id=?
            WHERE scope=? AND request_id=? AND epoch=? AND state='preparing' ''',
            ('accepted' if result.get('ok') else 'not_started', result.get('delegation_id'),
             claim['scope'], claim['request_id'], token))
