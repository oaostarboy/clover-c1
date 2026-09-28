"""Guard the user-owned bytes through repair, rollback and migration."""
import ast
import hashlib
import subprocess
from collections import Counter
from pathlib import Path
from unittest.mock import patch


DATA = {
    "memories/MEMORY.md": b"long-lived memory\n",
    "memories/USER.md": b"user preferences\n",
    "sessions/conversation.jsonl": b'{"role":"user","content":"hello"}\n',
    "memory_store.db": b"private memory fixture",
    "skills/my-skill/SKILL.md": b"---\nname: my-skill\n---\nMy skill\n",
    "cron/jobs.json": b'{"jobs":[]}\n',
}


def test_user_data_survives_repair_rollback_and_config_migration(tmp_path, monkeypatch):
    import sqlite3
    from clover_cli import backup, repair_cmd, update_cmd, config, update_restart_watcher

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    for name, contents in DATA.items():
        target = home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    state = home / "state.db"
    with sqlite3.connect(state) as db:
        db.execute("create table messages (text varchar)")
        db.execute("insert into messages values ('do not delete')")
    (home / "config.yaml").write_text("model:\n  provider: auto\n", encoding="utf-8")
    paths = [home / name for name in DATA] + [state]
    original = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

    with patch.object(repair_cmd, "get_clover_home", return_value=home), \
         patch.object(backup, "get_clover_home", return_value=home), \
         patch.object(update_cmd, "_run_post_update_safe_repairs"), \
         patch.object(update_cmd, "_venv_core_imports_healthy", return_value=(True, "")), \
         patch.object(update_cmd, "_check_and_apply_config_migration"), \
         patch.object(repair_cmd, "_repair_doctor_safe_items", return_value=[]):
        assert "Checked" in repair_cmd.run_repair()

    repo = tmp_path / "repo"
    repo.mkdir()
    python = repo / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fake venv")
    beacon = home / ".update_beacon.json"
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    with patch.object(update_restart_watcher.subprocess, "run", side_effect=fake_run):
        update_restart_watcher._rollback_checkout({
            "repo": str(repo), "pre_pull_sha": "a" * 40, "venv_python": str(python),
        }, beacon)

    with patch.object(config, "get_clover_home", return_value=home), \
         patch.object(config, "get_config_path", return_value=home / "config.yaml"):
        config.migrate_config(interactive=False, quiet=True)
    assert {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == original
    with sqlite3.connect(state) as db:
        assert db.execute("select text from messages").fetchone() == ("do not delete",)


# Exact AST calls, not substrings: adding ANY deletion to the update/rollback/repair
# paths requires a review and a documented allowlist entry. User-owned targets
# (memories, sessions, state.db, memory_store.db, skills) must NEVER be added.
# Existing sidecar deletion is allowed only for a corrupt database being replaced
# by update (WAL/shm/journal), not its main state.db file.
_ALLOWED_DELETES = {
    "update_cmd.py": {
        "response_path.unlink(missing_ok=True)",  # update prompt IPC response
        "prompt_path.unlink(missing_ok=True)",  # update prompt IPC request
        "db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)",  # corrupt DB sidecars only
        "shutil.rmtree(tmp_dir, ignore_errors=True)",  # temporary build dir
        "shutil.rmtree(leftover, ignore_errors=True)",  # abandoned staging dir
        "shutil.rmtree(backup, ignore_errors=True)",  # staging backup
        "os.remove(leftover)",  # abandoned staging file
        "shutil.rmtree(staging, ignore_errors=True)",  # update staging dir
        "os.remove(staging)",  # update staging file
        "os.remove(backup)",  # staging backup file
        "shutil.rmtree(dst, ignore_errors=True)",  # failed update staging destination
        "os.remove(dst)",  # failed update staging file
        "cache_file.unlink()",  # transient update cache
    },
    "update_restart_watcher.py": {
        "path.unlink()",  # validated restart/health beacon in watcher cleanup
        "beacon.unlink()",  # restart beacon
        "beacon.unlink(missing_ok=True)",  # restart beacon
    },
    "repair_cmd.py": set(),  # repair NEVER deletes user data, including sidecars
}


def test_update_and_repair_delete_calls_are_allowlisted():
    source_root = Path(__file__).resolve().parents[2] / "clover_cli"
    for filename, allowed in _ALLOWED_DELETES.items():
        tree = ast.parse((source_root / filename).read_text(encoding="utf-8"))
        seen = Counter()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            if name in {"rmtree", "unlink", "remove"}:
                assert ast.unparse(node) in allowed, f"Unreviewed delete in {filename}:{node.lineno}: {ast.unparse(node)}"
                seen[ast.unparse(node)] += 1
        # A second deletion behind an approved expression must not slip through.
        expected = Counter({expr: 1 for expr in allowed})
        if filename == "update_cmd.py":
            expected.update({"response_path.unlink(missing_ok=True)": 2,
                             "prompt_path.unlink(missing_ok=True)": 1})
        elif filename == "update_restart_watcher.py":
            expected.update({"beacon.unlink(missing_ok=True)": 2})
        assert seen == expected, f"Deletion count changed in {filename}: {seen - expected}"
