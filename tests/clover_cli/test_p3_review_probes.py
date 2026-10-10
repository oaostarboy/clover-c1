"""Review probes (Astra, P3 review): real completion methods, disk markers/receipts and build_info.

The "old process" is the real 1.3.0 ``build_info`` / ``release_notes`` source
(base commit 7d78893d, frozen verbatim in ``p3_review_data/``) executed against
a temp git checkout, so these tests exercise what a live 1.3.x gateway really
writes into the pending marker -- including the refreshed on-disk HEAD after an
earlier pull that never restarted that gateway.  The data files are fixtures of
shipped behaviour; do not "update" them.  Release versions and detached HEAD
files are explicit controlled fixtures; only the chat transport and process
boundary are simulated.
"""
import sys
import types
from pathlib import Path

import pytest
import clover_cli
from clover_cli import build_info as bi, release_notes as rn, update_receipt
from tests.clover_cli.test_release_notes import _drive, _marker, _SESSION_KEY

OLD = 'a' * 40
NEW = 'b' * 40

_DATA = Path(__file__).parent / 'p3_review_data'


def load_base(name, filename):
    """Execute a frozen 1.3.0 module under ``name`` (not importable normally)."""
    path = _DATA / filename
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    try:
        exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import gateway.run as gr
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('CLOVER_HOME', str(home))
    monkeypatch.setattr(gr, '_clover_home', home)
    root = tmp_path / 'checkout'
    (root / '.git').mkdir(parents=True)
    (root / '.git' / 'HEAD').write_text(OLD)
    (root / 'pyproject.toml').write_text('[project]\nversion="1.3.0"\n')
    notes = tmp_path / 'NOTES.md'
    notes.write_text('## 1.4.0 | Clover C1.4 | 2026-10-10\n- Review fixture feature.\n\n## 1.3.0 | Clover C1.3 | 2026-10-01\n- Old feature.\n')
    monkeypatch.setattr(rn, '_NOTES_PATH', notes, raising=False)
    # Resolve the actual default path used by load_release_notes.
    monkeypatch.setattr(rn, 'load_release_notes', lambda notes_path=None: rn.parse_release_notes(notes.read_text()))
    monkeypatch.setattr(bi, '_PROJECT_ROOT', root)
    monkeypatch.setattr(bi, '_BUILD_SHA_FILE', root / 'missing')
    monkeypatch.setattr(bi, '_boot_identity', None)
    monkeypatch.setattr(bi, '_code_identity_cache', None)
    monkeypatch.setattr(clover_cli, '__version__', '1.3.0')
    return home, root


def restart(root, monkeypatch, sha=NEW, version='1.4.0'):
    (root / '.git' / 'HEAD').write_text(sha)
    (root / 'pyproject.toml').write_text(f'[project]\nversion="{version}"\n')
    monkeypatch.setattr(clover_cli, '__version__', version)
    monkeypatch.setattr(bi, '_boot_identity', None)
    monkeypatch.setattr(bi, '_code_identity_cache', None)
    assert bi.capture_boot_identity()['sha'] == sha


@pytest.mark.asyncio
@pytest.mark.parametrize('site', ['watcher', 'legacy', 'died'])
@pytest.mark.parametrize('outcome', ['success', 'partial'])
@pytest.mark.parametrize('refreshed_before_marker', [False, True])
async def test_real_first_hop(setup, monkeypatch, site, outcome, refreshed_before_marker):
    home, root = setup
    old_bi = load_base('p3_base_build_info', 'base_build_info.py')
    old_rn = load_base('p3_base_release_notes', 'base_release_notes.py')
    old_bi.__file__ = str(root / 'clover_cli' / 'build_info.py')
    old_bi._BUILD_SHA_FILE = root / 'missing'
    # Represents the live 1.3 gateway's startup identity cache.
    assert old_bi.get_code_identity()['sha'] == OLD
    with monkeypatch.context() as old_process:
        old_process.setitem(sys.modules, 'clover_cli.build_info', old_bi)
        old_process.setattr(clover_cli, 'build_info', old_bi)
        if refreshed_before_marker:
            # A previous partial pull did not restart the 1.3 gateway;
            # its real collect_fleet_versions path refreshes the same cache.
            (root / '.git' / 'HEAD').write_text(NEW)
            update_receipt.collect_fleet_versions()
            assert old_bi.get_code_identity()['sha'] == NEW
        origin = old_rn.pending_marker_origin()
        assert origin['from_version'] == '1.3.0'
        assert origin['from_sha'] == (NEW if refreshed_before_marker else OLD)
        # Exactly what the old process wrote: no from_identity field.
        _marker(home, legacy=True, **origin, session_key=_SESSION_KEY)
    restart(root, monkeypatch)
    text = await _drive(site, home, outcome=outcome, pre=('1.3.0', OLD))
    assert "What's new:" in text, f'origin={origin}; site={site}; outcome={outcome}; text={text}'
    assert 'Review fixture feature.' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('site', ['watcher', 'legacy', 'died'])
