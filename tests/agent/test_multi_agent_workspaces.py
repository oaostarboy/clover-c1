"""B4 regression: a multi-agent install must not lose agents in silence.

WHAT WENT WRONG
`source_candidate` returns the FIRST workspace it finds. In an install holding
`workspace-alfred/` and `workspace-oracle/`, one agent got migrated and the
other produced no record at all: not an error, not a warning, not even a
"skipped" line naming it. A migration could report success having transferred
half of a two-agent production install, and the operator would find out when an
agent woke up as somebody else.

The fix does not try to migrate every agent in one pass, which would be a much
larger change with worse failure modes. It makes the omission LOUD: enumerate
the workspaces that hold an identity, name the one being migrated, and record
an error listing the ones that are not.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SCRIPT = (
    ROOT / "optional-skills" / "migration" / "openclaw-migration"
    / "scripts" / "openclaw_to_clover.py"
)
_spec = importlib.util.spec_from_file_location("o2c_b4", _SCRIPT)
o2c = importlib.util.module_from_spec(_spec)
sys.modules["o2c_b4"] = o2c
_spec.loader.exec_module(o2c)


def _migrator(source: Path):
    return o2c.Migrator(
        source_root=source,
        target_root=Path(tempfile.mkdtemp(prefix="b4-target-")),
        execute=False,
        workspace_target=None,
        overwrite=False,
        migrate_secrets=False,
        output_dir=Path(tempfile.mkdtemp(prefix="b4-out-")),
    )


def _install(**workspaces: dict) -> Path:
    """Build a fake source install. Maps workspace name -> {filename: text}."""
    root = Path(tempfile.mkdtemp(prefix="b4-src-"))
    for name, files in workspaces.items():
        name = name.replace("__", "-")
        d = root / name
        d.mkdir(parents=True)
        for fname, text in files.items():
            (d / fname).write_text(text)
    (root / "openclaw.json").write_text("{}")
    return root


def test_two_agent_install_records_an_error_naming_the_left_behind_agent():
    src = _install(
        workspace__main={"MEMORY.md": "# main"},
        workspace__alfred={"SOUL.md": "# Alfred", "MEMORY.md": "# Alfred mem"},
        workspace__oracle={"SOUL.md": "# Oracle"},
    )
    mig = _migrator(src)
    mig.warn_about_unmigrated_workspaces()

    hits = [i for i in mig.items if i.kind == "multi-agent-workspaces"]
    assert hits, "a multi-agent install produced no record at all"
    item = hits[0]
    assert item.status == "error", (
        "leaving a production agent behind is an error, not a note"
    )
    assert "workspace-alfred" in item.reason
    assert "workspace-oracle" in item.reason
    assert set(item.details["not_migrated"]) == {"workspace-alfred", "workspace-oracle"}


def test_single_agent_install_stays_quiet():
    """The common case must not grow a scary error."""
    src = _install(workspace={"SOUL.md": "# solo", "MEMORY.md": "# mem"})
    mig = _migrator(src)
    mig.warn_about_unmigrated_workspaces()
    assert not [i for i in mig.items if i.kind == "multi-agent-workspaces"]


def test_workspaces_without_an_identity_are_not_counted_as_agents():
    """An empty or scratch workspace dir is not a lost agent."""
    src = _install(
        workspace={"SOUL.md": "# solo"},
        workspace__scratch={"notes.txt": "junk"},
    )
    mig = _migrator(src)
    mig.warn_about_unmigrated_workspaces()
    assert not [i for i in mig.items if i.kind == "multi-agent-workspaces"]


def test_migrated_workspace_is_named_so_the_operator_knows_which_one_landed():
    src = _install(
        workspace__alfred={"SOUL.md": "# Alfred"},
        workspace__oracle={"SOUL.md": "# Oracle"},
    )
    mig = _migrator(src)
    mig.warn_about_unmigrated_workspaces()
    item = [i for i in mig.items if i.kind == "multi-agent-workspaces"][0]
    assert item.details["migrated"] in {"workspace-alfred", "workspace-oracle"}
    assert item.details["migrated"] not in item.details["not_migrated"]


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"[PASS] {name}")
        except AssertionError as exc:
            fails += 1
            print(f"[FAIL] {name}\n       {exc}")
    print("\n=== verdict ===")
    print("[PASS] no agent is lost quietly" if not fails else f"[FAIL] {fails} failing")
    sys.exit(1 if fails else 0)
