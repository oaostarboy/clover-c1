"""Every top-level module the installed package imports must be listed in
``[tool.setuptools] py-modules``.

A module missing from that list works from a source checkout (the repo root is
on ``sys.path``) but is absent from wheels and from setuptools' editable
finder, so ``import clover_state`` fails with ModuleNotFoundError as soon as
Clover runs from any other working directory (services, the TUI backend,
eval harnesses). This test closes the class, not one instance.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Top-level scripts that are entry points or dev tools, never imported by the
# installed package.
_NOT_INSTALLED = {"setup", "mini_swe_runner"}


def _listed_py_modules() -> set[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"^py-modules\s*=\s*\[(.*?)\]", text, re.S | re.M)
    assert m, "py-modules list not found in pyproject.toml"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _top_level_modules() -> set[str]:
    return {p.stem for p in ROOT.glob("*.py")} - _NOT_INSTALLED


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_every_top_level_module_imported_by_a_listed_module_is_listed():
    listed = _listed_py_modules()
    top = _top_level_modules()
    missing: dict[str, list[str]] = {}
    for mod in sorted(listed):
        path = ROOT / f"{mod}.py"
        if not path.exists():
            continue
        for dep in _imports_of(path) & top:
            if dep not in listed:
                missing.setdefault(dep, []).append(mod)
    assert not missing, f"top-level modules imported but not in py-modules: {missing}"


def test_every_top_level_module_is_listed_or_explicitly_excluded():
    unlisted = _top_level_modules() - _listed_py_modules()
    assert not unlisted, (
        f"add to [tool.setuptools] py-modules (or to _NOT_INSTALLED if it is a "
        f"dev-only script): {sorted(unlisted)}"
    )
