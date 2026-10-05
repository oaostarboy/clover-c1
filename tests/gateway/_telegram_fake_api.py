"""In-memory Telegram Bot API connector for native-progress tests.

Stands in ONLY for the network edge (``telegram.Bot``): the production
``TelegramAdapter``, ``GatewayStreamConsumer`` and gateway runner run
unmodified on top of it.  Every call is appended to ``calls`` in order, so
tests can assert on the exact wire traffic a user's client would receive.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


class FakeTelegramApi:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.call_times: List[float] = []
        self._ids = itertools.count(1000)
        # method name -> exception instance | callable(kwargs) -> result | awaitable
        self.fail: Dict[str, Any] = {}
        # method name -> seconds to sleep before answering (simulates network)
        self.delay: Dict[str, float] = {}
        # method name -> asyncio.Event that must be set before answering
        self.gate: Dict[str, asyncio.Event] = {}
        self.sticker_sets: Dict[str, Any] = {}
        self.accepted_draft_frames: List[Dict[str, Any]] = []

    # ── helpers ────────────────────────────────────────────────────────────
    def methods(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        return [kw for m, kw in self.calls if name is None or m == name]

    def rich_drafts(self) -> List[Dict[str, Any]]:
        return [
            kw["api_kwargs"] for m, kw in self.calls
            if m == "do_api_request:sendRichMessageDraft"
        ]

    async def _enter(self, key: str, kwargs: Dict[str, Any]) -> None:
        self.calls.append((key, kwargs))
        self.call_times.append(time.monotonic())
        method = key.split(":")[-1]
        gate = self.gate.get(method)
        if gate is not None:
            await gate.wait()
        delay = self.delay.get(method)
        if delay:
            await asyncio.sleep(delay)
        failure = self.fail.get(method)
        if failure is not None:
            if callable(failure) and not isinstance(failure, BaseException):
                failure = failure(kwargs)
            if isinstance(failure, BaseException):
                raise failure

    # ── telegram.Bot surface used by the adapter ───────────────────────────
    async def do_api_request(self, endpoint: str, api_kwargs: Optional[dict] = None, **kw):
        await self._enter(f"do_api_request:{endpoint}", {"api_kwargs": dict(api_kwargs or {}), **kw})
        if endpoint == "sendRichMessageDraft":
            self.accepted_draft_frames.append(dict(api_kwargs or {}))
            return True
        return SimpleNamespace(message_id=next(self._ids))

    async def send_message(self, **kwargs):
        recorded = dict(kwargs)
        await self._enter("send_message", recorded)
        message_id = next(self._ids)
        recorded["_message_id"] = message_id     # lets tests follow edits of one bubble
        return SimpleNamespace(message_id=message_id)

    async def send_message_draft(self, **kwargs):
        await self._enter("send_message_draft", dict(kwargs))
        return True

    async def edit_message_text(self, **kwargs):
        await self._enter("edit_message_text", dict(kwargs))
        return SimpleNamespace(message_id=kwargs.get("message_id"))

    async def delete_message(self, **kwargs):
        await self._enter("delete_message", dict(kwargs))
        return True

    async def send_chat_action(self, **kwargs):
        await self._enter("send_chat_action", dict(kwargs))
        return True

    async def get_sticker_set(self, name: str, **kwargs):
        await self._enter("get_sticker_set", {"name": name, **kwargs})
        result = self.sticker_sets.get(name)
        if result is None:
            raise LookupError("STICKERSET_INVALID")
        return result
