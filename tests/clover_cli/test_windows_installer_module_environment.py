"""Installer subprocess environment contracts and real Windows module loading."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("module_key", ["PSModulePath", "PSMODULEPATH", "psmodulepath"])
def test_windows_installer_rebuilds_shell_module_path_only(module_key, monkeypatch):
    from clover_cli import managed_uv

    env = {module_key: "foreign-shell-modules", "PATH": "preserved-path",
           "UV_INSTALL_DIR": "private-install", "UV_UNMANAGED_INSTALL": "private-install",
           "SystemRoot": "preserved-root", "PSExecutionPolicyPreference": "AllSigned"}
    original = dict(env)
    calls = []
    monkeypatch.setattr(managed_uv.subprocess, "run", lambda *args, **kw: calls.append((args, kw)))
    managed_uv._install_uv_windows(env)
    assert len(calls) == 1
    args, options = calls[0]
    assert options["env"] == {k: v for k, v in original.items() if k.upper() != "PSMODULEPATH"}
    assert env == original  # Never mutate caller or process-wide environment.
    assert options["check"] is True
    assert options["capture_output"] is True
    assert options["stdin"] == subprocess.DEVNULL
    assert args[0][-1] == "irm https://astral.sh/uv/install.ps1 | iex"


@pytest.mark.windows_only
def test_windows_installer_loads_own_security_module_after_pwsh_parent(monkeypatch):
    """Execute the real product launch boundary, replacing only network payload.

    Core's module directory is a real installed input, not a fabricated module.
    This is a module-loading regression, not standalone installer acceptance.
    """
    from clover_cli import managed_uv

    run = subprocess.run
    core = run(["pwsh", "-NoProfile", "-Command", "$PSHOME"],
               check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    assert (Path(core) / "Modules" / "Microsoft.PowerShell.Security").is_dir()
    env = dict(os.environ)
    for key in list(env):
        if key.upper() == "PSMODULEPATH":
            env.pop(key)
    desktop = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/Modules"
    env["PSModulePath"] = str(Path(core) / "Modules") + ";" + str(desktop)
    before = dict(env)
    results = []
    script = r"""
$ErrorActionPreference='Stop'
$policy = Get-ExecutionPolicy
$module = Get-Module Microsoft.PowerShell.Security
@{policy=$policy.ToString(); module=$module.Path; home=$PSHOME; edition=$PSVersionTable.PSEdition} | ConvertTo-Json -Compress
"""

    def observe(args, **kwargs):
        # Preserve actual executable, flags, environment, I/O and check behavior.
        assert args[-1] == "irm https://astral.sh/uv/install.ps1 | iex"
        result = run([*args[:-1], script], timeout=30, **kwargs)
        results.append(json.loads(result.stdout))
        return result

    monkeypatch.setattr(managed_uv.subprocess, "run", observe)
    managed_uv._install_uv_windows(env)
    assert env == before
    assert len(results) == 1
    result = results[0]
    assert result["edition"] == "Desktop"
    expected = Path(result["home"]) / "Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1"
    assert Path(result["module"]) == expected
