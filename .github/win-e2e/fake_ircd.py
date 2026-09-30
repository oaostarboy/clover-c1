"""Minimal local IRC server for the Windows /update e2e.

Stands in for the messaging platform so the REAL gateway handler
(gateway/slash_commands.py _handle_update_command) runs inside the gateway
the Windows launcher started, exactly like /update from Telegram: IRC is on
the /update allow-list and needs no external service.

Usage: python fake_ircd.py <log_file> <trigger_file> [port]
When <trigger_file> appears, the newest registered client receives a DM
"/update" from nick "ant"; the file is then renamed to <trigger_file>.sent.
Every line from the bot is logged ("<<"), every line to it ("->").
Runs on the runner's system Python, never the Clover venv (a venv process
would count as a venv holder).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

LOG = sys.argv[1]
TRIGGER = sys.argv[2]
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 6667

_lock = threading.Lock()
_clients: list[dict] = []


def log(text: str) -> None:
    with _lock, open(LOG, "a", encoding="utf-8") as fh:
        fh.write(time.strftime("%H:%M:%S ") + text + "\n")


def send(client: dict, line: str) -> None:
    try:
        client["sock"].sendall((line + "\r\n").encode("utf-8"))
        log("-> " + line)
    except OSError as exc:
        log(f"!! send failed: {exc}")


def handle(sock: socket.socket) -> None:
    client = {"sock": sock, "nick": None, "registered": False}
    with _lock:
        _clients.append(client)
    buf = b""
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                log("<< " + line)
                parts = line.split(" ")
                cmd = parts[0].upper() if parts else ""
                if cmd == "NICK" and len(parts) > 1:
                    client["nick"] = parts[1]
                elif cmd == "USER":
                    send(client, f":fake 001 {client['nick']} :Welcome to the fake network")
                    client["registered"] = True
                elif cmd == "PING":
                    token = parts[1].lstrip(":") if len(parts) > 1 else ""
                    send(client, f":fake PONG fake :{token}")
                elif cmd == "JOIN" and len(parts) > 1:
                    send(client, f":{client['nick']}!bot@localhost JOIN {parts[1]}")
                elif cmd == "QUIT":
                    return
    except OSError:
        pass
    finally:
        with _lock:
            if client in _clients:
                _clients.remove(client)
        log(f"-- client {client['nick']} disconnected")


def trigger_loop() -> None:
    while True:
        time.sleep(0.5)
        if not os.path.exists(TRIGGER):
            continue
        with _lock:
            live = [c for c in _clients if c["registered"]]
        if not live:
            continue
        target = live[-1]
        send(target, f":ant!ant@localhost PRIVMSG {target['nick']} :/update")
        os.replace(TRIGGER, TRIGGER + ".sent")


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(8)
    log(f"-- listening on {PORT}")
    threading.Thread(target=trigger_loop, daemon=True).start()
    while True:
        conn, _ = srv.accept()
        log("-- client connected")
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
