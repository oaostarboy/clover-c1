from pathlib import Path


def test_windows_native_install_path_docs_match_installer() -> None:
    doc = Path("website/docs/user-guide/windows-native.md").read_text()
    install = Path("scripts/install.ps1").read_text()

    # The launchers live in the managed binary dir OUTSIDE the git checkout
    # (CLOVER_HOME\bin, next to the managed uv) — NOT the whole venv\Scripts
    # (which would shadow the user's python, #83797) and NOT a dir inside
    # the checkout (which `clover update`'s autostash swept off disk).
    assert "%LOCALAPPDATA%\\clover\\bin" in doc
    assert (
        "Get-Command clover        # should print "
        "C:\\Users\\<you>\\AppData\\Local\\clover\\bin\\clover.exe"
    ) in doc
    # Installer exposes $CloverHome\bin, and must copy the launchers into it.
    assert '$cloverBin = "$CloverHome\\bin"' in install
    assert "clover.exe" in install and "clover-acp.exe" in install
    # Guard against regressions to either legacy layout.
    assert '$cloverBin = "$InstallDir\\venv\\Scripts"' not in install
    assert '$cloverBin = "$InstallDir\\bin"' not in install
