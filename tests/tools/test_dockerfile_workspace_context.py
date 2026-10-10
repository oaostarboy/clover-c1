"""Every root npm workspace that an image-built package depends on must be
copied into the Docker build context before ``npm install`` runs.

Regression: ``web`` depends on ``@clover/ui`` (workspace ``packages/clover-ui``)
but the Dockerfile never copied it, so ``npm run build`` in ``web`` failed with
TS2307 on every ``@clover/ui/...`` import.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILT_IN_IMAGE = ("web", "ui-tui")


def _workspace_dirs_by_name() -> dict[str, str]:
    out: dict[str, str] = {}
    for base in (REPO_ROOT / "packages").iterdir():
        pkg = base / "package.json"
        if pkg.is_file():
            out[json.loads(pkg.read_text(encoding="utf-8"))["name"]] = (
                base.relative_to(REPO_ROOT).as_posix()
            )
    return out


def test_dockerfile_copies_workspace_packages_before_npm_install() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    install_at = dockerfile.index("RUN npm install")
    before_install = dockerfile[:install_at]
    workspaces = _workspace_dirs_by_name()
    needed: set[str] = set()
    for pkg_dir in BUILT_IN_IMAGE:
        manifest = json.loads(
            (REPO_ROOT / pkg_dir / "package.json").read_text(encoding="utf-8")
        )
        for section in ("dependencies", "devDependencies"):
            for name in manifest.get(section, {}):
                if name in workspaces:
                    needed.add(workspaces[name])
    assert needed, "expected at least one packages/* workspace dependency"
    for rel in sorted(needed):
        pattern = rf"^COPY\s+(?:--\S+\s+)*{re.escape(rel)}/?\s+{re.escape(rel)}/?\s*$"
        assert re.search(pattern, before_install, re.M), (
            f"Dockerfile must COPY {rel}/ before `npm install`"
        )
        # The tracked workspace must not be dropped by .dockerignore.
        ignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        assert not re.search(rf"^/?{re.escape(rel.split('/')[0])}/?\s*$", ignore, re.M)
