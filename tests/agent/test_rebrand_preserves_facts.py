"""H5 regression: migration must not rewrite an agent's factual memory.

`rebrand_text` blanket-replaced OpenClaw/ClawdBot/MoltBot with Clover across
persona files AND memory entries. But an agent's notes legitimately DISCUSS the
old harness: "the OpenClaw gateway on port 18789 crash-looped", "Alfred runs
OpenClaw 2026.7.1-2". Rewriting those produces confident false statements the
agent cannot detect. For a security agent whose value is an accurate record,
that is corruption wearing a rename's clothes.
"""
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
MIG = Path(ROOT) / "optional-skills" / "migration" / "openclaw-migration" / "scripts"
for p in (ROOT, str(MIG)):
    if p not in sys.path:
        sys.path.insert(0, p)

from openclaw_to_clover import rebrand_text, rebrand_identity_only  # noqa: E402


def test_factual_memory_is_left_alone():
    fact = "The OpenClaw gateway on port 18789 crash-looped 47 times."
    assert rebrand_identity_only(fact) == fact, (
        "a factual note about OpenClaw was rewritten — the agent now believes "
        "something that never happened"
    )


def test_version_strings_survive():
    fact = "Alfred runs OpenClaw 2026.7.1-2 with a user-scoped node."
    assert "OpenClaw 2026.7.1-2" in rebrand_identity_only(fact)


def test_identity_line_is_rebranded():
    line = "You are running on OpenClaw."
    out = rebrand_identity_only(line)
    assert "Clover" in out, "the identity line should adopt the new harness name"


def test_mixed_document():
    doc = (
        "You are running on OpenClaw.\n"
        "Note: the OpenClaw incident on 2026-02-21 was a port collision.\n"
    )
    out = rebrand_identity_only(doc)
    assert "Clover" in out.splitlines()[0]
    assert "OpenClaw incident" in out, "the historical fact was corrupted"


def test_plain_rebrand_still_available_for_paths():
    """rebrand_text keeps its old behaviour for configs and paths."""
    assert "clover" in rebrand_text("~/.openclaw/workspace").lower()


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"[PASS] {name}")
        except AssertionError as e:
            fails += 1
            print(f"[FAIL] {name}\n       {e}")
    print("\n=== verdict ===")
    print("[PASS] memory keeps its facts" if not fails else f"[FAIL] {fails} failing")
    sys.exit(1 if fails else 0)
