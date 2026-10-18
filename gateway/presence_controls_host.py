"""Scoped /proactive presence controls, through existing command authorization."""
from agent import workstreams as ws
from agent.presence_state import ContextKey, ThoughtState
from agent.presence_runtime import SessionEvidence, clock
from cron.presence import ThoughtStore


async def command(runner, event, args, *, can_change):
    from clover_cli.managed_scope import load_managed_config, managed_config_keys
    if (getattr(event, 'internal', False) or event.source.platform.value != 'telegram'
            or event.source.chat_type not in {'dm','private'} or not runner._is_user_authorized(event.source)):
        return 'Presence controls require a current authenticated private Telegram conversation.'
    load_managed_config(strict=True)
    words = args.split()
    action = words[0] if words else 'status'
    store = ThoughtStore()
    if action != 'status':
        if not can_change:
            return 'Presence controls locked by current scoped admin policy.'
        pins = managed_config_keys(strict=True)
        if any(p == 'presence' or p.startswith('presence.') for p in pins):
            return 'Presence controls are managed by your administrator. No changes made.'
    if action == 'resume' and len(words) == 1:
        epoch = store.resume()
        return f'Presence reset for future thoughts only (epoch {epoch}); existing effective settings still apply.'
    if action == 'stop' and len(words) == 1:
        epoch = store.stop()
        return f'Presence stopped everywhere in this profile (epoch {epoch}). Requested replies remain available.'
    entry = await runner.async_session_store.get_or_create_session(event.source)
    db = runner.session_store._db
    scope = ws.scope_for_surface(platform='telegram', chat_type=event.source.chat_type,
                                chat_id=event.source.chat_id, user_id=event.source.user_id)
    key = ContextKey(scope, db.get_conversation_root(entry.session_id))
    state = ThoughtState(key, SessionEvidence(db, key), store, clock=clock)
    if action == 'status' and len(words) <= 1:
        epoch, stopped = store.initiative_status()
        rows = state.inspect()
        return (f'Presence: {"stopped" if stopped else "available only when explicitly enabled"}; epoch {epoch}.\n'
                + '\n'.join(f'{t.thought_id} r{t.revision} [{t.status}] {t.summary}' for t in rows)
                + '\nControls: /proactive presence stop | status | forget <id> | correct <id>. '
                'Correct removes the old thought; use Carry thought: to admit a replacement. '
                'Presence: ordinary clears tentative posture on your next requested reply.')
    if action in {'forget','correct'} and len(words) == 2:
        thought = next((t for t in state.inspect() if t.thought_id == words[1]), None)
        if thought is None:
            return 'No exact thought in this private conversation. No changes made.'
        state.invalidate_source(thought.source, status='deleted' if action == 'forget' else 'corrected')
        # Revokes any in-flight proposal on every profile surface as well.
        epoch = store.stop()
        return f'Thought {thought.thought_id} cleared; presence stopped at epoch {epoch}. Requested replies remain available.'
    return 'Use /proactive presence status | stop | forget <id> | correct <id>.'
