"""Read-only identity predicate for the private hosted updater-start checkpoint."""
import json
from pathlib import Path


def live_updater(beacon: Path, install: Path, home: Path, uid: int, process_factory):
    try:
        row = json.loads(beacon.read_text(encoding='utf-8'))
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
    except Exception:
        return None


def live_os_updater(install: Path, home: Path, uid: int, not_before: float, processes, process_factory):
    """The no-pull branch need not arm a rollback beacon. Prove its real actor.

    This only releases our own held model response; it authorizes no process
    mutation. Require a unique kernel-observed private console-script actor,
    not a base-Python name or a claimed environment alone.
    """
    matches = []
    script = str(install / 'venv/bin/clover')
    for proc in processes:
        try:
            argv = proc.cmdline()
            if len(argv) < 4 or argv[1:3] != [script, 'update'] or '--gateway' not in argv:
                continue
            created = proc.create_time()
            if (created < not_before or tuple(proc.uids()) != (uid, uid, uid)
                    or proc.environ().get('CLOVER_HOME') != str(home)):
                continue
            matches.append(dict(pid=proc.pid, create_time=created, argv=argv, repo=str(install)))
        except Exception:
            continue
    if len(matches) != 1:
        return None
    identity = matches[0]
    try:
        current = process_factory(identity['pid'])
        if (current.create_time() != identity['create_time'] or current.cmdline() != identity['argv']
                or tuple(current.uids()) != (uid, uid, uid)
                or current.environ().get('CLOVER_HOME') != str(home)):
            return None
    except Exception:
        return None
    return dict(identity, checkpoint_source='kernel-process', beacon_required=False)
