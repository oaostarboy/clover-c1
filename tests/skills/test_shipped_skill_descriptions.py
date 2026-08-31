"""Shipped skills must pass the validator that ships with them.

Reported from a Windows install on 2026-08-31: creating a skill with a
133-char description was rejected, but an audit of the 61 shipped skills
found one at 64 chars — ``clover-c1``, the product's own documentation
skill. The rule and the first-party content disagreed, which undermines
the constraint every time a user hits it.

The report is the reason this file exists, and the reason it is a TEST
rather than a one-line fix to that skill. A fixed description drifts back
the next time someone edits it. A test fails the build.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.skill_manager_tool import SKILL_PROMPT_DESC_LIMIT

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIRS = [REPO_ROOT / "skills", REPO_ROOT / "optional-skills"]

_DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


def _shipped_skills():
    """Yield (path, description) for every shipped SKILL.md with frontmatter."""
    for root in SKILL_DIRS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("SKILL.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            match = _DESC_RE.search(text)
            if match:
                yield path, match.group(1).strip().strip("'\"")


def test_shipped_skills_were_found():
    """Guard the guard: an empty scan must fail, not silently pass.

    A path typo or a layout change would make every assertion below vacuous
    while the suite still reported green. Silence and success must not look
    identical.
    """
    found = list(_shipped_skills())
    assert len(found) > 20, (
        f"only found {len(found)} shipped skills — the scan is probably "
        f"looking in the wrong place: {[str(d) for d in SKILL_DIRS]}"
    )


@pytest.mark.parametrize(
    "path,description",
    list(_shipped_skills()),
    ids=lambda v: v.parent.name if isinstance(v, Path) else "",
)
def test_shipped_skill_description_fits_the_prompt_budget(path, description):
    """Every shipped description must fit the limit new skills are held to."""
    assert len(description) <= SKILL_PROMPT_DESC_LIMIT, (
        f"{path.relative_to(REPO_ROOT)} has a {len(description)}-char "
        f"description, over the {SKILL_PROMPT_DESC_LIMIT}-char limit the "
        f"validator enforces on new skills:\n  {description!r}\n"
        f"The skill index truncates it, so the routing signal is lost."
    )
