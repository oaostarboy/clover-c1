"""Completeness is checked against actual default keys, not an assumed count."""
import json
from pathlib import Path

from clover_cli.config import DEFAULT_CONFIG
from clover_cli.proactive import ADVANCED_ENTRIES


def test_capability_manifest_covers_every_proactive_setting_and_advanced_page():
    manifest = json.loads(Path('docs/user/proactive-controls-coverage.json').read_text())

    def leaves(value, prefix):
        for key, child in value.items():
            name = prefix + '.' + key
            if isinstance(child, dict):
                yield from leaves(child, name)
            else:
                yield name

    required = set(leaves(DEFAULT_CONFIG['proactive'], 'proactive'))
    covered = {gate for row in manifest['capabilities'] for gate in row['gates']}
    assert required <= covered
    advanced = {row['text_command'].split()[-1] for row in manifest['capabilities']
                if row.get('advanced')}
    assert set(ADVANCED_ENTRIES) <= advanced
    for row in manifest['capabilities']:
        assert row['consumer'] and row['tests'] and row['menu'] and row['status']
