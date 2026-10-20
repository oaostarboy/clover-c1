"""Synthetic multiplex routing seams; real authenticated Telegram ingress/command."""
import asyncio
import pytest
from tests.gateway.test_emotions_independent_review import host  # noqa: F401


@pytest.mark.parametrize('secondary', [False, True])
def test_command_binds_resolved_runtime_home_before_profile_scope_switch(host, monkeypatch, tmp_path, secondary):
    from clover_constants import set_clover_home_override, reset_clover_home_override
    launch = tmp_path/'synthetic-launch-home'
    launch.mkdir()
    (launch/'config.yaml').write_text('presence:\n  enabled: false\n')
    runner = host.runner
    monkeypatch.setattr(runner.config, 'multiplex_profiles', True)
    monkeypatch.setattr(host.adapter, 'gateway_runner', runner)
    monkeypatch.setattr(runner, '_profile_name_for_source', lambda src: None if secondary else 'synthetic-runtime')
    monkeypatch.setattr(runner, '_resolve_profile_home_for_source', lambda src: host.home)
    if secondary:
        host.adapter.set_owner_profile('synthetic-runtime')
    captured = []
    monkeypatch.setattr(host.adapter, '_enqueue_text_event', captured.append)
    token = set_clover_home_override(launch)
    try:
        asyncio.run(host.adapter._handle_text_message(host.update('/emotions', message_id=201), object()))
    finally:
        reset_clover_home_override(token)
    assert len(captured) == 1
    event = captured[0]
    # The existing secondary host wrapper stamps only when ingress left it
    # empty. Binding at mint time must resolve the same namespace, not weaken
    # immutable audience/actor checks to tolerate arbitrary later retargeting.
    if secondary and event.source.profile is None:
        event.source.profile = 'synthetic-runtime'
    text = asyncio.run(runner._handle_emotions_command(event))
    assert 'Emotions: enabled' in text, text
    assert 'confidence=' not in text and not host.requests