@pytest.mark.parametrize('case', ['partial', 'refused', 'unknown-boot', 'no-restart', 'same-version', 'stale', 'repeated'])
async def test_real_head_matrix(setup, monkeypatch, site, case):
    from datetime import timedelta
    home, root = setup
    assert bi.capture_boot_identity()['sha'] == OLD
    (root / '.git' / 'HEAD').write_text(NEW)
    update_receipt.collect_fleet_versions()
    assert bi.get_code_identity()['sha'] == NEW
    assert bi.get_boot_identity()['sha'] == OLD
    assert bi.capture_boot_identity()['sha'] == OLD
    origin = rn.pending_marker_origin()
    assert origin['from_sha'] == OLD
    if case == 'repeated':
        update_receipt.collect_fleet_versions()
        assert rn.pending_marker_origin() == origin
    _marker(home, **origin, session_key=_SESSION_KEY)
    if case != 'no-restart':
        restart(root, monkeypatch, version='1.3.0' if case == 'same-version' else '1.4.0')
    if case == 'unknown-boot':
        monkeypatch.setattr(bi, '_boot_identity', None)
    args = {'receipt_age': -timedelta(days=3)} if case == 'stale' else {}
    text = await _drive(site, home, outcome='refused' if case == 'refused' else 'partial', pre=('1.3.0', OLD), **args)
    expect = case in {'partial', 'stale', 'repeated'}
    assert ("What's new:" in text) == expect, text
    assert 'Already up to date' not in text


@pytest.mark.asyncio
async def test_rolled_back_real_identity(setup, monkeypatch):
    home, root = setup
    _marker(home, from_version='1.3.0', from_sha=OLD)
    restart(root, monkeypatch)
    text = await _drive('died', home, outcome='rolled-back')
    assert "What's new:" not in text
    assert 'went back' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('site', ['watcher', 'legacy'])
async def test_rollback_after_earlier_unrestarted_pull(setup, monkeypatch, site):
    from clover_cli.update_restart_watcher import publish_gateway_verdict
    from tests.clover_cli import test_release_notes as helper
    home, root = setup
    # Gateway A stayed alive after an earlier partial pull of B (1.4).
    # A second attempt rolls back from C to pre-pull B, not running A.
    bi.capture_boot_identity()
    (root / '.git' / 'HEAD').write_text(NEW)
    update_receipt.collect_fleet_versions()
    _marker(home, **rn.pending_marker_origin(), session_key=_SESSION_KEY)
    restart(root, monkeypatch)
    publish_gateway_verdict(home / '.clover-update-heartbeat.json', 'rolled-back')
    assert (home / '.update_exit_code').read_text() == '1'
    monkeypatch.setitem(helper._EXIT_CODE, 'rolled-back', '1')
    text = await _drive(site, home, outcome='rolled-back', pre=('1.4.0', NEW))
    assert "What's new:" not in text, text


def test_status_still_refreshes_but_notes_identity_does_not(setup):
    from gateway.status import _get_code_identity_fields
    home, root = setup
    assert bi.capture_boot_identity()['sha'] == OLD
    assert _get_code_identity_fields()['code_sha'] == OLD
    (root / '.git' / 'HEAD').write_text(NEW)
    update_receipt.collect_fleet_versions()
    assert _get_code_identity_fields()['code_sha'] == NEW
    assert bi.get_boot_identity()['sha'] == OLD
    assert rn.pending_marker_origin()['from_sha'] == OLD
