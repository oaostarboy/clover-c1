"""The subagent/job card must show the model actually in use (#93412).

``AgentJobObserver`` (tools/agent_job_observer.py) drives the card for an
externally-registered ``terminal(agent_job=...)`` worker. Before this fix it
only ever recorded a model label ONCE (``not self.model`` gate), so a
mid-run model switch never reached the card:

  - a ``clover -z --activity-events`` worker's explicit ``model.fallback``
    event was ignored entirely;
  - a ``claude -p --output-format stream-json`` worker's own
    ``system/init`` message reporting a *different* model than the label
    passed at launch (the model actually drifted) was also ignored.

Both must relabel the card "Y (fallback from X)".
"""

from __future__ import annotations

import json

from tools.agent_job_observer import AgentJobObserver


class FakeSink:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def observe(self, event_type, tool_name=None, preview=None, args=None, **kw):
        self.calls.append((event_type, tool_name, preview, args, kw))


def _make_observer(parser: str, model: str = "") -> tuple[AgentJobObserver, FakeSink]:
    sink = FakeSink()
    obs = AgentJobObserver(
        session_id="sa-1",
        sink=sink,
        group_id="g1",
        index=0,
        title="job",
        model=model,
        parser=parser,
    )
    return obs, sink


class TestCloverActivityModelFallbackEvent:
    def test_model_fallback_event_relabels_the_card(self):
        obs, sink = _make_observer("clover-activity", model="")
        obs.feed(
            json.dumps(
                {"clover_activity": 1, "event": "start", "model": "gemini-3.8-pro"}
            )
            + "\n"
        )
        assert obs.model == "gemini-3.8-pro"
        obs.feed(
            json.dumps(
                {
                    "clover_activity": 1,
                    "event": "model.fallback",
                    "from": "gemini-3.8-pro",
                    "from_provider": "openai-codex",
                    "to": "claude-opus-5-5",
                    "to_provider": "anthropic",
                    "reason": "model_not_found",
                }
            )
            + "\n"
        )
        assert obs.model == "claude-opus-5-5 (fallback from gemini-3.8-pro)"

    def test_no_fallback_event_keeps_original_label(self):
        obs, sink = _make_observer("clover-activity", model="")
        obs.feed(
            json.dumps(
                {"clover_activity": 1, "event": "start", "model": "claude-opus-5-5"}
            )
            + "\n"
        )
        assert obs.model == "claude-opus-5-5"

    def test_repeated_start_with_same_model_does_not_relabel(self):
        """A duplicate/replayed 'start' naming the SAME model must not be
        mistaken for a fallback."""
        obs, sink = _make_observer("clover-activity", model="claude-opus-5-5")
        obs.feed(
            json.dumps(
                {"clover_activity": 1, "event": "start", "model": "claude-opus-5-5"}
            )
            + "\n"
        )
        assert obs.model == "claude-opus-5-5"


class TestClaudeStreamJsonModelDrift:
    def test_init_reporting_a_different_model_relabels(self):
        obs, sink = _make_observer("claude-stream-json", model="claude-opus-5-5")
        obs.feed(
            json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-opus-5-5"}
            )
            + "\n"
        )
        assert obs.model == "claude-opus-5-5"
        obs.feed(
            json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-haiku-4-5"}
            )
            + "\n"
        )
        assert obs.model == "claude-haiku-4-5 (fallback from claude-opus-5-5)"

    def test_a_second_identical_drift_does_not_re_label(self):
        obs, sink = _make_observer("claude-stream-json", model="claude-opus-5-5")
        obs.feed(
            json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-haiku-4-5"}
            )
            + "\n"
        )
        assert obs.model == "claude-haiku-4-5 (fallback from claude-opus-5-5)"
        obs.feed(
            json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-haiku-4-5"}
            )
            + "\n"
        )
        assert obs.model == "claude-haiku-4-5 (fallback from claude-opus-5-5)"
