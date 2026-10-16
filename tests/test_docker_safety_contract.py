"""Destructive-boundary probes use inert transport, never foreign resources."""
from types import SimpleNamespace
import subprocess

import pytest

from tests._docker_safety import DockerTestCapability


@pytest.fixture
def capability(tmp_path):
    cap = DockerTestCapability.__new__(DockerTestCapability)
    cap.docker = '/usr/bin/docker'
    cap.script = None
    cap.private = tmp_path
    cap.image = 'sha256:' + '1' * 64
    cap.image_name = 'unit-test-image'
    cap.label = 'clover.test-owner=unit-test-owner'
    cap.owned, cap.volumes, cap.created, cap.grants = {}, set(), set(), set()
    cap.transport = []

    def inert(cmd, *args, **options):
        cap.transport.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    cap.run = inert
    return cap


@pytest.mark.parametrize('command', [
    ['docker', 'rm', '-f', 'foreign-container'],
    ['docker', 'container', 'rm', '-f', 'foreign-container'],
    ['docker', 'volume', 'rm', '-f', 'foreign-volume'],
])
def test_unowned_removal_never_reaches_transport(capability, command):
    with pytest.raises(ValueError, match='owned|refused'):
        capability.execute(capability.run, command)
    assert capability.transport == []


def test_owned_name_removal_uses_original_id_not_replacement(capability):
    original = 'a' * 64
    capability.owned['clover-test-collision'] = original
    capability.owned[original] = original
    # A replacement's current name is deliberately not looked up or adopted.
    capability.execute(capability.run, ['docker', 'rm', '-f', 'clover-test-collision'])
    assert capability.transport == [['/usr/bin/docker', 'rm', '-f', original]]


def test_foreign_replacement_volume_is_not_removed(capability, monkeypatch):
    volume = 'clover-test-volume-' + '2' * 32
    capability.volumes.add(volume)
    monkeypatch.setattr(capability, '_inspect', lambda kind, target: {
        'Name': volume, 'Labels': {'clover.test-owner': 'foreign-owner'},
        'Driver': 'local', 'Options': None})
    with pytest.raises(ValueError, match='ownership|owned'):
        capability.execute(capability.run, ['docker', 'volume', 'rm', '-f', volume])
    assert capability.transport == []


def test_actual_container_name_teardown_never_removes_unowned_name(monkeypatch):
    from tests.docker.conftest import container_name
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append(args))
    fixture = container_name.__wrapped__(SimpleNamespace(node=SimpleNamespace(name='foreign_collision')))
    name = next(fixture)
    assert name.startswith('clover-test-')
    with pytest.raises(StopIteration):
        next(fixture)
    assert calls == []


def test_recovered_setup_cleanup_owns_only_created_ids(capability, monkeypatch):
    import json
    target, remaining = 'a' * 64, 'b' * 64
    infos = {value: {'Id': value, 'Name': '/must-not-adopt', 'Image': capability.image,
                    'Config': {'Labels': {'clover.test-owner': 'unit-test-owner'}},
                    'HostConfig': {}, 'Mounts': []} for value in [target, remaining]}
    removed = []

    def transport(cmd, *args, **options):
        if cmd[1] == 'run':
            return subprocess.CompletedProcess(cmd, 0, stdout=target)
        if cmd[1:3] == ['container', 'inspect']:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([infos[cmd[-1]]]))
        if cmd[1] == 'rm':
            removed.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0)

    def initial_inspection_failure(*args):
        raise ValueError('injected first post-create inspection failure')

    capability.run = transport
    monkeypatch.setattr(capability, '_inspect', initial_inspection_failure)
    with pytest.raises(ValueError, match='injected first'):
        capability.execute(transport, ['docker', 'run', '-d', '--name', 'clover-test-setup', capability.image_name])
    assert capability.created == {target} and capability.owned == {}
    capability.created.add(remaining)
    capability.owned[remaining] = remaining
    capability.cleanup()
    assert set(removed) == {target, remaining}
    assert set(capability.owned) == {target, remaining}
    assert not capability.grants


def test_pty_wrapper_keeps_only_one_safely_quoted_docker_command(capability):
    import shlex
    capability.script = '/usr/bin/script'
    inner = ['docker', 'run', '--rm', '-t', capability.image_name, 'sh', '-c', 'echo safe; echo still-container']
    prepared = capability.prepare(['script', '-qc', shlex.join(inner), '/dev/null'], (), {})
    assert prepared[:2] == ['/usr/bin/script', '-qc'] and prepared[-1] == '/dev/null'
    parsed = shlex.split(prepared[2])
    assert parsed[:2] == ['/usr/bin/docker', 'run']
    assert parsed[-3:] == inner[-3:]
    assert capability.image in parsed
    assert capability.label in parsed
    assert not capability.admit('Popen', prepared, (), {})  # no in-flight grant yet


def test_pty_wrapper_rejects_privileged_inner_command(capability):
    capability.script = '/usr/bin/script'
    with pytest.raises(ValueError):
        capability.prepare(['script', '-qc', 'docker run --rm --privileged unit-test-image gateway run', '/dev/null'], (), {})
