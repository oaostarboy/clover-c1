"""Real parallel SessionDB saves after a sidebar read returns, with counted effects."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

from clover_state import SessionDB
from clover_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable


def test_cron_helpers_and_parent_finish_after_reader_returns_without_replaying_effects(tmp_path):
    path = tmp_path / "state.db"
    owners = ["cron", "helper-1", "helper-2", "parent"]
    databases = [SessionDB(path) for _ in owners]
    observer = None
    cursor = None
    barrier = threading.Barrier(len(owners))
    try:
        for owner, db in zip(owners, databases):
            db.create_session(owner, source="test")
            db._conn.execute("PRAGMA busy_timeout=1")
            db._TRANSCRIPT_WRITE_PATIENCE_S = .2
        seed = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(6)]
        databases[0].append_messages_batch("cron", seed)
        expected_mode = "delete" if is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info) else "wal"
        assert databases[0]._conn.execute("PRAGMA journal_mode").fetchone()[0] == expected_mode
        # This uses the actual runtime's mode. No WAL safety gate is bypassed.
        observer = SessionDB(path, read_only=True)
        with observer._read_ctx() as connection:
            cursor = connection.execute("SELECT * FROM messages ORDER BY id")
            assert cursor.fetchone() is not None
            assert not connection.in_transaction
        # Caller keeps a cursor reference across later work. Its read lease has
        # ended, so it must no longer obstruct any writer or retain a snapshot.
        def work(item):
            owner, db = item
            expected = []
            ledger = tmp_path / (owner + ".effects.jsonl")
            barrier.wait(timeout=10)
            for i in range(5):
                assert not db._conn.in_transaction  # model/tool IO is outside transactions
                with ledger.open("a") as handle:
                    handle.write(json.dumps({"effect": owner + ":" + str(i)}) + "\n")
                messages = [{"role": "user", "content": f"{owner}:request:{i}"},
                            {"role": "assistant", "content": f"{owner}:receipt:{i}"}]
                db.append_messages_batch(owner, messages)
                expected.extend(messages)
                assert not db._conn.in_transaction
            return owner, expected
        with ThreadPoolExecutor(max_workers=len(owners)) as pool:
            expected = dict(pool.map(work, zip(owners, databases)))
        for owner, db in zip(owners, databases):
            rows = db.get_messages_as_conversation(owner)
            prefix = seed if owner == "cron" else []
            assert [(m["role"], m["content"]) for m in rows] == [(m["role"], m["content"]) for m in prefix + expected[owner]]
            effects = [json.loads(line) for line in (tmp_path / (owner + ".effects.jsonl")).read_text().splitlines()]
            assert effects == [{"effect": owner + ":" + str(i)} for i in range(5)]
    finally:
        if cursor is not None:
            cursor.close()
        if observer is not None:
            observer.close()
        for db in databases:
            db.close()
