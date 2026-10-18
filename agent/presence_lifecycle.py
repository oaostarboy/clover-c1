"""Presence revocation at existing configuration and Stop ownership boundaries."""
from clover_constants import get_clover_home


def existing_store():
    # A default-off installation never imports/creates the optional store here.
    if not (get_clover_home().resolve()/'cron'/'presence.db').exists():
        return None
    from cron.presence import ThoughtStore
    return ThoughtStore()


def before_config_write(before, after):
    store = existing_store()
    if store is None:
        return
    from agent.workstreams import load_settings
    from clover_cli.managed_scope import load_managed_config, apply_managed_overlay
    load_managed_config(strict=True)
    def allowed(value):
        value = apply_managed_overlay(value)
        section = value.get('presence', {})
        settings = load_settings(value)
        return isinstance(section, dict) and section.get('enabled') is True and settings.active and settings.suggestions_enabled
    old, new = allowed(before), allowed(after)
    if old and not new:
        from agent.presence_state import PresenceStoreError
        try:
            store.stop()
        except PresenceStoreError:
            # Corruption already makes every runtime fence hold. Still permit
            # the authoritative OFF write; never repair/delete the failed DB.
            import logging
            logging.getLogger(__name__).warning('Presence DB unavailable; saving disabled policy without modifying it')
    elif new and not old:
        # Re-enable never rearms old pending thoughts or unknown transport sends.
        store.resume()


def stop_existing_gateway_presence(runner, source):
    store = existing_store()
    if store is None:
        return
    if (source.platform.value != 'telegram' or source.chat_type not in {'dm','private'}
            or not runner._is_user_authorized(source) or not runner._proactive_caller_is_admin(source)):
        return
    from clover_cli.managed_scope import load_managed_config
    load_managed_config(strict=True)
    store.stop()


def note_human_input(profile):
    """Called only by the authenticated ingress mint, including queued input."""
    from clover_constants import set_clover_home_override, reset_clover_home_override
    token = set_clover_home_override(profile)
    try:
        from clover_cli.config import load_config
        section = load_config().get('presence', {})
        if not isinstance(section, dict) or section.get('enabled') is not True:
            return -1
        from agent.presence_runtime import effective_policy
        if not effective_policy()[0]:
            return -1
        from cron.presence import ThoughtStore
        return ThoughtStore().advance_generation()
    finally:
        reset_clover_home_override(token)


def stop_existing_desktop_presence(server, session):
    """Use the existing authenticated session owner, never client-supplied IDs."""
    from tui_gateway.methods_browser_control import _is_authenticated_identity
    transport = server.current_transport()
    if (transport is None or transport is not session.get('transport')
            or transport.closed or not _is_authenticated_identity(transport.auth_identity)):
        return False
    from clover_constants import set_clover_home_override, reset_clover_home_override
    token = set_clover_home_override(server._session_home(session))
    try:
        store = existing_store()
        if store is None:
            return False
        store.stop()
        return True
    finally:
        reset_clover_home_override(token)
