from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")


@pytest.mark.skipif(BASH is None, reason="bash is required for shell syntax checks")
@pytest.mark.parametrize(
    "script",
    ("scripts/dev-sandbox.sh", "tests/install/install-update-e2e.sh"),
)
def test_install_update_helpers_have_valid_bash_syntax(script: str) -> None:
    assert BASH is not None
    result = subprocess.run(
        [BASH, "-n", str(ROOT / script)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr