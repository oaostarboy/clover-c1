"""A notice sent over a relay must never seal the user's live answer stream.

Real ``RelayAdapter`` + in-memory connector. A native draft (Slack's
stream-is-the-message mode) is open in the chat; one notice sweep runs. An
unmarked relay send would be taken for the turn-final: the connector would get
``draft(final=True)`` carrying the notice text and the answer's remaining
frames would be swallowed by the seal tombstone.
"""

from __future__ import annotations

import pytest

from clover_state import AsyncSessionDB, SessionDB
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.inbox_notices import sweep_inbox_notices
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from tests.gateway.relay.stub_connector import StubConnector


def _descriptor(platform: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        contract_version=CONTRACT_VERSION, platform=platform, label=platform.title(),
        max_message_length=4000, supports_draft_streaming=True, supports_edit=True,
        supports_threads=True, markdown_dialect="mrkdwn", len_unit="chars",
        emoji="x", platform_hint="", pii_safe=False,
        supported_ops=("send", "edit", "draft"),
    )


async def _world(tmp_path, platform, thread_id):
    db = SessionDB(db_path=tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = db
    plat = Platform(platform)
    source = SessionSource(
        platform=plat, chat_id="C1", chat_type="channel", user_id="U1", scope_id="T1",
        thread_id=thread_id, delivered_via_upstream_relay=True,
    )
    entry = store.get_or_create_session(source)
    connector = StubConnector(_descriptor(platform))
    relay = RelayAdapter(PlatformConfig(enabled=True), _descriptor(platform), transport=connector)
    assert await relay.connect() is True
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._session_db = AsyncSessionDB(db)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner.config = GatewayConfig()
    runner.adapters = {Platform.RELAY: relay}
    runner._session_source_cache = {}
    db.inbox_put({
        "key": "deleg:d1", "profile": "default", "platform": platform, "chat_id": "C1",
        "thread_id": thread_id, "session_key": entry.session_key,
        "owner_root_id": entry.session_id, "kind": "delegation", "wake": 0,
        "title": "Audit", "payload_json": "{}", "shown_to_user": 0,
    })
    db.inbox_drop("deleg:d1", "unowned:test")
    return db, relay, connector, runner


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "discord"])
@pytest.mark.parametrize("thread_id", [None, "1700.0001"])
async def test_notice_does_not_seal_an_open_native_draft(tmp_path, platform, thread_id):
    db, relay, connector, runner = await _world(tmp_path, platform, thread_id)
    try:
        draft_meta = {"thread_id": thread_id} if thread_id else {}
        assert (await relay.send_draft("C1", 7, "partial answer", metadata=draft_meta)).success
        armed_before = dict(relay._open_draft_by_chat)
        assert armed_before == ({} if platform == "discord" else armed_before)
        before = len(connector.sent)

        out = await sweep_inbox_notices(AsyncSessionDB(db), runner._send_inbox_notice)

        assert out["sent"] == 1, out
        frames = connector.sent[before:]
        assert [f["op"] for f in frames] == ["send"], frames
        assert "Background result not delivered" in frames[0]["content"]
        assert not any(f["op"] == "draft" and f.get("final") for f in connector.sent), \
            "the notice sealed the user's answer stream"
        assert relay._open_draft_by_chat == armed_before  # still open
        assert "_interim_send" not in frames[0]["metadata"]  # internal marker never reaches the wire

        # The answer's next fragment still lands in the draft instead of being swallowed.
        await relay.send_draft("C1", 7, "partial answer, continued", metadata=draft_meta)
        assert connector.sent[-1]["op"] == "draft" and connector.sent[-1]["final"] is False
        assert connector.sent[-1]["content"] == "partial answer, continued"
    finally:
        db.close()
