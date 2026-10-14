"""Owner-authenticated DM metadata through real Telegram ingress and SQLite.

The sole test-only wiring is the documented additive parent prologue hook;
no main admission, SessionDB, gateway watcher or executor is replaced.
"""
import json
from dataclasses import asdict

import pytest

from agent import workstream_human_hooks as hooks, workstream_job_scope as adapter
from agent import workstreams as ws
from cron import jobs, notepad as np
from tests.gateway.test_trusted_human_origin_ingress import Harness, network_guard  # noqa: F401


@pytest.fixture
def dm(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=True)
    real = hooks.consume_persisted_turn
    h.admissions = []
    h.agents = []

    def composed(agent, *, message, row_id):
        result = adapter.consume_persisted_turn(agent, message=message, row_id=row_id)
        h.admissions.append(result)
        h.agents.append(agent)
        return real(agent, message=message, row_id=row_id)

    monkeypatch.setattr(hooks, "consume_persisted_turn", composed)
    scope = ws.scope_for_surface(platform="telegram", chat_type="dm", chat_id="12345", user_id="12345")
    blocker = np.create_workstream(scope, kind="blocker", title="CI gate", summary="wait for CI", fingerprint="ci")
    h.scope, h.blocker = scope, blocker["workstream_id"]
    yield h
    h.close()


def request(h, **changes):
    data = dict(scope=asdict(h.scope), workstream_id=h.blocker, prompt="watch CI",
                schedule="every 1h", monitor_script="ci-status.sh", approve=True)
    data.update(changes)
    return "workstream-monitor: " + json.dumps(data)


def test_authenticated_dm_creation_preserves_the_exact_scope(dm):
    dm.inbound_human(request(dm))
    result = dm.admissions[-1]
    assert result.handled, result.reason
    job = jobs.get_job(result.job_id)
    assert ws.job_scope(job) == dm.scope
    assert dm.scope.audience == "private:12345" and dm.scope.root == "12345"
    assert job["workstream_scope"]["scope"] == asdict(dm.scope)
    assert job["workstream_scope"]["owner"]["session_id"] == dm.session_id()
    assert job["deliver"] == "local"
