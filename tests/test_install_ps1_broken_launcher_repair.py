"""Windows Clover launcher must report a clear repair line if broken.

``Install-CloverCommandLaunchers`` in scripts/install.ps1 stages either a
copied ``clover.exe`` or, for a relocatable venv trampoline, a ``.cmd``
delegator that invokes the real exe inside ``venv\\Scripts``. Before this
fix the ``.cmd`` delegator was a bare ``"$src" %*`` — if the venv later goes
missing or breaks, running ``clover`` gives a raw "is not recognized"/file
-not-found error with no guidance, and the user cannot even run
``clover doctor`` to self-repair.

These tests lock the contract at the source level (the script only runs on
Windows, so Linux CI cannot execute the PowerShell path) — same convention
as tests/test_install_ps1_venv_rename_abort.py.
"""

from pathlib import Path

import pytest

_INSTALL_PS1 = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"


@pytest.fixture(scope="module")
def source() -> str:
    return _INSTALL_PS1.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    """Return the text of a PowerShell ``function <name> { ... }`` block."""
    start = source.index(f"function {name}")
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[brace : i + 1]
    raise AssertionError(f"unterminated function body for {name}")


def test_cmd_delegator_checks_launcher_exists_before_invoking(source: str):
    body = _function_body(source, "Install-CloverCommandLaunchers")

    # The .cmd delegator content is built as a single string assigned to
    # Set-Content; it must guard the invocation with an existence check
    # instead of a bare `"$src" %*`.
    cmd_value_start = body.index('Set-Content -Path (Join-Path $Destination "$launcher.cmd")')
    cmd_value = body[cmd_value_start : cmd_value_start + 600]

    assert "if not exist" in cmd_value, (
        "the .cmd delegator must check the target launcher exists before "
        "invoking it"
    )
    assert "re-run the installer" in cmd_value.lower() or "install.ps1" in cmd_value, (
        "the .cmd delegator must print a clear repair command when the "
        "launcher is missing"
    )
    assert "exit /b 1" in cmd_value


def test_cmd_delegator_still_invokes_launcher_on_success_path(source: str):
    body = _function_body(source, "Install-CloverCommandLaunchers")
    cmd_value_start = body.index('Set-Content -Path (Join-Path $Destination "$launcher.cmd")')
    cmd_value = body[cmd_value_start : cmd_value_start + 600]

    assert '"$src" %*' in cmd_value
