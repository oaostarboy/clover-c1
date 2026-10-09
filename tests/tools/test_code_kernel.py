#!/usr/bin/env python3
"""Tests for execute_code's session kernel mode.

``code_execution.kernel_mode: session`` keeps one Python child alive per
(task, mode, interpreter, cwd, tool-set) so state survives across calls.
These tests pin the contract:

  - default stays per-call (no state carries over unless opted in)
  - state persists across cells and reset=true discards it
  - a raised exception keeps the kernel (and its state) alive
  - a timeout kills the kernel; the next call gets a fresh one
  - fd-level output from user-spawned subprocesses reaches the result
  - sys.exit() inside a cell ends the kernel deliberately

Mode is sourced from ``code_execution.kernel_mode`` in config.yaml only;
tests patch ``_load_config`` directly, mirroring test_code_execution_modes.
"""

import json
import os
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import pytest

os.environ["TERMINAL_ENV"] = "local"


@pytest.fixture(autouse=True)
def _force_local_terminal(monkeypatch):
    """Mirror test_code_execution.py — guarantee local backend under xdist."""
    monkeypatch.setenv("TERMINAL_ENV", "local")


from tools.code_execution_tool import (
    DEFAULT_KERNEL_MODE,
    KERNEL_MODES,
    _get_kernel_mode,
    build_execute_code_schema,
    execute_code,
)
from tools.code_kernel import _KERNELS, shutdown_all_kernels


@contextmanager
def _kernel_config(**overrides):
    """Pin code_execution config; strict mode keeps the test hermetic."""
    config = {"mode": "strict", "kernel_mode": "session", "timeout": 30}
    config.update(overrides)
    with patch("tools.code_execution_tool._load_config", return_value=config):
        yield


@pytest.fixture(autouse=True)
def _fresh_kernel_registry():
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


def _run(code, **kwargs):
    return json.loads(execute_code(code, task_id="kernel-test", **kwargs))


class TestKernelModeResolution(unittest.TestCase):
    """kernel_mode is retired: session kernels are always on for local runs.

    _get_kernel_mode() survives only as a compat shim; a leftover
    kernel_mode key in user config (any value) must be ignored."""

    def test_session_is_always_on(self):
        self.assertEqual(DEFAULT_KERNEL_MODE, "session")
        with patch("tools.code_execution_tool._load_config", return_value={}):
            self.assertEqual(_get_kernel_mode(), "session")

    def test_leftover_config_key_is_ignored(self):
        for stale in ("per-call", "forever", "", None):
            with patch("tools.code_execution_tool._load_config",
                       return_value={"kernel_mode": stale}):
                self.assertEqual(_get_kernel_mode(), "session")


class TestSessionStatePersistence(unittest.TestCase):
    def test_state_persists_across_cells(self):
        with _kernel_config():
            first = _run("x = 41")
            self.assertEqual(first["status"], "success", first)
            self.assertEqual(first["kernel"]["reused"], False)
            second = _run("print(x + 1)")
        self.assertEqual(second["status"], "success", second)
        self.assertIn("42", second["output"])
        self.assertEqual(second["kernel"]["reused"], True)
        self.assertEqual(second["kernel"]["execution_count"], 2)

    def test_reset_discards_state(self):
        with _kernel_config():
            _run("x = 41")
            second = _run("print(x + 1)", reset=True)
        self.assertEqual(second["status"], "error", second)
        self.assertIn("NameError", second.get("error", ""))
        self.assertEqual(second["kernel"]["state_reset"], True)

    def test_exception_keeps_the_kernel_alive(self):
        with _kernel_config():
            _run("a = 7")
            boom = _run("1 / 0")
            self.assertEqual(boom["status"], "error")
            self.assertIn("ZeroDivisionError", boom["error"])
            after = _run("print(a)")
        self.assertEqual(after["status"], "success", after)
        self.assertIn("7", after["output"])
        self.assertEqual(after["kernel"]["reused"], True)

    def test_imports_persist(self):
        with _kernel_config():
            _run("import json as _j")
            second = _run("print(_j.dumps({'k': 1}))")
        self.assertIn('{"k": 1}', second["output"])


