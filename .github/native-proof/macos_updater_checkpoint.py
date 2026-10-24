"""Read-only identity predicate for the private hosted updater-start checkpoint."""
import json
from pathlib import Path


def live_updater(beacon: Path, install: Path, home: Path, uid: int, process_factory):
    try:
        row = json.loads(beacon.read_text())
        if (type(row.get('updater_pid')) is not int or row['updater_pid'] <= 0
                or row.get('updater_create_time') is None):
            return None
        # The pre-pause beacon intentionally has no rollback repo yet on the
        # no-pull path. Bind the actual kernel argv to the exact private console
        # script and its venv instead; never infer install from a shared base exe.
        if row.get('repo') is not None and Path(row['repo']).resolve() != install.resolve():
            return None
        if Path(row.get('venv_python', '')).parent.resolve() != (install / 'venv/bin').resolve():
            return None
        proc = process_factory(row['updater_pid'])
        argv = proc.cmdline()
        if (proc.create_time() != row['updater_create_time']
                or tuple(proc.uids()) != (uid, uid, uid)
                or proc.environ().get('CLOVER_HOME') != str(home)
                or len(argv) < 4 or argv[1:3] != [str(install / 'venv/bin/clover'), 'update']
                or '--gateway' not in argv):
            return None
        # Bind a second observation; this is not a claimed-env-only checkpoint.
        if proc.create_time() != row['updater_create_time']:
            return None
        return dict(pid=proc.pid, create_time=proc.create_time(), argv=argv, repo=str(install))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ProcessLookupError):
        return None
