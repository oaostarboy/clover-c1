"""Offline contract proof for the connected native email source; never an inbox."""
from tests.gateway._plugin_adapter_loader import load_plugin_adapter
from gateway.config import PlatformConfig


def test_connected_email_watcher_is_metadata_only_and_read_only(monkeypatch):
    mod = load_plugin_adapter('email')
    monkeypatch.setattr(mod, '_get_secret', lambda k, default='': {
        'EMAIL_ADDRESS': 'owner@example.invalid', 'EMAIL_PASSWORD': 'synthetic-secret',
        'EMAIL_IMAP_HOST': 'imap.example.invalid'}.get(k, default))
    adapter = mod.EmailAdapter(PlatformConfig(enabled=True))
    describe = getattr(adapter, 'inbox_watcher_metadata', None)
    assert callable(describe), 'native email adapter needs a bounded read-only watcher contract'
    metadata = describe()
    assert metadata['account'] == 'owner@example.invalid'
    assert metadata['folder'] == 'INBOX' and metadata['query'] == 'new UID only'
    assert 'synthetic-secret' not in repr(metadata)
    calls = []
    class Imap:
        old = False
        def __init__(self, *a, **kw): calls.append(('connect', a))
        def login(self, *a): calls.append(('login',))
        def select(self, folder, readonly=False):
            calls.append(('select', folder, readonly)); return 'OK', [b'1']
        def response(self, field): return field, [b'7' if field == 'UIDVALIDITY' else b'12']
        def uid(self, command, *args):
            calls.append((command, *args))
            from datetime import datetime, timezone
            when = '01-Jan-2000 00:00:00 +0000' if self.old else datetime.now(timezone.utc).strftime('%d-%b-%Y %H:%M:%S %z')
            meta = f'11 (UID 11 INTERNALDATE "{when}"'.encode()
            return 'OK', [(meta, b'From: sender@example.invalid\r\nSubject: urgent\r\nDate: today\r\n\r\n')]
        def logout(self): calls.append(('logout',))
    monkeypatch.setattr(mod.imaplib, 'IMAP4_SSL', Imap)
    assert adapter.inbox_watcher_read(None, max_items=2) == {'epoch': '7', 'cursor': 11, 'items': []}
    result = adapter.inbox_watcher_read({'epoch': '7', 'cursor': 10}, max_items=2)
    assert result['items'][0]['id'] == '11'
    Imap.old = True
    assert adapter.inbox_watcher_read({'epoch':'7','cursor':10}, max_items=2)['items'] == []
    assert all(c[2] is True for c in calls if c[0] == 'select')
    assert all('BODY.PEEK[HEADER.FIELDS' in c[-1] for c in calls if c[0] == 'fetch')
    assert not any(c[0].lower() in {'store', 'expunge', 'copy', 'append', 'search'} for c in calls)
