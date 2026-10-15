"""Preserve disposable-runner facts, including failure, without config/secrets."""
import json
import os
from pathlib import Path
import shutil


def stage(home, evidence, metadata):
    evidence.mkdir(parents=True, exist_ok=True)
    for name in ['logs', 'gateway_state.json', '.clover-last-update']:
        source = home / name
        if source.is_dir():
            shutil.copytree(source, evidence / name, dirs_exist_ok=True)
        elif source.is_file():
            shutil.copy2(source, evidence / name)
    (evidence / 'row.json').write_text(json.dumps(metadata, indent=2))


def main():
    temp = Path(os.environ['RUNNER_TEMP'])
    home = (Path(os.environ['LOCALAPPDATA']) / 'clover' if os.name == 'nt'
            else Path.home() / '.clover')
    fields = ['GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA', 'GITHUB_JOB',
              'RUNNER_OS', 'ROW_STATUS', 'OLD_REF', 'OLD_SHA', 'TARGET_REF',
              'TARGET_TREE', 'NEXT_REF', 'NEXT_SHA', 'NEXT_TREE', 'SETUP', 'UPDATE_PATH', 'PHASE']
    metadata = {key: os.environ.get(key) for key in fields}
    metadata['download_source'] = 'disposable local bare origin; real commit objects; NOT GitHub/main'
    metadata['chat_transport'] = 'local IRC gateway handler, NOT real Telegram'
    stage(home, temp / 'evidence', metadata)
    for name in ['irc.log', 'update-output.txt']:
        if (temp / name).is_file():
            shutil.copy2(temp / name, temp / 'evidence' / name)


if __name__ == '__main__':
    main()
