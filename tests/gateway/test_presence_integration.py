"""Real Telegram ingress -> agent -> durable W1/W2/W3 -> adapter-owned turn.

Synthetic HTTP provider and Telegram transport only. All networking is blocked.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import openai
import yaml
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.gateway.test_workstream_suggestion_offer import ACTIVE, OfferHarness
from agent import auxiliary_client as aux
from cron.presence import ThoughtStore


def setup_host(monkeypatch, tmp_path):
    h = OfferHarness(monkeypatch, tmp_path, proactive=ACTIVE)
    (h.home / 'config.yaml').write_text(yaml.safe_dump({
        'agent': {'disabled_toolsets': ['terminal', 'browser']},
        'display': {'tool_progress': 'off', 'thinking_progress': False},
        'compression': {'enabled': False},
        'proactive': ACTIVE,
        'presence': {'enabled': True},
        'auxiliary': {
            'background_review': {'enabled': False},
            'free_only': True,
            'presence': {'provider': 'openai', 'model': 'synthetic-model'},
        },
    }))
    monkeypatch.setattr('agent.image_routing._lookup_supports_vision', lambda *a, **kw: False)
    requests = []
    async def transport(request):
        requests.append(json.loads(request.content))
        content = ({'affect': {'valence': -0.8, 'arousal': 0.8, 'confidence': 0.9,
                               'posture': 'careful_brief'}, 'thought': None}
                   if 'valence' in str(json.loads(request.content)['messages'][0]) else {
                       'connection': 'Deployment packaging can verify manifest checksum before publishing.',
                       'evidence_indices': [0], 'confidence': 0.9})
        return httpx.Response(200, json={'id':'synthetic', 'object':'chat.completion', 'created':0,
            'model':'synthetic-model', 'choices':[{'index':0,'finish_reason':'stop','message':{
                'role':'assistant','content':json.dumps(content)}}]})
    def resolve(*a, **kw):
        return (openai.AsyncOpenAI(api_key='synthetic-key', base_url='https://api.openai.com/v1',
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))), 'synthetic-model')
    monkeypatch.setattr(aux, 'resolve_provider_client', resolve)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))
    h.adapter._bot = bot
    return h, requests, bot


import pytest
import asyncio
from gateway.platforms.base import SendResult


def native_command(h, text, message_id):
    expected = h.adapter.send.await_count+1
    async def invoke():
        await h.adapter._handle_text_message(h.update(text, message_id=message_id), object())
        deadline = asyncio.get_running_loop().time()+5
        while h.adapter.send.await_count < expected:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
    asyncio.run(invoke())
    sent = h.adapter.send.await_args
    return sent.args[1] if len(sent.args) > 1 else sent.kwargs['content']


def test_quiet_hours_and_managed_invalidity_make_zero_auxiliary_requests(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        config = yaml.safe_load((h.home/'config.yaml').read_text())
        config['presence']['quiet_hours'] = {'start_utc':0,'end_utc':24}
        (h.home/'config.yaml').write_text(yaml.safe_dump(config))
        h.inbound_human('Help verify deployment packaging', message_id=101)
        assert not requests and bot.send_message.await_count == 0, 'quiet hours dispatched inference'
        from clover_cli.managed_scope import ManagedPolicyError
        def invalid(*a, **kw):
            raise ManagedPolicyError('synthetic invalid managed policy')
        monkeypatch.setattr('clover_cli.managed_scope.load_managed_config', invalid)
        h.inbound_human('I am frustrated. Help verify deployment packaging', message_id=102)
        assert not requests and bot.send_message.await_count == 0
    finally:
        h.close()


def test_generic_stop_revokes_presence_without_a_later_replay(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        native_command(h, '/stop', 101)
        assert ThoughtStore().initiative_status()[1] is True, 'generic Stop missed durable presence epoch'
        h.inbound_human('Help verify deployment packaging', message_id=102)
        assert not requests and bot.send_message.await_count == 0
        assert h.results[-1]['final_response'] == 'ack'
    finally:
        h.close()


def test_config_off_then_on_invalidates_old_thoughts_without_replaying(monkeypatch, tmp_path):
    from clover_cli.config import save_config
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        config = yaml.safe_load((h.home/'config.yaml').read_text())
        config['presence']['enabled'] = False
        save_config(config, strip_defaults=False)
        assert ThoughtStore().initiative_status()[1] is True, 'config write missed revocation'
        config['presence']['enabled'] = True
        save_config(config, strip_defaults=False)
        h.inbound_human('Help verify deployment packaging', message_id=101)
        # Re-enabled ordinary text may now be assessed, but old thoughts never replay.
        assert len(requests) == 1 and 'valence' in str(requests[0]['messages'][0])
        assert not ThoughtStore().publications() and bot.send_message.await_count == 0
    finally:
        h.close()


def test_config_can_disable_corrupt_presence_store_without_repairing_it(monkeypatch, tmp_path):
    from clover_cli.config import save_config
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging', message_id=100)
        store = ThoughtStore()
        corrupt = b'synthetic corrupt presence DB; preserve for diagnosis'
        store.path.write_bytes(corrupt)
        config = yaml.safe_load((h.home/'config.yaml').read_text())
        config['presence']['enabled'] = False
        save_config(config, strip_defaults=False)
        assert yaml.safe_load((h.home/'config.yaml').read_text())['presence']['enabled'] is False
        assert store.path.read_bytes() == corrupt
        assert not requests and bot.send_message.await_count == 0
    finally:
        h.close()


def test_native_inspect_forget_resume_controls_are_scoped(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        status = native_command(h, '/proactive presence status', 101)
        assert 'deployment packaging' in status
        thought_id = next(line.split()[0] for line in status.splitlines() if '[pending]' in line)
        forgotten = native_command(h, '/proactive presence forget '+thought_id, 102)
        assert 'cleared' in forgotten
        resumed = native_command(h, '/proactive presence resume', 103)
        assert 'future thoughts only' in resumed, 'no real resume control'
        assert ThoughtStore().initiative_status() == (2, False)
        h.inbound_human('Help verify deployment packaging', message_id=104)
        assert len(requests) == 1 and 'valence' in str(requests[0]['messages'][0])
        assert not ThoughtStore().publications() and bot.send_message.await_count == 0
    finally:
        h.close()


def test_native_stop_control_revokes_profile_but_keeps_requested_replies(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        async def command():
            await h.adapter._handle_text_message(h.update('/proactive presence stop', message_id=101), object())
            deadline = asyncio.get_running_loop().time()+5
            while h.adapter.send.await_count < 2:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)
        asyncio.run(command())
        assert ThoughtStore().initiative_status() == (1, True), 'Stop did not revoke shared profile epoch'
        h.inbound_human('Help verify deployment packaging', message_id=102)
        assert h.results[-1]['final_response'] == 'ack'
        assert not requests and bot.send_message.await_count == 0
    finally:
        h.close()


@pytest.mark.parametrize('race', ['stop', 'delete', 'revoke', 'input', 'credential', 'desktop_input'])
def test_revocation_after_http_prevents_commit_and_publication(monkeypatch, tmp_path, race):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    observations = []
    real = aux.presence_completion
    async def racing(**kwargs):
        raw = await real(**kwargs)
        if race == 'stop':
            ThoughtStore().stop()
        elif race == 'delete':
            row_id = h.user_rows()[0]['id']
            h.db._execute_write(lambda conn: conn.execute('DELETE FROM messages WHERE id=?', (row_id,)))
        elif race == 'credential':
            h.adapter._bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)))
        elif race == 'desktop_input':
            from tui_gateway import server
            from tests.tui_gateway.test_presence_integration import DesktopTransport
            transport = DesktopTransport()
            session = {'session_key':'desktop-new-input', 'clover_home':str(h.home),
                'cwd':str(tmp_path), 'transport':transport}
            monkeypatch.setattr(server, '_sessions', {'desktop-race':session})
            generation = ThoughtStore().generation()
            submission = server._capture_ws_human_submission({'method':'prompt.submit',
                'params':{'session_id':'desktop-race','text':'New human input'}}, transport)
            observations.append((submission is not None, generation, ThoughtStore().generation()))
        elif race == 'revoke':
            (h.home/'config.yaml').write_text(yaml.safe_dump({'presence':{'enabled':False}, 'proactive':ACTIVE}))
        else:
            h.runner._invalidate_session_run_generation(next(iter(h.runner._session_run_generation)), reason='synthetic-new-input')
        return raw
    monkeypatch.setattr(aux, 'presence_completion', racing)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        h.inbound_human('Help verify deployment packaging', message_id=101)
        if race == 'desktop_input':
            assert len(observations) == 1 and observations[0][0]
            assert observations[0][2] == observations[0][1]+1, 'authenticated input missed shared durable fence'
        assert len(requests) == 1
        assert not ThoughtStore().publications()
        assert bot.send_message.await_count == 0
    finally:
        h.close()


@pytest.mark.parametrize('race', ['delete', 'stop'])
def test_revocation_inside_attempt_fence_prevents_native_publication(monkeypatch, tmp_path, race):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    real = ThoughtStore.attempt
    def attempt(store, *args):
        result = real(store, *args)
        if race == 'stop':
            store.stop()
        else:
            row_id = h.user_rows()[0]['id']
            h.db._execute_write(lambda conn: conn.execute('DELETE FROM messages WHERE id=?', (row_id,)))
        return result
    monkeypatch.setattr(ThoughtStore, 'attempt', attempt)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        h.inbound_human('Help verify deployment packaging', message_id=101)
        assert len(requests) == 1
        assert bot.send_message.await_count == 0, 'deleted evidence reached native publication'
        assert ThoughtStore().publications()[0]['state'] != 'acknowledged'
    finally:
        h.close()


def test_new_input_inside_proposal_owner_fence_rejects_durable_commit(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    real = ThoughtStore.propose
    def propose(store, *args, **kwargs):
        store.advance_generation()
        return real(store, *args, **kwargs)
    monkeypatch.setattr(ThoughtStore, 'propose', propose)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        h.inbound_human('Help verify deployment packaging', message_id=101)
        assert len(requests) == 1
        assert not ThoughtStore().publications(), 'new input committed an obsolete proposal'
        assert bot.send_message.await_count == 0
    finally:
        h.close()


def test_source_correction_inside_proposal_transaction_rejects_commit(monkeypatch, tmp_path):
    from agent.presence_state import ContextKey
    from agent.presence_runtime import clock
    from tests.gateway.test_workstream_suggestion_offer import scope
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    real = ThoughtStore.propose
    def propose(store, *args, **kwargs):
        key = ContextKey(scope(h), h.session_id())
        thought = store.list(key)[0]
        store.invalidate_source(key, thought.source, 'corrected', clock())
        return real(store, *args, **kwargs)
    monkeypatch.setattr(ThoughtStore, 'propose', propose)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        h.inbound_human('Help verify deployment packaging', message_id=101)
        assert len(requests) == 1
        assert not ThoughtStore().publications(), 'corrected thought acquired a stale proposal'
        assert bot.send_message.await_count == 0
    finally:
        h.close()


def test_unknown_native_send_is_held_across_next_event(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    bot.send_message.side_effect = TimeoutError('synthetic unknown transport')
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        h.inbound_human('Help verify deployment packaging', message_id=101)
        assert ThoughtStore().publications()[0]['state'] == 'unknown'
        h.inbound_human('Verify deployment packaging again', message_id=102)
        assert len(requests) == 1 and bot.send_message.await_count == 1
    finally:
        h.close()


def test_failed_requested_send_has_zero_auxiliary_work(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        h.adapter.send.return_value = SendResult(success=False, error='synthetic failure', retryable=False)
        h.inbound_human('Help verify deployment packaging', message_id=101)
        assert not requests and bot.send_message.await_count == 0
    finally:
        h.close()


def test_authenticated_frustration_changes_only_current_turn_compose(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('The previous attempt failed and I am frustrated. Please be brief.', message_id=100)
        wire = h.scripted.requests[-1]['messages']
        assert 'Tentative interaction posture: careful_brief' in wire[-1]['content'], 'no emotional context on real compose'
        assert h.user_rows()[-1]['content'] == 'The previous attempt failed and I am frustrated. Please be brief.'
        assert h.user_rows()[-1]['api_content'] == wire[-1]['content']
        h.inbound_human('Presence: ordinary', message_id=101)
        later = h.scripted.requests[-1]['messages']
        assert json.dumps(later[:len(wire)], sort_keys=True) == json.dumps(wire, sort_keys=True)
        assert 'Tentative interaction posture' not in later[-1]['content']
        assert len(requests) == 1 and bot.send_message.await_count == 0
    finally:
        h.close()


def test_authenticated_after_reply_emits_distinct_adapter_owned_turn(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        h.inbound_human('Help verify deployment packaging', message_id=101)
        assert len(requests) == 1, 'real after-reply path did not assess durable thought'
        assert bot.send_message.await_count == 1, 'no adapter-owned initiative turn'
        assert bot.send_message.await_args.kwargs['text'].startswith('A thought to check:')
        assert h.results[-1]['final_response'] == 'ack'
        assert h.scripted.calls == 2
        assert [r['role'] for r in h.db.get_messages(h.session_id()) if r['role'] != 'session_meta'] == ['user','assistant','user','assistant']
        assert ThoughtStore().publications()[0]['state'] == 'acknowledged'
        h.inbound_human('Verify deployment packaging again', message_id=102)
        assert len(requests) == 1 and bot.send_message.await_count == 1
    finally:
        h.close()
