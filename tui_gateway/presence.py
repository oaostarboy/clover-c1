"""Authenticated desktop after-reply/ACK adapter, using the shared presence owner.

Opaque capabilities are transport/principal/session/generation-bound, ephemeral,
short-lived and single-use. Restart cannot replay a provider or native publication.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, replace
import secrets
import threading
import time

from gateway.presence_delivery import SurfaceBinding


@dataclass
class Ticket:
    server: object
    sid: str
    session: dict
    transport: object
    identity: dict
    host: object
    expires: float
    context: object
    kind: str
    event: object = None
    thought_id: str = ''
    revision: int = 0
    session_generation: int = -1
    identity_instance: object = None


_tickets: dict[str, Ticket] = {}
_lock = threading.RLock()


def issue(ticket):
    with _lock:
        for token, old in list(_tickets.items()):
            if old.expires <= time.monotonic():
                _tickets.pop(token, None)
        if len(_tickets) >= 128:
            return None
        token = secrets.token_urlsafe(24)
        _tickets[token] = ticket
        return token


def current(ticket):
    from pathlib import Path
    from tui_gateway.methods_browser_control import _is_authenticated_identity
    server, session, transport = ticket.server, ticket.session, ticket.transport
    with server._sessions_lock:
        return (server._sessions.get(ticket.sid) is session
            and session.get('transport') is transport and not transport.closed
            and _is_authenticated_identity(transport.auth_identity)
            and transport.auth_identity == ticket.identity and transport.auth_identity is ticket.identity_instance
            and not session.get('_closing') and not session.get('_turn_cancel_requested')
            and session.get('agent') is ticket.host.agent
            and session.get('_human_ingress_generation') == ticket.session_generation
            and str(server._session_home(session)) == ticket.host.original_key.scope.profile
            and str(Path(server._session_cwd(session)).resolve()) == ticket.host.original_key.scope.project
            and (ticket.kind != 'project_return' or not session.get('running')))


class DesktopPresenceAdapter:
    def __init__(self, ticket):
        self.ticket = ticket
        self.presence_binding = SurfaceBinding('desktop', str(id(ticket.transport)),
            str(id(ticket.transport)), ticket.host.original_key.scope.root, ticket.sid, 'private',
            identity_link=ticket.host.link_id)

    async def send_presence(self, payload):
        ticket = self.ticket
        if not current(ticket) or not ticket.host.current():
            return None
        ack = replace(ticket, expires=time.monotonic()+5, kind='ack', event=threading.Event())
        token = issue(ack)
        if token is None:
            return None
        control = replace(ticket, expires=time.monotonic()+300, kind='control', event=None,
                          thought_id=payload['thought_id'], revision=payload['revision'])
        control_token = issue(control)
        if control_token is None:
            return None
        payload = dict(payload, ack_token=token, control_token=control_token)
        try:
            if ticket.transport.write(ticket.server._event_frame('presence.initiative', ticket.sid, payload)) is False:
                return None
            if await asyncio.to_thread(ack.event.wait, 4):
                return token if current(ticket) and ticket.host.current() else None
            return None
        finally:
            with _lock:
                _tickets.pop(token, None)


def stage_after_reply(server, sid, session, result, payload):
    from agent.presence_runtime import PresenceTurn
    from tui_gateway.methods_browser_control import _is_authenticated_identity
    agent = session.get('agent')
    host = getattr(agent, '_presence_turn', None)
    if agent is not None:
        agent._presence_turn = None
    transport = session.get('transport')
    identity = getattr(transport, 'auth_identity', None)
    if (not isinstance(host, PresenceTurn) or not _is_authenticated_identity(identity)
            or getattr(transport, 'closed', True) or payload.get('status') != 'complete'
            or not payload.get('text') or any(result.get(k) for k in ('failed','interrupted','partial','error'))
            or result.get('completed') is False):
        return
    from agent.presence_authority import callback_context
    host.arm_owner()
    ticket = Ticket(server, sid, session, transport, dict(identity), host,
                    time.monotonic()+30, callback_context(), 'after_reply',
                    session_generation=session.get('_human_ingress_generation', -1), identity_instance=identity)
    session['_presence_anchor'] = ticket
    host.gateway_current = lambda: current(ticket)
    token = issue(ticket)
    if token is not None:
        payload['presence_after_reply_token'] = token
        from agent.presence_runtime import effective_policy
        payload['presence_project_return_enabled'] = effective_policy()[1].get('presence',{}).get('project_return_enabled') is True


def handle_rpc(server, rid, params, action):
    if action == 'project_return':
        return server._ok(rid, project_return(server, params))
    if action == 'link':
        result = desktop_command(server, params,
            ['link', params.get('code'), 'confirm'] if params.get('confirmed') is True else [])
        return server._ok(rid, result)
    transport = server.current_transport()
    sid, token = params.get('session_id'), params.get('token')
    if not isinstance(sid, str) or not isinstance(token, str):
        return server._err(rid, 4403, 'Current authenticated presence capability required')
    with _lock:
        ticket = _tickets.get(token)
        if (ticket is None or ticket.sid != sid or ticket.transport is not transport
                or ticket.kind != action or ticket.expires <= time.monotonic()
                or not current(ticket)):
            return server._ok(rid, {'accepted':False})
        if action != 'control' and not ticket.context.copy().run(ticket.host.current):
            return server._ok(rid, {'accepted':False})
        # Inspect is read-only and retains the original short-lived capability;
        # it cannot extend expiry. Mutations and ACKs remain single-use.
        if action != 'control' or params.get('action') != 'inspect':
            _tickets.pop(token, None)
    if action == 'control':
        def apply():
            from clover_cli.managed_scope import load_managed_config, managed_config_keys
            load_managed_config(strict=True)
            operation = params.get('action')
            if operation not in {'inspect','stop','forget','correct'}:
                return {'accepted':False, 'text':'Unsupported presence control.'}
            host = ticket.host
            if host.store.initiative_status()[0] != host.epoch:
                return {'accepted':False, 'text':'Presence control was revoked. Reopen it.'}
            thought = next((t for t in host.state.inspect() if t.thought_id == ticket.thought_id
                            and t.revision == ticket.revision), None)
            if thought is None:
                return {'accepted':False, 'text':'Thought changed or expired. Reopen controls.'}
            if operation == 'inspect':
                return {'accepted':True, 'text':f'{thought.summary} [{thought.status}] Source: {thought.source.owner} {thought.source.source_id}'}
            if any(p == 'presence' or p.startswith('presence.') for p in managed_config_keys(strict=True)):
                return {'accepted':False, 'text':'Presence is managed by your administrator. No changes made.'}
            if operation in {'forget','correct'}:
                host.state.invalidate_source(thought.source, status='deleted' if operation == 'forget' else 'corrected')
            host.store.stop()
            ticket.transport.write(server._event_frame('presence.clear', sid, {}))
            return {'accepted':True, 'text':'Presence stopped in this profile. Requested replies remain available.'}
        try:
            return server._ok(rid, ticket.context.copy().run(apply))
        except Exception:
            return server._ok(rid, {'accepted':False,'text':'Current presence policy is unavailable. No changes made.'})
    if action == 'ack':
        ticket.event.set()
    else:
        def run():
            try:
                asyncio.run(ticket.host.publish(DesktopPresenceAdapter(ticket), None, surface='desktop'))
            except Exception:
                server.logger.debug('desktop presence held', exc_info=True)
        server._pool.submit(lambda: ticket.context.copy().run(run))
    return server._ok(rid, {'accepted':True})


def project_return(server, params):
    """One explicit authenticated view return; no timer, scan, or user utterance."""
    sid = params.get('session_id')
    if not isinstance(sid, str) or set(params) != {'session_id'}:
        return {'accepted':False}
    transport, session = server._current_session_steer_authority(sid)
    if session is None or transport is None or session.get('running'):
        return {'accepted':False}
    anchor = session.get('_presence_anchor')
    if not isinstance(anchor, Ticket) or anchor.transport is not transport or not current(anchor):
        return {'accepted':False}
    def prepare():
        from agent.presence_runtime import effective_policy
        enabled, config, _ = effective_policy()
        if (not enabled or config.get('presence',{}).get('project_return_enabled') is not True
                or anchor.host.store.initiative_status()[1]):
            return None
        host = anchor.host.project_return()
        ticket = replace(anchor, host=host, expires=time.monotonic()+30, kind='project_return')
        host.gateway_current = lambda: current(ticket)
        return ticket if host.current() else None
    try:
        ticket = anchor.context.copy().run(prepare)
    except Exception:
        return {'accepted':False}
    if ticket is None:
        return {'accepted':False}
    def run():
        try:
            asyncio.run(ticket.host.publish(DesktopPresenceAdapter(ticket), None,
                                            surface='desktop', kind='project_return'))
        except Exception:
            server.logger.debug('project-return presence held', exc_info=True)
    server._pool.submit(lambda: ticket.context.copy().run(run))
    return {'accepted':True}


def desktop_command(server, params, words):
    """Native /proactive presence controls; derive scope from the existing owner."""
    from pathlib import Path
    from agent import workstreams as ws
    from agent.presence_state import ContextKey, ThoughtState
    from agent.presence_runtime import SessionEvidence, clock
    from clover_constants import set_clover_home_override, reset_clover_home_override
    from clover_cli.managed_scope import load_managed_config, managed_config_keys
    from cron.presence import ThoughtStore
    from tui_gateway.methods_browser_control import _is_authenticated_identity, _principal_digest
    sid = params.get('session_id')
    if not isinstance(sid, str) or not sid or len(sid) > 512:
        return {'accepted':False, 'text':'Current literal session identifier required.'}
    transport, session = server._current_session_steer_authority(sid)
    if (transport is None or transport.closed or not _is_authenticated_identity(transport.auth_identity)
            or not words or any(not isinstance(w, str) for w in words)):
        return {'accepted':False, 'text':'Use the current authenticated desktop conversation.'}
    identity = dict(transport.auth_identity)
    home, project = server._session_home(session), str(Path(server._session_cwd(session)).resolve())
    def still_current():
        actual_transport, actual_session = server._current_session_steer_authority(sid)
        return (actual_transport is transport and actual_session is session and not transport.closed
            and transport.auth_identity == identity and server._session_home(session) == home
            and str(Path(server._session_cwd(session)).resolve()) == project)
    token = set_clover_home_override(home)
    try:
        load_managed_config(strict=True)
        action = words[0]
        if action != 'status' and any(k == 'presence' or k.startswith('presence.') for k in managed_config_keys(strict=True)):
            return {'accepted':False, 'text':'Presence is managed by your administrator. No changes made.'}
        agent = session.get('agent')
        db = getattr(agent, '_session_db', None)
        root = db.get_conversation_root(session.get('session_key')) if db is not None else None
        if not isinstance(root, str) or not root or not still_current():
            return {'accepted':False, 'text':'Send a normal message in this conversation first.'}
        original = ContextKey(ws.scope_for_surface(platform='desktop', chat_type='dm',
            user_id=_principal_digest(identity), project=project), root)
        store = ThoughtStore()
        key = store.linked_key(original)
        state = ThoughtState(key, SessionEvidence(db,key), store, clock=clock)
        if action == 'link' and len(words) == 3 and words[2] == 'confirm':
            accepted = store.accept_link(words[1], original, 'desktop', clock(), is_current=still_current)
            return {'accepted':accepted, 'text':'Presence surfaces linked for future explicit thoughts.' if accepted else 'Link code expired, already used, or outside this exact profile/project. No changes made.'}
        if action == 'unlink' and len(words) == 1:
            accepted = still_current() and store.unlink(original)
            return {'accepted':bool(accepted), 'text':'Presence link revoked. No old offers will replay.'}
        if action == 'status' and len(words) == 1:
            epoch, stopped = store.initiative_status()
            rows = state.inspect()
            return {'accepted':True, 'text':f'Presence epoch {epoch}; stopped={stopped}.\n' + '\n'.join(f'{t.thought_id} r{t.revision} [{t.status}] {t.summary}' for t in rows)}
        if action in {'stop','resume'} and len(words) == 1 and still_current():
            epoch = store.stop() if action == 'stop' else store.resume()
            return {'accepted':True, 'text':f'Presence {action}; epoch {epoch}. Resume admits future thoughts only.'}
        if action in {'forget','correct'} and len(words) == 2 and still_current():
            thought = next((t for t in state.inspect() if t.thought_id == words[1]), None)
            if thought is not None:
                state.invalidate_source(thought.source, status='deleted' if action == 'forget' else 'corrected')
                store.stop()
                return {'accepted':True, 'text':'Thought cleared and profile presence stopped.'}
        return {'accepted':False, 'text':'Use /proactive presence status | stop | resume | forget <id> | correct <id> | link <code> confirm | unlink.'}
    except Exception:
        return {'accepted':False, 'text':'Current presence policy or state is unavailable. No changes made.'}
    finally:
        reset_clover_home_override(token)
