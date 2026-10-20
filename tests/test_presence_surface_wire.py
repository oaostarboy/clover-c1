"""Offline native PTB serialization for the provisional W3 data contract.

This is NOT gateway ingress/authorization/claim integration. Telegram HTTP is
replaced with a synthetic endpoint. No real token, network, profile or model.
"""
from dataclasses import replace
import json
import socket

import pytest
from telegram import Bot, InlineKeyboardMarkup, Update
from telegram.request import BaseRequest

from gateway import presence_delivery as pd
from tests.gateway.test_presence_controls import synthetic_controls
from tests.gateway.test_presence_delivery import enabled_context, synthetic_offer_and_context


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("W3 contract tests must not use real networking")
    from tests.native_boundaries import private_socketpair_boundary
    pair, connect = private_socketpair_boundary(
        socket.socketpair, socket.socket.connect, forbidden)
    monkeypatch.setattr(socket, "socketpair", pair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


class SyntheticTelegramEndpoint(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, read_timeout=None,
                         write_timeout=None, connect_timeout=None, pool_timeout=None):
        params = request_data.parameters if request_data else {}
        name = url.rsplit("/", 1)[-1]
        self.calls.append((name, params))
        if name == "getMe":
            result = {"id": 123456, "is_bot": True, "first_name": "Synthetic bot"}
        else:
            result = {"message_id": 50, "date": 1800000000,
                      "chat": {"id": int(params.get("chat_id", 12345678)), "type": "private"},
                      "text": params.get("text", "Presence controls")}
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest.mark.asyncio
async def test_native_telegram_payload_ack_bound_buttons_and_serialized_callback_contract():
    _, offer, context = synthetic_offer_and_context()
    context = replace(enabled_context(context), binding=replace(context.binding,
                      principal_id="12345678", conversation_id="12345678"))
    prepared = pd.prepare_publication(offer, context).publication
    assert prepared is not None
    payload = pd.surface_payload(prepared, context)
    assert payload is not None
    endpoint = SyntheticTelegramEndpoint()
    bot = Bot("123456:synthetic-not-a-real-token", request=endpoint)
    async with bot:
        ack = await bot.send_message(**payload)
        assert ack.chat_id == 12345678 and ack.message_id == 50
        _, controls, control_context = synthetic_controls()
        control_context = replace(control_context, binding=context.binding,
                                  actor_id="12345678", message_id=str(ack.message_id))
        menu = controls.open_menu(control_context)
        assert hasattr(pd, "telegram_control_markup"), "native Telegram control data renderer is missing"
        markup = InlineKeyboardMarkup.de_json(pd.telegram_control_markup(menu), bot)
        await bot.edit_message_reply_markup(chat_id=ack.chat_id, message_id=ack.message_id,
                                           reply_markup=markup)
        native = next(params for method, params in endpoint.calls if method == "editMessageReplyMarkup")
        json.dumps(native["reply_markup"])
        buttons = [b for row in native["reply_markup"]["inline_keyboard"] for b in row]
        assert [b["text"] for b in buttons] == ["Inspect", "Correct", "Forget", "Stop everywhere"]
        raw = {"update_id": 1, "callback_query": {
            "id": "synthetic-tap", "chat_instance": "synthetic-chat", "data": buttons[-1]["callback_data"],
            "from": {"id": 12345678, "is_bot": False, "first_name": "Synthetic person"},
            "message": ack.to_dict(),
        }}
        query = Update.de_json(raw, bot).callback_query
        assert query is not None and query.message is not None
        current = replace(control_context, actor_id=str(query.from_user.id),
                          message_id=str(query.message.message_id),
                          binding=replace(control_context.binding, conversation_id=str(query.message.chat.id),
                                          chat_type=query.message.chat.type))
        request = controls.request(query.data, current)
        assert request is not None
        assert request.action == "stop" and request.scope is None
        assert controls.request(query.data, current) is None
    assert [m for m, _ in endpoint.calls] == ["getMe", "sendMessage", "editMessageReplyMarkup"]
    send = next(p for m, p in endpoint.calls if m == "sendMessage")
    assert "parse_mode" not in send
    assert send["disable_notification"] is True
