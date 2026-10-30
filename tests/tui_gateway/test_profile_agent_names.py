"""Profile roster names come from each profile, never the active home."""
from pathlib import Path

import tui_gateway.server as srv


def test_profile_names_follow_own_config_and_explicit_title(tmp_path, monkeypatch):
    home = tmp_path / '.clover'
    (home / 'profiles' / 'ops').mkdir(parents=True)
    (home / 'profiles' / 'untitled').mkdir()
    (home / 'profiles' / 'override').mkdir()
    (home / 'config.yaml').write_text('branding:\n  agent_name: Zyra\nagent:\n  name: Wrong\n')
    (home / 'profiles' / 'ops' / 'config.yaml').write_text('agent:\n  name: Oracle\n')
    (home / 'profiles' / 'override' / 'config.yaml').write_text('branding:\n  agent_name: Hidden\n')
    (home / 'profiles' / 'override' / 'profile.yaml').write_text('display_name: Explicit\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('CLOVER_HOME', str(home / 'profiles' / 'ops'))
    rows = srv._methods['profiles.list'](1, {'include_sessions': False})['result']['profiles']
    names = {row['name']: row['display_name'] for row in rows}
    assert names['default'] == 'Zyra'
    assert names['ops'] == 'Oracle'
    assert names['override'] == 'Explicit'
    assert names['untitled'] == ''
