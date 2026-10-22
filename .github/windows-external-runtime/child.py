"""Test-only child: real installed CLI, state, and explicitly marked fault seams.

No platform mocks, fabricated version probes, holder bypass, or WAL override.
Keep outside the product checkout on the runner.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')


def state_write(label):
    from clover_state import SessionDB
    from clover_constants import get_clover_home
    from tools.todo_tool import TodoStore
    db = SessionDB()
    try:
        sid = 'windows-external-runtime'
        if db.get_session(sid) is None:
            db.create_session(sid, 'cli')
        todo = TodoStore()
        conn = sqlite3.connect(db.db_path)
        try:
            previous = conn.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='tool' AND tool_name='todo' ORDER BY id DESC LIMIT 1",
                (sid,),
            ).fetchone()
        finally:
            conn.close()
        if previous is not None:
            snap = json.loads(previous[0])
            todo.restore(snap['todos'], revision=snap['revision'])
        db.append_message(sid, 'user', label)
        todo.write([{'id': label, 'content': label, 'status': 'pending'}], merge=True)
        db.append_message(sid, 'assistant', tool_calls=[{
            'id': 'todo-' + label, 'type': 'function',
            'function': {'name': 'todo', 'arguments': '{}'},
        }])
        db.append_message(sid, 'tool', content=json.dumps(todo.snapshot()),
                          tool_name='todo', tool_call_id='todo-' + label)
    finally:
        db.close()
    return str(get_clover_home() / 'state.db')


def state_read():
    from clover_constants import get_clover_home
    from tools.todo_tool import TodoStore
    path = get_clover_home() / 'state.db'
    conn = sqlite3.connect(path)
    try:
        integrity = conn.execute('PRAGMA integrity_check').fetchall()
        journal = conn.execute('PRAGMA journal_mode').fetchone()[0]
        # Observe only this disposable fixture's exact synthetic rows.
        rows = conn.execute('SELECT role,content FROM messages WHERE session_id=? ORDER BY id',
                            ('windows-external-runtime',)).fetchall()
    finally:
        conn.close()
    # Hydrate the latest complete tool snapshot, not a union of historical
    # pending statuses that could hide a later task deletion or completion.
    snapshots = [json.loads(content) for role, content in rows if role == 'tool' and content]
    todo = TodoStore()
    if snapshots:
        snap = snapshots[-1]
        todo.restore(snap['todos'], revision=snap['revision'])
    pending = [x['id'] for x in todo.read() if x['status'] == 'pending']
    return {'integrity': integrity, 'journal_mode': journal,
            'user_labels': [c for role, c in rows if role == 'user'], 'pending': pending}


def identity():
    from clover_cli.managed_uv import _default_live_venv, _PROJECT_ROOT
    from clover_cli import update_cmd
    # The genuine old update_cmd has no selector helper; record absence, not a
    # fabricated selection. The post-upgrade verifier still requires its path.
    updater_selector = getattr(update_cmd, '_update_venv_dir', None)
    from clover_cli.gateway import get_python_path
    from clover_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable
    conn = sqlite3.connect(':memory:')
    try:
        source_id = conn.execute('select sqlite_source_id()').fetchone()[0]
    finally:
        conn.close()
    return dict(windows=platform.platform(), os_name=os.name,
                executable=sys.executable, prefix=sys.prefix, base_prefix=sys.base_prefix,
                python=platform.python_version(), sqlite=sqlite3.sqlite_version,
                sqlite_source_id=source_id,
                sqlite_unsafe=is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info),
                source=str(_PROJECT_ROOT), selected_runtime=str(_default_live_venv(_PROJECT_ROOT)),
                selected_updater_runtime=(str(updater_selector()) if updater_selector is not None else None),
                selected_gateway_python=get_python_path())


def observe_installer_error(runtime, output):
    """Transparent test-only observer: retain the real installer's failure."""
    import subprocess
    from agent.redact import redact_sensitive_text
    original = runtime._install_uv_windows
    def observed(env):
        try:
            return original(env)
        except subprocess.CalledProcessError as exc:
            def text(value):
                if isinstance(value, bytes):
                    value = value.decode('utf-8', errors='replace')
                return redact_sensitive_text(value or '', force=True, file_read=True, redact_url_credentials=True)
            try:
                save(output, {'returncode': exc.returncode,
                              'stdout': text(exc.stdout), 'stderr': text(exc.stderr)})
            except Exception:
                # Diagnostic I/O/redaction failure must not replace the real
                # installer failure or turn a refused update into success.
                pass
            raise
    runtime._install_uv_windows = observed


def update(fault, output):
    # Import the real CLI under its canonical module name. All lifecycle guards
    # remain active. Faults execute only in explicitly named negative rows.
    from clover_cli import main
    from clover_cli import managed_uv as runtime
    observe_installer_error(runtime, Path(output).with_name('uv-installer-error.json'))
    if fault == 'provision':
        def fail(*args, **kwargs):
            save(output, {'reached': 'provision', 'identity': identity()})
            raise OSError('EXPLICIT TEST FAULT: provision unavailable')
        runtime._install_safe_python_generation = fail
    elif fault == 'post-smoke':
        original = runtime._smoke_candidate_venv
        live = Path(sys.prefix)
        def fail_after_real_smoke(path):
            result = original(path)
            if Path(path) == live:
                # A real new transcript + pending task lands AFTER promotion,
                # BEFORE the product's synchronous rollback. Never restore DB.
                state_write('new-state-before-rollback')
                save(output, {'reached': 'post-smoke', 'real_smoke_ok': result[0],
                              'identity': identity(), 'state': state_read()})
                return False, 'EXPLICIT TEST FAULT: post-cutover rejection', result[2]
            return result
        runtime._smoke_candidate_venv = fail_after_real_smoke
    sys.argv = ['clover', 'update', '--yes']
    main.main()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=['identity', 'seed', 'read', 'hold', 'update'])
    p.add_argument('--output', required=True)
    p.add_argument('--label', default='before-update')
    p.add_argument('--fault', choices=['none', 'provision', 'post-smoke'], default='none')
    a = p.parse_args()
    if a.mode == 'identity':
        save(a.output, identity())
    elif a.mode == 'seed':
        state_write(a.label)
        save(a.output, state_read())
    elif a.mode == 'read':
        save(a.output, state_read())
    elif a.mode == 'hold':
        import clover_state
        save(a.output, {'pid': os.getpid(), 'identity': identity()})
        sys.stdin.buffer.read(1)
    else:
        update(a.fault, a.output)


if __name__ == '__main__':
    main()
