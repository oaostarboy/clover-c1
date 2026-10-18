"""Actual owner <-> mounted TS renderer over offline file IPC.

Only HTTP and login are synthetic. Bootstrap readiness precedes issuance of the
unchanged production five-second ACK capability; no fake ACK or TTL override.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from cron.presence import ThoughtStore
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.tui_gateway.test_presence_integration import desktop_host
from tui_gateway import server


def test_actual_desktop_native_frame_mounts_and_roundtrips_owner_ack(monkeypatch, tmp_path, record_property):
    root = Path(__file__).resolve().parents[2]
    node = shutil.which('node')
    vitest = root/'node_modules/vitest/vitest.mjs'
    if not node or not vitest.is_file():
        pytest.skip('Native renderer bridge requires isolated desktop npm dependencies and Node')
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    native_write = transport.write
    fixture = tmp_path/'actual-native-frame.json'
    ready = Path(str(fixture)+'.ready.json')
    receipts = []
    command = [node, str(vitest), 'run', '--project', 'ui',
        'src/app/chat/presence-native-bridge.test.tsx', '--maxWorkers', '1', '--no-file-parallelism']
    # A log file cannot deadlock on a full PIPE during fresh bundler startup.
    log = (tmp_path/'renderer.log').open('w+')
    child = subprocess.Popen(command, cwd=root/'apps/desktop',
        env=dict(os.environ, CLOVER_PRESENCE_NATIVE_FRAME=str(fixture),
                 CLOVER_PRESENCE_NATIVE_SESSION=sid), stdout=log, stderr=subprocess.STDOUT, text=True)

    def write_json(path, value):
        staged = Path(str(path)+'.tmp')
        staged.write_text(json.dumps(value))
        staged.replace(path)

    def output():
        log.flush()
        log.seek(0)
        return log.read()

    def write(frame):
        if frame.get('params',{}).get('type') != 'presence.initiative':
            return native_write(frame)
        assert ready.exists(), 'owner issued live publication before mounted renderer readiness'
        transport.frames.append(frame)
        write_json(fixture, frame)
        deadline = time.monotonic()+15
        response = None
        while child.poll() is None:
            rpc_path = Path(str(fixture)+'.rpc.json')
            if rpc_path.exists() and response is None:
                request = json.loads(rpc_path.read_text())
                assert request['method'] == 'presence.ack'
                # Actual renderer request -> actual live owner, including expiry.
                response = server.dispatch(request, transport=transport)
                write_json(Path(str(fixture)+'.response.json'), response)
            assert time.monotonic() < deadline, 'mounted native renderer stalled'
            time.sleep(0.005)
        rendered = Path(str(fixture)+'.rendered.json')
        receipts.append((child.returncode, output(), response,
                         json.loads(rendered.read_text()) if rendered.exists() else None))
        return child.returncode == 0

    monkeypatch.setattr(transport, 'write', write)
    try:
        startup_deadline = time.monotonic()+45
        while not ready.exists():
            assert child.poll() is None, output()
            assert time.monotonic() < startup_deadline, 'renderer never reported mounted readiness'
            time.sleep(0.01)
        readiness = json.loads(ready.read_text())
        assert readiness['session_id'] == sid and readiness['activeElement'] == 'BODY'
        record_property('mounted_renderer_readiness', json.dumps(readiness))
        send('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.')
        complete = send('Help verify deployment packaging')
        reply = server.dispatch({'id':'after','method':'presence.after_reply', 'params':{
            'session_id':sid,'token':complete['presence_after_reply_token']}}, transport=transport)
        assert reply['result']['accepted']
        deadline = time.monotonic()+20
        while not receipts:
            assert time.monotonic() < deadline, output()
            time.sleep(0.02)
        code, renderer_output, response, rendered = receipts[0]
        record_property('renderer_command_output', renderer_output)
        record_property('actual_owner_ack', json.dumps(response))
        record_property('actual_server_frame', fixture.read_text())
        record_property('actual_auxiliary_request', json.dumps(requests))
        record_property('actual_renderer_state', json.dumps(rendered))
        assert code == 0, renderer_output
        assert response['result']['accepted'] is True
        assert rendered['accepted'] and rendered['activeElement'] == 'BODY'
        assert len(rendered['controls']) == 4
        assert json.loads(fixture.read_text())['params']['payload']['text'] in rendered['text']
        while ThoughtStore().publications()[0]['state'] == 'attempting':
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert ThoughtStore().publications()[0]['state'] == 'acknowledged'
        assert len(requests) == 1
        assert [r['role'] for r in h.db.get_messages('desktop-stored') if r['role'] != 'session_meta'] == ['user','assistant','user','assistant']
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        log.close()
        agent.close()
        h.close()
