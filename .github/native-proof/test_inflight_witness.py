"""Portable verifier contracts, NOT native update/service acceptance.

All message/effect/reply data below are explicit synthetic test inputs. Real
native callers must produce these through their gateway, tool and receiver.
"""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

MODULE = Path(__file__).with_name('inflight_witness.py')


class InflightWitnessTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(), 'missing read-only in-flight witness')
        spec = importlib.util.spec_from_file_location('inflight_witness', MODULE)
        assert spec is not None and spec.loader is not None
        self.w = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.w)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = self.home / 'state.db'
        self.effect = self.home / 'effects.txt'
        self.reply = self.home / 'received.txt'
        self.effect.write_text('tool-effect-unique\n', encoding='utf-8')
        self.reply.write_text('', encoding='utf-8')
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT, tool_calls TEXT)')
            db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?)', [
                (1, 's', 'user', 'synthetic pending task', None, None),
                (2, 's', 'assistant', '', None, json.dumps([{'id': 'tc-1'}])),
                (3, 's', 'tool', 'synthetic effect complete', 'tc-1', None),
            ])

    def before(self):
        return self.w.capture(self.home, 's', 'tc-1', self.effect,
                              'tool-effect-unique', 'reply-unique')

    def finish(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO messages VALUES (4,?,?,?,?,?)',
                       ('s', 'assistant', 'reply-unique', None, None))
        self.reply.write_text('reply-unique\n', encoding='utf-8')

    def verify(self, before):
        return self.w.verify(before, self.home, self.effect, self.reply)

    def test_readonly_roundtrip_accepts_preserved_rows_once(self):
        before_bytes = self.db.read_bytes()
        before = self.before()
        self.assertEqual(self.db.read_bytes(), before_bytes)
        self.finish()
        before_bytes = self.db.read_bytes()
        self.assertEqual(self.verify(before)['scope'], 'inflight-state-and-local-receiver-only')
        self.assertEqual(self.db.read_bytes(), before_bytes)

    def test_lost_prior_tool_result_rejected(self):
        before = self.before()
        self.finish()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('DELETE FROM messages WHERE id=3')
        with self.assertRaisesRegex(ValueError, 'prior transcript'):
            self.verify(before)

    def test_replayed_tool_effect_rejected(self):
        before = self.before()
        self.finish()
        self.effect.write_text('tool-effect-unique\ntool-effect-unique\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'effect count'):
            self.verify(before)

    def test_saved_but_undelivered_reply_rejected(self):
        before = self.before()
        self.finish()
        self.reply.write_text('', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'received reply count'):
            self.verify(before)

    def test_completed_turn_cannot_be_mislabelled_inflight(self):
        self.finish()
        with self.assertRaisesRegex(ValueError, 'in-flight tool tail'):
            self.before()

    def test_wrong_tool_call_rejected(self):
        with self.assertRaisesRegex(ValueError, 'tool call'):
            self.w.capture(self.home, 's', 'wrong', self.effect,
                           'tool-effect-unique', 'reply-unique')

    def test_duplicate_canonical_reply_rejected(self):
        before = self.before()
        self.finish()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO messages VALUES (5,?,?,?,?,?)',
                       ('s', 'assistant', 'reply-unique', None, None))
        with self.assertRaisesRegex(ValueError, 'canonical reply count'):
            self.verify(before)

    def test_missing_database_does_not_create_one(self):
        self.db.unlink()
        with self.assertRaises((ValueError, sqlite3.OperationalError)):
            self.before()
        self.assertFalse(self.db.exists())


if __name__ == '__main__':
    unittest.main()
