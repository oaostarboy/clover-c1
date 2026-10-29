"""Admit identified legacy gateways without removing runtime identity fences.

This is NOT a quiescence detector. Empty ledgers and zero agents cannot prove an
old process has no pre-ledger or initiating slash reply. A gateway that does not
advertise the carrier-through-ACK drain implementation (every Clover <= 1.5.6
gateway) is restarted after a bounded idle wait; see
``clover_cli.gateway.wait_for_legacy_gateway_idle``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

CAPABILITY = 'reply-carriers-through-ack-v1'


def _macos_framework_wrapper(wrapper, child, argv: list[str], command: list[str], home: Path) -> bool:
    """Bind framework argv aliases to the live launchd job and OUR venv.

    A shared base executable or VIRTUAL_ENV claim is not venv ownership. Use
    launchd's loaded program/arguments (not an editable plist), bound to the
    wrapper PID/UID, in addition to the admitted child's parent/start identity.
    Unknown/unreadable evidence fails closed. Never used on Windows/Linux.
    """
    import os
    import re
    import subprocess
    import sys

    if sys.platform != 'darwin':
        return False
    try:
        interpreter = Path(command[0])
        own_bin = Path(sys.executable).parent.resolve()
        if (not interpreter.is_absolute() or interpreter.parent.resolve() != own_bin
                or own_bin != (Path(sys.prefix) / 'bin').resolve()
                or not (Path(sys.prefix) / 'pyvenv.cfg').is_file()
                or not re.fullmatch(r'python(?:3(?:\.\d+)?)?', interpreter.name)):
            return False
        base = Path(sys._base_executable).resolve()
        if base.parent.name == 'bin':
            base = base.parent.parent / 'Resources/Python.app/Contents/MacOS/Python'
        if ('/Python.framework/' not in str(base)
                or not base.samefile(wrapper.exe()) or not base.samefile(child.exe())
                or argv[0] not in (command[0], str(base))
                or child.cmdline()[0] not in (command[0], str(base))):
            return False
        uid = os.getuid()  # windows-footgun: ok — guarded by the darwin-only return above
        if (wrapper.ppid() != 1 or tuple(wrapper.uids()) != (uid, uid, uid)
                or tuple(child.uids()) != (uid, uid, uid)):
            return False
        wrapper_env = wrapper.environ()
        for env in (wrapper_env, child.environ()):
            if env.get('VIRTUAL_ENV') and Path(env['VIRTUAL_ENV']).resolve() != Path(sys.prefix).resolve():
                return False
            if env.get('CLOVER_HOME') and Path(env['CLOVER_HOME']).resolve() != home.resolve():
                return False
        label = wrapper_env.get('XPC_SERVICE_NAME', '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', label) or label == '0':
            return False
        loaded = subprocess.run(
            ['/bin/launchctl', 'print', f'gui/{uid}/{label}'],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
        if loaded.returncode != 0:
            return False
        # Only top-level fields; nested service/endpoint PIDs are not owners.
        lines = loaded.stdout.splitlines()
        if (lines.count(f'\tpid = {wrapper.pid}') != 1
                or lines.count(f'\tprogram = {command[0]}') != 1
                or lines.count('\targuments = {') != 1):
            return False
        start = lines.index('\targuments = {') + 1
        end = lines.index('\t}', start)
        args = lines[start:end]
        expected = [command[0], *argv[1:]]
        return args == ['\t\t' + value for value in expected]
    except Exception:
        return False


def _proved_process_tree_wrapper(pid: int, admitted: dict[int, tuple[Path, int]]) -> bool:
    """Recognize a legitimate gateway process tree wrapper (such as a venv launcher).

    On Windows (and similar multi-process virtualenv/shim environments), running
    ``clover gateway run`` or ``python.exe -m clover_cli.main gateway run`` through
    a venv is a two-process chain: the venv launcher (e.g. ``venv\\Scripts\\python.exe``)
    spawns the underlying interpreter from uv's managed directory. The inner worker
    writes the PID file and runtime state (and is admitted), leaving the launcher
    parent in ``unmapped``.

    To prove the parent is a legitimate launcher rather than an unknown gateway:
    1. It must be an ancestor of an admitted gateway child in a contiguous
       gateway execution chain (direct parent or launcher ancestor).
    2. It must be live with a stable start time <= child start time.
    3. Its command line must be a valid Clover ``gateway run`` command.
    4. Its command line and environment (if readable) must belong to the same
       profile/CLOVER_HOME as the admitted child.
    5. Truly unknown gateways (unrelated processes, mismatched profiles,
       processes without an admitted child) fail closed.
    """
    import psutil
    from gateway.status import (
        _get_process_start_time,
        looks_like_gateway_command_line,
        _command_line_belongs_to_profile,
    )

    try:
        start = _get_process_start_time(pid)
        if start is None:
            return False
        wrapper = psutil.Process(pid)
        argv = wrapper.cmdline()
        if not argv:
            return False
        cmdline_str = " ".join(str(part) for part in argv)
        if "clover_cli.stderr_timestamp" in cmdline_str:
            return False
        if not looks_like_gateway_command_line(cmdline_str):
            return False

        for child_pid, (home, child_start) in admitted.items():
            if _get_process_start_time(child_pid) != child_start:
                continue
            curr = psutil.Process(child_pid)
            curr_pid = child_pid
            is_valid_ancestor = False
            while True:
                try:
                    parent_pid = curr.ppid()
                except Exception:
                    break
                if parent_pid == curr_pid or parent_pid <= 0:
                    break
                try:
                    parent = psutil.Process(parent_pid)
                    parent_argv = parent.cmdline()
                    parent_cmd = " ".join(str(p) for p in parent_argv or [])
                except Exception:
                    break
                if not looks_like_gateway_command_line(parent_cmd):
                    break
                if not _command_line_belongs_to_profile(parent_cmd, home):
                    break
                p_start = _get_process_start_time(parent_pid)
                if p_start is None or p_start > child_start:
                    break
                try:
                    p_env = parent.environ()
                except Exception:
                    p_env = {}
                if p_env.get("CLOVER_HOME"):
                    try:
                        if Path(p_env["CLOVER_HOME"]).resolve() != home.resolve():
                            break
                    except Exception:
                        break
                if parent_pid == pid:
                    if _get_process_start_time(pid) == start:
                        is_valid_ancestor = True
                    break
                curr = parent
                curr_pid = parent_pid

            if is_valid_ancestor:
                return True
    except Exception:
        return False
    return False


def _proved_stderr_wrapper(pid: int, admitted: dict[int, tuple[Path, int]]) -> bool:
    """Recognize our exact wrapper or launcher around an already admitted direct child.

    A parent relationship alone is not evidence: another gateway, an unreadable
    parent, or a wrapper whose worker lacks capability must still block update.
    """
    import psutil
    from gateway.status import _get_process_start_time
    from clover_cli.stderr_timestamp import _is_clover_gateway_run_argv, _prepare_child_command

    try:
        start = _get_process_start_time(pid)
        if start is None:
            return False
        wrapper = psutil.Process(pid)
        argv = wrapper.cmdline()
        if len(argv) >= 4 and argv[1:4] == ['-m', 'clover_cli.stderr_timestamp', '--error-log']:
            if (len(argv) < 7 or not Path(argv[0]).is_absolute()
                    or argv[5] != '--'):
                return False
            command = argv[6:]
            if not _is_clover_gateway_run_argv(command):
                return False
            for child_pid, (home, child_start) in admitted.items():
                child = psutil.Process(child_pid)
                if child.ppid() != pid:
                    continue
                child_command = child.cmdline()
                expected = command
                if child_command != command:
                    # Historical launchd plists omit the flag which the loaded
                    # wrapper adds. Require its real environment, not ambient env.
                    expected = _prepare_child_command(command, wrapper.environ())
                import sys
                framework = sys.platform == 'darwin' and '/Python.framework/' in argv[0]
                if command[0] != argv[0] or child_command != expected or framework:
                    if (not child_command or child_command[1:] != expected[1:]
                            or not _macos_framework_wrapper(wrapper, child, argv, command, home)):
                        continue
                if (Path(argv[4]).resolve() == (home/'logs/gateway.error.log').resolve()
                        and _get_process_start_time(child_pid) == child_start
                        and child.ppid() == pid
                        and _get_process_start_time(pid) == start):
                    return True
            return False
        return _proved_process_tree_wrapper(pid, admitted)
    except Exception:
        return False
    return False


def _unsafe_gateways() -> list[str]:
    from clover_cli.gateway import find_gateway_pids, find_profile_gateway_processes
    from clover_cli.update_cmd import _gateway_pid_install_verdict
    from gateway.status import _get_process_start_time, runtime_status_pid_is_live

    processes = find_profile_gateway_processes(strict=True)
    by_pid = {p.pid: p for p in processes}
    reasons = []
    admitted = {}
    unmapped = []
    for pid in set(find_gateway_pids(all_profiles=True)) | set(by_pid):
        verdict = _gateway_pid_install_verdict(pid)
        proc = by_pid.get(pid)
        # A mapped profile may use an external venv for THIS source. Only an
        # unmapped, positively different install can be excluded here.
        if proc is None and verdict == 'other':
            continue
        if proc is None:
            unmapped.append(pid)
            continue
        row = {}
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
            )
        except (OSError, ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            reasons.append(f'{proc.profile} (PID {pid}): delivery-aware restart is not established')
        else:
            capability = row.get('update_restart_capability')
            if capability is None:
                reasons.append(f'legacy: {proc.profile} (PID {pid}): identified legacy runtime')
            elif capability != CAPABILITY:
                reasons.append(f'{proc.profile} (PID {pid}): unknown restart contract')
                continue
            admitted[pid] = (proc.path, row['start_time'])
    for pid in unmapped:
        if not (_proved_stderr_wrapper(pid, admitted) or _proved_process_tree_wrapper(pid, admitted)):
            reasons.append(f'PID {pid}: no owner-bound gateway status')
    return reasons


def require_delivery_safe_update() -> dict:
    """Permit missing legacy capability only after positive owner validation.

    Released callers may ignore the return value. Unknown identity still exits
    through SystemExit, which their broad Exception handlers cannot turn into
    a forced restart. Exit 1 avoids old notifications claiming nothing changed.
    """
    try:
        reasons = _unsafe_gateways()
    except Exception:
        defer_unsafe_restart('Gateway discovery could not establish runtime ownership.')
    unsafe = [reason for reason in reasons if not reason.startswith('legacy: ')]
    if unsafe:
        defer_unsafe_restart('; '.join(unsafe))
    status = 'legacy-proceed' if reasons else 'capable'
    record = {'status': status, 'required_capability': CAPABILITY, 'reasons': reasons}
    if not reasons:
        return record
    print(
        '\n\u2139 The running gateway predates restart-safe reply delivery. The update '
        'continues: the gateway is restarted when it is idle (waiting up to a '
        'couple of minutes; if it stays busy the restart remains pending). An '
        'ordinary reply may be delivered once more during the first transition.',
        flush=True,
    )
    for reason in reasons:
        print('  ' + reason, flush=True)
    try:
        from clover_cli import update_receipt
        if update_receipt._current is None:
            update_receipt.begin_update_receipt()
        if update_receipt._current is not None:
            update_receipt._current.data['delivery_restart_guard'] = record
    except Exception:
        pass  # the receipt is evidence, not a gate
    return record



def defer_unsafe_restart(detail: str) -> NoReturn:
    """Stop without signalling, preserving pending intent and newest saved state."""
    print(f'\n✗ Update not completed: {detail}', flush=True)
    print('Source or dependencies may already have changed. Automatic completion remains deferred; '
          'pending restart intent and saved state are retained. Resolve the reported condition '
          'and retry clover update.', flush=True)
    try:
        disarmed = disarm_owned_restart_watcher()
        from clover_cli import update_receipt
        if update_receipt._current is None:
            update_receipt.begin_update_receipt()
        if update_receipt._current is not None:
            update_receipt._current.data['delivery_restart_guard'] = {
                'status': 'deferred', 'reasons': [detail], 'owned_watcher_disarmed': disarmed,
            }
        update_receipt.finalize_update_receipt('failed', stop_reason=detail)
    finally:
        # Frozen callers catch Exception and then force-restart. Always escape
        # through BaseException, even if saving the failure receipt also fails.
        raise SystemExit(1)



def disarm_owned_restart_watcher() -> bool:
    """Stand down THIS updater's detached rollback watcher. Returns True if it was disarmed.

    Used when the updater stops after the pull but before any restart. The
    frozen watcher would otherwise see "updater gone, HEAD moved, gateway still
    old", probe the intentionally still-old gateway, roll the checkout back and
    relaunch it. Only a beacon bound to our own live PID and create time is
    removed; another updater's beacon, the fleet restart marker, the snapshot
    and the database are never touched.
    """
    import os
    from clover_cli import update_restart_watcher as watcher

    beacon = watcher.beacon_path()
    try:
        raw = beacon.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if not (data.get('updater_pid') == os.getpid()
            and data.get('updater_create_time') is not None
            and watcher._updater_identity_alive(data) is True):
        return False
    try:
        if beacon.read_bytes() != raw:
            return False
        beacon.unlink()
    except OSError:
        return False
    return True
