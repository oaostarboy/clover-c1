"""Explicit terminal maintenance boundary; NOT automatic /update acceptance.

Use the released external-drain protocol without asking old code to exit.
Finish the real held turn, require its product-written durable ACK, then stop,
update, and always start through public commands. Never infer an ACK from a
receiver or rewrite an uncertain obligation. This fixture is scoped to one
known held turn with no other inbound sender; it is not a universal idle proof.
"""
from contextlib import closing
import json
import sqlite3
import subprocess
import time


def read_acknowledged_boundary(home, token):
    """Read the real outgoing payload, which may include a configured greeting."""
    with closing(sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT obligation_id,state,attempts,owner_pid FROM delivery_obligations WHERE instr(content, ?) > 0', (token,)).fetchall()
        pending = db.execute("SELECT count(*) FROM delivery_obligations WHERE state NOT IN ('delivered','abandoned')").fetchone()[0]
    if len(rows) == 1 and rows[0][1] == 'delivered' and pending == 0:
        return dict(obligation_id=rows[0][0], state=rows[0][1], attempts=rows[0][2], owner_pid=rows[0][3], pending=pending, at=time.time())
    return None


def perform(*, home, cli, py, env, E, TOKEN, before, run, wait):
    # These modules and public marker protocol exist on the released source.
    run([py, '-c', 'from gateway.drain_control import write_drain_request; '
         'write_drain_request(principal="terminal-maintenance", suppress_notification=True)'],
        'quiet-begin-external-drain')

    def paused():
        state = json.loads((home / 'gateway_state.json').read_text())
        assert state['pid'] == before['pid']
        assert not state.get('restart_requested'), 'external drain must not request process exit'
        return state if state.get('gateway_state') == 'draining' else None

    drain = wait(paused, 30)
    (E / 'quiet-held-drain.json').write_text(json.dumps(drain, indent=2))
    assert not (E / 'release').exists()
    (E / 'release').touch()

    def acknowledged():
        # A canonical transcript or receipt at the peer is not this assertion.
        return read_acknowledged_boundary(home, TOKEN)

    ack = wait(acknowledged, 120)
    assert ack['owner_pid'] == before['pid']
    (E / 'quiet-durable-ack.json').write_text(json.dumps(ack, indent=2))
    run([*cli, 'gateway', 'stop'], 'quiet-public-stop')
    props = subprocess.check_output(['systemctl', '--user', 'show', 'clover-gateway.service', '--property=ActiveState,MainPID'], env=env, text=True)
    (E / 'quiet-stopped-unit.txt').write_text(props)
    parsed = dict(line.split('=', 1) for line in props.splitlines() if '=' in line)
    assert parsed['MainPID'] == '0' and parsed['ActiveState'] == 'inactive', props
    began = time.time()
    # The external drain belongs to this maintenance operation, not the next
    # process. Cancel it only after the old gateway is stopped.
    run([py, '-c', 'from gateway.drain_control import clear_drain_request; clear_drain_request()'], 'quiet-clear-owned-drain')
    try:
        run([*cli, 'update', '--yes'], 'quiet-public-update')
    finally:
        run([*cli, 'gateway', 'start'], 'quiet-public-start')
    (E / 'quiet-boundary.json').write_text(json.dumps(dict(
        status='ACKNOWLEDGED_STOP_UPDATE_START_EXECUTED', began=began,
        public_commands=True, driver_owned_stop_start=True,
        automatic_updater_restart=False, held_turn_finished_before_stop=True,
        ambiguous_deliveries_suppressed=False), indent=2))
    return began
