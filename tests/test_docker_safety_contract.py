"""Destructive-boundary probes use inert transport, never foreign resources."""
from types import SimpleNamespace
import subprocess

import pytest

from tests._docker_safety import DockerTestCapability


@pytest.fixture
def capability(tmp_path):
    cap = DockerTestCapability.__new__(DockerTestCapability)
    cap.docker = '/usr/bin/docker'
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
