"""Installed official optional skills follow the repo copy when untouched.

Optional skills are installed once (``clover skills install official/...``)
and nothing used to refresh them, so a fix to an optional skill's
``scripts/`` / ``references/`` / ``SKILL.md`` never reached existing installs
(found live: an updated ``council`` skill left the old scripts in place, so
councils launched by the agent got no card).
"""

import json
import logging
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.skills_sync import _lock_style_hash, sync_skills

INSTALL_PATH = "autonomous-ai-agents/council"


def _write_skill(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


V1 = {
    "SKILL.md": "---\nname: council\n---\n# council v1\n",
    "scripts/council_run.py": "print('v1')\n",
    "references/models.json": '{"v": 1}\n',
    "references/old_notes.md": "shipped in v1 only\n",
}
V2 = {
    "SKILL.md": "---\nname: council\n---\n# council v2\n",
    "scripts/council_run.py": "print('v2')\n",
    "references/models.json": '{"v": 2}\n',
}


class Env:
    def __init__(self, tmp_path: Path):
        self.bundled = tmp_path / "skills"
        self.bundled.mkdir()
        self.optional = tmp_path / "optional-skills"
        self.skills = tmp_path / "home" / "skills"
        self.skills.mkdir(parents=True)
        self.manifest = self.skills / ".bundled_manifest"
        self.lock = self.skills / ".hub" / "lock.json"
        self.dest = self.skills / INSTALL_PATH
        self.src = self.optional / INSTALL_PATH

    def ship(self, files):
        """Put a version of the skill into the repo's optional-skills tree."""
        if self.src.exists():
            import shutil
            shutil.rmtree(self.src)
        _write_skill(self.src, files)

    def install(self, files, *, record=True):
        """Install a version of the skill, as `clover skills install` does."""
        _write_skill(self.dest, files)
        if record:
            self.lock.parent.mkdir(parents=True, exist_ok=True)
            self.lock.write_text(json.dumps({"version": 1, "installed": {"council": {
                "source": "official",
                "identifier": f"official/{INSTALL_PATH}",
                "install_path": INSTALL_PATH,
                "content_hash": _lock_style_hash(self.dest),
                "files": sorted(files),
            }}}))

    def sync(self):
        with ExitStack() as stack:
            stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=self.bundled))
            stack.enter_context(patch("tools.skills_sync._get_optional_dir", return_value=self.optional))
            stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", self.skills))
            stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", self.manifest))
            return sync_skills(quiet=True, refresh_optional=True)

    def tree(self, root=None):
        root = root or self.dest
        return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
                for p in sorted(root.rglob("*")) if p.is_file()}

    def lock_entry(self):
        return json.loads(self.lock.read_text(encoding="utf-8"))["installed"]["council"]

    def backups(self):
        root = self.skills / ".restore-backups"
        return sorted(root.glob("official-optional-refresh-*")) if root.exists() else []


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_untouched_old_copy_is_refreshed_including_scripts_and_references(env):
    env.install(V1)
    env.ship(V2)

    result = env.sync()

    assert result["optional_refreshed"] == ["council"]
    assert env.tree() == V2  # SKILL.md, scripts/, references/ all current; v1-only file gone
    # nothing is deleted: the previous version is kept in a backup
    [backup] = env.backups()
    assert env.tree(backup / INSTALL_PATH) == V1
    # the lock now records the refreshed content, so the copy still counts as untouched
    assert env.lock_entry()["content_hash"] == _lock_style_hash(env.dest)
    assert sorted(env.lock_entry()["files"]) == sorted(V2)


def test_only_a_scripts_or_references_change_is_still_picked_up(env):
    env.install(V1)
    env.ship({**V1, "scripts/council_run.py": "print('fixed')\n"})
    assert env.sync()["optional_refreshed"] == ["council"]
    assert (env.dest / "scripts/council_run.py").read_text(encoding="utf-8") == "print('fixed')\n"

    env.ship({**V1, "scripts/council_run.py": "print('fixed')\n", "references/models.json": '{"v": 3}\n'})
    assert env.sync()["optional_refreshed"] == ["council"]
    assert (env.dest / "references/models.json").read_text(encoding="utf-8") == '{"v": 3}\n'


def test_refresh_is_idempotent(env):
    env.install(V1)
    env.ship(V2)
    env.sync()
    again = env.sync()
    assert again["optional_refreshed"] == []
    assert again["optional_user_modified"] == []
    assert len(env.backups()) == 1


def test_user_edited_copy_is_left_alone_and_logged_once(env, caplog):
    env.install(V1)
    (env.dest / "scripts/council_run.py").write_text("print('my tweak')\n", encoding="utf-8")
    env.ship(V2)

    with caplog.at_level(logging.INFO, logger="tools.skills_sync"):
        result = env.sync()

    assert result["optional_refreshed"] == []
    assert result["optional_user_modified"] == ["council"]
    assert (env.dest / "scripts/council_run.py").read_text(encoding="utf-8") == "print('my tweak')\n"
    assert env.tree()["SKILL.md"] == V1["SKILL.md"]
    assert env.backups() == []
    lines = [r.getMessage() for r in caplog.records if "council" in r.getMessage()]
    assert len(lines) == 1 and "local edits" in lines[0]


