"""Conservative, snapshot-first install repair; never restore user state implicitly."""
import contextlib
import io
import logging
import sys

from clover_constants import get_clover_home, venv_python_path

logger = logging.getLogger(__name__)


def _repair_doctor_safe_items() -> list[str]:
    """Run only doctor's CA, stale root key and source version checks."""
    from clover_cli import doctor
    from clover_cli.config import read_user_config_raw, atomic_config_write

    fixed = []
    issues = []
    doctor.check_certificates(should_fix=True, issues=issues)
    if issues:
        fixed.append("CA bundle needs manual repair")
    path = get_clover_home() / "config.yaml"
    if path.is_file():
        raw = read_user_config_raw(path)
        keys = [key for key in ("provider", "base_url") if isinstance(raw.get(key), str)]
        if keys:
            model = raw.get("model")
            if not isinstance(model, dict):
                model = {"default": model.strip()} if isinstance(model, str) and model.strip() else {}
                raw["model"] = model
            for key in keys:
                if not model.get(key):
                    model[key] = raw[key]
                raw.pop(key)
            atomic_config_write(path, raw)
            fixed.append("moved stale provider settings")
    if (doctor.PROJECT_ROOT / ".git").exists():
        before = doctor._read_pyproject_version()
        from clover_cli import __version__
        if before and before != __version__:
            doctor._check_version_consistency([], should_fix=True)
            fixed.append("corrected version drift")
    return fixed


def _repair_dependencies() -> bool:
    """Reuse the updater's dependency installer, never mutate the user home."""
    from clover_cli import update_cmd
    from clover_cli import main
    from clover_cli.managed_uv import ensure_uv, managed_python_env
    root = update_cmd._m().PROJECT_ROOT
    uv = ensure_uv()
    if uv:
        python = venv_python_path(root / "venv", windows=sys.platform == "win32")
        if not python.exists():
            import subprocess
            subprocess.run([uv, "venv", "venv"], cwd=root, check=True)
        env = managed_python_env()
        env["VIRTUAL_ENV"] = str(root / "venv")
        main._install_python_dependencies_with_optional_fallback([uv, "pip"], env=env, group="all")
    else:
        main._install_python_dependencies_with_optional_fallback([sys.executable, "-m", "pip"], group="all")
    return update_cmd._venv_core_imports_healthy()[0]


def run_repair() -> str:
    """Snapshot first; repair safe items; report anything requiring human action."""
    from clover_cli import backup, update_cmd

    try:
        snapshot = backup.create_quick_snapshot(label="pre-repair")
    except Exception:
        logger.exception("Repair snapshot failed")
        return "Couldn't take a safety snapshot, so nothing was repaired. Run clover doctor for details."
    if not snapshot:
        return "Couldn't take a safety snapshot, so nothing was repaired. Run clover doctor for details."

    fixed = []
    manual = []
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        def attempt(label, action):
            try:
                return action()
            except Exception:
                logger.exception("Repair step failed: %s", label)
                manual.append(label)
                return None

        post_output = io.StringIO()
        with contextlib.redirect_stdout(post_output):
            attempt("safe install checks", update_cmd._run_post_update_safe_repairs)
        if "Cleared" in post_output.getvalue():
            fixed.append("cleared stuck git locks")
        if "Reseeded bundled skills" in post_output.getvalue():
            fixed.append("restored bundled skills")
        if "Core runtime dependencies missing" in post_output.getvalue():
            manual.append("core runtime imports (run clover doctor)")
        health = attempt("Python packages", update_cmd._venv_core_imports_healthy)
        if health is not None and not health[0]:
            if attempt("Python packages", _repair_dependencies):
                fixed.append("reinstalled missing packages")
            else:
                manual.append("Python packages (run clover update)")
        attempt("config migration", lambda: update_cmd._check_and_apply_config_migration(assume_yes=True, gateway_mode=False))
        fixes = attempt("doctor safe checks", _repair_doctor_safe_items)
        if fixes:
            fixed.extend(fixes)
        db = get_clover_home() / "state.db"
        if db.exists():
            result = attempt("state.db check", lambda: backup.verify_sqlite_integrity(db))
            if result is not None and not result.get("valid"):
                from clover_cli.backup import _quick_snapshot_root
                root = _quick_snapshot_root(get_clover_home())
                valid = []
                for candidate in root.iterdir() if root.exists() else ():
                    state = candidate / "state.db"
                    if state.is_file() and backup.verify_sqlite_integrity(state).get("valid"):
                        valid.append(candidate.name)
                if valid:
                    manual.append(f"state.db is corrupt; valid snapshot {sorted(valid)[-1]}; open Clover chat and type /snapshot restore {sorted(valid)[-1]} (restores the whole snapshot)")
                else:
                    manual.append("state.db is corrupt; no valid snapshot found. Run clover doctor")
    summary = f"Checked 6 things. Fixed {len(fixed)}" + (f": {', '.join(fixed)}." if fixed else ".")
    if manual:
        summary += " Needs you: " + "; ".join(manual) + "."
    return summary + " Your chats and memories weren't touched."


def cmd_repair(args):
    print(run_repair())
