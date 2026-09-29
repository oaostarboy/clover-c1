"""Minimal OpenAI-compatible model server for the Windows update e2e (D5).

A chat request whose messages contain "LONG-TURN" is a slow agent turn: it
streams a "." every 3 s for <slow_seconds>, then "LONG-TURN-DONE". Every
other request (titles, auxiliary calls) answers "ok" at once. The runner
starts the long turn, waits for "long turn started" in the log, then runs
the update; the turn must come back complete, not interrupted.

Usage: python fake_llm.py <log_file> <port> <slow_seconds>
Runs on the runner's system Python, never the Clover venv.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = sys.argv[1]
PORT = int(sys.argv[2])
SLOW = float(sys.argv[3])
MODEL = "fake-slow"

_lock = threading.Lock()


def log(text: str) -> None:
    with _lock, open(LOG, "a", encoding="utf-8") as fh:
        fh.write(time.strftime("%H:%M:%S ") + text + "\n")


def _chunk(content: str | None, finish: str | None = None, role: bool = False) -> bytes:
    delta: dict = {}
    if role:
        delta["role"] = "assistant"
    if content is not None:
        delta["content"] = content
    body = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return b"data: " + json.dumps(body).encode("utf-8") + b"\n\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):  # keep stderr quiet
        pass

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        log(f"GET {self.path}")
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "e2e"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            req = {}
        if not self.path.rstrip("/").endswith("/chat/completions"):
            log(f"POST {self.path} -> 404")
            self._json(404, {"error": {"message": "not found"}})
            return
        long_turn = "LONG-TURN" in json.dumps(req.get("messages") or [])
        stream = bool(req.get("stream"))
        log(f"POST {self.path} stream={stream} long={long_turn}")
        if long_turn:
            log("long turn started")
        try:
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(_chunk("", role=True))
                self.wfile.flush()
                if long_turn:
                    end = time.monotonic() + SLOW
                    while time.monotonic() < end:
                        time.sleep(3)
                        self.wfile.write(_chunk("."))
                        self.wfile.flush()
                self.wfile.write(_chunk("LONG-TURN-DONE" if long_turn else "ok"))
                self.wfile.write(_chunk(None, finish="stop"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                if long_turn:
                    time.sleep(SLOW)
                self._json(200, {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": MODEL,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": "LONG-TURN-DONE" if long_turn else "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })
            if long_turn:
                log("long turn finished")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            log(f"client went away: {exc!r}" + (" (long turn CUT OFF)" if long_turn else ""))


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    log(f"-- listening on {PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