def test_extra_user_file_counts_as_an_edit(env):
    env.install(V1)
    (env.dest / "scripts/mine.py").write_text("print('mine')\n", encoding="utf-8")
    env.ship(V2)
    result = env.sync()
    assert result["optional_user_modified"] == ["council"]
    assert (env.dest / "scripts/mine.py").exists()
    assert env.tree()["SKILL.md"] == V1["SKILL.md"]


def test_generated_bytecode_is_not_an_edit(env):
    env.install(V1)
    cache = env.dest / "scripts" / "__pycache__"
    cache.mkdir()
    (cache / "council_run.cpython-312.pyc").write_bytes(b"\x00\x01")
    env.ship(V2)
    assert env.sync()["optional_refreshed"] == ["council"]
    assert env.tree() == V2


@pytest.mark.parametrize("extra", [".git/config", ".github/workflows/local.yml", ".archive/old.txt", "venv/local.txt"])
def test_extra_files_in_previously_excluded_dirs_are_user_edits(env, extra):
    env.install(V1)
    path = env.dest / extra
    path.parent.mkdir(parents=True)
    path.write_text("keep me", encoding="utf-8")
    env.ship(V2)
    assert env.sync()["optional_user_modified"] == ["council"]
    assert path.read_text(encoding="utf-8") == "keep me"


def test_crlf_in_installed_copy_does_not_count_as_user_edit(env):
    env.install(V1)
    for path in env.dest.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    env.ship(V2)
    assert env.sync()["optional_refreshed"] == ["council"]


def test_plain_gateway_sync_does_not_refresh_optional_skill(env):
    env.install(V1)
    env.ship(V2)
    with ExitStack() as stack:
        stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=env.bundled))
        stack.enter_context(patch("tools.skills_sync._get_optional_dir", return_value=env.optional))
        stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", env.skills))
        stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", env.manifest))
        result = sync_skills(quiet=True)
    assert result["optional_refreshed"] == []
    assert env.tree() == V1


def test_restore_backup_tree_is_excluded_from_skill_discovery(env):
    from agent.skill_utils import is_excluded_skill_path
    from tools.skills_sync import _index_installed_skill_dirs_by_name

    old = env.skills / ".restore-backups" / "old" / INSTALL_PATH
    _write_skill(old, V1)
    assert is_excluded_skill_path(old / "SKILL.md")
    assert "council" not in _index_installed_skill_dirs_by_name()


def test_restore_backups_keep_three_per_skill(env):
    from tools.skills_sync import _prune_optional_restore_backups

    root = env.skills / ".restore-backups"
    for n in range(5):
        _write_skill(root / f"official-optional-refresh-2026010{n}" / INSTALL_PATH, V1)
    _prune_optional_restore_backups(root, Path(INSTALL_PATH))
    assert len(list(root.glob("official-optional-refresh-*/" + INSTALL_PATH))) == 3


def test_skill_that_is_not_installed_is_not_installed_by_update(env):
    env.ship(V2)
    result = env.sync()
    assert result["optional_refreshed"] == []
    assert not env.dest.exists()


def test_removed_installed_skill_is_not_reinstalled(env):
    """Lock says installed, but the user deleted the directory."""
    env.install(V1)
    import shutil
    shutil.rmtree(env.dest)
    env.ship(V2)
    result = env.sync()
    assert result["optional_refreshed"] == []
    assert not env.dest.exists()


def test_unrecorded_manual_copy_is_never_overwritten(env):
    """No lock entry -> we cannot prove the copy is an untouched shipped version."""
    env.install(V1, record=False)
    env.ship(V2)
    result = env.sync()
    assert result["optional_refreshed"] == []
    assert env.tree() == V1


def test_copy_refreshed_by_hand_gets_its_lock_repaired_without_a_backup(env):
    env.install(V1)
    env.ship(V2)
    _write_skill(env.dest, V2)          # someone copied v2 in manually
    (env.dest / "references/old_notes.md").unlink()
    result = env.sync()
    assert result["optional_refreshed"] == []
    assert env.backups() == []
    assert env.lock_entry()["content_hash"] == _lock_style_hash(env.dest)
    # ...so the NEXT repo change is still recognised as an untouched copy
    env.ship({**V2, "scripts/council_run.py": "print('v3')\n"})
    assert env.sync()["optional_refreshed"] == ["council"]
    assert (env.dest / "scripts/council_run.py").read_text(encoding="utf-8") == "print('v3')\n"


def test_hub_installed_non_official_skills_are_ignored(env):
    env.install(V1)
    data = json.loads(env.lock.read_text(encoding="utf-8"))
    data["installed"]["council"]["source"] = "github"
    env.lock.write_text(json.dumps(data), encoding="utf-8")
    env.ship(V2)
    assert env.sync()["optional_refreshed"] == []
    assert env.tree() == V1
