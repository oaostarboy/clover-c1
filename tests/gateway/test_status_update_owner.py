"""Owner-bound update admission must survive subordinate health reports."""
import json
from types import SimpleNamespace

import pytest

from gateway import status

CAPABILITY = "reply-carriers-through-ack-v1"


@pytest.fixture
def records(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    owner = dict(pid=101, start_time=11, kind="clover-gateway",
                 argv=["clover", "gateway", "run"], clover_home=str(tmp_path))
    writer = dict(owner, pid=202, start_time=22)
    current = [owner]
    monkeypatch.setattr(status, "_build_pid_record", lambda: dict(current[0]))
    monkeypatch.setattr(status, "_get_code_identity_fields", lambda: {
        "code_sha": "owner-code" if current[0] == owner else "helper-code"})
    monkeypatch.setattr(status, "_pid_exists", lambda pid: pid in (101, 202))
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: {101: 11, 202: 22}.get(pid))
    (tmp_path / "gateway.pid").write_text(json.dumps(owner))
    status.write_runtime_status(gateway_state="running", active_agents=3,
                                update_restart_capability=CAPABILITY)
    return SimpleNamespace(owner=owner, writer=writer, current=current,
                           path=tmp_path / "gateway_state.json", home=tmp_path)


def test_subordinate_health_preserves_exact_owner_admission(records):
    records.current[0] = records.writer
    status.write_runtime_status(platform="feishu", platform_state="connected")
    row = json.loads(records.path.read_text())
    assert row["pid"] == records.owner["pid"]
    assert row["start_time"] == records.owner["start_time"]
    assert row["update_restart_capability"] == CAPABILITY
    assert row["code_sha"] == "owner-code"
    assert row["active_agents"] == 3
    assert row["platforms"]["feishu"]["writer_pid"] == records.writer["pid"]
    assert row["platforms"]["feishu"]["writer_start_time"] == records.writer["start_time"]


@pytest.mark.parametrize("fields", [
    {"update_restart_capability": "forged"},
    {"gateway_state": "stopped"},
    {"active_agents": 0},
    {"restart_requested": True},
    {"exit_reason": "helper exit"},
    {"served_profiles": []},
    {"clear_profile_platforms": True},
])
def test_health_writer_cannot_change_owner_contract(records, fields):
    original = records.path.read_bytes()
    records.current[0] = records.writer
    status.write_runtime_status(**fields)
    assert records.path.read_bytes() == original


def test_overlapping_health_write_cannot_restore_stale_owner_state(records, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    read = status._read_json_file
    helper_read = threading.Event()
    release_helper = threading.Event()
    owner_attempt = threading.Event()
    current = threading.local()
    monkeypatch.setattr(status, "_build_pid_record", lambda: dict(current.record))

    def delayed_read(path):
        row = read(path)
        if threading.current_thread().name.startswith("helper") and path == records.path:
            helper_read.set()
            assert release_helper.wait(2)
        return row

    monkeypatch.setattr(status, "_read_json_file", delayed_read)

    def helper():
        current.record = records.writer
        status.write_runtime_status(platform="feishu", platform_state="connected")

    def owner():
        current.record = records.owner
        owner_attempt.set()
        status.write_runtime_status(active_agents=7)

    with ThreadPoolExecutor(1, thread_name_prefix="helper") as h, ThreadPoolExecutor(1) as o:
        helper_job = h.submit(helper)
        assert helper_read.wait(2)
        owner_job = o.submit(owner)
        assert owner_attempt.wait(2)
        # With serialization the owner cannot finish against the stale read.
        # Without it the owner finishes first and the helper erases its count.
        try:
            owner_job.result(timeout=0.1)
        except TimeoutError:
            pass
        release_helper.set()
        helper_job.result(timeout=2)
        owner_job.result(timeout=2)
    row = json.loads(records.path.read_text())
    assert row["active_agents"] == 7
    assert row["update_restart_capability"] == CAPABILITY
    assert row["platforms"]["feishu"]["state"] == "connected"


@pytest.mark.parametrize("change", ["pid", "reused", "unknown-time", "dead", "home", "kind"])
def test_unverified_owner_cannot_be_preserved_by_health_writer(records, monkeypatch, change):
    owner = dict(records.owner)
    if change == "pid":
        owner["pid"] = 303
    elif change == "reused":
        monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 99)
    elif change == "unknown-time":
        monkeypatch.setattr(status, "_get_process_start_time", lambda pid: None)
    elif change == "dead":
        monkeypatch.setattr(status, "_pid_exists", lambda pid: False)
    elif change == "home":
        owner["clover_home"] = str(records.home / "different-profile")
    elif change == "kind":
        owner["kind"] = "unrelated-worker"
    (records.home / "gateway.pid").write_text(json.dumps(owner))
    original = records.path.read_bytes()
    records.current[0] = records.writer
    status.write_runtime_status(platform="feishu", platform_state="connected")
    assert records.path.read_bytes() == original


