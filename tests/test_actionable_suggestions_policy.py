"""Saved authorization is fresh even when startup file signatures are unchanged."""
import asyncio
import contextlib
import os
from pathlib import Path

import pytest
import yaml

from agent import actionable_suggestions as actions
from clover_cli import managed_scope
from cron import jobs, notepad
from tests.gateway.test_workstream_suggestion_offer import network_guard, scope  # noqa: F401
from tests.test_actionable_suggestions_wire import native, native_tap  # noqa: F401


def replace_same_stamp(path, text):
    stamp = path.stat()
    assert len(text.encode()) == stamp.st_size
    replacement = path.with_suffix('.replacement')
    replacement.write_text(text)
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_mtime_ns == stamp.st_mtime_ns
    assert path.stat().st_size == stamp.st_size


def tap_unchanged(native):
    h, target, app, endpoint = native
    before, calls = jobs.load_jobs(), h.scripted.calls
    asyncio.run(app.process_update(native_tap(app)))
    assert h.scripted.calls == calls
    assert jobs.load_jobs() == before
    rows = notepad.list_receipts(scope(h), h.results[-1]['workstream_suggestion']['workstream_id'])
    assert not any(r['kind'] == 'executed' for r in rows)
    assert not any('"state":"verified"' in r['detail'] for r in rows)
    assert not any('Verified:' in p.get('text', '') for _, p in endpoint.calls)


@pytest.mark.parametrize('layer', ['user', 'managed'])
@pytest.mark.parametrize('boundary', ['dispatch', 'owner'])
def test_same_stamp_revocation_inside_final_boundary(native, monkeypatch, tmp_path, layer, boundary):
    h, target, app, endpoint = native
    directory = tmp_path / 'managed'
    directory.mkdir()
    monkeypatch.setattr(managed_scope, 'get_managed_dir', lambda: directory)
    path = h.home / 'config.yaml' if layer == 'user' else directory / 'config.yaml'
    if layer == 'managed':
        path.write_text('{}\n')
    body = yaml.safe_load(path.read_text())
    body['agent'] = {'disabled_toolsets': ['foobar1']}
    path.write_text(yaml.safe_dump(body))
    proposal = actions.pending(h.results[-1]['workstream_suggestion']['action']['token'])
    assert proposal is not None
    # Establish the pre-race policy, not a new permission grant.
    proposal.policy = actions._policy(proposal.agent)
    original = path.read_text()
    injected = False

    def revoke():
        nonlocal injected
        if not injected:
            injected = True
            replace_same_stamp(path, original.replace('foobar1', 'cronjob'))

    if boundary == 'owner':
        original_lock = jobs._jobs_lock

        @contextlib.contextmanager
        def locked(*args, **kwargs):
            with original_lock(*args, **kwargs):
                if proposal.state == 'started':
                    revoke()
                yield
        monkeypatch.setattr(jobs, '_jobs_lock', locked)
    else:
        from clover_cli import plugins

        def middleware(*args, **kwargs):
            revoke()
            return None, None
        monkeypatch.setattr(plugins, '_dispatch_pre_tool_call_hooks', middleware)
    tap_unchanged(native)
    assert injected


@pytest.mark.parametrize('layer', ['user', 'managed'])
@pytest.mark.parametrize('body', ['broken: [', 'false', '[]', 'agent: false',
                                  'agent:\n  disabled_toolsets: false',
                                  'agent:\n  disabled_toolsets: [false]',
                                  'agent:\n  disabled_toolsets: "[broken"',
                                  'platform_toolsets: false', 'approvals: false',
                                  'security:\n  approval: false', 'unreadable'])
