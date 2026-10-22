"""Read-only witness for an already-produced tool result across an update.

This checks real canonical rows, a tool-owned effect ledger and the receiver's
new-replies-only ledger. It neither starts a turn nor restarts a service. A
native driver must reach its real provider/tool barrier first. A passing witness
alone is NOT service, native-update or real-Telegram acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def rows(home, session):
    try:
        path = (Path(home) / 'state.db').resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError('missing canonical database') from exc
    # Do not instantiate SessionDB: observation must not initialize/migrate a
    # database or change its journal mode. Read WAL normally; immutable=1 would
    # incorrectly ignore committed WAL content.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        result = [dict(row) for row in db.execute(
            'SELECT id, role, content, tool_call_id, tool_calls FROM messages '
            'WHERE session_id=? ORDER BY id', (session,))]
    if not result:
        raise ValueError('missing canonical session transcript')
    return result


def line_count(path, value):
    return Path(path).read_text(encoding='utf-8').splitlines().count(value)


def call_count(transcript, call_id):
    count = 0
    for row in transcript:
        if row['role'] != 'assistant' or not row['tool_calls']:
            continue
        calls = json.loads(row['tool_calls'])
        if not isinstance(calls, list):
            raise ValueError('malformed canonical tool calls')
        count += sum(call.get('id') == call_id for call in calls)
    return count


def capture(home, session, call_id, effect_file, effect_token, reply_token):
    if not all(isinstance(v, str) and v and '\n' not in v and '\r' not in v
               for v in [session, call_id, effect_token, reply_token]):
        raise ValueError('expected nonempty single-line identities/tokens')
    transcript = rows(home, session)
    if call_count(transcript, call_id) != 1:
        raise ValueError('expected exactly one canonical tool call')
    if transcript[-1]['role'] != 'tool' or transcript[-1]['tool_call_id'] != call_id:
        raise ValueError('expected already-produced in-flight tool tail')
    if any(row['role'] == 'assistant' and row['content'] == reply_token for row in transcript):
        raise ValueError('reply already exists before update')
    if line_count(effect_file, effect_token) != 1:
        raise ValueError('expected pre-update tool effect count exactly one')
    return {'session': session, 'call_id': call_id, 'effect_token': effect_token,
            'reply_token': reply_token, 'rows': transcript,
            'scope': 'inflight-state-and-local-receiver-only'}


def verify(before, home, effect_file, reply_file):
    after = rows(home, before['session'])
    # IDs and content/protocol fields must survive; new migration metadata
    # columns are intentionally not frozen by this portable witness.
    old = before['rows']
    if not old or after[:len(old)] != old:
        raise ValueError('lost or changed prior transcript')
    if call_count(after, before['call_id']) != 1:
        raise ValueError('canonical tool call was replayed')
    results = [row for row in after if row['role'] == 'tool'
               and row['tool_call_id'] == before['call_id']]
    if len(results) != 1:
        raise ValueError('canonical tool result was duplicated')
    if line_count(effect_file, before['effect_token']) != 1:
        raise ValueError('post-update tool effect count must remain one')
    replies = [row for row in after[len(old):] if row['role'] == 'assistant'
               and row['content'] == before['reply_token']]
    if len(replies) != 1:
        raise ValueError('canonical reply count must be exactly one')
    if line_count(reply_file, before['reply_token']) != 1:
        raise ValueError('received reply count must be exactly one')
    return {'scope': 'inflight-state-and-local-receiver-only',
            'session': before['session'], 'prior_rows': len(old),
            'after_rows': len(after), 'tool_effects': 1, 'received_replies': 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ['before', 'after']:
        p = sub.add_parser(command)
        p.add_argument('--home', required=True)
        p.add_argument('--effect-file', required=True)
        p.add_argument('--output', required=True)
        if command == 'before':
            for name in ['session', 'call-id', 'effect-token', 'reply-token']:
                p.add_argument('--' + name, required=True)
        else:
            p.add_argument('--before', required=True)
            p.add_argument('--reply-file', required=True)
    args = parser.parse_args()
    if args.command == 'before':
        result = capture(args.home, args.session, args.call_id, args.effect_file,
                         args.effect_token, args.reply_token)
    else:
        result = verify(json.loads(Path(args.before).read_text(encoding='utf-8')),
                        args.home, args.effect_file, args.reply_file)
    # Only evidence output is written, never source state or service controls.
    Path(args.output).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
