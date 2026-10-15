"""The fixed holder must contain mutation before its indirect CLI import."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_recovery_and_launcher_are_contained_before_real_scanner_import(tmp_path):
    env = {k: os.environ[k] for k in (
        "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "TMPDIR", "HOME", "USERPROFILE", "LANG", "TZ", "CLOVER_HOME",
        "CLOVER_TEST_ISOLATION",
    ) if k in os.environ}
    result = subprocess.run(
        [sys.executable, "-I", "-B",
         str(ROOT / "tests/clover_cli/_bootstrap_refusal_probe.py"),
         str(tmp_path / "owned-recovery")],
        cwd=str(ROOT), env=env, shell=False, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {"recovery_contained": True, "launcher_refused": True,
                       "armed_control_reached_owned_trap": True}