def test_unknown_saved_authorization_never_executes(native, monkeypatch, tmp_path, layer, body):
    h, target, app, endpoint = native
    directory = tmp_path / 'managed'
    directory.mkdir()
    monkeypatch.setattr(managed_scope, 'get_managed_dir', lambda: directory)
    path = h.home / 'config.yaml' if layer == 'user' else directory / 'config.yaml'
    if body == 'unreadable':
        path.write_text('{}')
        original_read = Path.read_text

        def read(self, *args, **kwargs):
            if self == path:
                raise PermissionError('synthetic private profile denial')
            return original_read(self, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', read)
    else:
        path.write_text(body)
    tap_unchanged(native)


@pytest.mark.parametrize('layer', ['user', 'managed'])
def test_same_stamp_unrelated_edit_keeps_legitimate_action(native, monkeypatch, tmp_path, layer):
    h, target, app, endpoint = native
    directory = tmp_path / 'managed'
    directory.mkdir()
    monkeypatch.setattr(managed_scope, 'get_managed_dir', lambda: directory)
    path = h.home / 'config.yaml' if layer == 'user' else directory / 'config.yaml'
    if layer == 'managed':
        path.write_text('{}\n')
    body = yaml.safe_load(path.read_text())
    body['agent'] = {'max_turns': 71}
    body['display'] = {'skin': 'foobar1'}
    path.write_text(yaml.safe_dump(body))
    proposal = actions.pending(h.results[-1]['workstream_suggestion']['action']['token'])
    assert proposal is not None
    proposal.policy = actions._policy(proposal.agent)
    replace_same_stamp(path, path.read_text().replace('foobar1', 'foobar2').replace('71', '72'))
    calls = h.scripted.calls
    asyncio.run(app.process_update(native_tap(app)))
    assert h.scripted.calls == calls
    job = jobs.get_job(target)
    assert job is not None and job['enabled'] is False
    assert any('Verified:' in p.get('text', '') for _, p in endpoint.calls)


@pytest.mark.parametrize('layer', ['user', 'managed'])
def test_fresh_authorization_does_not_flush_startup_cache(native, monkeypatch, tmp_path, layer):
    from clover_cli.config import load_config_readonly

    h, target, app, endpoint = native
    directory = tmp_path / 'managed'
    directory.mkdir()
    monkeypatch.setattr(managed_scope, 'get_managed_dir', lambda: directory)
    path = h.home / 'config.yaml' if layer == 'user' else directory / 'config.yaml'
    if layer == 'managed':
        path.write_text('{}\n')
    body = yaml.safe_load(path.read_text())
    body['agent'] = {'disabled_toolsets': ['foobar1']}
    path.write_text(yaml.safe_dump(body))
    cached = load_config_readonly()
    proposal = actions.pending(h.results[-1]['workstream_suggestion']['action']['token'])
    assert proposal is not None
    before = actions._policy(proposal.agent)
    replace_same_stamp(path, path.read_text().replace('foobar1', 'cronjob'))
    assert actions._policy(proposal.agent) != before
    assert load_config_readonly() is cached
    assert cached['agent']['disabled_toolsets'] == ['foobar1']
    assert before


@pytest.mark.parametrize('layer', ['user', 'managed'])
@pytest.mark.parametrize('value', ['[cronjob]', '"cronjob"', "\"['cronjob']\""])
def test_saved_explicit_deny_cannot_create_another_offer(native, monkeypatch, tmp_path, layer, value):
    h, target, app, endpoint = native
    directory = tmp_path / 'managed'
    directory.mkdir()
    monkeypatch.setattr(managed_scope, 'get_managed_dir', lambda: directory)
    path = h.home / 'config.yaml' if layer == 'user' else directory / 'config.yaml'
    artifact = h.results[-1]['workstream_suggestion']
    proposal = actions.pending(artifact['action']['token'])
    assert proposal is not None and 'cronjob' in proposal.agent.valid_tool_names
    path.write_text('agent:\n  disabled_toolsets: ' + value + '\n')
    before = jobs.load_jobs()
    assert actions.propose(proposal.agent, proposal.turn, artifact, is_current=proposal.is_current) is None
    assert jobs.load_jobs() == before


@pytest.mark.parametrize('body', ['', '# empty policy\n', 'null', '{}', 'agent: null',
                                  'agent:\n  disabled_toolsets: []'])
def test_empty_and_absent_authorization_is_not_a_revocation(monkeypatch, tmp_path, body):
    from clover_cli import config

    user, managed = tmp_path / 'config.yaml', tmp_path / 'admin'
    managed.mkdir()
    monkeypatch.setattr(config, 'get_config_path', lambda: user)
    monkeypatch.setattr(managed_scope, 'get_managed_dir', lambda: managed)
    baseline = actions._saved_authorization()
    user.write_text(body)
    (managed / 'config.yaml').write_text(body)
    assert actions._saved_authorization() == baseline


@pytest.mark.skipif(os.name == 'nt', reason='Symlink creation needs Windows privileges')
def test_dangling_settings_link_is_not_absent_policy(monkeypatch, tmp_path):
    from clover_cli import config

    path = tmp_path / 'config.yaml'
    path.symlink_to(tmp_path / 'missing')
    monkeypatch.setattr(config, 'get_config_path', lambda: path)
    monkeypatch.setattr(managed_scope, 'get_managed_dir', lambda: None)
    with pytest.raises(ValueError, match='Current saved permissions'):
        actions._saved_authorization()
