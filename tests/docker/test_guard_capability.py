"""A real, private Docker container may run a gateway; the host may not."""
import subprocess

import pytest


def test_owned_container_gateway_argv_is_admitted(built_image, container_name):
    # This used to trip the host gateway guard before Docker ran at all.
    result = subprocess.run(['docker', 'run', '--rm', built_image,
                             'gateway', 'run', '--help'],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert 'Traceback' not in result.stderr


def test_docker_capability_does_not_admit_host_gateway():
    with pytest.raises(AssertionError, match='REAL'):
        subprocess.run(['python', '-m', 'clover_cli.main', 'gateway', 'run'])


@pytest.mark.parametrize('flag', ['--privileged', '--pid=host', '--network=host', '--volumes-from=foreign'])
def test_gateway_shaped_unsafe_container_options_are_refused(built_image, flag):
    with pytest.raises((AssertionError, ValueError)):
        subprocess.run(['docker', 'run', '--rm', flag, built_image, 'gateway', 'run'],
                       capture_output=True, timeout=10)


def test_foreign_container_is_not_adopted(built_image):
    with pytest.raises((AssertionError, ValueError)):
        subprocess.run(['docker', 'exec', 'foreign-container', 'clover', 'gateway', 'run'],
                       capture_output=True, timeout=10)


def test_host_mount_is_refused(built_image):
    with pytest.raises((AssertionError, ValueError)):
        subprocess.run(['docker', 'run', '--rm', '-v', '/:/opt/data', built_image,
                        'gateway', 'run'], capture_output=True, timeout=10)


def test_direct_docker_mutation_has_no_implicit_grant():
    with pytest.raises(AssertionError, match='ungranted Docker'):
        subprocess.Popen(['docker', 'rm', '-f', 'foreign-container'])


def test_readonly_docker_cannot_override_executable():
    import sys
    with pytest.raises(AssertionError, match='ungranted Docker'):
        with subprocess.Popen(['docker', 'inspect', 'foreign-container'], executable=sys.executable,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
            proc.wait(timeout=10)
