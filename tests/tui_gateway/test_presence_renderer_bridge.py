"""Actual Python application adapter <-> mounted TS renderer over offline file IPC.

Only provider HTTP and login transport are synthetic. The ACK is dispatched to
that live authenticated owner's real RPC handler while its publication waits.
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
    receipts = []
    def write(frame):
        if frame.get('params',{}).get('type') != 'presence.initiative':
            return native_write(frame)
        transport.frames.append(frame)
        fixture.write_text(json.dumps(frame))
        command = [node, str(vitest), 'run', '--project', 'ui',
            'src/app/chat/presence-native-bridge.test.tsx', '--maxWorkers', '1', '--no-file-parallelism']
        child = subprocess.Popen(command, cwd=root/'apps/desktop',
            env=dict(os.environ, CLOVER_PRESENCE_NATIVE_FRAME=str(fixture)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic()+25
        response = None
        try:
            while child.poll() is None:
                rpc_path = Path(str(fixture)+'.rpc.json')
                if rpc_path.exists() and response is None:
                    request = json.loads(rpc_path.read_text())
                    # This is the real renderer's request, not a guessed ACK.
                    assert request['method'] == 'presence.ack'
                    response = server.dispatch(request, transport=transport)
                    Path(str(fixture)+'.response.json').write_text(json.dumps(response))
                assert time.monotonic() < deadline, 'mounted native renderer stalled'
                time.sleep(0.005)
            output, _ = child.communicate(timeout=3)
            receipts.append((child.returncode, output, response,
                json.loads(Path(str(fixture)+'.rendered.json').read_text())
                if Path(str(fixture)+'.rendered.json').exists() else None))
            return child.returncode == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    monkeypatch.setattr(transport, 'write', write)
    try:
        send('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.')
        complete = send('Help verify deployment packaging')
        reply = server.dispatch({'id':'after','method':'presence.after_reply', 'params':{
            'session_id':sid,'token':complete['presence_after_reply_token']}}, transport=transport)
        assert reply['result']['accepted']
        deadline = time.monotonic()+30
        while not receipts:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        code, output, response, rendered = receipts[0]
        record_property('renderer_command_output', output)
        record_property('actual_owner_ack', json.dumps(response))
        record_property('actual_server_frame', fixture.read_text())
        record_property('actual_auxiliary_request', json.dumps(requests))
        record_property('actual_renderer_state', json.dumps(rendered))
        assert code == 0, output
        assert response['result']['accepted'] is True
        assert rendered['accepted'] and rendered['activeElement'] == 'BODY'
        assert len(rendered['controls']) == 4
        assert fixture.exists() and json.loads(fixture.read_text())['params']['payload']['text'] in rendered['text']
        while ThoughtStore().publications()[0]['state'] == 'attempting':
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert ThoughtStore().publications()[0]['state'] == 'acknowledged'
        assert len(requests) == 1
        assert [r['role'] for r in h.db.get_messages('desktop-stored') if r['role'] != 'session_meta'] == ['user','assistant','user','assistant']
    finally:
        agent.close()
        h.close()
