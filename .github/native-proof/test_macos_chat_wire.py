"""Local wire-fixture checks only; native acceptance executes real gateway IO."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from typing import Any
from openai import OpenAI
from macos_chat_wire import ChatWire

ROOT = Path(__file__).resolve().parents[2]


class ChatWireTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / 'artifacts/suni-ownership/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wire = ChatWire(self.root, self.root / 'effect')
        self.wire.start()
        self.addCleanup(self.wire.close)
        self.client: Any = OpenAI(api_key='local-dummy', base_url=f'http://127.0.0.1:{self.wire.model.server_address[1]}/v1', max_retries=0)
        self.addCleanup(self.client.close)
        self.messages: Any = [{'role': 'user', 'content': 'MAC_NATIVE_ACCEPTANCE'}]
        self.tools: Any = [{'type': 'function', 'function': {'name': 'write_file', 'parameters': {'type': 'object'}}}]

    def test_sdk_nonstream_requests_one_file_tool_without_faking_its_effect(self):
        reply = self.client.chat.completions.create(model='native-fixture', messages=self.messages, tools=self.tools)
        call = reply.choices[0].message.tool_calls[0]
        self.assertEqual(call.function.name, 'write_file')
        self.assertEqual(json.loads(call.function.arguments), {'path': str(self.root / 'effect'), 'content': 'ONE_NATIVE_TOOL_EFFECT\n'})
        self.assertEqual(self.wire.tool_calls, 1)
        self.assertFalse((self.root / 'effect').exists(), 'only the real gateway may create this effect')

    def test_sdk_stream_preserves_tool_call(self):
        chunks = list(self.client.chat.completions.create(model='native-fixture', messages=self.messages, tools=self.tools, stream=True))
        self.assertEqual(chunks[0].choices[0].delta.tool_calls[0].function.name, 'write_file')
        self.assertEqual(chunks[-1].choices[0].finish_reason, 'tool_calls')

    def test_complete_reply_waits_for_controller_release(self):
        messages = self.messages + [{'role': 'tool', 'tool_call_id': 'call_native_effect', 'content': 'fixture tool protocol result'}]
        replies = []
        thread = threading.Thread(target=lambda: replies.append(self.client.chat.completions.create(model='native-fixture', messages=messages)))
        thread.start()
        deadline = time.monotonic() + 5
        while not any(e['kind'] == 'accepted_turn_waiting_after_tool' for e in self.wire.events):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(replies, [])
        self.wire.release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(replies[0].choices[0].message.content, self.wire.token)

    def test_real_irc_socket_registration_human_delivery_and_reply_capture(self):
        with socket.create_connection(('127.0.0.1', self.wire.irc.server_address[1]), timeout=3) as sock:
            sock.sendall(b'NICK cloverbot\r\nUSER bot 0 * :bot\r\n')
            self.assertIn(b'001 cloverbot', sock.recv(4096))
            deadline = time.monotonic() + 3
            while not self.wire.ready():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            self.wire.send('/update')
            self.assertIn(b'PRIVMSG cloverbot :/update', sock.recv(4096))
            sock.sendall(('PRIVMSG ant :' + self.wire.token + '\r\n').encode())
            while not self.wire.bot_messages():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            self.assertEqual(self.wire.bot_messages(), [self.wire.token])


if __name__ == '__main__':
    unittest.main(verbosity=2)
