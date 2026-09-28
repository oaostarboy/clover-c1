"""Behavioral contract for xurl / x_search routing guidance.

These tests assert structural invariants (required topics + placement of the
routing guidance), not frozen prose snapshots.

Placement contract (July 2026):
- The xurl SKILL must NOT name `x_search` (or any other credential-gated
  surface): the skill loads even when that tool isn't registered, so it must
  describe its own search distinctively in its own terms (raw, engageable
  post objects as the authenticated account).
- Cross-surface routing guidance lives where both surfaces are known to
  exist together: the x_search feature docs, the toolset description, and
  the tools-config setup note.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
XURL_SKILL = REPO_ROOT / "skills" / "social-media" / "xurl" / "SKILL.md"



def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _contains_any(text: str, *needles: str) -> bool:
    lowered = text.lower()
    return any(n.lower() in lowered for n in needles)


def test_xurl_skill_never_names_credential_gated_surfaces():
    """The skill must be self-contained: no cross-references to tools that
    may not be registered (x_search is check_fn-gated on xAI credentials)."""
    lowered = _read(XURL_SKILL).lower()
    assert "x_search" not in lowered
    assert "web_search" not in lowered


def test_xurl_skill_search_is_distinct_standalone():
    """Search must be described so an agent can route correctly even when
    another X search surface exists — raw engageable posts, authenticated."""
    text = _read(XURL_SKILL)
    assert _contains_any(text, "raw post")
    assert _contains_any(text, "authenticated")
    assert _contains_any(text, "engage", "engageable")
    # Distinguish from synthesized-answer surfaces in xurl's own terms.
    assert _contains_any(text, "summarized answer", "summary of a topic")





