"""Consumer-level existing safety contracts, not helper-only acceptance."""
import json
import threading
import httpx
import pytest
from gateway.config import Platform
from tests.test_proactive_picker import picker, home  # noqa: F401
from tests.gateway.test_proactive_command import event
from tests.test_paid_inbox_admission import setup
from tests.test_paid_inbox_runner import InputBinding, accepted


def test_budget_exhaustion_after_restart_is_inspectable_without_third_physical_attempt(home):
    from cron import inbox_watcher as w
    b = InputBinding(home)
    job = accepted(home, b)
    b.deliver_ok = True
    def fetch(cursor, *, max_items):
        b.reads += 1
        uid = 10 if cursor is None else cursor['cursor'] + 1
        return {'epoch':'1', 'cursor':uid, 'items':[] if cursor is None else
            [{'id':str(uid), 'from':'x', 'subject':'urgent', 'date':'today'}]}
    b.fetch = fetch
    for stamp in (172800, 174600, 176400):
        w.run(job, b, now=stamp)
    assert len(b.attempts) == 2
    b.client.close()
    restarted = InputBinding(home)
    restarted.fetch = fetch
    w.run(job, restarted, now=178200)
    assert not restarted.attempts and not restarted.sent
    d = w.Store(w.state_path(home)).read()
    assert d['budget']['attempts'] == 2 and d['status'] == 'daily allowance exhausted'
    assert d['budget']['output_tokens'] == 512
    restarted.client.close()


@pytest.mark.parametrize('output', ['empty', 'below', 'invalid-json', 'truncated'])
def test_quiet_or_invalid_classification_never_delivers_or_rebills_the_same_mail(home, output):
    from cron import inbox_watcher as w
    b = InputBinding(home)
    job = accepted(home, b)
    w.run(job, b, now=172800)
    b.items = [{'id':'11', 'from':'x', 'subject':'PRIVATE-RUNTIME-CANARY', 'date':'today'}]
    def transport(request):
        b.attempts.append(json.loads(request.content))
        content = {'empty':'{"scores":[]}', 'below':'{"scores":[{"index":0,"score":1,"reason":"low"}]}',
                   'invalid-json':'PRIVATE-RUNTIME-CANARY', 'truncated':'{"scores":[]}'}[output]
        return httpx.Response(200, json={'id':'offline', 'object':'chat.completion', 'created':0,
            'model':'gpt-4o-mini-2024-07-18', 'choices':[{'index':0,
                'finish_reason':'length' if output == 'truncated' else 'stop',
                'message':{'role':'assistant','content':content}}]})
    b.client._client._transport = httpx.MockTransport(transport)
    w.run(job, b, now=174600)
    assert len(b.attempts) == 1 and not b.sent
    b.items = []
    w.run(job, b, now=176400)
    assert len(b.attempts) == 1 and not b.sent
    d = w.Store(w.state_path(home)).read()
    assert d['budget']['attempts'] == 1 and d['cursor']['cursor'] == 11
    assert 'PRIVATE-RUNTIME-CANARY' not in w.state_path(home).read_bytes().decode(errors='ignore')
    b.client.close()


def test_total_batch_input_ceiling_is_enforced_before_physical_request(home):
    from cron import inbox_watcher as w
    b = InputBinding(home)
    job = accepted(home, b)
    w.run(job, b, now=172800)
    b.fetch = lambda *a, **k: {'epoch':'1', 'cursor':15, 'items':[
        {'id':str(uid), 'from':'X'*500, 'subject':'Y'*500, 'date':'Z'*500} for uid in range(11,16)]}
    w.run(job, b, now=174600)
    assert not b.attempts and not b.sent
    assert w.Store(w.state_path(home)).read()['status'] == 'input allowance denied'
    b.client.close()


def test_concurrent_full_ticks_claim_one_cadence_without_duplicate_read_or_billing(home):
    from cron import inbox_watcher as w
    b = InputBinding(home)
    job = accepted(home, b)
    w.run(job, b, now=172800)
    b.items = [{'id':'11', 'from':'x', 'subject':'urgent', 'date':'today'}]
    start, release = threading.Event(), threading.Event()
    original = b.fetch
    def fetch(*args, **kwargs):
        start.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    b.fetch = fetch
    first = threading.Thread(target=w.run, args=(job,b), kwargs={'now':174600})
    first.start()
    assert start.wait(10)
    w.run(job, b, now=174600)
    release.set()
    first.join(10)
    assert not first.is_alive() and len(b.attempts) == 1 and b.reads == 2
    assert w.Store(w.state_path(home)).usage()['attempts'] == 1
    b.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['malformed-policy','policy-read-error','source-account','source-folder','source-key','telegram-key','revoked-admin'])
async def test_current_runtime_scope_loss_blocks_source_and_physical_dispatch(picker, home, monkeypatch, tmp_path, change):
    from cron import inbox_watcher as w, jobs
    from gateway.inbox_watcher_controls import Binding
    r, a = setup(picker, monkeypatch)
    review = await r._handle_message(event(
        '/proactive watcher review native-imap INBOX 30 7 2 openai this-private-chat headers-only', uid='123',chat_id='123'))
    await r._handle_message(event(review.splitlines()[-1],uid='123',chat_id='123'))
    job = jobs.load_jobs()[0]
    if change == 'malformed-policy':
        path = tmp_path/'managed'; path.mkdir()
        (path/'config.yaml').write_text('[broken')
        monkeypatch.setenv('CLOVER_MANAGED_DIR', str(path))
    if change == 'policy-read-error':
        from clover_cli import managed_scope
        def unreadable(*args, **kwargs): raise PermissionError('PRIVATE-RUNTIME-CANARY')
        monkeypatch.setattr(managed_scope, 'load_managed_config', unreadable)
    if change == 'source-account': r.adapters[Platform.EMAIL].account = 'different@example.invalid'
    if change == 'source-folder': r.adapters[Platform.EMAIL].folder = 'Archive'
    if change == 'source-key': r.adapters[Platform.EMAIL].fingerprint = 'b'*64
    if change == 'telegram-key': a.config.token = 'synthetic-rotated'
    if change == 'revoked-admin': r.config.platforms[Platform.TELEGRAM].extra['allow_admin_from'] = ['other']
    source = event('/proactive watcher',uid='123',chat_id='123').source
    source_reads = []
    def forbidden_source(*args, **kwargs):
        source_reads.append(True)
        raise AssertionError('scope denial must precede source access')
    r.adapters[Platform.EMAIL].inbox_watcher_read = forbidden_source
    import agent.auxiliary_client as aux
    monkeypatch.setattr(aux, 'create_bounded_inbox_client', lambda *a: pytest.fail('runtime denial must precede dispatch'))
    w.run(job, Binding(r,source), now=172800)
    d = w.Store(w.state_path(home)).read()
    assert not source_reads and d['budget']['attempts'] == 0 and d['cursor'] is None
    assert d['status'] != 'quiet; baseline or no new headers'
    assert 'PRIVATE-RUNTIME-CANARY' not in d['status']
