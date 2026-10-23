"""Fail closed before restarting a gateway with an unproved drain contract.

This is a compatibility gate, NOT a quiescence detector. Empty ledgers and zero
agents cannot prove an old process has no pre-ledger or initiating slash reply.
The loaded runner must advertise the carrier-through-ACK drain implementation.
"""
from __future__ import annotations

import json
from pathlib import Path

CAPABILITY = 'reply-carriers-through-ack-v1'


def _unsafe_gateways() -> list[str]:
    from clover_cli.gateway import find_gateway_pids, find_profile_gateway_processes
    from clover_cli.update_cmd import _gateway_pid_install_verdict
    from gateway.status import _get_process_start_time, runtime_status_pid_is_live

    processes = find_profile_gateway_processes(strict=True)
    by_pid = {p.pid: p for p in processes}
    reasons = []
    for pid in set(find_gateway_pids(all_profiles=True)) | set(by_pid):
        verdict = _gateway_pid_install_verdict(pid)
        proc = by_pid.get(pid)
        # A mapped profile may use an external venv for THIS source. Only an
        # unmapped, positively different install can be excluded here.
        if proc is None and verdict == 'other':
            continue
        if proc is None:
            reasons.append(f'PID {pid}: no owner-bound gateway status')
            continue
        try:
            row = json.loads((proc.path/'gateway_state.json').read_text(encoding='utf-8'))
            valid = (
                row.get('pid') == pid
                and row.get('kind') == 'clover-gateway'
                # The diagnostic liveness helper tolerates an unreadable live
                # creation time. Restart admission must positively bind this
                # capability to its owner, including on the second probe.
                and row.get('start_time') is not None
                and row.get('start_time') == _get_process_start_time(pid)
                and Path(row.get('clover_home', '')).resolve() == proc.path.resolve()
                and runtime_status_pid_is_live(row)
                and row.get('update_restart_capability') == CAPABILITY
            )
        except (OSError, ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            reasons.append(f'{proc.profile} (PID {pid}): delivery-aware restart is not established')
    return reasons


def require_delivery_safe_update() -> None:
    """Refuse without stopping anything; old callers must unwind via SystemExit.

    Invoked at modern entry, catch-up, and the long-standing freshly imported
    managed_uv post-pull API. The last hook protects a released updater without
    modifying its loaded code. Do not catch this as an ordinary optional error.
    """
    try:
        reasons = _unsafe_gateways()
    except Exception:
        reasons = ['gateway discovery could not establish delivery-aware restart']
    if not reasons:
        return
    detail = (
        'Automatic update deferred: the running gateway cannot prove it waits '
        'for complete reply delivery. No gateway was stopped or restarted. '
        'If source was already pulled, it is staged only; this is NOT a completed upgrade. '
        'Do not retry /update or force-stop it. Arrange one-time terminal maintenance: '
        'pause all inbound and internal producers, let accepted work and transport '
        'delivery finish, and verify their durable acknowledgements. An empty agent '
        'count or ledger alone is insufficient. Then run clover gateway stop; '
        'clover update --yes; always run clover gateway start even if update fails. '
        'Keep unresolved deliveries and the pre-update snapshot for recovery. '
        'See docs/user-guide/updating.md#legacy-gateway-maintenance.'
    )
    print('\n✗ ' + detail, flush=True)
    for reason in reasons:
        print('  ' + reason, flush=True)
    from clover_cli import update_receipt
    if update_receipt._current is None:
        update_receipt.begin_update_receipt()
    if update_receipt._current is not None:
        update_receipt._current.data['delivery_restart_guard'] = {
            'status': 'refused', 'required_capability': CAPABILITY,
            'reasons': reasons, 'maintenance_required': True,
        }
    # An old detached watcher otherwise probes the intentionally still-old
    # gateway and may roll back/relaunch after this refusal. Retire only OUR
    # live updater's beacon before unwinding; retain the fleet restart marker
    # and rollback snapshot. A missing beacon is the legacy watcher's supported
    # stand-down signal, not a delivery acknowledgement.
    from clover_cli import update_restart_watcher as watcher
    import os
    beacon = watcher.beacon_path()
    try:
        raw = beacon.read_bytes()
    except FileNotFoundError:
        raw = None
    if raw is not None:
        data = json.loads(raw)
        if (data.get('updater_pid') == os.getpid()
                and data.get('updater_create_time') is not None
                and watcher._updater_identity_alive(data) is True):
            if beacon.read_bytes() != raw:
                raise RuntimeError('Update watcher ownership changed during refusal')
            beacon.unlink()
            if update_receipt._current is not None:
                update_receipt._current.data['delivery_restart_guard']['owned_watcher_disarmed'] = True
    update_receipt.finalize_update_receipt('refused', stop_reason=detail)
    raise SystemExit(2)
