"""`clover -z ... --activity-events`: a real worker process, a local mock model.

The worker runs this checkout's CLI in a subprocess against an in-process
OpenAI-compatible mock (no network, no credentials, disposable CLOVER_HOME)
and makes a real terminal tool call. Asserts the documented contract: stdout
carries only the final answer; stderr carries versioned, redacted JSONL
activity (tool start/outcome, public note, result) and never reasoning.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SECRET = "sk-ant-api03-" + "C" * 48


def _handler(queue, fail_once=False):
    class Mock(BaseHTTPRequestHandler):
        _failed = False
        def do_POST(self):  # noqa: N802
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if fail_once and req.get('tools') and not type(self)._failed:
                type(self)._failed = True
                body = json.dumps({'error': {'message': 'test-owned temporary overload', 'type': 'server_error'}}).encode()
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            msg = queue.pop(0) if (req.get("tools") and queue) else {"content": "ok", "tool_calls": None}
            finish = "tool_calls" if msg["tool_calls"] else "stop"
            usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            if req.get("stream"):
                chunks = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}]
                if msg["content"]:
                    chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": msg["content"]}}]})
                for i, tc in enumerate(msg["tool_calls"] or []):
                    chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [dict(tc, index=i)]}}]})
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for c in chunks:
                    self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            body = json.dumps({"id": "m", "usage": usage, "choices": [{"index": 0, "finish_reason": finish,
                               "message": {"role": "assistant", "content": msg["content"],
                                           "tool_calls": msg["tool_calls"]}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            body = json.dumps({"data": [{"id": "mock-luna", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **k):
            pass

    return Mock


@pytest.fixture
def mock_worker_env(tmp_path, request):
    notes = tmp_path / "notes.txt"
    notes.write_text("smoke\n", encoding="utf-8")
    queue = [
        {"content": f"<think>PRIVATE plan with {SECRET}</think>Checking the notes file.",
         "tool_calls": [{"id": "call_1", "type": "function", "function": {
             "name": "terminal",
             "arguments": json.dumps({"command": f"echo {SECRET} && cat {notes}"})}}]},
        {"content": "The notes file says smoke.", "tool_calls": None},
    ]
    srv = HTTPServer(("127.0.0.1", 0), _handler(queue, fail_once=getattr(request,'param',False)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home = tmp_path / "worker_home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n"
        f"  base_url: http://127.0.0.1:{srv.server_address[1]}/v1\n"
        "  default: mock-luna\n  api_key: local-mock-key\n  context_length: 128000\n",
        encoding="utf-8",
    )
    yield home, tmp_path
    srv.shutdown()


def _run(home, cwd, *extra):
    env = dict(os.environ, CLOVER_HOME=str(home))
    return subprocess.run(
        [sys.executable, str(REPO / "clover"), "-z", "Read notes.txt", "-t", "terminal", *extra],
        env=env, cwd=str(cwd), capture_output=True, text=True, timeout=120,
    )


def test_activity_events_stream_is_structured_redacted_and_reasoning_free(mock_worker_env):
    home, cwd = mock_worker_env
    proc = _run(home, cwd, "--activity-events")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "The notes file says smoke."
    events = [json.loads(line) for line in proc.stderr.splitlines() if line.startswith("{")]
    assert all(e["clover_activity"] == 1 for e in events)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "result"
    assert "tool.started" in kinds and "tool.completed" in kinds and "note" in kinds
    states = [e['status'] for e in events if e['event'] == 'status']
    assert 'requesting' in states and 'waiting' in states and 'provider_result' in states
    assert states.index('requesting') < states.index('waiting') < states.index('provider_result')
    started = next(e for e in events if e["event"] == "tool.started")
    assert started["tool"] == "terminal" and "cat" in started["summary"]
    completed = next(e for e in events if e["event"] == "tool.completed")
    assert completed["is_error"] is False
    notes = [e["text"] for e in events if e["event"] == "note"]
    assert notes == ["Checking the notes file."]
    assert events[-1] == {"clover_activity": 1, "event": "result", "status": "completed",
                          "text": "The notes file says smoke."}
    assert SECRET not in proc.stderr and "PRIVATE" not in proc.stderr
    assert "smoke\n" not in proc.stderr  # tool output is never written


@pytest.mark.parametrize('mock_worker_env',[True],indirect=True)
def test_actual_retry_producer_emits_bounded_public_status(mock_worker_env):
    home,cwd=mock_worker_env
    proc=_run(home,cwd,'--activity-events')
    assert proc.returncode==0, proc.stderr
    events=[json.loads(line) for line in proc.stderr.splitlines() if line.startswith('{')]
    states=[e['status'] for e in events if e['event']=='status']
    assert 'retrying' in states
    assert states.index('retrying') < states.index('provider_result')
    assert events[-1]['status']=='completed'
    assert 'test-owned temporary overload' not in proc.stderr


def test_without_flag_stderr_stays_quiet(mock_worker_env):
    home, cwd = mock_worker_env
    proc = _run(home, cwd)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "The notes file says smoke."
    assert "clover_activity" not in proc.stderr
