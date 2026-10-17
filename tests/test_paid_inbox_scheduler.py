"""Existing scheduler path invokes only the owned bounded script, never AIAgent."""
import pytest
from tests.test_paid_inbox_runner import InputBinding, accepted
from tests.gateway.test_proactive_command import home  # noqa: F401


def test_real_no_agent_scheduler_path_exercises_accepted_job(home,monkeypatch):
    from cron import scheduler,inbox_watcher as w
    b=InputBinding(home); job=accepted(home,b)
    import gateway.inbox_watcher_controls as controls
    monkeypatch.setattr(controls,'binding_for_job',lambda *args:b)
    # Script seam supplies time only in the fake binding test, not runtime config.
    monkeypatch.setattr(w.time,'time',lambda:172800)
    ok,doc,reply,error=scheduler.run_job(job)
    assert b.reads==1, 'registered script job did not reach the bounded runner'
    assert ok and not reply and error is None
    b.client.close()
