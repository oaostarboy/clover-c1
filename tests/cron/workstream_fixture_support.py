"""External discovery boundaries for the text-only workstream runtime tests."""
import pytest


def watcher_until(h, predicate, timeout=15):
    from tests.cron.test_workstream_dm_scope import watcher_until as original

    # Delivery means adapter acceptance, not completion of the synthetic turn.
    return original(h, lambda: predicate() and not h.adapter._active_sessions, timeout)


@pytest.fixture(autouse=True)
def scripted_vision_discovery(monkeypatch):
    # Tool availability probes run before call_llm and can consult models.dev
    # and local-server capability endpoints. Scripting text providers alone
    # cannot isolate this independent route. Keep the socket guard intact.
    import agent.auxiliary_client as auxiliary

    monkeypatch.setattr(auxiliary, 'resolve_vision_provider_client',
                        lambda *args, **kwargs: (None, None, None))
