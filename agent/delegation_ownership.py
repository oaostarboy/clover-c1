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
from typing import Any


def scope_for(agent: Any, task_id: str = '') -> dict:
    from clover_constants import get_clover_home
    from tools.delegate_tool import _capture_parent_workspace
    from tools.approval import get_current_session_key
    _, cwd = _capture_parent_workspace(agent, task_id)
    return {'profile_home': str(get_clover_home().resolve()),
            'session_id': str(getattr(agent, 'session_id', '') or ''),
            'stop_epoch': str(getattr(agent, '_orchestration_stop_epoch', 0)),
            'session_key': get_current_session_key(default='') or '',
            'workspace': os.path.realpath(cwd) if cwd else ''}


def active_owned_jobs(agent: Any, task_id: str = '') -> list[dict]:
    from tools import async_delegation as ad
    scope = scope_for(agent, task_id)
    if not scope['session_id'] or not scope['workspace'] or getattr(agent, '_interrupt_requested', False) is True:
        return []
    with ad._records_lock:
        candidates = [dict(r) for r in ad._records.values()
                      if r.get('owner_scope') == scope and r.get('job_epoch')
                      and r.get('status') == 'running' and not r.get('_interrupted_at')]
    found = []
    for record in candidates:
        did = record['delegation_id']
        row = ad.get_durable_delegation(did)
        plan = ad.get_continuation_plan(did)
        if not row or row['state'] != 'running' or (plan and plan.get('cancelled')):
            continue
        found.append({'delegation_id': did, 'job_epoch': record['job_epoch'],
                      'goals': record.get('goals') or [record.get('goal')],
                      'requested_routes': record.get('requested_routes') or [],
                      'model': record.get('model'), 'state': 'running',
                      'owner_scope': scope})
    return found


def existing_owner(agent: Any, delegation_id: str, task_id: str = '') -> dict | None:
    return next((r for r in active_owned_jobs(agent, task_id)
                 if r['delegation_id'] == delegation_id), None)


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
