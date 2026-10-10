import asyncio
import concurrent.futures
import threading
import time

import pytest
from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

from gateway.browser_control_broker import (
    ControllerCancelled,
    ControllerRejected,
    ControllerScope,
    ControllerUnavailable,
)
from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _browser_controller_ws_sender,
)
from tools.browser_extension_router import route_browser_tool


API_KEY = "-".join(("fixture", "neutral", "api", "key", "123"))
CONTROL_PROTOCOL = "clover-browser-control-v1"
REAL_BROWSER_CAPABILITIES = {
    "browser_back",
    "browser_click",
    "browser_navigate",
    "browser_press",
    "browser_screenshot",
    "browser_scroll",
    "browser_snapshot",
    "browser_tab_activate",
    "browser_tabs",
    "browser_type",
}


class _SessionDB:
    def __init__(self):
        self.sessions = {
            "session-fixture": {"id": "session-fixture", "source": "api_server"},
            "remote-session-fixture": {
                "id": "remote-session-fixture",
                "source": "api_server",
            },
        }

    def get_session(self, session_id):
        return self.sessions.get(session_id)


def _ticket_protocol(ticket):
    return f"clover-browser-control-ticket.{ticket}"


def _adapter(*, key=API_KEY):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": key} if key else {})
    )
    adapter._session_db = _SessionDB()
    return adapter


def _app(adapter):
    app = web.Application()
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    app.router.add_post(
        "/v1/browser-control/register", adapter._handle_browser_control_register
    )
    app.router.add_get(
        "/v1/browser-control/ws", adapter._handle_browser_control_ws
    )
    return app


