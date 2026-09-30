"""Fresh discovery must protect callers from pre-fix updater modules."""
from pathlib import Path

from clover_cli import gateway


def test_legacy_all_profiles_discovery_spares_another_venv(monkeypatch):
    monkeypatch.setattr(gateway.sys, "platform", "linux")
    monkeypatch.setattr(gateway, "is_windows", lambda: False)
    monkeypatch.setattr(gateway, "is_macos", lambda: False)
    monkeypatch.setattr(gateway.sys, "prefix", "/opt/owned/venv")
    monkeypatch.setattr(gateway.sys, "base_prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "executable", "/opt/owned/venv/bin/python")
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway, "_get_service_pids", lambda **kw: {22, 33})
    monkeypatch.setattr(gateway, "_scan_gateway_pids", lambda *a, **kw: [11, 22, 33, 44])
    commands = {
        "/proc/11/cmdline": b"/opt/owned/venv/bin/python\0-m\0clover_cli.main\0gateway\0run\0",
        "/proc/22/cmdline": b"/opt/foreign/venv/bin/python\0/opt/foreign/venv/bin/clover\0gateway\0run\0",
        "/proc/33/cmdline": b"/opt/owned/venv/bin/python3.11\0/opt/owned/venv/bin/clover\0--profile\0work\0gateway\0run\0",
    }
    def read_bytes(path):
        if str(path) not in commands:
            raise FileNotFoundError(str(path))
        return commands[str(path)]
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    # These are the exact discovery arguments used by the already-loaded
    # legacy updater after it imports the newly installed gateway module.
    assert set(gateway.find_gateway_pids(exclude_pids=set(), all_profiles=True)) == {11, 33}
    assert gateway.find_gateway_pids(exclude_pids={11, 33}, all_profiles=True) == []


def test_venv_identity_survives_shared_base_interpreter(monkeypatch, tmp_path):
    owned = tmp_path / "owned" / "bin"
    foreign = tmp_path / "foreign" / "bin"
    owned.mkdir(parents=True)
    foreign.mkdir(parents=True)
    base = tmp_path / "base-python"
    base.touch()
    for directory in (owned, foreign):
        (directory / "python").symlink_to(base)
    assert (owned / "python").resolve() == (foreign / "python").resolve()
    monkeypatch.setattr(gateway.sys, "prefix", str(owned.parent))
    monkeypatch.setattr(gateway.sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(gateway.sys, "executable", str(owned / "python"))
    commands = {
        "/proc/11/cmdline": str(owned / "python").encode() + b"\0-m\0clover_cli.main\0gateway\0run\0",
        "/proc/22/cmdline": str(foreign / "python").encode() + b"\0-m\0clover_cli.main\0gateway\0run\0",
    }
    monkeypatch.setattr(Path, "read_bytes", lambda p: commands[str(p)])
    assert gateway._gateway_pid_belongs_to_install(11)
    assert not gateway._gateway_pid_belongs_to_install(22)


def test_system_python_requires_matching_source_or_module_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway.sys, "prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "base_prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(gateway, "PROJECT_ROOT", tmp_path)
    commands = {
        "/proc/11/cmdline": b"/usr/bin/python3\0" + str(tmp_path / "clover_cli" / "main.py").encode() + b"\0gateway\0run\0",
        "/proc/22/cmdline": b"/usr/bin/python3\0/foreign/clover_cli/main.py\0gateway\0run\0",
        "/proc/33/cmdline": b"/usr/bin/python3\0-m\0clover_cli.main\0gateway\0run\0",
        "/proc/44/cmdline": b"/usr/bin/python3\0-m\0clover_cli.main\0gateway\0run\0",
    }
    monkeypatch.setattr(Path, "read_bytes", lambda p: commands[str(p)])
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **kw: tmp_path if str(p) == "/proc/33/cwd" else (tmp_path / "foreign" if str(p) == "/proc/44/cwd" else original_resolve(p, *a, **kw)))
    assert gateway._gateway_pid_belongs_to_install(11)
    assert not gateway._gateway_pid_belongs_to_install(22)
    assert gateway._gateway_pid_belongs_to_install(33)
    assert not gateway._gateway_pid_belongs_to_install(44)


def test_renamed_setproctitle_argv_is_owned_when_module_cwd_matches(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway.sys, "prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "base_prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(gateway, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(Path, "read_bytes", lambda p: b"clover\0gateway\0run\0")
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **kw: tmp_path if str(p) in {"/proc/11/cwd", "/proc/22/cwd"} else original_resolve(p, *a, **kw))
    assert gateway._gateway_pid_belongs_to_install(11)


def test_system_python_service_module_launch_in_install_working_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway.sys, "prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "base_prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(gateway, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(Path, "read_bytes", lambda p: b"/usr/bin/python3\0-u\0-m\0clover_cli.main\0gateway\0run\0")
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **kw: tmp_path if str(p) in {"/proc/11/cwd", "/proc/22/cwd"} else original_resolve(p, *a, **kw))
    assert gateway._gateway_pid_belongs_to_install(11)


def test_relative_gateway_script_owned_in_install_working_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway.sys, "prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "base_prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(gateway, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(Path, "read_bytes", lambda p: b"/usr/bin/python3\0clover_cli/main.py\0gateway\0run\0")
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **kw: tmp_path if str(p) in {"/proc/11/cwd", "/proc/22/cwd"} else original_resolve(p, *a, **kw))
    assert gateway._gateway_pid_belongs_to_install(11)


def test_virtualenv_service_launch_with_python_options_and_sibling_profile(monkeypatch, tmp_path):
    executable = tmp_path / "venv/bin/python"
    monkeypatch.setattr(gateway.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(gateway.sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setattr(gateway.sys, "executable", str(executable))
    monkeypatch.setattr(gateway, "PROJECT_ROOT", tmp_path)
    commands = {
        11: str(executable).encode() + b"\0-u\0-m\0clover_cli.main\0--profile\0sibling\0gateway\0run\0",
        22: str(executable.parent / "python3.11").encode() + b"\0clover_cli/main.py\0--profile\0sibling\0gateway\0run\0",
    }
    monkeypatch.setattr(Path, "read_bytes", lambda p: commands[int(str(p).split("/")[-2])])
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **kw: tmp_path if str(p) in {"/proc/11/cwd", "/proc/22/cwd"} else original_resolve(p, *a, **kw))
    assert gateway._gateway_pid_belongs_to_install(11)
    assert gateway._gateway_pid_belongs_to_install(22)


def test_system_python_foreign_script_data_argument_is_not_owned(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway.sys, "prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "base_prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(gateway, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(Path, "read_bytes", lambda p: b"/usr/bin/python3\0foreign.py\0clover_cli/main.py\0")
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **kw: tmp_path if str(p) == "/proc/11/cwd" else original_resolve(p, *a, **kw))
    assert not gateway._gateway_pid_belongs_to_install(11)


def test_system_python_command_string_trailing_args_are_not_owned(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway.sys, "prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "base_prefix", "/usr")
    monkeypatch.setattr(gateway.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(gateway, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(Path, "read_bytes", lambda p: b"/usr/bin/python3\0-c\0print('ok')\0clover_cli.main\0")
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **kw: tmp_path if str(p) == "/proc/11/cwd" else original_resolve(p, *a, **kw))
    assert not gateway._gateway_pid_belongs_to_install(11)
