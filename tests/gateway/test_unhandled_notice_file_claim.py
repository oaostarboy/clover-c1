"""The restart-notice file is safe to share between processes.

A gateway still shutting down, a freshly started gateway and a standalone
gateway on the same profile home can all touch the file.  A row is delivered by
exactly one of them, an append is never erased by someone else's rewrite, a
short write never leaves a half row behind, and an unreadable line is kept for
a person to look at instead of vanishing.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clover_constants import get_clover_home
from gateway import unhandled_on_restart as unhandled
from tests.gateway.test_restart_unhandled_notice import _human

REPO = Path(__file__).resolve().parents[2]

_PRELUDE = """
import asyncio, json, sys, time
from pathlib import Path
from gateway import unhandled_on_restart as u
from tests.gateway.test_restart_unhandled_notice import _gateway
sync_dir = Path(sys.argv[1]); me = sys.argv[2]

def wait_for(name, timeout=30):
    deadline = time.time() + timeout
    while not (sync_dir / name).exists():
        if time.time() > deadline:
            raise SystemExit("timed out waiting for " + name)
        time.sleep(0.02)
"""

# Both consumers read the file before either one claims anything.
_READ_THEN_CLAIM = _PRELUDE + """
real_read = u.read_rows
def synced_read(home):
    rows = real_read(home)
    (sync_dir / ("read-" + me)).touch()
    wait_for("read-a"); wait_for("read-b")
    return rows
u.read_rows = synced_read

# ...and each then tries to be inside the claim's rewrite at the same moment.
# With a real lock the second one is held out, so the first gives up waiting.
real_rewrite = u._rewrite
def meeting_rewrite(path, rows):
    (sync_dir / ("rewrite-" + me)).touch()
    deadline = time.time() + 1.5
    while len(list(sync_dir.glob("rewrite-*"))) < 2 and time.time() < deadline:
        time.sleep(0.02)
    return real_rewrite(path, rows)
u._rewrite = meeting_rewrite

async def main():
    runner, adapter = _gateway()
    await runner._deliver_unhandled_on_restart_notices()
    print(json.dumps({"sent": len(adapter.sent)}))
asyncio.run(main())
"""

# The consumer is paused after reading, right where it rewrites the file.
_PAUSED_CLAIM = _PRELUDE + """
real_rewrite = u._rewrite
def paused_rewrite(path, rows):
    (sync_dir / "paused").touch()
    wait_for("release")
    return real_rewrite(path, rows)
u._rewrite = paused_rewrite

async def main():
    runner, adapter = _gateway()
    await runner._deliver_unhandled_on_restart_notices()
    print(json.dumps({"sent": len(adapter.sent)}))
asyncio.run(main())
"""

_APPEND = """
import sys
from gateway import unhandled_on_restart as u
from pathlib import Path
home = Path(sys.argv[1])
u.append_rows(home, [{"id": "late-row", "platform": "telegram", "chat_id": "999",
                      "preview": "late", "queued_at": 1.0, "attempts": 0}])
"""


def _spawn(script: str, *args: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    # The script goes through a file: the suite's subprocess guard refuses any
    # argv that looks like it starts a real gateway, and inline code would.
    path = Path(env["CLOVER_HOME"]) / f"child-{abs(hash(script)) % 10**8}.py"
    path.write_text(script, encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(path), *args],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=90)
    assert proc.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1]) if out.strip() else {}


def _seed(home: Path, text: str = "hello there") -> dict:
    row = unhandled.row_for(_human(text), "default")
    unhandled.append_rows(home, [row])
    return row


def _ids(home: Path) -> list[str]:
    return [row["id"] for row in unhandled.read_rows(home)]


def test_two_processes_that_read_the_same_row_send_exactly_one_notice(tmp_path):
    home = get_clover_home()
    _seed(home)
    sync = tmp_path / "sync"
    sync.mkdir()

    first = _spawn(_READ_THEN_CLAIM, str(sync), "a")
    second = _spawn(_READ_THEN_CLAIM, str(sync), "b")
    sent = [_finish(first)["sent"], _finish(second)["sent"]]

    assert sum(sent) == 1, f"each process sent {sent}"
    assert _ids(home) == []


def test_a_row_appended_while_another_process_is_claiming_survives(tmp_path):
    home = get_clover_home()
    claimed = _seed(home)
    sync = tmp_path / "sync"
    sync.mkdir()

    claimer = _spawn(_PAUSED_CLAIM, str(sync), "claimer")
    deadline = time.time() + 30
    while not (sync / "paused").exists():
        assert time.time() < deadline, "claimer never reached its rewrite"
        assert claimer.poll() is None, claimer.stderr.read()
        time.sleep(0.02)

    appender = _spawn(_APPEND, str(home))
    time.sleep(0.3)
    (sync / "release").touch()

    assert _finish(claimer)["sent"] == 1
    out, err = appender.communicate(timeout=90)
    assert appender.returncode == 0, err
    remaining = _ids(home)
    assert "late-row" in remaining
    assert claimed["id"] not in remaining


def test_a_short_write_still_leaves_a_readable_row(monkeypatch):
    home = get_clover_home()
    real_write = os.write

    def short_write(fd, data):
        return real_write(fd, data[:7])

    monkeypatch.setattr(unhandled.os, "write", short_write)
    row = unhandled.row_for(_human("a message long enough to be split"), "default")

    assert unhandled.append_rows(home, [row]) == 1
    monkeypatch.undo()

    rows = unhandled.read_rows(home)
    assert [r["id"] for r in rows] == [row["id"]]
    assert rows[0]["preview"] == "a message long enough to be split"


def test_a_partial_trailing_line_is_kept_aside_and_logged_never_dropped(caplog):
    home = get_clover_home()
    good = _seed(home, "survivor")
    path = unhandled.file_path(home)
    with path.open("ab") as handle:
        handle.write(b'{"id": "torn", "platform": "tele')

    with caplog.at_level(logging.WARNING):
        rows = unhandled.read_rows(home)

    assert [r["id"] for r in rows] == [good["id"]]
    assert any("torn" in r.getMessage() or "unreadable" in r.getMessage() for r in caplog.records)
    corrupt = path.with_name(path.name + ".corrupt")
    assert 'torn' in corrupt.read_text()


def test_a_complete_last_line_without_a_newline_is_still_a_row():
    home = get_clover_home()
    row = unhandled.row_for(_human("no newline at the end"), "default")
    path = unhandled.file_path(home)
    path.write_text(json.dumps(row))

    assert [r["id"] for r in unhandled.read_rows(home)] == [row["id"]]


def test_append_after_a_torn_line_is_not_glued_to_it():
    home = get_clover_home()
    path = unhandled.file_path(home)
    path.write_bytes(b'{"id": "torn"')

    fresh = unhandled.row_for(_human("after the crash"), "default")
    unhandled.append_rows(home, [fresh])

    assert [r["id"] for r in unhandled.read_rows(home)] == [fresh["id"]]