async def _wait_for_controller_ready(
    broker, *, session_id, principal_id, transport_family, capability, owner, timeout
):
    """Wait for real identity-bound broker selection, never for HTTP 101."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        scope = broker.scope_for_session(
            session_id=session_id,
            task_id=None,
            principal_id=principal_id,
            transport_family=transport_family,
        )
        if (
            scope is not None
            and broker.select(scope, capability) is not None
            and broker.is_owner(scope, owner)
        ):
            return scope
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(
                f"browser controller not ready for {transport_family}/{capability}"
            )
        await asyncio.sleep(min(0.01, remaining))


def _registration_body(**overrides):
    payload = {
        "protocol_version": 1,
        "controller_id": "controller-fixture",
        "browser_profile_id": "browser-profile-fixture",
        "session_id": "session-fixture",
        "capabilities": ["controller.noop", "browser_navigate"],
        "principal_id": "spoofed-client-principal",
        "product": {
            "id": "chromium",
            "engine": "chromium",
            "label": "Chromium browser",
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_registration_grants_only_the_exact_real_action_allowlist(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    requested = [
        "controller.noop",
        *sorted(REAL_BROWSER_CAPABILITIES),
        "browser_cdp",
        "browser_evaluate",
        "browser_upload",
        "arbitrary.capability",
    ]
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(capabilities=requested),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status == 201
        registration = await response.json()

    assert set(registration["scope"]["capabilities"]) == {
        "controller.noop",
        *REAL_BROWSER_CAPABILITIES,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"protocol_version": 2}, "browser_control_protocol_unsupported"),
        ({"protocol_version": True}, "browser_control_protocol_unsupported"),
        ({"capabilities": []}, "browser_control_no_capabilities"),
        (
            {"capabilities": ["browser_cdp", "arbitrary.capability"]},
            "browser_control_no_capabilities",
        ),
    ],
)
async def test_registration_rejects_unsupported_protocol_or_empty_capability_intersection(
    monkeypatch, overrides, code
):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(**overrides),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        body = await response.json()

    assert response.status == 400
    assert body["error"]["code"] == code


def test_route_table_advertises_registration_and_controller_ws_without_replacing_existing_routes():
    adapter = _adapter()
    routes = {(method, path) for method, path, _handler in adapter._http_route_table()}
    assert ("POST", "/v1/browser-control/register") in routes
    assert ("GET", "/v1/browser-control/ws") in routes
    assert ("POST", "/v1/chat/completions") in routes


def test_ws_sender_treats_wait_timeout_as_in_flight_and_real_error_as_failure(monkeypatch):
    class WS:
        closed = False

        async def send_json(self, _frame):
            return None

    class Future:
        def __init__(self, error, *, done=False):
            self.error = error
            self._done = done
            self.callbacks = []

        def result(self, timeout=None):
            if self.error is not None:
                raise self.error
            return None

        def add_done_callback(self, callback):
            self.callbacks.append(callback)

        def done(self):
            return self._done

    timeout_future = Future(concurrent.futures.TimeoutError())
    def return_timeout(coro, _loop):
        coro.close()
        return timeout_future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", return_timeout)
    sender = _browser_controller_ws_sender(WS(), object(), wait_timeout=0.01)
    sender({"method": "browser.controller.command"})
    assert len(timeout_future.callbacks) == 1

    error_future = Future(ConnectionError("socket write failed"))
    def return_error(coro, _loop):
        coro.close()
        return error_future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", return_error)
    sender = _browser_controller_ws_sender(WS(), object(), wait_timeout=0.01)
    with pytest.raises(ConnectionError, match="socket write failed"):
        sender({"method": "browser.controller.command"})

    completed_timeout = Future(concurrent.futures.TimeoutError(), done=True)
    def return_completed_timeout(coro, _loop):
        coro.close()
        return completed_timeout

    monkeypatch.setattr(
        asyncio,
        "run_coroutine_threadsafe",
        return_completed_timeout,
    )
    sender = _browser_controller_ws_sender(WS(), object(), wait_timeout=0.01)
    with pytest.raises(concurrent.futures.TimeoutError):
        sender({"method": "browser.controller.command"})


def test_api_agent_context_binds_server_principal_and_transport_family():
    from gateway.session_context import clear_session_vars, get_session_env

    adapter = _adapter()
    tokens = adapter._bind_api_server_session(
        session_id="session-fixture",
        browser_control_principal="principal-fixture",
        browser_control_transport_family="local-api",
    )
    try:
        assert (
            get_session_env("CLOVER_BROWSER_CONTROL_PRINCIPAL")
            == "principal-fixture"
        )
        assert (
            get_session_env("CLOVER_BROWSER_CONTROL_TRANSPORT_FAMILY")
            == "local-api"
        )
    finally:
        clear_session_vars(tokens)


@pytest.mark.asyncio
async def test_capabilities_are_truthful_and_disabled_by_default(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: False)
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.get(
            "/v1/capabilities", headers={"Authorization": f"Bearer {API_KEY}"}
        )
        assert response.status == 200
        data = await response.json()

    control = data["features"]["browser_extension_control"]
    assert control["enabled"] is False
    assert control["protocol_version"] == 1
    assert set(control["capabilities"]) == {"controller.noop", *REAL_BROWSER_CAPABILITIES}
    assert control["real_browser_actions"] is True
    assert control["transports"] == {
        "local_vps": "websocket-subprotocol-ticket",
        "cloud": "authenticated-gateway-rpc",
    }
    assert data["endpoints"]["browser_control_register"] == {
        "method": "POST",
        "path": "/v1/browser-control/register",
    }
    assert data["endpoints"]["browser_control_ws"] == {
        "method": "GET",
        "path": "/v1/browser-control/ws",
    }


@pytest.mark.asyncio
async def test_api_middleware_stamps_server_control_identity_for_agent_entry():
    from gateway.platforms.api_server import (
        _api_request_browser_control_principal,
        _api_request_browser_control_transport_family,
    )

    adapter = _adapter()

    async def inspect(_request):
        return web.json_response(
            {
                "principal": _api_request_browser_control_principal.get(),
                "transport_family": (
                    _api_request_browser_control_transport_family.get()
                ),
            }
        )

    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app.router.add_get("/inspect", inspect)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/inspect")
        body = await response.json()

    assert body == {
        "principal": adapter._derive_browser_control_principal("default"),
        "transport_family": "local-api",
    }


@pytest.mark.asyncio
async def test_registration_requires_enabled_feature_and_configured_bearer_auth(monkeypatch):
    disabled = _adapter()
    monkeypatch.setattr(disabled, "_browser_control_enabled", lambda: False)
    async with TestClient(TestServer(_app(disabled))) as client:
        response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status == 404

    unkeyed = _adapter(key="")
    monkeypatch.setattr(unkeyed, "_browser_control_enabled", lambda: True)
    async with TestClient(TestServer(_app(unkeyed))) as client:
        response = await client.post(
            "/v1/browser-control/register", json=_registration_body()
        )
        assert response.status == 403
        assert (await response.json())["error"]["code"] == "browser_control_auth_required"

    keyed = _adapter()
    monkeypatch.setattr(keyed, "_browser_control_enabled", lambda: True)
    async with TestClient(TestServer(_app(keyed))) as client:
        response = await client.post(
            "/v1/browser-control/register", json=_registration_body()
        )
        assert response.status == 401
        response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(session_id=""),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status == 400

        response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(session_id="not-a-server-session"),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status == 403
        assert (await response.json())["error"]["code"] == (
            "browser_control_session_forbidden"
        )


@pytest.mark.asyncio
async def test_controller_ws_rechecks_feature_flag_before_consuming_ticket(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        ticket = (await response.json())["ticket"]
        monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: False)
        with pytest.raises(WSServerHandshakeError) as disabled:
            await client.ws_connect(
                "/v1/browser-control/ws",
                protocols=[CONTROL_PROTOCOL, _ticket_protocol(ticket)],
            )
        assert disabled.value.status == 404

        # Neither a missing protocol nor the legacy query-string shape may
        # consume the one-shot credential.
        monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
        with pytest.raises(WSServerHandshakeError) as query_ticket:
            await client.ws_connect(f"/v1/browser-control/ws?ticket={ticket}")
        assert query_ticket.value.status == 401
        ws = await client.ws_connect(
            "/v1/browser-control/ws",
            protocols=[CONTROL_PROTOCOL, _ticket_protocol(ticket)],
        )
        await ws.close()


@pytest.mark.asyncio
async def test_local_api_ticket_ws_noop_round_trip_filters_spoofed_identity_and_disabled_actions(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status == 201
        registration = await response.json()
        assert registration["protocol_version"] == 1
        assert registration["ticket"]
        assert registration["ticket_expires_at"] > time.time()
        assert 0 < registration["ticket_expires_in_seconds"] <= 30
        assert registration["ws_path"] == "/v1/browser-control/ws"
        assert registration["scope"]["principal_id"] != "spoofed-client-principal"
        assert registration["scope"]["transport_family"] == "local-api"
        assert set(registration["scope"]["capabilities"]) == {
            "controller.noop",
            "browser_navigate",
        }

        ws = await client.ws_connect(
            "/v1/browser-control/ws",
            protocols=[CONTROL_PROTOCOL, _ticket_protocol(registration["ticket"])],
        )
        await ws.send_json(
            {
                "method": "browser.controller.heartbeat",
                "params": {"nonce": "heartbeat-api-fixture"},
            }
        )
        heartbeat = await ws.receive_json(timeout=2.0)
        assert heartbeat == {
            "method": "browser.controller.heartbeat",
            "params": {"nonce": "heartbeat-api-fixture", "ok": True},
        }
        scope = ControllerScope(
            principal_id=registration["scope"]["principal_id"],
            profile_id=registration["scope"]["profile_id"],
            session_id=registration["scope"]["session_id"],
            controller_id=registration["scope"]["controller_id"],
            browser_profile_id=registration["scope"]["browser_profile_id"],
            transport_family=registration["scope"]["transport_family"],
            capabilities=frozenset(registration["scope"]["capabilities"]),
        )

        pending = asyncio.create_task(
            asyncio.to_thread(
                adapter._browser_control_broker.dispatch,
                scope,
                action="controller.noop",
                arguments={"echo": "local-api"},
                tool_call_id="tool-call-fixture",
            )
        )
        command = await ws.receive_json(timeout=2.0)
        assert command["method"] == "browser.controller.command"
        assert command["params"]["action"] == "controller.noop"
        await ws.send_json(
            {
                "method": "browser.controller.result",
                "params": {
                    "command_id": command["params"]["command_id"],
                    "ok": True,
                    "result": {"echo": "local-api"},
                },
            }
        )
        assert await asyncio.wait_for(pending, timeout=2.0) == {"echo": "local-api"}

        rejected = asyncio.create_task(
            asyncio.to_thread(
                adapter._browser_control_broker.dispatch,
                scope,
                action="controller.noop",
                arguments={"echo": "reject"},
                tool_call_id="tool-call-rejected",
            )
        )
        rejected_command = await ws.receive_json(timeout=2.0)
        await ws.send_json(
            {
                "method": "browser.controller.result",
                "params": {
                    "command_id": rejected_command["params"]["command_id"],
                    "ok": "false",
                    "error": {"code": "controller_rejected", "message": "fixture rejection"},
                },
            }
        )
        with pytest.raises(ControllerRejected, match="controller_rejected"):
            await asyncio.wait_for(rejected, timeout=2.0)
        await ws.close()

        with pytest.raises(WSServerHandshakeError) as replay:
            await client.ws_connect(
                "/v1/browser-control/ws",
                protocols=[CONTROL_PROTOCOL, _ticket_protocol(registration["ticket"])],
            )
        assert replay.value.status == 401


@pytest.mark.asyncio
async def test_real_browser_action_routes_through_controller_without_legacy_fallback(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(capabilities=["browser_snapshot"]),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status == 201
        registration = await response.json()
        attach_entered = threading.Event()
        release_attach = threading.Event()
        attach_finished = threading.Event()
        attached_owner = None
        pending_owner = None
        real_attach = adapter._browser_control_broker.attach

        def delayed_real_attach(scope, send, *, owner=None):
            nonlocal attached_owner, pending_owner
            attach_entered.set()
            pending_owner = owner
            try:
                if not release_attach.wait(timeout=1.0):
                    raise TimeoutError("test did not release broker attachment")
                real_attach(scope, send, owner=owner)
                attached_owner = owner
            finally:
                attach_finished.set()

        monkeypatch.setattr(adapter._browser_control_broker, "attach", delayed_real_attach)
        ws = await client.ws_connect(
            "/v1/browser-control/ws",
            protocols=[CONTROL_PROTOCOL, _ticket_protocol(registration["ticket"])],
        )

        early_route = None
        pending = None
        primary_error = None
        try:
            legacy_calls = []
            # HTTP 101 / ws_connect completion is not broker readiness: hold the
            # actual attach body and prove the authenticated socket has upgraded.
            assert await asyncio.wait_for(asyncio.to_thread(attach_entered.wait, 1.0), timeout=1.5)
            assert not attach_finished.is_set()
            with pytest.raises(TimeoutError, match="browser controller not ready"):
                await _wait_for_controller_ready(
                    adapter._browser_control_broker,
                    session_id="session-fixture",
                    principal_id=registration["scope"]["principal_id"],
                    transport_family="local-api",
                    capability="browser_snapshot",
                    owner=pending_owner,
                    timeout=0.05,
                )

            early_calls = []
            early_route = asyncio.create_task(
                asyncio.to_thread(
                    route_browser_tool,
                    "browser_snapshot",
                    {"include": "accessibility"},
                    fallback=lambda: early_calls.append(True) or "legacy-result",
                    broker=adapter._browser_control_broker,
                    enabled=True,
                    session_id="session-fixture",
                    principal_id=registration["scope"]["principal_id"],
                    transport_family="local-api",
                    tool_call_id="tool-call-before-attach",
                )
            )
            try:
                early_result = await asyncio.wait_for(early_route, timeout=0.5)
            except ControllerUnavailable:
                early_result = "controller-unavailable"
            assert early_calls == [True] or early_result == "controller-unavailable"

            release_attach.set()
            ready_scope = await _wait_for_controller_ready(
                adapter._browser_control_broker,
                session_id="session-fixture",
                principal_id=registration["scope"]["principal_id"],
                transport_family="local-api",
                capability="browser_snapshot",
                owner=pending_owner,
                timeout=1.5,
            )
            assert await asyncio.wait_for(asyncio.to_thread(attach_finished.wait, 1.0), timeout=1.5)
            assert attached_owner is pending_owner
            assert adapter._browser_control_broker.is_owner(ready_scope, attached_owner)
            assert adapter._browser_control_broker.select(ready_scope, "browser_snapshot") is not None

            pending = asyncio.create_task(
                asyncio.to_thread(
                    route_browser_tool,
                    "browser_snapshot",
                    {"include": "accessibility"},
                    fallback=lambda: legacy_calls.append(True) or "legacy-result",
                    broker=adapter._browser_control_broker,
                    enabled=True,
                    session_id="session-fixture",
                    principal_id=registration["scope"]["principal_id"],
                    transport_family="local-api",
                    tool_call_id="tool-call-real-action",
                )
            )
            command = await ws.receive_json(timeout=2.0)
            assert command["method"] == "browser.controller.command"
            assert command["params"]["action"] == "browser_snapshot"
            assert command["params"]["arguments"] == {"include": "accessibility"}
            await ws.send_json(
                {
                    "method": "browser.controller.result",
                    "params": {
                        "command_id": command["params"]["command_id"],
                        "ok": True,
                        "result": {
                            "title": "Example Domain",
                            "url": "https://example.test/",
                            "refs": [],
                        },
                    },
                }
            )

            assert await asyncio.wait_for(pending, timeout=2.0) == (
                '{"title": "Example Domain", "url": "https://example.test/", "refs": []}'
            )
            assert legacy_calls == []
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_errors = []
            # Let a started pre-ready to_thread route finish while the attach
            # gate is still held. Cancelling its asyncio wrapper cannot stop
            # the underlying worker thread.
            if early_route is not None and not early_route.done():
                try:
                    await asyncio.wait_for(asyncio.shield(early_route), timeout=0.5)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            # Always unblock the real attach thread before waiting for it or
            # closing the socket. The worker signals completion even on error.
            release_attach.set()
            if attach_entered.is_set():
                try:
                    assert await asyncio.wait_for(
                        asyncio.to_thread(attach_finished.wait, 1.5), timeout=1.75
                    )
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if not ws.closed:
                try:
                    await asyncio.wait_for(ws.close(), timeout=1.0)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            routes = [task for task in (early_route, pending) if task is not None]
            if routes:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(asyncio.gather(*routes, return_exceptions=True)), timeout=1.5
                    )
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                if primary_error is not None:
                    primary_error.add_note(
                        "browser fixture cleanup errors: " + "; ".join(map(str, cleanup_errors))
                    )
                else:
                    raise cleanup_errors[0]



@pytest.mark.asyncio
async def test_local_api_same_identity_reconnect_completes_command_started_on_old_socket(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    async with TestClient(TestServer(_app(adapter))) as client:
        first_response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(capabilities=["browser_snapshot"]),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        first = await first_response.json()
        first_ws = await client.ws_connect(
            "/v1/browser-control/ws",
            protocols=[CONTROL_PROTOCOL, _ticket_protocol(first["ticket"])],
        )

        pending = asyncio.create_task(
            asyncio.to_thread(
                route_browser_tool,
                "browser_snapshot",
                {},
                fallback=lambda: "legacy-result",
                broker=adapter._browser_control_broker,
                enabled=True,
                session_id="session-fixture",
                principal_id=first["scope"]["principal_id"],
                transport_family="local-api",
                tool_call_id="tool-call-reconnect",
            )
        )
        command = await first_ws.receive_json(timeout=2.0)
        await first_ws.close()
        await asyncio.sleep(0)
        assert not pending.done()

        second_response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(capabilities=["browser_snapshot"]),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        second = await second_response.json()
        second_ws = await client.ws_connect(
            "/v1/browser-control/ws",
            protocols=[CONTROL_PROTOCOL, _ticket_protocol(second["ticket"])],
        )
        await second_ws.send_json(
            {
                "method": "browser.controller.result",
                "params": {
                    "command_id": command["params"]["command_id"],
                    "ok": True,
                    "result": {"reconnected": True},
                },
            }
        )
        assert await asyncio.wait_for(pending, timeout=2.0) == '{"reconnected": true}'
        await second_ws.close()


@pytest.mark.asyncio
async def test_local_api_explicit_detach_is_hard_and_stale_socket_cannot_detach_refresh(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    async with TestClient(TestServer(_app(adapter))) as client:
        first_response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(capabilities=["controller.noop"]),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        first = await first_response.json()
        first_ws = await client.ws_connect(
            "/v1/browser-control/ws",
            protocols=[CONTROL_PROTOCOL, _ticket_protocol(first["ticket"])],
        )
        second_response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(capabilities=["controller.noop"]),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        second = await second_response.json()
        second_ws = await client.ws_connect(
            "/v1/browser-control/ws",
            protocols=[CONTROL_PROTOCOL, _ticket_protocol(second["ticket"])],
        )

        await first_ws.send_json(
            {"method": "browser.controller.detach", "params": {}}
        )
        with pytest.raises(asyncio.TimeoutError):
            await first_ws.receive_json(timeout=0.05)

        pending = asyncio.create_task(
            asyncio.to_thread(
                adapter._browser_control_broker.dispatch,
                ControllerScope(
                    principal_id=second["scope"]["principal_id"],
                    profile_id=second["scope"]["profile_id"],
                    session_id=second["scope"]["session_id"],
                    controller_id=second["scope"]["controller_id"],
                    browser_profile_id=second["scope"]["browser_profile_id"],
                    transport_family=second["scope"]["transport_family"],
                    capabilities=frozenset(second["scope"]["capabilities"]),
                ),
                action="controller.noop",
                tool_call_id="tool-call-explicit-detach",
            )
        )
        command = await second_ws.receive_json(timeout=2.0)
        await second_ws.send_json(
            {"method": "browser.controller.detach", "params": {}}
        )
        detached = await second_ws.receive_json(timeout=2.0)
        assert detached == {
            "method": "browser.controller.detach",
            "params": {"ok": True},
        }
        with pytest.raises(ControllerCancelled):
            await asyncio.wait_for(pending, timeout=2.0)
        assert command["method"] == "browser.controller.command"
        await first_ws.close()
        await second_ws.close()


@pytest.mark.asyncio
async def test_remote_api_uses_the_same_authenticated_noop_round_trip(monkeypatch):
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    monkeypatch.setattr(
        adapter,
        "_browser_control_transport_family",
        lambda request: "remote-api",
    )
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/browser-control/register",
            json=_registration_body(session_id="remote-session-fixture"),
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        registration = await response.json()
        assert response.status == 201
        assert registration["scope"]["transport_family"] == "remote-api"

        ws = await client.ws_connect(
            "/v1/browser-control/ws",
            protocols=[CONTROL_PROTOCOL, _ticket_protocol(registration["ticket"])],
        )
        scope = ControllerScope(
            principal_id=registration["scope"]["principal_id"],
            profile_id=registration["scope"]["profile_id"],
            session_id=registration["scope"]["session_id"],
            controller_id=registration["scope"]["controller_id"],
            browser_profile_id=registration["scope"]["browser_profile_id"],
            transport_family="remote-api",
            capabilities=frozenset(registration["scope"]["capabilities"]),
        )
        pending = asyncio.create_task(
            asyncio.to_thread(
                adapter._browser_control_broker.dispatch,
                scope,
                action="controller.noop",
                arguments={"family": "remote-api"},
                tool_call_id="tool-call-remote",
            )
        )
        command = await ws.receive_json(timeout=2.0)
        await ws.send_json(
            {
                "method": "browser.controller.result",
                "params": {
                    "command_id": command["params"]["command_id"],
                    "ok": True,
                    "result": {"family": "remote-api"},
                },
            }
        )
        assert await asyncio.wait_for(pending, timeout=2.0) == {
            "family": "remote-api"
        }
        await ws.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("failure_boundary", ["before_route", "after_route_created"])
async def test_readiness_test_cleans_up_actual_fixture_on_early_failure(monkeypatch, failure_boundary):
    """Bridge injected setup faults through the real readiness test and cleanup."""
    import pytest
    from aiohttp.test_utils import TestClient
    from tests.gateway import test_browser_control_api as browser_test

    sentinel = RuntimeError(f"injected-{failure_boundary}")
    observations = {
        "adapter": None,
        "ws": None,
        "close_calls": 0,
        "close_durations": [],
        "scope": None,
        "events": [],
        "disconnect_event": threading.Event(),
        "disconnect_results": [],
    }
    real_adapter_factory = browser_test._adapter
    real_ws_connect = TestClient.ws_connect
    real_wait_for = asyncio.wait_for
    real_event_factory = threading.Event
    real_asyncio_module = asyncio
    real_threading_module = threading
    real_ready = browser_test._wait_for_controller_ready

    class ObservedEvent:
        def __init__(self):
            self.event = real_event_factory()
            import inspect
            caller = inspect.currentframe().f_back
            if caller and caller.f_code.co_name == "test_real_browser_action_routes_through_controller_without_legacy_fallback":
                observations["events"].append(self)

        def set(self):
            self.event.set()

        def wait(self, timeout=None):
            return self.event.wait(timeout)

        def is_set(self):
            return self.event.is_set()

    def adapter_factory():
        adapter = real_adapter_factory()
        observations["adapter"] = adapter
        real_disconnect = adapter._browser_control_broker.disconnect

        def observed_disconnect(scope, *args, **kwargs):
            result = real_disconnect(scope, *args, **kwargs)
            if scope == observations["scope"]:
                observations["disconnect_results"].append(result)
                observations["disconnect_event"].set()
            return result

        adapter._browser_control_broker.disconnect = observed_disconnect
        real_attach = adapter._browser_control_broker.attach

        def attach(scope, send, *, owner=None):
            observations["scope"] = scope
            return real_attach(scope, send, owner=owner)

        adapter._browser_control_broker.attach = attach
        return adapter

    async def ws_connect(client, *args, **kwargs):
        ws = await real_ws_connect(client, *args, **kwargs)
        observations["ws"] = ws
        real_close = ws.close

        async def close(*args, **kwargs):
            import time
            observations["close_calls"] += 1
            started = time.monotonic()
            try:
                return await real_close(*args, **kwargs)
            finally:
                observations["close_durations"].append(time.monotonic() - started)

        ws.close = close
        return ws

    async def ready(*args, **kwargs):
        if failure_boundary == "before_route":
            raise sentinel
        return await real_ready(*args, **kwargs)

    injected = False

    async def wait_for(fut, *args, **kwargs):
        nonlocal injected
        if not injected and failure_boundary == "after_route_created" and isinstance(fut, asyncio.Task):
            coro = fut.get_coro()
            frame = getattr(coro, "cr_frame", None)
            if frame and frame.f_locals.get("func") is route_browser_tool:
                injected = True
                raise sentinel
        return await real_wait_for(fut, *args, **kwargs)

    monkeypatch.setattr(browser_test, "_adapter", adapter_factory)
    monkeypatch.setattr(TestClient, "ws_connect", ws_connect)
    from types import SimpleNamespace

    threading_proxy = SimpleNamespace(
        **{
            name: getattr(real_threading_module, name)
            for name in dir(real_threading_module)
            if not name.startswith("__")
        }
    )
    threading_proxy.Event = ObservedEvent
    asyncio_proxy = SimpleNamespace(
        **{
            name: getattr(real_asyncio_module, name)
            for name in dir(real_asyncio_module)
            if not name.startswith("__")
        }
    )
    asyncio_proxy.wait_for = wait_for
    monkeypatch.setattr(browser_test, "threading", threading_proxy)
    monkeypatch.setattr(browser_test, "asyncio", asyncio_proxy)
    assert real_threading_module.Event is real_event_factory
    assert real_asyncio_module.wait_for is real_wait_for
    monkeypatch.setattr(browser_test, "_wait_for_controller_ready", ready)
    isolated_patch = pytest.MonkeyPatch()
    try:
        with pytest.raises(RuntimeError) as raised:
            await browser_test.test_real_browser_action_routes_through_controller_without_legacy_fallback(isolated_patch)
        assert raised.value is sentinel
    finally:
        isolated_patch.undo()

    assert observations["ws"] is not None
    assert observations["close_calls"] >= 1
    assert observations["close_durations"][0] <= 1.0
    assert observations["ws"].closed
    # Event order is attach-entered, release gate, attach-finished.
    entered, release, finished = observations["events"]
    assert entered.is_set()
    assert release.is_set()
    assert finished.is_set()
    adapter = observations["adapter"]
    scope = observations["scope"]
    assert scope is not None
    disconnected = await real_wait_for(
        asyncio.to_thread(observations["disconnect_event"].wait, 2.5),
        timeout=3.0,
    )
    assert disconnected, "actual broker disconnect did not complete after websocket teardown"
    assert any(result is True for result in observations["disconnect_results"])
    assert adapter._browser_control_broker.select(scope, "browser_snapshot") is None


@pytest.mark.asyncio
async def test_ws_teardown_disconnects_controller_even_if_handler_is_cancelled_while_queued(
    monkeypatch,
):
    """A cancel racing the handler's teardown must not skip broker.disconnect.

    ``disconnect`` is offloaded to the default executor.  When every worker is
    busy the call is still queued, and cancelling the awaiting handler used to
    cancel that queued work item too, leaving the controller marked connected
    (the hosted ``[before_route]`` flake).  Saturating a one-worker executor
    makes the race deterministic instead of load dependent.
    """
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    broker = adapter._browser_control_broker
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    attached = threading.Event()
    disconnected = threading.Event()
    disconnect_calls = []
    release_worker = threading.Event()
    real_attach = broker.attach
    real_disconnect = broker.disconnect

    def attach(scope, send, *, owner=None):
        result = real_attach(scope, send, owner=owner)
        attached.set()
        return result

    def disconnect(scope, *args, **kwargs):
        result = real_disconnect(scope, *args, **kwargs)
        disconnect_calls.append((scope, result))
        disconnected.set()
        return result

    monkeypatch.setattr(broker, "attach", attach)
    monkeypatch.setattr(broker, "disconnect", disconnect)

    handler_tasks = []
    real_handler = adapter._handle_browser_control_ws

    async def tracked_handler(request):
        handler_tasks.append(asyncio.current_task())
        return await real_handler(request)

    app = web.Application()
    app.router.add_post(
        "/v1/browser-control/register", adapter._handle_browser_control_register
    )
    app.router.add_get("/v1/browser-control/ws", tracked_handler)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/browser-control/register",
                json=_registration_body(capabilities=["browser_snapshot"]),
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            registration = await response.json()
            ws = await client.ws_connect(
                "/v1/browser-control/ws",
                protocols=[CONTROL_PROTOCOL, _ticket_protocol(registration["ticket"])],
            )
            deadline = loop.time() + 2.0
            while not attached.is_set() and loop.time() < deadline:
                await asyncio.sleep(0.005)
            assert attached.is_set()

            # Occupy the only worker so the handler's teardown call queues.
            executor.submit(release_worker.wait, 10.0)
            await ws.close()
            deadline = loop.time() + 2.0
            while executor._work_queue.qsize() < 1 and loop.time() < deadline:
                await asyncio.sleep(0.005)
            assert executor._work_queue.qsize() >= 1, "teardown call never queued"
            assert not disconnect_calls

            handler_tasks[0].cancel()
            await asyncio.sleep(0.05)
            release_worker.set()
            deadline = loop.time() + 2.0
            while not disconnected.is_set() and loop.time() < deadline:
                await asyncio.sleep(0.005)

        assert disconnected.is_set(), "cancelled handler skipped broker.disconnect"
        assert len(disconnect_calls) == 1
        assert disconnect_calls[0][1] is True
    finally:
        release_worker.set()
        executor.shutdown(wait=True, cancel_futures=True)
