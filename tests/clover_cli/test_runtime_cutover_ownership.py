"""Mutation boundaries for an explicitly selected external environment."""
from pathlib import Path
from unittest.mock import patch
import pytest
from clover_cli import managed_uv as m


@pytest.mark.parametrize('layout', ['external', 'local-without-config'])
def test_explicit_foreign_external_environment_is_refused_before_probe(tmp_path, layout):
    root=tmp_path/'checkout';root.mkdir()
    (root/'pyproject.toml').write_text('[project]\n',encoding='utf-8')
    foreign=tmp_path/'foreign-venv' if layout == 'external' else root/'venv'
    foreign.mkdir()
    from clover_constants import venv_python_path
    python=venv_python_path(foreign)
    python.parent.mkdir(parents=True)
    python.write_text('must remain untouched',encoding='utf-8')
    with patch.object(m,'probe_sqlite_runtime',side_effect=AssertionError('probe is not authorized')) as probe:
        result=m.repair_vulnerable_runtime('unexecuted',project_root=root,venv_dir=foreign)
    assert result.status == 'refused'
    assert 'ownership' in result.detail
    probe.assert_not_called()
    assert python.read_text(encoding='utf-8') == 'must remain untouched'


@pytest.mark.parametrize('preexisting', [False, True])
def test_runtime_cutover_arms_existing_restart_obligation_before_swap(tmp_path, preexisting):
    from clover_constants import get_clover_home
    from tests.clover_cli.test_managed_uv import _make_runtime_install, _runtime_info
    root,live,sentinel=_make_runtime_install(tmp_path)
    before=sentinel.read_bytes()
    candidate=root/'.clover-runtime'/'candidate';candidate.mkdir(parents=True)
    old=_runtime_info(m._venv_python(live),sqlite_version=(3,50,4))
    safe=_runtime_info(tmp_path/'safe-python',sqlite_version=(3,53,4))
    marker=get_clover_home()/'fleet_restart_pending'
    assert not marker.exists()
    original = b'prior pull still needs its own restart\n' if preexisting else None
    if original is not None:
        marker.write_bytes(original)
    def cutover(*args,**kwargs):
        assert marker.is_file(), 'restart obligation must exist BEFORE replacing the live runtime'
        return False,None,None,'deliberate pre-swap hold'
    generation=m.managed_python_install_dir(root)/'test-generation';generation.mkdir(parents=True)
    with (patch.object(m,'_PROJECT_ROOT',root),
          patch.object(m,'probe_sqlite_runtime',return_value=old),
          patch.object(m,'_refresh_managed_uv_catalog',return_value=False),
          patch.object(m,'_install_safe_python_generation',return_value=(generation,safe.executable,safe)),
          patch.object(m,'_stage_candidate_venv',return_value=candidate),
          patch.object(m,'_cut_over_candidate',side_effect=cutover)):
        result=m.repair_vulnerable_runtime('uv',project_root=root)
    assert result.status == 'failed'
    assert sentinel.read_bytes() == before
    assert (marker.read_bytes() if marker.exists() else None) == original
    assert not m._runtime_restart_pending_path(root).exists()


@pytest.mark.linux_only
def test_cross_filesystem_candidate_is_refused_before_parking_live_environment(tmp_path):
    # Kernel-real distinct mounts, not simulated platform/stat results.
    import tempfile
    root=tmp_path/'checkout';root.mkdir()
    live=root/'venv';live.mkdir()
    sentinel=live/'retained';sentinel.write_text('old environment',encoding='utf-8')
    with tempfile.TemporaryDirectory(dir='/dev/shm') as other_mount:
        candidate=Path(other_mount)/'candidate';candidate.mkdir()
        if candidate.stat().st_dev == live.parent.stat().st_dev:
            pytest.skip('distinct temporary filesystem unavailable')
        with patch.object(m,'_rename_with_retry',side_effect=AssertionError('no rename is authorized')):
            ok,backup,info,detail=m._cut_over_candidate(candidate,project_root=root,live=live)
        assert not ok and backup is None and info is None
        assert 'filesystem' in detail
        assert sentinel.read_text(encoding='utf-8') == 'old environment'
        assert candidate.is_dir()
