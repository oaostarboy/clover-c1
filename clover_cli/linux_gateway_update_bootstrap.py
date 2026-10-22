"""Continue a legacy PATH-selected Linux chat update in its requesting runtime.

Old gateways resolve ``clover`` through PATH before their own interpreter. A
freshly pulled managed_uv hook can repair that first update without changing the
old gateway or broadening any restart-ownership predicate. The child uses the
public CLI, all admission gates, and its live ancestor's existing update lock.
"""
from __future__ import annotations

from importlib import metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def _editable_source_matches(live: Path, root: Path) -> bool:
    from clover_cli.update_preflight import editable_dir_from_direct_url

    owned = []
    for site in live.glob('lib/python*/site-packages'):
        if not site.resolve().is_relative_to(live.resolve()):
            return False
        for dist in metadata.distributions(path=[str(site)]):
            name = (dist.metadata.get('Name') or '').lower().replace('_', '-')
            if name == 'clover-c1':
                source = editable_dir_from_direct_url(dist.read_text('direct_url.json') or '')
                owned.append(source is not None and source.samefile(root))
    return owned == [True]


def _requesting_runtime(root: Path, invoking: Path | None) -> Path | None:
    """Join the active home, live PID, inherited venv and installed source proof.

    Ambient VIRTUAL_ENV alone never chooses a target, and a common resolved base
    Python never proves ownership. Any missing/contradictory evidence stands down.
    """
    if not sys.platform.startswith('linux') or invoking is None:
        return None
    if 'update' not in sys.argv[1:] or '--gateway' not in sys.argv[1:]:
        return None
    try:
        from clover_constants import get_clover_home
        from gateway.control_socket import identify_gateway
        from gateway.status import runtime_status_pid_is_live, _get_process_start_time
        import psutil

        home = get_clover_home()
        identity = identify_gateway(home)
        if (not identity or identity.get('kind') != 'clover-gateway'
                or identity.get('start_time') is None
                or Path(identity['clover_home']).resolve() != home.resolve()
                or not runtime_status_pid_is_live(identity)):
            return None
        process = psutil.Process(identity['pid'])
        argv, env = process.cmdline(), process.environ()
        if len(argv) < 5 or argv[1:] != ['-m', 'clover_cli.main', 'gateway', 'run']:
            return None
        python = Path(argv[0])
        if (not python.is_absolute() or python.parent.name != 'bin'
                or not re.fullmatch(r'python(?:3(?:\.\d+)?)?', python.name)):
            return None
        live = python.parent.parent
        if (live.is_symlink() or not (live/'pyvenv.cfg').is_file()
                or not python.is_file() or live.samefile(invoking)):
            return None
        if (env.get('VIRTUAL_ENV') != str(live)
                or os.environ.get('VIRTUAL_ENV') != str(live)
                or Path(env.get('CLOVER_HOME', '')).resolve() != home.resolve()):
            return None
        if not _editable_source_matches(live, root):
            return None
        # Recheck the kernel start identity after filesystem/metadata discovery.
        if (not runtime_status_pid_is_live(identity)
                or _get_process_start_time(identity['pid']) != identity['start_time']):
            return None
        return python
    except Exception:
        # Discovery is read-only and fail-closed, including unavailable optional
        # process inspection on a damaged old environment.
        return None


def continue_requesting_gateway_update(root: Path, invoking: Path | None) -> None:
    python = _requesting_runtime(root, invoking)
    if python is None:
        return
    from clover_cli import update_receipt as receipt
    from clover_constants import get_clover_home

    # The pre-pull receipt belongs to the still-running old updater module.
    # Keep it, including its original SHA/plan, instead of replacing the update
    # with an unrelated "already current" success receipt from the child.
    parent = receipt._current
    if parent is None:
        return
    print('→ Continuing this update in the requesting gateway virtual environment…', flush=True)
    env = dict(os.environ, VIRTUAL_ENV=str(python.parent.parent))
    env['PATH'] = str(python.parent) + os.pathsep + env.get('PATH', '')
    # Deliberately do NOT set CLOVER_UPDATE_REEXEC or skip-preflight: this is a
    # normal public update under UpdateLock's existing live-ancestor contract.
    child = subprocess.Popen(
        [str(python), '-m', 'clover_cli.main', *sys.argv[1:]],
        cwd=root, env=env, stdin=subprocess.DEVNULL,
    )
    code = child.wait()
    result = None
    try:
        candidate = json.loads((get_clover_home()/'logs/update_receipts/latest.json').read_text(encoding='utf-8'))
        if candidate.get('pid') == child.pid:
            result = candidate
    except (OSError, ValueError, TypeError):
        pass
    if result is not None:
        parent.data.setdefault('steps', []).extend(result.get('steps', []))
        parent.data.setdefault('skips', []).extend(result.get('skips', []))
        for key in ('gateway_restart', 'fleet', 'runtime_outcomes', 'post_update'):
            if key in result:
                parent.data[key] = result[key]
    # Exit 0 alone is not verification. Missing/partial/refused child receipts
    # must not be promoted by the legacy parent's command-boundary finalizer.
    if code == 0 and (result is None or result.get('outcome') != 'success'):
        code = 1
    parent.data['linux_gateway_runtime_handoff'] = {
        'parent_pid': os.getpid(), 'child_pid': child.pid,
        'interpreter': str(python), 'exit_code': code,
        'child_outcome': result.get('outcome') if result else 'unverified',
    }
    # Unwind the OLD updater through its existing receipt/lock/output finalizers.
    # It must not resume its stale local-venv dependency/restart phase afterwards.
    raise SystemExit(code)
