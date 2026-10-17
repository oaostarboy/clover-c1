"""Current saved authority, not an enabled job, permits watcher effects."""
import os
from pathlib import Path

import pytest

from tests.test_proactive_picker import picker, home  # noqa: F401
from tests.test_paid_inbox_admission import setup
from tests.gateway.test_proactive_command import event


@pytest.mark.parametrize('value', ['[cronjob]', '"cronjob"', '"[\'cronjob\']"', '" cronjob "', '[all]', '["*"]'])
def test_saved_denial_cannot_mint_fresh_paid_consent(picker, home, monkeypatch, value):
    from cron.inbox_watcher import Denied, Draft, state_path
    from gateway.inbox_watcher_controls import Binding
    from cron.jobs import load_jobs

    runner, _ = setup(picker, monkeypatch)
    (home / 'config.yaml').write_text('agent:\n  disabled_toolsets: ' + value + '\n')
    binding = Binding(runner, event('/proactive watcher', uid='123', chat_id='123').source)
    with pytest.raises((Denied, ValueError)):
        draft = Draft(binding)
        for choice in ['source', 'destination', 'route', 'cadence 30', 'urgency 7', 'limits 2']:
            draft.choose(choice)
        draft.render()
        draft.accept('accept-extra-usage')
    assert not load_jobs() and not state_path(home).exists()


@pytest.mark.parametrize('layer', ['user', 'managed'])
@pytest.mark.parametrize('selection', ['[]', '[file]', '"[\'file\']"'])
def test_current_platform_tool_selection_must_allow_job_control(picker, home, monkeypatch, tmp_path, layer, selection):
    from cron.inbox_watcher import Denied, Draft, state_path
    from gateway.inbox_watcher_controls import Binding
    from cron.jobs import load_jobs

    runner, _ = setup(picker, monkeypatch)
    path = home / 'config.yaml'
    if layer == 'managed':
        directory = tmp_path / 'managed'
        directory.mkdir()
        monkeypatch.setenv('CLOVER_MANAGED_DIR', str(directory))
        path = directory / 'config.yaml'
    path.write_text('platform_toolsets:\n  telegram: ' + selection + '\n')
    binding = Binding(runner, event('/proactive watcher', uid='123', chat_id='123').source)
    with pytest.raises((Denied, ValueError)):
        Draft(binding)
    assert not load_jobs() and not state_path(home).exists()


def test_binding_cannot_read_another_profiles_authorization(picker, home, monkeypatch, tmp_path):
    from cron.inbox_watcher import Denied
    from gateway.inbox_watcher_controls import Binding
    from clover_constants import get_clover_home

    runner, _ = setup(picker, monkeypatch)
    binding = Binding(runner, event('/proactive watcher', uid='123', chat_id='123').source)
    binding.snapshot()
    other = tmp_path / 'other-profile'
    other.mkdir()
    monkeypatch.setenv('CLOVER_HOME', str(other))
    assert get_clover_home().resolve() == other.resolve()
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(Denied):
        binding.snapshot()
    assert other / 'config.yaml' not in reads


def test_bad_saved_policy_does_not_log_its_private_parser_text(picker, home, monkeypatch, caplog):
    from gateway.inbox_watcher_controls import Binding

    runner, _ = setup(picker, monkeypatch)
    path = home / 'config.yaml'
    path.write_text('PRIVATE-WATCHER-POLICY-CANARY: [invalid')
    binding = Binding(runner, event('/proactive watcher', uid='123', chat_id='123').source)
    with pytest.raises(Exception):
        binding.snapshot()
    assert 'PRIVATE-WATCHER-POLICY-CANARY' not in caplog.text
    assert not list(home.glob('*.corrupt.*'))


def replace_same_stamp(path, text):
    before = path.stat()
    assert before.st_size == len(text.encode())
    replacement = path.with_suffix('.replacement')
    replacement.write_text(text)
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_size == before.st_size


