"""Install desktop entry, macOS application bundle, and Windows shortcuts for Clover Desktop.

Cross-platform launcher registration so Clover Desktop appears in:
- Linux: XDG applications menu (~/.local/share/applications/clover.desktop)
- macOS: Applications folder (/Applications/Clover.app or ~/Applications/Clover.app)
- Windows: Start Menu and Desktop shortcuts (Clover.lnk)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return sys.platform == "win32"


def is_linux() -> bool:
    return sys.platform.startswith(("linux", "freebsd", "openbsd", "netbsd"))


def install_linux_desktop_entry(project_root: Path) -> Optional[Path]:
    """Install the Linux XDG desktop entry."""
    try:
        from clover_cli.linux_desktop_entry import install_desktop_entry, is_supported

        if not is_supported():
            return None
        return install_desktop_entry(project_root)
    except Exception:
        return None


def _can_write_target(target: Path) -> bool:
    try:
        if target.exists():
            return os.access(target, os.W_OK)
        parent = target.parent
        return parent.exists() and os.access(parent, os.W_OK)
    except Exception:
        return False


def install_macos_desktop_entry(
    desktop_dir: Path,
    packaged_executable: Optional[Path] = None,
    preferred_dest: Optional[Path] = None,
) -> Optional[Path]:
    """Install/copy Clover.app into /Applications or ~/Applications (macOS, best-effort)."""
    if not is_macos() and preferred_dest is None:
        return None

    # 1. Locate source Clover.app
    source_app: Optional[Path] = None
    if packaged_executable is not None and packaged_executable.exists():
        # .../Clover.app/Contents/MacOS/Clover -> parents[2] is Clover.app
        try:
            cand = packaged_executable.parents[2]
            if cand.name.endswith(".app") and cand.is_dir():
                source_app = cand
        except IndexError:
            pass

    if source_app is None:
        release_dir = desktop_dir / "release"
        if release_dir.is_dir():
            candidates = [p for p in release_dir.glob("mac*/Clover.app") if p.is_dir()]
            if candidates:
                source_app = max(candidates, key=lambda p: p.stat().st_mtime)

    if source_app is None or not source_app.is_dir():
        return None

    # 2. Determine destination
    if preferred_dest is not None:
        dest_app = preferred_dest
    else:
        system_app = Path("/Applications/Clover.app")
        user_app = Path.home() / "Applications" / "Clover.app"
        if _can_write_target(system_app):
            dest_app = system_app
        else:
            dest_app = user_app

    # If source is already at destination, nothing to do
    try:
        if dest_app.resolve() == source_app.resolve():
            return dest_app
    except Exception:
        pass

    try:
        dest_app.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        if dest_app != Path.home() / "Applications" / "Clover.app":
            dest_app = Path.home() / "Applications" / "Clover.app"
            dest_app.parent.mkdir(parents=True, exist_ok=True)
        else:
            return None

    # 3. Copy via ditto (preserves resource forks, codesign, xattrs) or shutil.copytree
    tmp_dest = dest_app.parent / f".Clover.app.tmp.{os.getpid()}"
    if tmp_dest.exists():
        shutil.rmtree(tmp_dest, ignore_errors=True)

    ditto_bin = shutil.which("ditto") or "/usr/bin/ditto"
    copied = False
    if os.path.exists(ditto_bin):
        try:
            res = subprocess.run(
                [ditto_bin, str(source_app), str(tmp_dest)],
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
            copied = (res.returncode == 0) and tmp_dest.is_dir()
        except Exception:
            copied = False

    if not copied:
        if tmp_dest.exists():
            shutil.rmtree(tmp_dest, ignore_errors=True)
        try:
            shutil.copytree(source_app, tmp_dest, symlinks=True)
            copied = tmp_dest.is_dir()
        except Exception:
            return None

    # 4. Atomic swap into place
    if dest_app.exists():
        old_backup = dest_app.parent / f".Clover.app.old.{os.getpid()}"
        if old_backup.exists():
            shutil.rmtree(old_backup, ignore_errors=True)
        try:
            os.replace(dest_app, old_backup)
            os.replace(tmp_dest, dest_app)
            shutil.rmtree(old_backup, ignore_errors=True)
        except OSError:
            shutil.rmtree(dest_app, ignore_errors=True)
            os.replace(tmp_dest, dest_app)
    else:
        os.replace(tmp_dest, dest_app)

    # 5. Fixups: strip quarantine, touch for LaunchServices
    xattr_bin = shutil.which("xattr") or "/usr/bin/xattr"
    if os.path.exists(xattr_bin):
        try:
            subprocess.run([xattr_bin, "-cr", str(dest_app)], check=False, capture_output=True, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass

    touch_bin = shutil.which("touch") or "/usr/bin/touch"
    if os.path.exists(touch_bin):
        try:
            subprocess.run([touch_bin, str(dest_app)], check=False, capture_output=True, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass

    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    )
    if lsregister.exists():
        try:
            subprocess.run([str(lsregister), "-f", str(dest_app)], check=False, capture_output=True, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass

    return dest_app


def install_windows_desktop_entry(
    desktop_dir: Path,
    packaged_executable: Optional[Path] = None,
    custom_shortcut_paths: Optional[List[Path]] = None,
) -> List[Path]:
    """Create Start Menu and Desktop shortcuts for Clover.exe (Windows, best-effort)."""
    if not is_windows() and custom_shortcut_paths is None:
        return []

    target_exe: Optional[Path] = None
    if packaged_executable is not None and packaged_executable.exists():
        target_exe = packaged_executable
    else:
        release_dir = desktop_dir / "release"
        candidates = [
            release_dir / "win-unpacked" / "Clover.exe",
            release_dir / "win-arm64-unpacked" / "Clover.exe",
            release_dir / "win-ia32-unpacked" / "Clover.exe",
        ]
        existing = [p for p in candidates if p.is_file()]
        if existing:
            target_exe = max(existing, key=lambda p: p.stat().st_mtime)

    if target_exe is None or not target_exe.is_file():
        return []

    work_dir = target_exe.parent
    icon_ico = work_dir / "resources" / "icon.ico"
    icon_loc = f"{icon_ico},0" if icon_ico.exists() else f"{target_exe},0"

    shortcuts: List[Path] = []
    if custom_shortcut_paths is not None:
        shortcuts = custom_shortcut_paths
    else:
        appdata = os.environ.get("APPDATA")
        start_dir = (
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if appdata
            else Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        )
        shortcuts.append(start_dir / "Clover.lnk")

        userprofile = os.environ.get("USERPROFILE")
        desk_dir = Path(userprofile) / "Desktop" if userprofile else Path.home() / "Desktop"
        shortcuts.append(desk_dir / "Clover.lnk")

    ps_bin = shutil.which("powershell") or shutil.which("powershell.exe") or "powershell"
    target_list_str = ", ".join(f"'{str(p)}'" for p in shortcuts)
    ps_command = f"""
