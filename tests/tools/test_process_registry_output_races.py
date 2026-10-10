"""Output/completion races in the process registry (C1.4 R13).

Real subprocesses, real reader threads: no mocked Popen.
"""

import sys
import threading
import time

import pytest

import tools.process_registry as pr
from tools.process_registry import ProcessRegistry


@pytest.fixture(autouse=True)
def _no_systemd_scope():
    original = pr._SYSTEMD_SCOPE_AVAILABLE
    pr._SYSTEMD_SCOPE_AVAILABLE = False
    yield
    pr._SYSTEMD_SCOPE_AVAILABLE = original


class _SlowEmitRegistry(ProcessRegistry):
    """A reader that is slow to consume (a busy gateway), so it lags the child."""

    def _emit_output(self, session, chunk):
        time.sleep(0.01)
        super()._emit_output(session, chunk)


_LINES_THEN_END = (
    f"{sys.executable} -c \"import sys; "
    "[sys.stdout.write('line%06d\\n' % i) for i in range(40000)]; "
    "sys.stdout.write('FINAL_LINE\\n')\""
)


def test_reconcile_while_reader_lags_keeps_whole_ordered_output(tmp_path):
    reg = _SlowEmitRegistry()
    session = reg.spawn_local(_LINES_THEN_END, cwd=str(tmp_path))
    session.notify_on_complete = True

    # The child exits while the reader is still far behind; poll() reconciles.
    while session.process.poll() is None:
        time.sleep(0.01)
    deadline = time.monotonic() + 30
    first = None
    while time.monotonic() < deadline:
        res = reg.poll(session.id)
        if res["status"] == "exited":
            first = res
            break
        time.sleep(0.01)
    assert first is not None, "session never reported exited"

    assert session._completion_event.wait(30)
    note = reg.completion_queue.get(timeout=5)
    time.sleep(0.5)  # let a straggling reader append (the bug)
    assert session.output_buffer.endswith("FINAL_LINE\n"), session.output_buffer[-60:]
    assert note["output"].rstrip().endswith("FINAL_LINE"), note["output"][-60:]
    # The rolling buffer keeps a tail, so its first line may be cut; the rest is contiguous.
    nums = [int(x[4:]) for x in session.output_buffer.splitlines()[1:-1]]
    assert nums == list(range(nums[0], nums[0] + len(nums))), "output lost or reordered"
    assert nums[-1] == 39999
    assert reg.completion_queue.empty()


def test_oneshot_linger_returns_only_after_notification_is_queued(tmp_path):
    """`exited` is visible before the completion notice is queued; a one-shot
    parent that exits on `exited` loses the follow-up."""
    reg = ProcessRegistry()
    session = reg.spawn_local(f"{sys.executable} -c \"print('done')\"", cwd=str(tmp_path))
    session.notify_on_complete = True

    # Widen the window between "exited" and "notice queued" with a slow
    # checkpoint write (the step _move_to_finished runs in between).
    real_write = reg._write_checkpoint

    def slow_write():
        time.sleep(0.5)
        real_write()

    reg._write_checkpoint = slow_write
    out = reg.wait_for_pending_completions(timeout=10, poll_interval=0.05)
    assert session.id in out["completed"]
    assert not reg.completion_queue.empty(), "linger returned before the notice was queued"


def test_reconcile_with_reader_mid_chunk_neither_reorders_nor_loses_output(tmp_path):
    """The reader holds the first chunk (read, not yet buffered) when a poll()
    reconcile sees the child exit. The reconcile must not read the pipe behind
    the reader's back and append later bytes before the earlier chunk."""
    gate = threading.Event()
    held = threading.Event()

    class _Gated(ProcessRegistry):
        def _clean_shell_noise(self, text):
            held.set()
            gate.wait(10)
            return text

    reg = _Gated()
    cmd = (
        f"{sys.executable} -c \"import sys; "
        "[sys.stdout.write('line%05d\\n' % i) for i in range(2500)]\""
    )
    session = reg.spawn_local(cmd, cwd=str(tmp_path))
    session.notify_on_complete = True
    assert held.wait(10)
    while session.process.poll() is None:
        time.sleep(0.01)

    threading.Timer(0.3, gate.set).start()
    reg.poll(session.id)
    assert session._completion_event.wait(15)
    time.sleep(0.3)

    lines = [x for x in session.output_buffer.splitlines() if x.startswith("line")]
    assert lines == [f"line{i:05d}" for i in range(2500)]
    note = reg.completion_queue.get(timeout=5)
    assert note["output"].rstrip().endswith("line02499")
    assert reg.completion_queue.empty()


def test_duplicate_finisher_does_not_release_waiters_before_owner_publishes(tmp_path):
    reg = ProcessRegistry()
    session = reg.spawn_local(f"{sys.executable} -c \"import time; time.sleep(30)\"", cwd=str(tmp_path))
    session.notify_on_complete = True

    in_write = threading.Event()
    release = threading.Event()
    real_write = reg._write_checkpoint

    def slow_write():
        if not in_write.is_set():  # only the owner's write is held open
            in_write.set()
            release.wait(10)
        real_write()

    reg._write_checkpoint = slow_write
    with session._lock:
        session.exited = True
        session.exit_code = 0
    owner = threading.Thread(target=reg._move_to_finished, args=(session,))
    owner.start()
    assert in_write.wait(10)
    reg._move_to_finished(session)  # duplicate: reader vs kill/reconcile
    assert not session._completion_event.is_set(), "duplicate released waiters before the notice"
    release.set()
    owner.join(10)
    assert session._completion_event.is_set()
    assert reg.completion_queue.qsize() == 1
    session.process.kill()


def test_killed_pty_session_is_not_extended_by_a_straggling_reader(tmp_path):
    """After a kill the session output is what the kill reported."""
    reg = ProcessRegistry()
    session = reg.spawn_local(
        "printf start; sleep 0.5; printf late", cwd=str(tmp_path), use_pty=True)
    deadline = time.monotonic() + 10
    while "start" not in session.output_buffer and time.monotonic() < deadline:
        time.sleep(0.02)
    snap = reg.kill_process(session.id)
    assert snap["status"] == "killed"
    time.sleep(1.0)
    assert session.output_buffer.endswith(snap["output"][-5:]) or "late" not in session.output_buffer
    assert "late" not in session.output_buffer
