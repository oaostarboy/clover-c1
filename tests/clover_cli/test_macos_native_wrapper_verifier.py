"""Regression for the hosted foreground-verifier refusal, without live I/O."""
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('macos_native_state', Path(__file__).parents[2] / '.github/native-proof/macos_wrapper_state.py')
assert spec is not None and spec.loader is not None
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def test_framework_worker_without_checkout_path_is_selected_with_its_wrapper():
    home = Path('/private-test-home/.clover')
    framework = '/opt/homebrew/Frameworks/Python.framework/Resources/Python'
    rows = [(1981, [framework, '-m', 'clover_cli.stderr_timestamp', '--', '/install/venv/bin/python', '-m', 'clover_cli.main', 'gateway', 'run'], str(home)),
            (1982, [framework, '-m', 'clover_cli.main', 'gateway', 'run'], str(home)),
            (8888, [framework, '-m', 'clover_cli.main', 'gateway', 'run'], '/foreign/home')]
    candidates = state.select_candidates(rows, home)
    assert candidates == {1981, 1982}
    state.require_roles(candidates, 1982, 1981, True)


@pytest.mark.parametrize('candidates,worker,wrapper,proved', [
    ({1981}, 1982, 1981, True),
    ({1982}, 1982, 1981, True),
    ({1981, 1982, 9999}, 1982, 1981, True),
    ({1981, 1982}, 1982, 1981, False),
    ({1982}, 1982, 1982, True),
])
def test_no_ancestor_or_unproved_extra_process_exemption(candidates, worker, wrapper, proved):
    with pytest.raises(ValueError, match='unproved gateway topology'):
        state.require_roles(candidates, worker, wrapper, proved)
