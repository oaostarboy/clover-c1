from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

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


def test_dev_sandbox_installer_shortcut_uses_local_fixture_route() -> None:
    script = (ROOT / "scripts/dev-sandbox.sh").read_text(encoding="utf-8")
    match = re.search(r"curl\s+-fsSL\s+(\S+)", script)
    assert match, "installer shortcut must pass an explicit fixture URL to curl"

    url = urlsplit(match.group(1))
    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "clover-c1.",
        "/install.sh",
    )


def test_stage2_node_trusts_the_sandbox_proxy_ca() -> None:
    script = (ROOT / "scripts/sandbox/stage2-run.sh").read_text(encoding="utf-8")
    assert "--setenv NODE_EXTRA_CA_CERTS /work/certs/ca.pem" in script
    assert "--setenv NODE_EXTRA_CA_CERTS /work/certs/real-ca.pem" not in script