@pytest.fixture(params=['user', 'managed'])
def runtime(request, picker, home, monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    import httpx
    from cron import inbox_watcher as w, jobs
    from clover_cli import config
    from gateway.config import Platform
    from gateway.inbox_watcher_controls import Binding
    from tests.test_paid_inbox_runner import accepted

    runner, _ = setup(picker, monkeypatch)
    path = home / 'config.yaml'
    if request.param == 'managed':
        directory = tmp_path / 'managed'
        directory.mkdir()
        monkeypatch.setenv('CLOVER_MANAGED_DIR', str(directory))
        path = directory / 'config.yaml'
    path.write_text('agent:\n  disabled_toolsets: [foobar1]\n')
    cached = config.load_config_readonly()
    binding = Binding(runner, event('/proactive watcher', uid='123', chat_id='123').source)
    effects = SimpleNamespace(reads=[], physical=[], sent=[], on_read=lambda: None,
                              on_response=lambda: None, deliver_ok=False)

    def read(cursor, *, max_items):
        effects.reads.append(cursor)
        if cursor is None:
            return {'epoch': '1', 'cursor': 10, 'items': []}
        effects.on_read()
        uid = cursor['cursor'] + 1
        return {'epoch': '1', 'cursor': uid, 'items': [
            {'id': str(uid), 'from': 'synthetic@example.invalid',
             'subject': 'PRIVATE-WATCHER-HEADER-CANARY', 'date': 'today'}]}

    def endpoint(req):
        assert str(req.url) == 'https://api.openai.com/v1/chat/completions'
        usage = w.Store(w.state_path(home)).usage()
        assert usage['attempts'] == len(effects.physical) + 1
        effects.physical.append(json.loads(req.content))
        effects.on_response()
        return httpx.Response(200, json={'id': 'offline', 'object': 'chat.completion', 'created': 0,
            'model': 'gpt-4o-mini-2024-07-18', 'choices': [{'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': '{"scores":[{"index":0,"score":9,"reason":"urgent"}]}'}}]})

    def deliver(outbox, guard):
        guard()
        effects.sent.append(outbox.copy())
        return effects.deliver_ok

    runner.adapters[Platform.EMAIL].inbox_watcher_read = read
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda *args, **kwargs: httpx.MockTransport(endpoint))
    binding.deliver = deliver
    job = accepted(home, binding)
    assert job is not None
    registered = jobs.get_job(job['id'])
    assert registered is not None and registered['enabled'] is True
    assert w.Store(w.state_path(home)).read()['policy']['state'] == 'active'
    w.run(job, binding, now=172800)
    assert len(effects.reads) == 1 and not effects.physical and not effects.sent

    def deny():
        replace_same_stamp(path, path.read_text().replace('foobar1', 'cronjob'))

    return SimpleNamespace(binding=binding, job=job, path=path, effects=effects,
                           deny=deny, cached=cached, home=home, layer=request.param)


@pytest.mark.parametrize('stage', ['before-read', 'after-read', 'physical-client', 'after-response', 'queued-delivery'])
def test_atomic_saved_denial_stops_next_effect_including_outbox(runtime, monkeypatch, stage):
    import contextlib
    from cron import inbox_watcher as w
    import agent.auxiliary_client as aux

    h = runtime
    if stage == 'before-read':
        h.deny()
    elif stage == 'after-read':
        h.effects.on_read = h.deny
    elif stage == 'physical-client':
        factory = aux.create_bounded_inbox_client

        @contextlib.contextmanager
        def client(*args, **kwargs):
            with factory(*args, **kwargs) as current:
                h.deny()  # inside physical client seam, before reservation/HTTP
                yield current
        monkeypatch.setattr(aux, 'create_bounded_inbox_client', client)
    elif stage == 'after-response':
        h.effects.on_response = h.deny
    w.run(h.job, h.binding, now=174600)
    state = w.Store(w.state_path(h.home)).read()
    if stage == 'queued-delivery':
        assert state['outbox'] is not None and len(h.effects.sent) == 1
        before = state['budget'].copy()
        h.deny()
        w.run(h.job, h.binding, now=176400)
        state = w.Store(w.state_path(h.home)).read()
        assert state['outbox'] is not None and len(h.effects.sent) == 1
        assert len(h.effects.reads) == 2 and state['budget'] == before
    else:
        assert not h.effects.sent
        assert len(h.effects.reads) == (1 if stage == 'before-read' else 2)
    expected_attempts = 1 if stage in ('queued-delivery', 'after-response') else 0
    assert len(h.effects.physical) == state['budget']['attempts'] == expected_attempts
    assert state['status'] != 'alert delivery confirmed'
    assert 'PRIVATE-WATCHER-HEADER-CANARY' not in w.canonical(state)


@pytest.mark.parametrize('body', ['broken: [', 'false', '[]', 'agent: false',
    'agent:\n  disabled_toolsets: false', 'agent:\n  disabled_toolsets: [false]',
    'agent:\n  disabled_toolsets: "[broken"', 'platform_toolsets: false',
    'platform_toolsets:\n  telegram: [false]', 'approvals: false',
    'security:\n  approval: false', 'unreadable',
    pytest.param('dangling', marks=pytest.mark.skipif(os.name == 'nt', reason='Symlink creation needs Windows privileges'))])