class TestKernelLifecycle(unittest.TestCase):
    def test_timeout_kills_the_kernel_and_reports_state_loss(self):
        with _kernel_config(timeout=1):
            slow = _run("import time\ntime.sleep(30)")
            self.assertEqual(slow["status"], "timeout", slow)
            self.assertIn("state was lost", slow["error"])
        self.assertEqual(len(_KERNELS), 0)
        with _kernel_config():
            fresh = _run("print('alive')")
        self.assertEqual(fresh["status"], "success", fresh)
        self.assertEqual(fresh["kernel"]["reused"], False)
        self.assertIn("alive", fresh["output"])

    def test_idle_kernel_is_reaped_without_a_followup_execution(self):
        """The idle timeout must reclaim the real child while its owner is quiet."""
        import tempfile
        import threading
        import time

        from tools.approval import reset_current_session_key, set_current_session_key
        from tools.code_kernel import reap_idle_kernels

        old_home = os.environ.get("CLOVER_HOME")
        test_home = tempfile.TemporaryDirectory(prefix="clover-kernel-home-")
        os.environ["CLOVER_HOME"] = test_home.name
        token = set_current_session_key("idle-reap-repro")
        stop_reaper = threading.Event()
        try:
            with _kernel_config(kernel_idle_timeout=1):
                result = _run("print('idle-reap-ready')")
                self.assertEqual(result["status"], "success", result)
                kernel = next(iter(_KERNELS.values()))
                child = kernel.proc
                self.assertIsNotNone(child)
                self.assertIsNone(child.poll())
                pid = child.pid
                rss_kb = None
                try:
                    with open(f"/proc/{pid}/status", encoding="utf-8") as status_file:
                        for line in status_file:
                            if line.startswith("VmRSS:"):
                                rss_kb = int(line.split()[1])
                                break
                except OSError:
                    pass

                def run_reaper():
                    while not stop_reaper.wait(0.05):
                        reap_idle_kernels()

                # Exercise the production reaper on a periodic housekeeping cadence.
                reaper = threading.Thread(target=run_reaper, daemon=True)
                reaper.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and child.poll() is None:
                    time.sleep(0.05)
                stop_reaper.set()
                reaper.join(timeout=1)
                self.assertIsNotNone(
                    child.poll(),
                    "idle kernel runner survived past kernel_idle_timeout without a new execution",
                )
                self.assertNotIn(kernel.key, _KERNELS)
                self.assertFalse(os.path.exists(f"/proc/{pid}"))
                self.assertGreater(rss_kb or 0, 0)

                fresh = _run("print('fresh-after-idle-reap')")
                self.assertEqual(fresh["status"], "success", fresh)
                self.assertFalse(fresh["kernel"]["reused"])
                self.assertTrue(fresh["kernel"]["state_lost"])
                self.assertIn("in-memory state was lost", fresh["kernel"]["note"])
                self.assertIn("fresh-after-idle-reap", fresh["output"])
        finally:
            stop_reaper.set()
            reset_current_session_key(token)
            if old_home is None:
                os.environ.pop("CLOVER_HOME", None)
            else:
                os.environ["CLOVER_HOME"] = old_home
            test_home.cleanup()

    def test_long_running_cell_is_not_reaped_as_idle(self):
        import threading
        import time

        from tools.approval import reset_current_session_key, set_current_session_key
        from tools.code_kernel import reap_idle_kernels

        started = threading.Event()
        finished = threading.Event()
        result = {}

        def run_cell():
            token = set_current_session_key("idle-reap-busy")
            try:
                with _kernel_config(kernel_idle_timeout=1, timeout=5):
                    started.set()
                    result.update(_run("import time\ntime.sleep(2)"))
            finally:
                reset_current_session_key(token)
                finished.set()

        worker = threading.Thread(target=run_cell)
        worker.start()
        self.assertTrue(started.wait(timeout=1))
        deadline = time.monotonic() + 3
        kernel = None
        while time.monotonic() < deadline:
            candidate = next(iter(_KERNELS.values()), None)
            if candidate is not None and candidate.proc is not None:
                kernel = candidate
                break
            time.sleep(0.02)
        self.assertIsNotNone(kernel)
        child = kernel.proc
        time.sleep(1.2)
        self.assertEqual(reap_idle_kernels(), 0)
        self.assertIsNotNone(child)
        self.assertIsNone(child.poll())
        self.assertTrue(finished.wait(timeout=3))
        worker.join(timeout=1)
        self.assertEqual(result.get("status"), "success", result)

    def test_reaper_does_not_touch_runner_from_separate_clover_home(self):
        import signal
        import subprocess
        import tempfile
        import time

        from tools.approval import reset_current_session_key, set_current_session_key
        from tools.code_kernel import reap_idle_kernels

        old_home = os.environ.get("CLOVER_HOME")
        local_home = tempfile.TemporaryDirectory(prefix="clover-kernel-local-")
        other_home = tempfile.TemporaryDirectory(prefix="clover-kernel-other-")
        os.environ["CLOVER_HOME"] = local_home.name
        token = set_current_session_key("home-local")
        other_env = os.environ.copy()
        other_env["CLOVER_HOME"] = other_home.name
        other_env["PYTHONPATH"] = os.getcwd()
        other_script = (
            "import json, time\n"
            "from unittest.mock import patch\n"
            "from tools.code_execution_tool import execute_code\n"
            "from tools.code_kernel import _KERNELS, shutdown_all_kernels\n"
            "with patch('tools.code_execution_tool._load_config', "
            "return_value={'mode':'strict','timeout':20,'kernel_idle_timeout':1800}):\n"
            "    execute_code(\"print('other-home-ready')\", task_id='other-home')\n"
            "print(json.dumps({'pid': next(iter(_KERNELS.values())).proc.pid}), flush=True)\n"
            "time.sleep(60)\n"
        )
        other_host = subprocess.Popen(
            [sys.executable, "-c", other_script],
            env=other_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with _kernel_config(kernel_idle_timeout=1):
                local_result = _run("print('local-home-ready')")
                self.assertEqual(local_result["status"], "success", local_result)
                local_kernel = next(iter(_KERNELS.values()))
                local_pid = local_kernel.proc.pid
                other_line = other_host.stdout.readline()
                self.assertTrue(other_line, "other-home child did not report runner PID")
                other_pid = json.loads(other_line)["pid"]
                time.sleep(1.2)
                self.assertEqual(reap_idle_kernels(), 1)
                self.assertFalse(os.path.exists(f"/proc/{local_pid}"))
                self.assertTrue(os.path.exists(f"/proc/{other_pid}"))
        finally:
            other_host.send_signal(signal.SIGINT)
            try:
                other_host.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                other_host.kill()
                other_host.communicate()
            reset_current_session_key(token)
            if old_home is None:
                os.environ.pop("CLOVER_HOME", None)
            else:
                os.environ["CLOVER_HOME"] = old_home
            local_home.cleanup()
            other_home.cleanup()

    def test_sys_exit_ends_the_kernel(self):
        with _kernel_config():
            done = _run("import sys\nsys.exit(0)")
            self.assertEqual(done["kernel"].get("ended"), True, done)
            self.assertEqual(len(_KERNELS), 0)
            fresh = _run("print('respawned')")
        self.assertEqual(fresh["kernel"]["reused"], False)
        self.assertIn("respawned", fresh["output"])

    def test_subprocess_fd_output_reaches_the_result(self):
        code = (
            "import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', \"print('raw-passthrough')\"])\n"
        )
        with _kernel_config():
            result = _run(code)
        self.assertEqual(result["status"], "success", result)
        self.assertIn("raw-passthrough", result["output"])


class TestSchemaSurface(unittest.TestCase):
    def test_reset_parameter_is_declared(self):
        with _kernel_config():
            schema = build_execute_code_schema(mode="strict")
        self.assertIn("reset", schema["parameters"]["properties"])

    def test_kernel_persistence_is_taught_unconditionally(self):
        """Persistence is woven into the tool's main description (always-on
        since #96787, integrated in the schema diet) — every session must be
        told state survives across calls, in strict and project mode alike,
        regardless of any stale kernel_mode key in config."""
        with _kernel_config():
            schema = build_execute_code_schema(mode="strict")
        self.assertIn("persistent session kernel", schema["description"])
        self.assertIn("reset", schema["parameters"]["properties"])
        with _kernel_config(kernel_mode="per-call"):
            stale_schema = build_execute_code_schema(mode="strict")
        self.assertIn("persistent session kernel", stale_schema["description"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


class TestKernelOwnershipAndLifecycle(unittest.TestCase):
    """The kernel belongs to the conversation, and its lifetime is bounded.

    run_agent mints a fresh task id per top-level turn, so a task-keyed
    kernel would neither survive the next user turn nor ever be disposed
    with anything. The owner is the approval session key; disposal rides
    the same session boundary that clears approval/yolo state, idle
    kernels are reaped, and the process-wide live count is capped (the
    lifecycle shape carried forward from clover-c1#88637).
    """

    def _run_as(self, session_key, code, task_id, **kwargs):
        from tools.approval import reset_current_session_key, set_current_session_key

        token = set_current_session_key(session_key)
        try:
            return json.loads(execute_code(code, task_id=task_id, **kwargs))
        finally:
            reset_current_session_key(token)

    def test_state_survives_across_turns_of_one_conversation(self):
        # Two top-level turns: same session, different per-turn task ids.
        with _kernel_config():
            first = self._run_as("conv-a", "x = 41", task_id="turn-1")
            self.assertEqual(first["status"], "success", first)
            second = self._run_as("conv-a", "print(x + 1)", task_id="turn-2")
        self.assertEqual(second["status"], "success", second)
        self.assertIn("42", second["output"])
        self.assertEqual(second["kernel"]["reused"], True)

    def test_sessions_are_isolated_from_each_other(self):
        # Same task id, different sessions: no state may cross.
        with _kernel_config():
            self._run_as("conv-a", "x = 41", task_id="turn-1")
            other = self._run_as("conv-b", "print(x + 1)", task_id="turn-1")
        self.assertEqual(other["status"], "error", other)
        self.assertIn("NameError", other.get("error", ""))

    def test_delegated_children_get_their_own_kernels(self):
        """A delegated child runs in a COPY of the parent's context and
        inherits the parent's approval session key — the naive owner
        resolution attached the child to the parent's kernel and leaked
        in-memory state across the delegation boundary (both directions,
        verified live). The owner must be qualified for child contexts."""
        from agent.delegation_context import delegated_child_context

        with _kernel_config():
            self._run_as("conv-a", "parent_secret = 'p'", task_id="turn-1")
            with delegated_child_context("child-1"):
                leak = self._run_as(
                    "conv-a",
                    "print(globals().get('parent_secret', 'ISOLATED'))",
                    task_id="child-task",
                )
                self._run_as("conv-a", "child_secret = 'c'", task_id="child-task")
            back = self._run_as(
                "conv-a",
                "print(globals().get('child_secret', 'ISOLATED'))",
                task_id="turn-2",
            )
        self.assertIn("ISOLATED", leak.get("output", ""), leak)
        self.assertIn("ISOLATED", back.get("output", ""), back)

    def test_two_delegated_children_are_isolated_from_each_other(self):
        """Sibling children in one batch must not share a kernel either —
        each child context carries its own delegation session id."""
        from agent.delegation_context import delegated_child_context

        with _kernel_config():
            with delegated_child_context("child-A"):
                self._run_as("conv-a", "sibling_secret = 'A'", task_id="t")
            with delegated_child_context("child-B"):
                peek = self._run_as(
                    "conv-a",
                    "print(globals().get('sibling_secret', 'ISOLATED'))",
                    task_id="t",
                )
        self.assertIn("ISOLATED", peek.get("output", ""), peek)

    def test_session_clear_disposes_the_owners_kernels(self):
        from tools.approval import clear_session

        with _kernel_config():
            self._run_as("conv-a", "x = 41", task_id="turn-1")
            self.assertEqual(len(_KERNELS), 1)
            kernel = next(iter(_KERNELS.values()))
            self.assertTrue(kernel.alive())
            clear_session("conv-a")
            self.assertEqual(len(_KERNELS), 0)
            kernel.proc.wait(timeout=10)
            self.assertFalse(kernel.alive())
            # The next turn in a cleared session starts fresh.
            after = self._run_as("conv-a", "print('x' in dir())", task_id="turn-2")
        self.assertEqual(after["status"], "success", after)
        self.assertIn("False", after["output"])

    def test_live_kernels_are_capped_lru_across_owners(self):
        with _kernel_config(max_session_kernels=2):
            kernels = []
            for index in range(4):
                self._run_as(f"conv-{index}", "x = 1", task_id=f"turn-{index}")
                kernels.append(list(_KERNELS.values()))
            self.assertLessEqual(len(_KERNELS), 2)
            live_owners = {key[0] for key in _KERNELS}
            # The two most recently used owners survive.
            self.assertEqual(live_owners, {"conv-2", "conv-3"})
        # Evicted kernels are actually dead, not orphaned.
        evicted = [
            kernel
            for snapshot in kernels
            for kernel in snapshot
            if kernel.key not in _KERNELS
        ]
        for kernel in evicted:
            kernel.proc.wait(timeout=10)
            self.assertFalse(kernel.alive())

    def test_idle_kernels_are_reaped(self):
        import time as time_module

        with _kernel_config(kernel_idle_timeout=1):
            self._run_as("conv-a", "x = 41", task_id="turn-1")
            stale = next(iter(_KERNELS.values()))
            time_module.sleep(1.2)
            # Any owner's next call sweeps expired kernels process-wide.
            self._run_as("conv-b", "y = 1", task_id="turn-2")
            self.assertNotIn(stale.key, _KERNELS)
            stale.proc.wait(timeout=10)
            self.assertFalse(stale.alive())


class TestPerCellRpcAuthority(unittest.TestCase):
    """Interpreter state persists across cells; RPC authority must not."""

    def _recorder(self, seen):
        def _handle(tool_name, tool_args, task_id=None):
            from tools.thread_context import _callback_api

            get_approval, _get_sudo, _set_a, _set_s = _callback_api()
            seen.append(
                {
                    "tool": tool_name,
                    "task_id": task_id,
                    "approval_cb": get_approval(),
                }
            )
            return json.dumps({"ok": True})

        return _handle

    def test_a_later_cells_rpc_runs_under_that_cells_authority(self):
        from tools.terminal_tool import set_approval_callback

        seen = []
        cell = "import clover_tools\nclover_tools.web_search(query='q')\n"
        with _kernel_config(), patch(
            "model_tools.handle_function_call", new=self._recorder(seen)
        ):
            def cb_one():
                return "one"

            def cb_two():
                return "two"

            set_approval_callback(cb_one)
            try:
                first = _run(cell)
                set_approval_callback(cb_two)
                second = _run(cell)
            finally:
                set_approval_callback(None)
        self.assertEqual(first["status"], "success", first)
        self.assertEqual(second["status"], "success", second)
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0]["approval_cb"], cb_one)
        self.assertIs(seen[1]["approval_cb"], cb_two)
        self.assertEqual(seen[0]["task_id"], "kernel-test")

    def test_cross_cell_alias_dispatches_under_the_current_cell(self):
        # Adversarial cross-cell dataflow: a callable captured in cell 1 and
        # invoked by an opaque global name in cell 2 still crosses the RPC
        # boundary — under cell 2's authority, allow-list, and budget — the
        # operative enforcement a per-script static scan cannot provide once
        # state persists (composition contract with the execute-code guard).
        from tools.terminal_tool import set_approval_callback

        seen = []
        with _kernel_config(), patch(
            "model_tools.handle_function_call", new=self._recorder(seen)
        ):
            def cb_one():
                return "one"

            def cb_two():
                return "two"

            set_approval_callback(cb_one)
            try:
                first = _run("import clover_tools\nalias = clover_tools.web_search\n")
                set_approval_callback(cb_two)
                second = _run("alias(query='q')\n")
            finally:
                set_approval_callback(None)
        self.assertEqual(first["status"], "success", first)
        self.assertEqual(second["status"], "success", second)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0]["approval_cb"], cb_two)

    def test_a_settled_cells_authority_refuses_dispatch(self):
        from tools.code_kernel import CellAuthority

        authority = CellAuthority("turn-1")
        authority.retire()
        result = authority.dispatch("web_search", {"query": "q"})
        self.assertIn("No active execute_code cell", result)

    def test_each_cell_installs_a_fresh_authority(self):
        with _kernel_config():
            _run("x = 1")
            kernel = next(iter(_KERNELS.values()))
            first_authority = kernel.cell_authority
            self.assertFalse(first_authority.active)
            _run("y = 2")
            self.assertIsNot(kernel.cell_authority, first_authority)
            self.assertFalse(kernel.cell_authority.active)


