"""Credential-free localhost IRC + deterministic model fixtures, not AI output.

The real installed gateway consumes this wire and executes its real file tool.
Only the private acceptance file is requested; no product imports or overrides.
"""
from __future__ import annotations
import http.server
import json
from pathlib import Path
import socketserver
import threading
import time
from typing import Any


class ChatWire:
    def __init__(self, evidence: Path, effect: Path):
        self.evidence, self.effect = evidence, effect
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.clients = []
        self.events = []
        self.token = 'MAC_NATIVE_TURN_COMPLETE'
        self.tool_calls = 0
        outer = self

        class IRC(socketserver.StreamRequestHandler):
            def handle(self):
                client = {'socket': self.request, 'nick': None, 'registered': False}
                with outer.lock:
                    outer.clients.append(client)
                try:
                    for raw in self.rfile:
                        line = raw.decode('utf-8', 'replace').rstrip('\r\n')
                        outer.record('irc_in', line=line)
                        parts = line.split(' ')
                        if parts[0] == 'NICK':
                            client['nick'] = parts[1]
                        elif parts[0] == 'USER':
                            self.wfile.write(f":fixture 001 {client['nick']} :Welcome\r\n".encode())
                            self.wfile.flush()
                            client['registered'] = True
                        elif parts[0] == 'PING':
                            self.wfile.write(('PONG ' + ' '.join(parts[1:]) + '\r\n').encode())
                            self.wfile.flush()
                        elif parts[0] == 'JOIN':
                            self.wfile.write(f":{client['nick']}!bot@localhost JOIN {parts[1]}\r\n".encode())
                            self.wfile.flush()
                        elif parts[0] == 'QUIT':
                            break
                finally:
                    with outer.lock:
                        if client in outer.clients:
                            outer.clients.remove(client)

        class Model(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
            def do_GET(self):
                data = json.dumps({'object': 'list', 'data': [{'id': 'native-fixture', 'object': 'model'}]}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(data)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                if not self.path.endswith('/chat/completions'):
                    outer.record('unsupported_model_path', path=self.path)
                    self.send_error(400, 'fixture supports chat completions only')
                    return
                messages = body.get('messages', [])
                primary = any('MAC_NATIVE_ACCEPTANCE' in str(m.get('content', '')) for m in messages if m.get('role') == 'user')
                has_tool = any(m.get('role') == 'tool' and m.get('tool_call_id') == 'call_native_effect' for m in messages)
                tools = [t.get('function', {}).get('name') for t in body.get('tools', [])]
                message: Any
                if primary and not has_tool:
                    if 'write_file' not in tools:
                        outer.record('missing_real_file_tool', tools=tools)
                        self.send_error(400, 'real write_file tool is required')
                        return
                    with outer.lock:
                        outer.tool_calls += 1
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': 'call_native_effect', 'type': 'function', 'function': {
                            'name': 'write_file', 'arguments': json.dumps({'path': str(outer.effect), 'content': 'ONE_NATIVE_TOOL_EFFECT\n'})}}]}
                    finish = 'tool_calls'
                    outer.record('model_tool_request', call_id='call_native_effect')
                elif primary and has_tool:
                    outer.record('accepted_turn_waiting_after_tool')
                    if not outer.release.wait(180):
                        self.send_error(504, 'controller did not release accepted turn')
                        return
                    message = {'role': 'assistant', 'content': outer.token}
                    finish = 'stop'
                    outer.record('model_complete_reply')
                else:
                    message = {'role': 'assistant', 'content': 'Local acceptance auxiliary response.'}
                    finish = 'stop'
                    outer.record('auxiliary_model_call')
                if body.get('stream'):
                    delta: Any = dict(message)
                    if 'tool_calls' in delta:
                        delta['tool_calls'] = [dict(index=0, **delta['tool_calls'][0])]
                    chunks = [dict(id='chatcmpl-local-fixture', object='chat.completion.chunk', created=int(time.time()), model='native-fixture', choices=[dict(index=0, delta=delta, finish_reason=None)]),
                              dict(id='chatcmpl-local-fixture', object='chat.completion.chunk', created=int(time.time()), model='native-fixture', choices=[dict(index=0, delta={}, finish_reason=finish)])]
                    data = (''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n').encode()
                    self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                else:
                    data = json.dumps(dict(id='chatcmpl-local-fixture', object='chat.completion', created=int(time.time()), model='native-fixture', choices=[dict(index=0, message=message, finish_reason=finish)], usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))).encode()
                    self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

        class TCP(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
        self.irc = TCP(('127.0.0.1', 0), IRC)
        self.model = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Model)
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.irc, self.model)]

    def start(self):
        for thread in self.threads:
            thread.start()

    def record(self, kind, **fields):
        with self.lock:
            row = dict(kind=kind, at=time.time(), **fields)
            self.events.append(row)
            with (self.evidence / 'chat-wire.jsonl').open('a') as f:
                f.write(json.dumps(row) + '\n')

    def ready(self):
        with self.lock:
            return any(c['registered'] for c in self.clients)

    def send(self, text):
        with self.lock:
            live = [c for c in self.clients if c['registered']]
            if not live:
                raise RuntimeError('no registered private IRC client')
            client = live[-1]
            client['socket'].sendall(f":ant!ant@localhost PRIVMSG {client['nick']} :{text}\r\n".encode())
        self.record('human_message_sent', text=text)

    def bot_messages(self):
        with self.lock:
            return [e['line'].split(' :', 1)[1] for e in self.events
                    if e['kind'] == 'irc_in' and e['line'].startswith('PRIVMSG ant :')]

    def close(self):
        self.release.set()
        for server in (self.model, self.irc):
            server.shutdown(); server.server_close()