$shell = New-Object -ComObject WScript.Shell
$targets = @({target_list_str})
foreach ($lnk in $targets) {{
    try {{
        $dir = Split-Path -Parent $lnk
        if (!(Test-Path $dir)) {{ New-Item -ItemType Directory -Path $dir -Force | Out-Null }}
        $sc = $shell.CreateShortcut($lnk)
        $sc.TargetPath = '{str(target_exe)}'
        $sc.WorkingDirectory = '{str(work_dir)}'
        $sc.IconLocation = '{icon_loc}'
        $sc.Description = 'Clover Cognition'
        $sc.Save()
    }} catch {{}}
}}
"""
    try:
        subprocess.run(
            [ps_bin, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
            check=False,
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        pass

    return shortcuts


def register_desktop_entries(
    desktop_dir: Path,
    project_root: Path,
    packaged_executable: Optional[Path] = None,
) -> None:
    """Register Clover Desktop in the host OS launcher / application menus."""
    if is_macos():
        dest = install_macos_desktop_entry(desktop_dir, packaged_executable=packaged_executable)
        if dest:
            print(f"✓ macOS application installed: {dest}")
    elif is_windows():
        created = install_windows_desktop_entry(desktop_dir, packaged_executable=packaged_executable)
        if created:
            print(f"✓ Created Windows shortcuts: {', '.join(str(p) for p in created)}")
    else:
        entry = install_linux_desktop_entry(project_root)
        if entry:
            print(f"✓ Desktop launcher entry installed: {entry}")


def is_desktop_provisioned(
    desktop_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> bool:
    """Return True if Clover Desktop or its system launchers are already provisioned."""
    root = project_root
    if root is None:
        try:
            from clover_cli.main import PROJECT_ROOT
            root = PROJECT_ROOT
        except Exception:
            root = Path(__file__).resolve().parents[1]

    d_dir = desktop_dir if desktop_dir is not None else (root / "apps" / "desktop")

    # Check packaged executable in release dir
    try:
        from clover_cli.main import _desktop_packaged_executable
        if _desktop_packaged_executable(d_dir) is not None:
            return True
    except Exception:
        pass

    if is_macos():
        if Path("/Applications/Clover.app").exists():
            return True
        if (Path.home() / "Applications" / "Clover.app").exists():
            return True
        release_dir = d_dir / "release"
        if release_dir.is_dir() and any(release_dir.glob("mac*/Clover.app")):
            return True
    elif is_windows():
        appdata = os.environ.get("APPDATA")
        if appdata and (Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Clover.lnk").exists():
            return True
        if (Path.home() / "Desktop" / "Clover.lnk").exists():
            return True
        release_dir = d_dir / "release"
        if release_dir.is_dir() and (release_dir / "win-unpacked" / "Clover.exe").exists():
            return True
    else:
        if (Path.home() / ".local" / "share" / "applications" / "clover.desktop").exists():
            return True
        release_dir = d_dir / "release"
        if release_dir.is_dir() and any(
            (release_dir / f"{d}" / name).exists()
            for d in ("linux-unpacked", "linux-arm64-unpacked")
            for name in ("clover", "Clover")
        ):
            return True

    return False


def spawn_background_desktop_provision(
    project_root: Optional[Path] = None,
) -> Optional[subprocess.Popen]:
    """Spawn a detached low-priority background process to build desktop and register launchers."""
    log_file = None
    try:
        root = project_root
        if root is None:
            from clover_cli.main import PROJECT_ROOT
            root = PROJECT_ROOT

        from clover_constants import get_clover_home, with_clover_node_path
        build_env = with_clover_node_path()

        try:
            log_dir = get_clover_home() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "desktop-build.log"
            log_file = open(log_path, "a", encoding="utf-8")
        except Exception:
            log_file = subprocess.DEVNULL

        cmd = [sys.executable, "-m", "clover_cli.main", "desktop", "--build-only"]
        popen_kwargs = {
            "cwd": root,
            "env": build_env,
            "stdout": log_file,
            "stderr": subprocess.STDOUT if log_file != subprocess.DEVNULL else subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            creationflags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
            )
            popen_kwargs["creationflags"] = creationflags
        else:
            popen_kwargs["start_new_session"] = True
            def _low_prio():
                try:
                    os.nice(10)
                except Exception:
                    pass
            popen_kwargs["preexec_fn"] = _low_prio

        proc = subprocess.Popen(cmd, **popen_kwargs)
        if log_file is not None and hasattr(log_file, "close"):
            try:
                log_file.close()
            except Exception:
                pass
        return proc
    except Exception:
        if log_file is not None and hasattr(log_file, "close"):
            try:
                log_file.close()
            except Exception:
                pass
        return None


def maybe_auto_provision_desktop_on_startup(
    project_root: Optional[Path] = None,
) -> Optional[subprocess.Popen]:
    """Lightweight, non-blocking check on gateway startup to auto-provision desktop.

    If desktop auto-build is appropriate for the host environment and the
    desktop app / launcher is not yet provisioned, spawn a detached
    low-priority background process to build and register it without
    blocking startup.
    """
    if os.environ.get("CLOVER_SKIP_AUTO_DESKTOP_BUILD") == "1":
        return None
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return None

    try:
        from clover_cli.update_cmd import _should_auto_build_desktop_on_update
        if not _should_auto_build_desktop_on_update():
            return None
    except Exception:
        return None

    root = project_root
    if root is None:
        try:
            from clover_cli.main import PROJECT_ROOT
            root = PROJECT_ROOT
        except Exception:
            root = Path(__file__).resolve().parents[1]

    desktop_dir = root / "apps" / "desktop"
    if not (desktop_dir / "package.json").exists():
        return None

    if is_desktop_provisioned(desktop_dir, root):
        return None

    return spawn_background_desktop_provision(root)