class TestKernelReservation(unittest.TestCase):
    def test_selected_kernel_cannot_be_reaped_before_cell_lock(self):
        from tools import code_kernel
        from tools.approval import reset_current_session_key, set_current_session_key

        owner = "reservation-gap-owner"

        def run_as_owner(code, task_id):
            token = set_current_session_key(owner)
            try:
                return json.loads(execute_code(code, task_id=task_id))
            finally:
                reset_current_session_key(token)

        with _kernel_config(kernel_idle_timeout=1, timeout=10):
            seeded = run_as_owner("held = 42", "reservation-seed")
            self.assertEqual(seeded["status"], "success", seeded)
            key = next(iter(_KERNELS))
            self.assertEqual(key[0], owner)
            kernel = _KERNELS[key]
            child_pid = kernel.proc.pid
            entered = threading.Event()
            release = threading.Event()
            result = {}
            errors = []
            original_authority = code_kernel.CellAuthority
            caller_ident = None

            def pause_after_selection(task_id):
                self.assertEqual(code_kernel._resolve_owner(task_id), owner)
                self.assertIs(_KERNELS[key], kernel)
                self.assertEqual(kernel.proc.pid, child_pid)
                authority = original_authority(task_id)
                if threading.get_ident() == caller_ident:
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("reservation barrier release timed out")
                return authority

            def invoke():
                nonlocal caller_ident
                caller_ident = threading.get_ident()
                try:
                    result["cell"] = run_as_owner("print(held)", "reservation-cell")
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=invoke, name="test-owned-reservation-cell")
            with patch.object(code_kernel, "CellAuthority", side_effect=pause_after_selection):
                worker.start()
                self.assertTrue(entered.wait(5), "execution missed selection-to-authority barrier")
                self.assertEqual(kernel.pending_cells, 1)
                time.sleep(1.15)
                self.assertEqual(code_kernel.reap_idle_kernels(), 0)
                self.assertIs(_KERNELS.get(key), kernel)
                self.assertEqual([item.proc.pid for item in _KERNELS.values()], [child_pid])
                self.assertIsNone(kernel.proc.poll())
                release.set()
                worker.join(8)
            self.assertFalse(worker.is_alive(), "selected cell did not finish")
            self.assertEqual(errors, [])
            self.assertEqual(result["cell"]["status"], "success", result)
            self.assertEqual(result["cell"]["output"].strip(), "42")
            self.assertTrue(result["cell"]["kernel"]["reused"])
            self.assertEqual(kernel.pending_cells, 0)
            self.assertEqual(kernel.proc.pid, child_pid)
            self.assertIsNone(kernel.proc.poll())
