"""H3 regression: a string platform_toolsets entry must not widen permissions.

`platform_toolsets: {telegram: clover-acp}` is the obvious way to write a single
toolset. It used to fail an isinstance(list) check and fall through to the
PLATFORM DEFAULT — granting more tools than the user asked for, including
terminal and file access, with no warning at all.

A permission boundary that fails OPEN is worse than one that errors. This is
Alfred's category: he is the security agent.
"""
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from clover_cli.tools_config import _get_platform_tools  # noqa: E402


def _names(cfg, platform="telegram"):
    return _get_platform_tools(cfg, platform)


def test_string_entry_is_honoured_not_ignored():
    as_string = _names({"platform_toolsets": {"telegram": "clover-acp"}})
    as_list = _names({"platform_toolsets": {"telegram": ["clover-acp"]}})
    assert as_string == as_list, (
        f"string form resolved to {sorted(as_string)} but list form gave "
        f"{sorted(as_list)} — the string was ignored and the platform default "
        "was granted instead"
    )


def test_string_entry_does_not_grant_the_default():
    """The actual security claim: string != default."""
    restricted = _names({"platform_toolsets": {"telegram": "clover-acp"}})
    default = _names({})
    assert restricted != default, (
        "a restricted string config resolved to the FULL platform default — "
        "this silently widens permissions"
    )


def test_empty_string_falls_back_to_default_not_nothing():
    got = _names({"platform_toolsets": {"telegram": "   "}})
    assert got, "an empty value should fall back to the default, not to no tools"


def test_list_form_unchanged():
    got = _names({"platform_toolsets": {"telegram": ["clover-acp"]}})
    assert got


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
        except Exception as e:
            fails += 1
            print(f"[ERROR] {name}: {type(e).__name__}: {e}")
    print("\n=== verdict ===")
    print("[PASS] string toolset config no longer widens permissions" if not fails
          else f"[FAIL] {fails} failing")
    sys.exit(1 if fails else 0)
