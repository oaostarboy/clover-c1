"""Acceptance checks for a released 1.5.6 -> candidate update with a reply in flight.

Shared by the Linux, macOS and Windows 1.5.6 acceptance drivers. Contract, set by
the maintainers for the one-time transition from a 1.5.6 gateway:

* exactly ONE external tool effect and ONE canonical tool call/result;
* prior canonical rows unchanged (nothing lost, nothing rewritten);
* at most ONE received copy of the final reply text beyond the original, and any
  extra copy must carry the visible "may be a duplicate" recovery marker;
* state.db passes quick_check and is readable by the v1.5.6 code itself.

These are read-only: they never start a turn, touch a service or edit a database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

import inflight_witness as w

MARKER = 'may be a duplicate'


def verify_relaxed(before, home, effect_file, reply_file, evidence=None, irc_log=None):
    """Like inflight_witness.verify, but allows one MARKED duplicate reply text."""
    after = w.rows(home, before['session'])
    old = before['rows']
    if not old or after[:len(old)] != old:
        raise ValueError('lost or changed prior transcript')
    if w.call_count(after, before['call_id']) != 1:
        raise ValueError('canonical tool call was replayed')
    results = [r for r in after if r['role'] == 'tool' and r['tool_call_id'] == before['call_id']]
    if len(results) != 1:
        raise ValueError('canonical tool result was duplicated')
    effects = w.line_count(effect_file, before['effect_token'])
    if effects != 1:
        raise ValueError(f'tool effect count must stay one, got {effects}')
    replies = [r for r in after[len(old):] if r['role'] == 'assistant' and r['content'] == before['reply_token']]
    if len(replies) != 1:
        raise ValueError(f'canonical reply count must be exactly one, got {len(replies)}')
    received = w.line_count(reply_file, before['reply_token'])
    marked = 0
    if irc_log is not None and Path(irc_log).exists():
        marked = sum(MARKER in line for line in Path(irc_log).read_text(errors='replace').splitlines())
    if not 1 <= received <= 2:
        raise ValueError(f'received reply copies must be 1 or 2, got {received}')
    if received == 2 and irc_log is not None and marked < 1:
        raise ValueError('a second reply copy arrived without the visible duplicate marker')
    if marked > 1:
        raise ValueError(f'more than one marked duplicate: {marked}')
    result = {'scope': 'legacy-156-transition', 'session': before['session'],
              'prior_rows': len(old), 'after_rows': len(after), 'tool_effects': effects,
              'received_replies': received, 'marked_duplicate_lines': marked,
              'repeated_ordinary_reply_text': received - 1}
    if evidence is not None:
        Path(evidence, 'legacy156-delivery-counts.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def state_db_check(home):
    """quick_check + readable by the released 1.5.6 code (read-only open)."""
    db = Path(home) / 'state.db'
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        quick = conn.execute('PRAGMA quick_check').fetchone()[0]
        mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
        sessions = conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        messages = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    if quick != 'ok':
        raise ValueError('state.db quick_check: ' + quick)
    return {'quick_check': quick, 'journal_mode': mode, 'sessions': sessions, 'messages': messages}


def readable_by_156(repo, home, python, session, scratch):
    """Open the database with the v1.5.6 SessionDB (as a rollback would)."""
    scratch = Path(scratch)
    src = scratch / 'v156-src'
    src.mkdir(parents=True, exist_ok=True)
    archive = scratch / 'v156.tar'
    subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', '-o', str(archive), 'v1.5.6'], check=True)
    with tarfile.open(archive) as tar:
        tar.extractall(src)
    code = (
        "import json,sys;from clover_state import SessionDB;"
        "import clover_state;assert clover_state.__file__.startswith(sys.argv[1]),clover_state.__file__;"
        "db=SessionDB(read_only=True);"
        "m=db.get_messages(sys.argv[2]);print(json.dumps(dict(messages=len(m),roles=[x['role'] for x in m])))"
    )
    env = dict(os.environ, CLOVER_HOME=str(home), PYTHONPATH=str(src), CLOVER_STATE_DB_GUARD_BYPASS='1')
    out = subprocess.run([str(python), '-c', code, str(src), session], env=env, cwd=str(scratch),
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise ValueError('v1.5.6 could not read state.db: ' + (out.stderr or out.stdout)[-2000:])
    return json.loads(out.stdout.strip().splitlines()[-1])
