"""Notices over a REAL ``RelayAdapter`` and its in-memory connector.

``RelayAdapter.connect`` never sets the inherited ``is_connected`` flag, and a
notice has no fresh inbound turn to prime the adapter's per-chat caches, so the
sender must (a) judge readiness the way outbound replies on a relay route do
(the authenticated transport fronts the logical platform) and (b) send through
``send_for_platform`` with the logical platform and the route's persisted
tenant/user metadata.
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


def _descriptor(platform="discord") -> CapabilityDescriptor:
    return CapabilityDescriptor(
        contract_version=CONTRACT_VERSION, platform=platform, label=platform.title(),
        max_message_length=2000, supports_draft_streaming=False, supports_edit=True,
        supports_threads=True, markdown_dialect=platform, len_unit="chars",
        emoji="x", platform_hint="", pii_safe=False,
    )


@pytest.fixture
def world(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = db
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="C1", chat_type="channel", user_id="U1",
        scope_id="G1", delivered_via_upstream_relay=True,
    )
    entry = store.get_or_create_session(source)
    connector = StubConnector(_descriptor())
    relay = RelayAdapter(PlatformConfig(enabled=True), _descriptor(), transport=connector)
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._session_db = AsyncSessionDB(db)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner.config = GatewayConfig()
    runner.adapters = {Platform.RELAY: relay}
    runner._session_source_cache = {}
    db.inbox_put({
        "key": "deleg:d1", "profile": "default", "platform": "discord", "chat_id": "C1",
        "thread_id": None, "session_key": entry.session_key, "owner_root_id": entry.session_id,
        "kind": "delegation", "wake": 0, "title": "Audit", "payload_json": "{}", "shown_to_user": 0,
    })
    db.inbox_drop("deleg:d1", "unowned:test")
    try:
        yield db, relay, connector, runner
    finally:
        db.close()


@pytest.mark.asyncio
async def test_notice_reaches_a_connected_relay_with_platform_and_tenant_metadata(world):
    db, relay, connector, runner = world
    assert await relay.connect() is True
    assert connector.connected and relay.is_connected is False  # the trap

    out = await sweep_inbox_notices(AsyncSessionDB(db), runner._send_inbox_notice)

    assert out["sent"] == 1, out
    (action,) = connector.sent
    assert (action["op"], action["chat_id"]) == ("send", "C1")
    assert action["metadata"]["scope_id"] == "G1"
    assert action["metadata"]["user_id"] == "U1"
    assert "Background result not delivered" in action["content"]
    assert connector.sent_platforms == ["discord"]
    assert db.inbox_get("deleg:d1")["notice_state"] == "sent"


@pytest.mark.asyncio
async def test_notice_is_deferred_until_the_relay_fronts_the_platform(world):
    db, relay, connector, runner = world
    connector._identities = []  # handshake identity set not established yet

    out = await sweep_inbox_notices(AsyncSessionDB(db), runner._send_inbox_notice)

    assert out["deferred"] == 1 and connector.sent == []
    assert db.inbox_get("deleg:d1")["notice_state"] == "pending"
