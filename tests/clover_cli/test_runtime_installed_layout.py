"""Real interpreter discovery for existing external editable installations."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest
from clover_constants import venv_python_path


@pytest.mark.parametrize('metadata_mode', ['owned', 'owned-shadowed', 'foreign', 'missing', 'malformed'])
@pytest.mark.parametrize('layout', ['venv', '.venv', 'both', 'neither'])
def test_running_external_venv_is_selected_only_with_own_installed_metadata(tmp_path, metadata_mode, layout):
    checkout = Path(__file__).resolve().parents[2]
    source = tmp_path / 'checkout'
    (source / 'clover_cli').mkdir(parents=True)
    # Import unmodified product modules from a disposable checkout. The real
    # checkout may have .venv, venv, both, or an owned external test runtime.
    for name in ('__init__.py', 'managed_uv.py', 'sqlite_runtime.py', 'update_preflight.py'):
        shutil.copy2(checkout / 'clover_cli' / name, source / 'clover_cli' / name)
    shutil.copy2(checkout / 'clover_constants.py', source / 'clover_constants.py')
    for local in ('venv', '.venv'):
        if layout in (local, 'both'):
            local_python = venv_python_path(source / local, windows=os.name == 'nt')
            local_python.parent.mkdir(parents=True)
            local_python.write_bytes(b'local interpreter placeholder')
    live = tmp_path/'external environment'
    venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(live)
    python = venv_python_path(live, windows=os.name == 'nt')
    site = Path(subprocess.check_output(
        [str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
        text=True,
    ).strip())
    # Synthetic installed distribution, real Python prefix/site and real product
    # module imports. No network, live environment or source installation writes.
    (site/'source.pth').write_text(str(source)+'\n', encoding='utf-8')
    dist = site/'clover_c1-0.dist-info'
    dist.mkdir()
    (dist/'METADATA').write_text('Metadata-Version: 2.1\nName: clover-c1\nVersion: 0\n', encoding='utf-8')
    target = source if metadata_mode.startswith('owned') else tmp_path/'different-checkout'
    target.mkdir(exist_ok=True)  # foreign proof names a real, different checkout
    if metadata_mode != 'missing':
        (dist/'direct_url.json').write_text(
            '{broken' if metadata_mode == 'malformed' else json.dumps({'url':target.as_uri(),'dir_info':{'editable':True}}),
            encoding='utf-8',
        )
    neutral = tmp_path/'neutral'
    neutral.mkdir()
    if metadata_mode == 'owned-shadowed':
        shadow = neutral/'clover_c1.egg-info'
        shadow.mkdir()
        (shadow/'PKG-INFO').write_text('Metadata-Version: 2.1\nName: clover-c1\nVersion: 0\n', encoding='utf-8')
    code = '''
import json,sys
from pathlib import Path
from clover_cli.managed_uv import _default_live_venv
# A source egg-info before site-packages must not erase installed PEP610 proof.
sys.path.insert(0,str(Path.cwd()))
print(json.dumps({'prefix':sys.prefix,'selected':str(_default_live_venv(Path(sys.argv[1])))}))
'''
    child = subprocess.run([str(python), '-I', '-c', code, str(source)], cwd=neutral,
                           text=True, capture_output=True, timeout=30)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert Path(result['prefix']) == live
    fallback = '.venv' if layout == '.venv' else 'venv'
    expected = live if metadata_mode.startswith('owned') else source / fallback
    assert Path(result['selected']) == expected
    assert python.is_file()  # discovery is read-only
