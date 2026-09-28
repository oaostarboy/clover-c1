"""delegate_task pinning + subagent card fallback labeling (#93412).

Two things this closes:

  1. A ``delegate_task`` per-task tier resolution or a ``delegation.model``
     config pin is an explicit model choice, same as ``clover -z -m`` --
     it must set ``model_pinned=True`` on the child ``AIAgent`` so a
     model_not_found on that pinned model aborts instead of silently
     walking the parent's configured fallback chain. Plain inheritance
     (no override at all) must NOT be pinned.
  2. The subagent/job card's ``model`` field must show "Y (fallback from
     X)" once the live child has actually switched, using the same
     ``_provider_fallback_active`` / ``_primary_runtime`` bookkeeping
     ``try_activate_fallback`` already maintains -- no new plumbing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tools.delegate_tool import _build_child_agent, _model_label_for_child

from tests.tools.test_delegate_tiers import _StubChild, _StubParent, _run_child


def _make_mock_parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.model = "parent-default-model"
    return parent


class TestBuildChildAgentModelPinned:
    def test_explicit_model_override_pins_the_child(self):
        parent = _make_mock_parent()
        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="do the thing",
                context=None,
                toolsets=None,
                model="anthropic/claude-opus-5-5",
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
                role="leaf",
            )
        _, kwargs = MockAgent.call_args
        assert kwargs["model_pinned"] is True
        assert kwargs["model"] == "anthropic/claude-opus-5-5"

    def test_plain_inheritance_is_not_pinned(self):
        """No override at all (model=None) -- the child just inherits the
        parent's model; that is NOT an explicit pin."""
        parent = _make_mock_parent()
        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="do the thing",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
                role="leaf",
            )
        _, kwargs = MockAgent.call_args
        assert kwargs["model_pinned"] is False
        assert kwargs["model"] == parent.model


class TestModelLabelForChild:
    def test_no_fallback_returns_the_plain_model(self):
        child = MagicMock()
        child.model = "claude-opus-5-5"
        child._provider_fallback_active = False
        assert _model_label_for_child(child) == "claude-opus-5-5"

    def test_fallback_active_labels_from_and_to(self):
        child = MagicMock()
        child.model = "claude-opus-5-5"
        child._provider_fallback_active = True
        child._primary_runtime = {"model": "gemini-3.8-pro"}
        assert (
            _model_label_for_child(child)
            == "claude-opus-5-5 (fallback from gemini-3.8-pro)"
        )

    def test_fallback_flag_set_but_same_model_is_not_relabeled(self):
        """Defensive: don't label 'X (fallback from X)' if the primary
        snapshot happens to match the current model."""
        child = MagicMock()
        child.model = "claude-opus-5-5"
        child._provider_fallback_active = True
        child._primary_runtime = {"model": "claude-opus-5-5"}
        assert _model_label_for_child(child) == "claude-opus-5-5"

    def test_no_model_returns_none(self):
        child = MagicMock()
        child.model = None
        assert _model_label_for_child(child) is None


class TestSubagentCardShowsFallbackLabel:
    """End-to-end through _run_single_child's own result-entry
    construction, reusing the lightweight stub from test_delegate_tiers.py
    (mirrors TestResultEntryModel there)."""

    def test_completed_entry_model_shows_fallback_label(self):
        child = _StubChild(["done"])
        child.model = "claude-opus-5-5"
        child._provider_fallback_active = True
        child._primary_runtime = {"model": "gemini-3.8-pro"}
        entry = _run_child(child)
        assert entry["model"] == "claude-opus-5-5 (fallback from gemini-3.8-pro)"

    def test_completed_entry_model_plain_when_no_fallback(self):
        child = _StubChild(["done"])
        child.model = "claude-opus-5-5"
        entry = _run_child(child)
        assert entry["model"] == "claude-opus-5-5"
