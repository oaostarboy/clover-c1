"""Narrow native controls/runtime binding for the separately consented mail job.

Only a connected native email adapter and private native Telegram are supported.
No source or provider is contacted by discovery. Standalone cron, multiplexed
transport profiles, public destinations and every other model route fail closed.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
import re
import secrets
import threading
import time

from cron.inbox_watcher import Denied, Draft, ROUTE, Store, canonical, digest, revoke, state_path


class Binding:
    def __init__(self, runner, source, *, loop=None):
        from clover_constants import get_clover_home
        self.home = get_clover_home().resolve()
        self.runner, self.source, self.loop = runner, copy.deepcopy(source), loop

    def revocation_scope(self):
        return self.snapshot(revocation=True)

    def snapshot(self, *, revocation=False):
        from gateway.config import Platform
        from clover_cli import config, managed_scope
        from gateway.slash_access import policy_from_extra
        source, runner = self.source, self.runner
        if (source.platform != Platform.TELEGRAM or source.chat_type not in ('dm', 'private')
                or not source.user_id or str(source.user_id) != str(source.chat_id)
                or not str(source.chat_id).isdigit() or int(source.chat_id) <= 0
                or source.thread_id is not None or getattr(source, 'is_bot', False)
                or getattr(runner.config, 'multiplex_profiles', False)):
            raise Denied('available only in the owner private Telegram chat; multiplex unavailable')
        if Path(runner._resolve_profile_home_for_source(source)).resolve() != self.home:
            raise Denied('cross-profile watcher unavailable')
        # Dependency on M1: an old forgiving signature raises and fails closed.
        managed = managed_scope.load_managed_config(strict=True)
        raw = config.require_readable_config_before_write()
        # Recheck fresh file policy as well as current gateway/transport admission.
        local = raw.get('platforms', {}).get('telegram', {})
        for section in (local, local.get('extra', {})):
            if not isinstance(section, dict) or not policy_from_extra(section, 'dm').is_admin(source.user_id):
                raise Denied('current private command policy denied')
        if not runner._proactive_caller_is_admin(source):
            raise Denied('current authenticated command authority denied')
        pin = managed.get('proactive', {}).get('inbox_watcher', {})
        if not isinstance(pin, dict) or pin:
            raise Denied('watcher controls managed; ask administrator')
        adapter = runner._adapter_for_source(source)
        from gateway.run import GatewayRunner
        credential = GatewayRunner._adapter_credential_fingerprint(adapter)
        # Native-only: relay and delegated transports are not approved routes.
        if (adapter is not runner.adapters.get(Platform.TELEGRAM) or credential is None
                or getattr(adapter, 'authorization_is_upstream', False) is True):
            raise Denied('native private delivery unavailable')
        email = runner.adapters.get(Platform.EMAIL)
        describe = getattr(email, 'inbox_watcher_metadata', None)
        if not revocation and not callable(describe):
            raise Denied('connect the native Email adapter first; Gmail/other sources unavailable')
        metadata = None if revocation else describe()
        if not revocation and (not isinstance(metadata, dict) or set(metadata) != {'type','account','folder','query','fingerprint'}
                or metadata['type'] != 'native-imap-headers-v1' or metadata['folder'] != 'INBOX'
                or metadata['query'] != 'new UID only' or not isinstance(metadata['account'], str)
                or not 1 <= len(metadata['account']) <= 254 or any(ord(c)<32 for c in metadata['account'])
                or not re.fullmatch(r'[0-9a-f]{64}', metadata['fingerprint'])):
            raise Denied('supported bounded source metadata unavailable')
        # Only policy data, no mail. Changes invalidate all current review tokens.
        authority = {'managed':managed, 'local_platform':local,
                     'runtime_admin':runner.config.platforms[Platform.TELEGRAM].extra.get('allow_admin_from')}
        return {'home':str(self.home), 'actor':str(source.user_id), 'source':metadata,
                'destination':{'platform':'telegram', 'chat_id':str(source.chat_id),
                               'thread_id':None, 'credential':credential},
                'route':ROUTE, 'authority':digest(authority)}

    def validate_route(self):
        from agent.secret_scope import get_secret
        key = get_secret('OPENAI_API_KEY', '')
        if not isinstance(key, str) or not key:
            raise Denied('OpenAI API key unavailable; no watcher activated')
        return digest({'fixed_openai_key':key})

    def fetch(self, cursor, *, max_items):
        from gateway.config import Platform
        return self.runner.adapters[Platform.EMAIL].inbox_watcher_read(cursor, max_items=max_items)

    def request(self, messages, limits, reserve):
        from agent.secret_scope import get_secret
        from agent.auxiliary_client import create_bounded_inbox_client, call_bounded_inbox
        self.validate_route()
        # Fixed endpoint; never auto/OAuth/pool/relay/fallback. No default SDK retries.
        with create_bounded_inbox_client(get_secret('OPENAI_API_KEY', '')) as client:
            return call_bounded_inbox(client=client, messages=messages,
                                      max_output_tokens=limits.output_tokens, reserve=reserve)

    def deliver(self, outbox, guard):
        from gateway.config import Platform
        adapter = self.runner.adapters[Platform.TELEGRAM]
        if self.loop is None or not self.loop.is_running():
            return False  # standalone transport fallback is deliberately unavailable
        async def send():
            guard()  # current policy again inside the actual send coroutine
            result = await adapter.send_inbox_watcher_alert(str(self.source.chat_id), outbox['count'], guard)
            # Same adapter SendResult contract as the scheduler: not send-attempt proof.
            return result.success is True and bool(result.message_id)
        future = asyncio.run_coroutine_threadsafe(send(), self.loop)
        try:
            return future.result(timeout=30)
        except Exception:
            future.cancel()
            return False


def binding_for_job(job, adapters, loop):
    from gateway.config import Platform
    from gateway.session import SessionSource
    from clover_constants import get_clover_home
    metadata = job.get('inbox_watcher') or {}
    if Path(metadata.get('home', '')).resolve() != get_clover_home().resolve():
        raise Denied('watcher profile changed')
    p = Store(state_path(get_clover_home())).read()['policy']
    if not p or p['state'] != 'active':
        raise Denied('watcher consent inactive')
    adapter = (adapters or {}).get(Platform.TELEGRAM)
    runner = getattr(adapter, 'gateway_runner', None)
    if runner is None:
        raise Denied('live native gateway required; standalone watcher unavailable')
    s = p['snapshot']
    source = SessionSource(platform=Platform.TELEGRAM, chat_type='dm',
        user_id=s['actor'], chat_id=s['destination']['chat_id'], thread_id=s['destination']['thread_id'])
    return Binding(runner, source, loop=loop)


def status(binding):
    # Status, like Off, must survive source removal or credential rotation.
    # It reads only the exact current private administrative scope and receipt.
    snapshot = binding.revocation_scope()
    path = state_path(snapshot['home'])
    if not path.exists():
        return '📬 Watcher Off · extra model usage only after a separate paid review.'
    d = Store(path).read()
    p = d['policy']
    if p and any(p['snapshot'][k] != snapshot[k] for k in ('home','actor','destination')):
        raise Denied('watcher belongs to another private scope')
    b = d['budget']
    cap = p['snapshot']['limits']['attempts_day'] if p else 0
    return (f"📬 Watcher {p['state'] if p else 'disabled'} · {d['status']}\n"
            f"UTC allowance day {b['day']}: {max(0,cap-b['attempts'])} attempts left; "
            f"{b['attempts']} attempted, {b['input_units']} input units, {b['output_tokens']} output tokens reserved. "
            "Failed/uncertain attempts count. Main Off does not stop independent jobs. /proactive watcher off")


# Real text parity, bound to exact profile+actor+private destination. Memory-only
# reviews expire and disappear on restart. Acceptance never mints a wider scope.
_reviews = {}
_review_lock = threading.RLock()


def text_command(binding, args):
    try:
        parts = args.split()
        if parts == ['watcher','off']:
            return revoke(binding)
        if parts == ['watcher','retry-registration']:
            from cron.inbox_watcher import retry_registration
            result = retry_registration(binding)
            return f"📬 Watcher {result['state']} · owned job {result['job_id']}. /proactive watcher off"
        if parts in (['watcher'], ['watcher','status']):
            return status(binding) + ('\nReview: /proactive watcher review native-imap INBOX '
                '30 7 2 openai this-private-chat headers-only\n(30/60 min, threshold 7/9, attempts 0/2/8 per UTC day).')
        key = digest(binding.snapshot())
        if (len(parts) == 10 and parts[:4] == ['watcher','review','native-imap','INBOX']
                and parts[7:] == ['openai','this-private-chat','headers-only']):
            draft = Draft(binding)
            for value in ['source','destination','route','cadence '+parts[4],
                          'urgency '+parts[5], 'limits '+parts[6]]:
                draft.choose(value)
            token = secrets.token_hex(12)
            with _review_lock:
                for k in list(_reviews):
                    if time.monotonic() - _reviews[k][1].created > 300:
                        del _reviews[k]
                if len(_reviews) >= 128:
                    raise Denied('too many pending watcher reviews')
                _reviews[key] = (token, draft)
            return draft.render()['text'] + '\n/proactive watcher accept-extra-usage '+token
        if len(parts) == 3 and parts[:2] == ['watcher','accept-extra-usage']:
            with _review_lock:
                token, draft = _reviews.pop(key, (None, None))
                if token is None or not secrets.compare_digest(token, parts[2]):
                    raise Denied('expired or foreign paid review token')
                result = draft.accept('accept-extra-usage')
            return f"📬 Watcher {result['state']} · owned job {result['job_id']}. /proactive watcher off"
        return ('Unknown watcher choice; nothing changed.\n/proactive watcher review native-imap INBOX '
                '30 7 2 openai this-private-chat headers-only\nThen read the review and accept its exact token. watcher on/accept/Save cannot enable it.')
    except Exception:
        return '📬 Watcher unavailable or review refused; no activation confirmed. Check current source, private admin policy, route and limits.'
