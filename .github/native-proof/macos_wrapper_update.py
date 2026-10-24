"""Real update acceptance on a DISPOSABLE hosted macOS launchd installation.

CLI, real localhost IRC chat, or staged-source pending/no-pull chat catchup.
The deterministic local model asks the real gateway for exactly one file-tool
write and a complete reply while /update is accepted. No Telegram credentials,
product patches, replayed turns, or manufactured result/receipt are used.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--product-sha', required=True)
    parser.add_argument('--old-sha', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--scenario', choices=('cli', 'chat', 'pending-chat'), default='cli')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if (sys.platform != 'darwin' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'):
        parser.error('only a disposable GitHub-hosted macOS runner is permitted')
    for sha in (args.product_sha, args.old_sha):
        if not re.fullmatch('[0-9a-f]{40}', sha):
            parser.error('literal immutable lowercase 40-character commit required')
    evidence = args.evidence.resolve()
    if not evidence.is_relative_to(root):
        parser.error('evidence must stay inside checkout')
    install = root / '.macos-native-install'
    home = root / '.macos-native-home'
    target = f'gui/{os.getuid()}/ai.clover.gateway'
    if install.exists() or home.exists() or evidence.exists():
        parser.error('fresh private install, home and evidence required; no adoption')
    if subprocess.run(['/bin/launchctl', 'print', target], capture_output=True).returncode == 0:
        parser.error('default launchd label already exists; no adoption')
    evidence.mkdir(parents=True)
    home.mkdir()
    neutral = home / 'neutral'
    neutral.mkdir()
    clover_home = home / '.clover'
    clover_home.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in (
        'PYTHONPATH', 'VIRTUAL_ENV', 'PYTHONHOME', 'CLOVER_PROFILE', 'CLOVER_HOME',
        'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY', 'TELEGRAM_BOT_TOKEN')}
    env.update(HOME=str(home), CLOVER_HOME=str(clover_home), PYTHONDONTWRITEBYTECODE='1')
    report = dict(product_sha=args.product_sha, old_sha=args.old_sha,
                  scope='native-framework-launchd-frozen-cli-old2new', passed=False,
                  chat_update='NOT EXERCISED', accepted_turn_tool_effect_reply='NOT EXERCISED',
                  pending_no_pull_catchup='NOT EXERCISED', live_hosts_touched=False)
    owned = False
    updater = None
    chat = None
    chat_started = False
    chat_finished = False
    report['scenario'] = args.scenario
    effect = clover_home / 'native-tool-effect.txt'

    def run(argv, name, timeout=180):
        with (evidence / (name + '.log')).open('w') as log:
            subprocess.run([str(a) for a in argv], cwd=neutral, env=env,
                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                           timeout=timeout, check=True)

    def git(*argv):
        return subprocess.check_output(['git', '-C', str(install), *argv], env=env, text=True).strip()

    def wait_until(predicate, detail, seconds=180):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(detail)
            time.sleep(0.1)

    try:
        run(['git', 'clone', '--no-hardlinks', '--no-checkout', root, install], 'clone')
        run(['git', '-C', install, 'checkout', '--detach', args.old_sha], 'old-checkout')
        run([sys.executable, '-m', 'venv', install / 'venv'], 'venv')
        py = install / 'venv/bin/python'
        binary = install / 'venv/bin/clover'
        # Explicit interpreter; never inherit a live venv. Normal editable user
        # installation, not an overlay or a modified old updater.
        run([sys.executable, '-m', 'uv', '--no-config', 'pip', 'install', '--python', py,
             '-e', str(install) + '[messaging]'], 'dependencies', timeout=600)
        run([py, root / '.github/native-proof/proof.py', 'identity', '--repo', install,
             '--sha', args.old_sha, '--tree', git('rev-parse', args.old_sha + '^{tree}'),
             '--output', evidence / 'before-imports.json'], 'before-imports')
        run([py, root / '.github/native-proof/proof.py', 'origin', '--repo', install,
             '--mirror', root / '.macos-native-origin.git', '--target', args.product_sha], 'fixture-origin')
        # Only the local API adapter is enabled. No external transport or model
        # credential exists; synthetic accepted chat work remains explicitly unrun.
        (clover_home / '.env').write_text(
            'API_SERVER_ENABLED=true\nAPI_SERVER_KEY=macos-proof-dummy-key-123456\n'
            'API_SERVER_PORT=18791\nAPI_SERVER_HOST=127.0.0.1\n')
        (clover_home / 'config.yaml').write_bytes((install / 'cli-config.yaml.example').read_bytes())
        if args.scenario != 'cli':
            from macos_chat_wire import ChatWire
            chat = ChatWire(evidence, effect)
            chat.start()
            with (clover_home / '.env').open('a') as f:
                f.write(f'IRC_SERVER=127.0.0.1\nIRC_PORT={chat.irc.server_address[1]}\n'
                        'IRC_USE_TLS=false\nIRC_CHANNEL=#clover\nIRC_NICKNAME=cloverbot\n'
                        'IRC_ALLOWED_USERS=ant\nGATEWAY_ALLOW_ALL_USERS=true\n'
                        'OPENAI_API_KEY=private-local-fixture-dummy-key\n')
            for key, value in [('model.provider', 'custom'), ('model.default', 'native-fixture'),
                               ('model.base_url', f'http://127.0.0.1:{chat.model.server_address[1]}/v1'),
                               ('model.api_mode', 'chat_completions')]:
                run([binary, 'config', 'set', key, value], 'configure-' + key)
        run([binary, 'gateway', 'install', '--start-now', '--start-on-login'], 'gateway-install')
        owned = True
        run([py, root / '.github/native-proof/macos_wrapper_state.py', '--source', root,
             '--guard-sha', args.product_sha, '--home', clover_home, '--sha', args.old_sha, '--seconds', '0',
             '--output', evidence / 'before-state.json'], 'before-state', timeout=210)
        import psutil
        state = json.loads((evidence / 'before-state.json').read_text())
        worker = psutil.Process(state['pid'])
        wrapper = psutil.Process(worker.ppid())
        loaded = subprocess.check_output(['/bin/launchctl', 'print', target], text=True)
        report['before_process'] = dict(worker_pid=worker.pid, wrapper_pid=wrapper.pid,
            worker_start=worker.create_time(), wrapper_start=wrapper.create_time(),
            worker_argv=worker.cmdline(), wrapper_argv=wrapper.cmdline(),
            launchd_selected=[l for l in loaded.splitlines() if l.startswith(('\tpid =', '\tprogram ='))])
        if (f'\tpid = {wrapper.pid}' not in loaded.splitlines()
                or worker.pid == wrapper.pid or wrapper.ppid() != 1
                or worker.cmdline()[0] == str(py) or wrapper.cmdline()[0] == str(py)):
            raise ValueError('normal launchd wrapper/child framework normalization not exercised')
        run([py, root / '.github/native-proof/proof.py', 'begin', '--output', evidence / 'begin.json'], 'begin')
        if args.scenario == 'cli':
            with (evidence / 'update.log').open('w') as log:
                updater = subprocess.Popen([str(binary), 'update', '--yes', '--gateway'],
                    cwd=neutral, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                # wait() does not force-kill an updater on timeout, unlike run().
                exit_code = updater.wait(timeout=900)
        else:
            assert chat is not None
            wait_until(chat.ready, 'private IRC adapter did not register')
            chat.send('MAC_NATIVE_ACCEPTANCE: use write_file exactly once to create ' + str(effect)
                      + ' containing ONE_NATIVE_TOOL_EFFECT followed by a newline, then reply only MAC_NATIVE_TURN_COMPLETE.')
            wait_until(lambda: effect.is_file() and any(e['kind'] == 'accepted_turn_waiting_after_tool' for e in chat.events),
                       'real gateway did not execute the file tool and continue its accepted turn')
            if effect.read_text() != 'ONE_NATIVE_TOOL_EFFECT\n' or chat.tool_calls != 1:
                raise ValueError('accepted tool effect missing, wrong, or requested more than once')
            if args.scenario == 'pending-chat':
                # Reproduce staged new files with the OLD process still serving.
                # The marker comes from the real current producer, with a fresh
                # actual producer PID/time; no Suni receipt or status is copied.
                run(['git', '-C', install, 'merge', '--ff-only', args.product_sha], 'stage-source')
                run([py, '-c', 'from clover_cli.update_cmd import _write_fleet_restart_pending_marker; '
                     + f'_write_fleet_restart_pending_marker(expected_sha={args.product_sha!r})'], 'seed-pending')
                marker = clover_home / 'fleet_restart_pending'
                (evidence / 'seeded-pending-marker.txt').write_bytes(marker.read_bytes())
                unchanged = json.loads((clover_home / 'gateway_state.json').read_text())
                if unchanged.get('pid') != state['pid'] or unchanged.get('code_sha') != args.old_sha:
                    raise ValueError('staged-source fixture changed or re-attested the old runtime')
                report['pending_seed_worker'] = dict(pid=unchanged['pid'], code_sha=unchanged['code_sha'])
            chat.send('/update')
            chat_started = True
            wait_until(lambda: (clover_home / '.update_pending.json').is_file(), 'real chat update handler did not accept command')
            (evidence / 'chat-pending-origin.json').write_bytes((clover_home / '.update_pending.json').read_bytes())
            wait_until(lambda: (clover_home / 'logs/update_receipts/latest.json').is_file(),
                       'chat updater did not publish its real run receipt')
            chat.record('controller_releases_accepted_turn')
            chat.release.set()
            wait_until(lambda: chat.bot_messages().count(chat.token) == 1, 'accepted turn did not deliver its complete reply')
            wait_until(lambda: (clover_home / '.update_exit_code').is_file(), 'chat updater did not finish', seconds=900)
            exit_code = int((clover_home / '.update_exit_code').read_text().strip())
            chat_finished = True
            wait_until(lambda: sum('Clover update finished' in m for m in chat.bot_messages()) == 1,
                       'chat completion report missing or duplicated', seconds=180)
            output = clover_home / '.update_output.txt'
            if output.is_file():
                (evidence / 'update.log').write_bytes(output.read_bytes())
        report['updater_exit_code'] = exit_code
        receipt = clover_home / 'logs/update_receipts/latest.json'
        if receipt.is_file():
            (evidence / 'actual-update-receipt.json').write_bytes(receipt.read_bytes())
        if exit_code != 0:
            raise ValueError(f'frozen CLI updater failed with exit {exit_code}')
        run([py, root / '.github/native-proof/macos_wrapper_state.py', '--source', root,
             '--guard-sha', args.product_sha, '--home', clover_home, '--sha', args.product_sha, '--seconds', '90',
             '--previous', evidence / 'before-state.json', '--output', evidence / 'after-state.json'],
            'after-state', timeout=300)
        run([py, root / '.github/native-proof/proof.py', 'receipt', '--home', clover_home,
             '--begin', evidence / 'begin.json', '--old', args.product_sha if args.scenario == 'pending-chat' else args.old_sha, '--target', args.product_sha,
             '--output', evidence / 'update-receipt.json'], 'receipt')
        run([py, root / '.github/native-proof/proof.py', 'identity', '--repo', install,
             '--sha', args.product_sha, '--tree', git('rev-parse', args.product_sha + '^{tree}'),
             '--output', evidence / 'after-imports.json'], 'after-imports')
        if chat is not None:
            messages = chat.bot_messages()
            if (messages.count(chat.token) != 1 or chat.tool_calls != 1
                    or effect.read_text() != 'ONE_NATIVE_TOOL_EFFECT\n'
                    or sum('Clover update finished' in m for m in messages) != 1):
                raise ValueError('chat turn/tool effect/completion was missing or replayed')
            report['chat_update'] = 'PASS'
            report['accepted_turn_tool_effect_reply'] = 'PASS'
            report['chat_counts'] = dict(tool_calls=chat.tool_calls, complete_turn_replies=messages.count(chat.token),
                                        update_completion_reports=sum('Clover update finished' in m for m in messages))
            (evidence / 'chat-user-visible-messages.json').write_text(json.dumps(messages, indent=2) + '\n')
            if args.scenario == 'pending-chat':
                if (clover_home / 'fleet_restart_pending').exists():
                    raise ValueError('pending restart intent was not resolved after verified restart')
                report['pending_no_pull_catchup'] = 'PASS'
        report['passed'] = True
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if chat is not None:
            chat.release.set()
        if owned and (updater is None or updater.poll() is not None) and (not chat_started or chat_finished):
            try:
                run([install / 'venv/bin/clover', 'gateway', 'stop'], 'owned-cleanup', timeout=90)
                report['label_absent_after_cleanup'] = subprocess.run(
                    ['/bin/launchctl', 'print', target], capture_output=True).returncode != 0
            except Exception as exc:
                report['cleanup_error'] = type(exc).__name__
                report['passed'] = False
        if clover_home.exists():
            for name in ('gateway.log', 'gateway.error.log'):
                log = clover_home / 'logs' / name
                if log.is_file():
                    (evidence / name).write_bytes(log.read_bytes()[-200000:])
        if chat is not None:
            chat.close()
        for name in ('.update_output.txt', '.update_exit_code'):
            path = clover_home / name
            if path.is_file():
                (evidence / (name.lstrip('.') + '.diagnostic')).write_bytes(path.read_bytes()[-200000:])
        (evidence / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