def test_unknown_current_authorization_blocks_without_source_or_spend(runtime, monkeypatch, body):
    from cron import inbox_watcher as w

    h = runtime
    if body == 'unreadable':
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path == h.path:
                raise PermissionError('PRIVATE-WATCHER-UNREADABLE-CANARY')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', read)
    elif body == 'dangling':
        h.path.unlink()
        h.path.symlink_to(h.path.with_name('missing.yaml'))
    else:
        h.path.write_text(body)
    w.run(h.job, h.binding, now=174600)
    state = w.Store(w.state_path(h.home)).read()
    assert len(h.effects.reads) == 1 and not h.effects.physical and not h.effects.sent
    assert state['budget']['attempts'] == 0 and state['outbox'] is None
    assert 'PRIVATE-WATCHER-UNREADABLE-CANARY' not in w.canonical(state)


def test_same_stamp_denial_does_not_flush_startup_cache(runtime):
    from clover_cli import config

    h = runtime
    original = h.binding.snapshot()
    h.deny()
    current = h.binding.snapshot()  # valid denial stays inspectable
    assert current != original and current.get('saved_tool_denied') is True
    assert config.load_config_readonly() is h.cached


def test_saved_all_tools_alias_revokes_current_watcher_same_stamp(runtime):
    from cron import inbox_watcher as w

    h = runtime
    replace_same_stamp(h.path, h.path.read_text().replace('foobar1', 'all    '))
    w.run(h.job, h.binding, now=174600)
    assert len(h.effects.reads) == 1 and not h.effects.physical and not h.effects.sent
    assert w.Store(w.state_path(h.home)).usage()['attempts'] == 0


def test_tool_revocation_still_allows_private_watcher_off(runtime):
    from cron import inbox_watcher as w, jobs

    h = runtime
    h.deny()
    result = w.revoke(h.binding)
    assert 'revoked' in result
    assert w.Store(w.state_path(h.home)).read()['policy']['state'] == 'revoked'
    registered = jobs.get_job(h.job['id'])
    assert registered is not None and registered['enabled'] is False
    assert not h.effects.physical and not h.effects.sent


@pytest.mark.parametrize('body', ['', '# empty\n', 'null', '{}', 'agent: null',
    'agent:\n  disabled_toolsets: []', 'agent:\n  disabled_toolsets: [clover-telegram]',
    'platform_toolsets:\n  telegram: [cronjob]', 'platform_toolsets:\n  telegram: null',
    'platform_toolsets:\n  telegram: [all]'])
def test_unconfigured_and_explicit_allowed_controls_really_register(picker, home, monkeypatch, body):
    from cron import inbox_watcher as w, jobs
    from gateway.inbox_watcher_controls import Binding
    from tests.test_paid_inbox_runner import accepted

    runner, _ = setup(picker, monkeypatch)
    path = home / 'config.yaml'
    path.write_text(body)
    binding = Binding(runner, event('/proactive watcher', uid='123', chat_id='123').source)
    assert binding.snapshot() == binding.snapshot()
    assert path.read_text() == body  # permission reads must not migrate settings
    job = accepted(home, binding)
    assert job is not None
    registered = jobs.get_job(job['id'])
    assert registered is not None and registered['enabled'] is True
    state = w.Store(w.state_path(home)).read()
    assert state['policy']['state'] == 'active' and state['policy']['job_id'] == job['id']
    assert state['budget']['attempts'] == 0


def test_unrelated_same_stamp_user_edit_keeps_real_runtime_allowed(picker, home, monkeypatch):
    from cron import inbox_watcher as w
    from gateway.config import Platform
    from gateway.inbox_watcher_controls import Binding
    from tests.test_paid_inbox_runner import accepted

    runner, _ = setup(picker, monkeypatch)
    path = home / 'config.yaml'
    path.write_text('agent:\n  max_turns: 71\ndisplay:\n  skin: foobar1\n')
    binding = Binding(runner, event('/proactive watcher', uid='123', chat_id='123').source)
    job = accepted(home, binding)
    original = binding.snapshot()
    replace_same_stamp(path, path.read_text().replace('71', '72').replace('foobar1', 'foobar2'))
    assert binding.snapshot() == original
    reads = []

    def read(cursor, *, max_items):
        reads.append(cursor)
        return {'epoch': '1', 'cursor': 10, 'items': []}

    runner.adapters[Platform.EMAIL].inbox_watcher_read = read
    w.run(job, binding, now=172800)
    assert reads == [None]
    state = w.Store(w.state_path(home)).read()
    assert state['cursor'] == {'epoch': '1', 'cursor': 10} and state['budget']['attempts'] == 0
