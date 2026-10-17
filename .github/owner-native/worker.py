"""Disposable native subprocess; proof values travel only over stdin/PTY."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3] / 'source'
# Hosted helper checkout is sibling of the exact product checkout.
if not (ROOT / 'gateway').is_dir():
    ROOT = Path(__file__).resolve().parents[3].parent / 'source'
sys.path.insert(0, str(ROOT))

if sys.argv[1] == 'console':
    from console_phase_probe import start
    done = start()
    from clover_cli.pairing import telegram_owner_command
    result = telegram_owner_command('owner-confirm')
    done()
    sys.exit(0 if result else 2)

v = json.load(sys.stdin)
from gateway.pairing import VerifiedOwnerStore
s = VerifiedOwnerStore('123456:test-token', home=Path(v['home']))
op = sys.argv[1]
if op == 'confirm':
    print(json.dumps(s.confirm(v['code'])))
elif op == 'owner':
    print(json.dumps(s.owner_user_id()))
elif op == 'revoke':
    s.revoke()
    print(json.dumps(s.owner_user_id()))
elif op == 'prove':
    print(json.dumps(s.prove(v['challenge'], v['uid'])))
elif op == 'lock-denied':
    # Genuine competing-process nonblocking primitive denial, not a shim.
    with s.path.with_suffix('.lock').open('a+b') as f:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            print(json.dumps({'denied': True, 'errno': exc.errno, 'winerror': getattr(exc, 'winerror', None)}))
        else:
            print(json.dumps({'denied': False}))
elif op == 'lock':
    print('READY', flush=True)
    with s._lock():
        print('ACQUIRED', flush=True)
