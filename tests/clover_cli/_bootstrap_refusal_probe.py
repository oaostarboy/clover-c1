"""Owned startup-boundary probe; installer/launcher traps NEVER do real work."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> None:
    owned = Path(sys.argv[1])
    owned.mkdir()
    marker = owned / ".update-incomplete"
    original_marker = '{"attempts": 0}'
    marker.write_text(original_marker)
    (owned / "pyproject.toml").write_text('[project]\nname = "owned-refusal-probe"\n')
    assert "PYTEST_CURRENT_TEST" not in os.environ
    assert "CLOVER_IMPORT_PROBE" not in os.environ
    assert "clover_cli.main" not in sys.modules

    from clover_cli import _early_recovery as recovery, _install_repair as repair

    # Only the owned marker can be consulted. An armed recovery path may reach
    # this harmless trap, never ensurepip, pip, uv, or a launcher file operation.
    recovery._project_root = lambda: owned
    calls = []

    def refuse_install(*args, **kwargs):
        calls.append("installer")
        raise RuntimeError("owned installer trap: no installation performed")

    def refuse_launcher(*args, **kwargs):
        calls.append("launcher")
        raise RuntimeError("owned launcher trap: no launcher writes performed")

    repair.run_core_install = refuse_install
    recovery._run_repair_install = refuse_install
    repair.ensure_windows_bin_launchers = refuse_launcher

    spec = importlib.util.spec_from_file_location(
        "inert_holder_under_test", ROOT / "tests/clover_cli/_inert_venv_holder.py"
    )
    assert spec is not None and spec.loader is not None
    holder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(holder)
    assert "clover_cli.main" not in sys.modules
    assert holder.scan() == 0  # REAL scanner and indirect CLI import, not a stub.
    assert "clover_cli.main" in sys.modules
    assert calls == [], f"startup reached mutation entrypoints: {calls}"
    assert marker.read_text() == original_marker
    assert not (owned / ".update-incomplete.lock").exists()
    assert os.environ.get("CLOVER_IMPORT_PROBE") == "1"

    # Linux does not take main.py's Windows branch. Directly test the same
    # pre-import launcher fence on this host without calling any real writer.
    try:
        repair.ensure_windows_bin_launchers(
            owned, windows=True, user_path_entries=[str(owned / "bin")]
        )
    except RuntimeError as exc:
        assert "inert holder" in str(exc), str(exc)
    else:
        raise AssertionError("startup launcher mutation was not refused")
    assert calls == []

    # Negative control: this is a genuinely armed owned marker. Removing ONLY
    # the early-recovery switch reaches our harmless installer refusal trap.
    os.environ.pop("CLOVER_IMPORT_PROBE")
    recovery.recover_if_needed()
    assert calls == ["installer"], calls
    assert json.loads(marker.read_text())["attempts"] == 1
    assert not (owned / ".update-incomplete.lock").exists()
    print(json.dumps({"recovery_contained": True, "launcher_refused": True,
                      "armed_control_reached_owned_trap": True}))


if __name__ == "__main__":
    main()
