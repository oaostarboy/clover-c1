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
    from clover_cli.pairing import telegram_owner_command
    sys.exit(0 if telegram_owner_command('owner-confirm') else 2)

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
elif op == 'lock':
    print('READY', flush=True)
    with s._lock():
        print('ACQUIRED', flush=True)
