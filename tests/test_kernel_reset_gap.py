import json
import threading
from unittest.mock import patch

from tools import code_kernel
from tools.approval import reset_current_session_key, set_current_session_key
from tools.code_execution_tool import execute_code

OWNER = "luna-reset-during-reservation"


def test_reset_while_same_owner_cell_is_paused_keeps_all_children_tracked(monkeypatch, tmp_path):
    code_kernel.shutdown_all_kernels()
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    config = {"mode": "strict", "kernel_mode": "session", "timeout": 10,
              "kernel_idle_timeout": 1800, "max_session_kernels": 4}
    with patch("tools.code_execution_tool._load_config", return_value=config):
        def run_owner(code, task_id, **kwargs):
            token = set_current_session_key(OWNER)
            try:
                return json.loads(execute_code(code, task_id=task_id, **kwargs))
            finally:
                reset_current_session_key(token)

        assert run_owner("held = 42", "reset-seed")["status"] == "success"
        key = next(iter(code_kernel._KERNELS))
        old = code_kernel._KERNELS[key]
        old_pid = old.proc.pid
        entered, release = threading.Event(), threading.Event()
        results, errors = {}, []
        original = code_kernel.CellAuthority
        caller = None

        def paused(task_id):
            assert code_kernel._resolve_owner(task_id) == OWNER
            authority = original(task_id)
            if threading.get_ident() == caller:
                assert code_kernel._KERNELS.get(key) is old
                entered.set()
                assert release.wait(5)
            return authority

        def stale_cell():
            nonlocal caller
            caller = threading.get_ident()
            try:
                results["stale"] = run_owner("print(held)", "reset-stale")
            except BaseException as exc:
                errors.append(repr(exc))

        worker = threading.Thread(target=stale_cell, name="test-owned-reset-gap")
        new_kernel = None
        new_pid = None
        try:
            with patch.object(code_kernel, "CellAuthority", side_effect=paused):
                worker.start()
                assert entered.wait(5)
                if hasattr(old, "pending_cells"):
                    assert old.pending_cells == 1
                assert old.proc.pid == old_pid
                results["reset"] = run_owner("fresh = 99; print('reset-ok')", "reset-call", reset=True)
                assert results["reset"]["status"] == "success"
                new_kernel = code_kernel._KERNELS[key]
                assert new_kernel is not old
                new_pid = new_kernel.proc.pid
                release.set()
                worker.join(8)
            assert not worker.is_alive()
            assert code_kernel._KERNELS.get(key) is new_kernel
            assert new_kernel.proc.pid == new_pid and new_kernel.proc.poll() is None
            assert results["stale"]["status"] != "success"
            assert old.proc.poll() is not None
            assert errors == []
        finally:
            release.set()
            worker.join(8)
            if new_kernel is not None and new_kernel.proc is not None and new_kernel.proc.poll() is None:
                if code_kernel._KERNELS.get(key) is not new_kernel:
                    code_kernel._teardown(new_kernel)
            code_kernel.shutdown_all_kernels()
            if new_kernel is not None and new_kernel.proc is not None:
                new_kernel.proc.wait(timeout=5)
                assert new_kernel.proc.poll() is not None
            old.proc.wait(timeout=5)
            assert old.proc.poll() is not None