@pytest.mark.parametrize("home", [None, "", "   "])
def test_missing_or_malformed_home_is_not_an_owner_binding(records, monkeypatch, home):
    monkeypatch.chdir(records.home)
    owner = dict(records.owner, clover_home=home)
    row = json.loads(records.path.read_text())
    row["clover_home"] = home
    (records.home / "gateway.pid").write_text(json.dumps(owner))
    records.path.write_text(json.dumps(row))
    original = records.path.read_bytes()
    records.current[0] = records.writer
    status.write_runtime_status(platform="feishu", platform_state="connected")
    assert records.path.read_bytes() == original


def test_real_owner_change_drops_previous_contract_until_loaded_runner_attests(records):
    records.current[0] = records.writer
    (records.home / "gateway.pid").write_text(json.dumps(records.writer))
    status.write_runtime_status(gateway_state="starting")
    row = json.loads(records.path.read_text())
    assert row["pid"] == records.writer["pid"]
    assert "update_restart_capability" not in row
    status.write_runtime_status(gateway_state="running", update_restart_capability=CAPABILITY)
    assert json.loads(records.path.read_text())["update_restart_capability"] == CAPABILITY


def test_health_writer_never_repairs_missing_capability(records):
    row = json.loads(records.path.read_text())
    row.pop("update_restart_capability")
    records.path.write_text(json.dumps(row))
    records.current[0] = records.writer
    status.write_runtime_status(session_store={"status": "ok"})
    row = json.loads(records.path.read_text())
    assert "update_restart_capability" not in row
    assert row["pid"] == records.owner["pid"]
    assert row["session_store"] == {"status": "ok"}


def test_owner_replacement_during_health_merge_is_not_overwritten(records, monkeypatch):
    original = records.path.read_bytes()

    def replace_owner(pid):
        (records.home / "gateway.pid").write_text(json.dumps(records.writer))
        return 11

    monkeypatch.setattr(status, "_get_process_start_time", replace_owner)
    records.current[0] = records.writer
    status.write_runtime_status(platform="feishu", platform_state="connected")
    assert records.path.read_bytes() == original


@pytest.mark.parametrize("capability_present", [True, False])
def test_frozen_158_caller_preserves_pre_pull_boundary(records, monkeypatch, capability_present):
    """Original 1.5.8 entry + guard: no pull is reachable after refusal."""
    import sys
    from pathlib import Path
    from types import ModuleType
    from clover_cli import gateway, update_cmd, update_receipt

    fixture = Path(__file__).parents[1] / "fixtures" / "update_158"
    guard = ModuleType("clover_cli.update_delivery_guard")
    exec(compile((fixture / "update_delivery_guard.py.txt").read_text(), "frozen-158-guard", "exec"), guard.__dict__)
    monkeypatch.setitem(sys.modules, "clover_cli.update_delivery_guard", guard)
    monkeypatch.setattr(update_receipt, "_current", None)
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **kw: [
        SimpleNamespace(profile="default", path=records.home, pid=101)])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [101])
    monkeypatch.setattr(update_cmd, "_gateway_pid_install_verdict", lambda pid: "own")
    row = json.loads(records.path.read_text())
    if not capability_present:
        row.pop("update_restart_capability")
        records.path.write_text(json.dumps(row))
    records.current[0] = records.writer
    status.write_runtime_status(platform="feishu", platform_state="connected")
    reached = []

    def after_guard():
        reached.append("first-pre-pull-work")
        return SimpleNamespace(_capture_active_lazy_features=lambda: [])

    namespace = {"_m": after_guard}
    exec(compile((fixture / "update_cmd.py.txt").read_text(), "frozen-158-entry", "exec"), namespace)
    if capability_present:
        namespace["_cmd_update_impl"](SimpleNamespace(), True)
        assert reached == ["first-pre-pull-work"]
    else:
        with pytest.raises(SystemExit) as exc:
            namespace["_cmd_update_impl"](SimpleNamespace(), True)
        assert exc.value.code == 2
        assert reached == []
