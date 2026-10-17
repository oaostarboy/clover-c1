"""Independent public-wire privacy probes; synthetic values only."""
import json
import pytest
from tests.test_automatic_helper_checklists import child_todo, settle
from tools import delegate_tool as dt
from tools import delegation_inflight as inflight
from agent import ack_checklist as ac

# conftest is auto-discovered here; the external reviewer proposal loaded it explicitly.
pytest_plugins = ['tests.test_automatic_helper_checklists']
pytestmark = pytest.mark.unified_card

CASES = [
    ('prose_password', 'Verify password=' + 'ReviewOnlyOpaque42' + ' for login', 'ReviewOnlyOpaque42'),
    ('prose_token', 'Verify token=' + 'ReviewOnlyToken73' + ' for login', 'ReviewOnlyToken73'),
    ('vendor_key', 'Rotate ' + 'sk-' + 'ReviewOnlyKey12345678901234567890', 'ReviewOnlyKey12345678901234567890'),
    ('private_path', 'Read /home/review-private-owner/project/secrets.txt', '/home/review-private-owner'),
    ('markup_link', 'Check [login](https://review.invalid/private-token)', 'https://review.invalid'),
    ('quoted_secret', 'Check {"password": "ReviewOnlyQuoted52"}', 'ReviewOnlyQuoted52'),
    ('case_spacing', 'Verify PaSsWoRd \t = ReviewOnlyCase81 for login', 'ReviewOnlyCase81'),
    ('quoted_spaces', 'Verify TOKEN = "Review Only Quoted 82" for login', 'Review Only Quoted 82'),
    ('single_quotes', "Verify 'password' : 'Review Only Quoted 83' for login", 'Review Only Quoted 83'),
    ('controls', 'Verify pass\x00word=ReviewOnlyControl84 for login', 'ReviewOnlyControl84'),
    ('markup_key', 'Verify **token**=<b>ReviewOnlyMarkup85</b> for login', 'ReviewOnlyMarkup85'),
    ('html_entities', 'Verify password&#61;&quot;ReviewOnlyEntity86&quot; for login', 'ReviewOnlyEntity86'),
    ('quoted_newline', 'Verify token="ReviewOnlyLine87\nprivate tail" for login', 'ReviewOnlyLine87'),
    ('long_value', 'Verify token=' + 'ReviewOnlyLong88' * 20 + ' for login', 'ReviewOnlyLong88'),
    ('backticks', 'Verify token=`Review Only Backtick89` for login', 'Backtick89'),
    ('curly_quotes', 'Verify password=“Review Only Curly90” for login', 'Curly90'),
    ('format_control', 'Verify to\u200bken=ReviewOnlyFormat91 for login', 'ReviewOnlyFormat91'),
    ('ansi_key', 'Verify pass\x1b[31mword=ReviewOnlyAnsi92 for login', 'ReviewOnlyAnsi92'),
]

@pytest.mark.asyncio
@pytest.mark.parametrize('name,label,forbidden', CASES, ids=[c[0] for c in CASES])
async def test_native_todo_secret_must_not_reach_public_card(launch, name, label, forbidden, record_property):
    out = json.loads(dt.delegate_task(parent_agent=launch.root, goal='Check login form', background=True))
    assert out['status'] == 'dispatched'
    await settle(launch.pub)
    child_todo(launch.kids[0], [{'id':'s1','content':label,'status':'in_progress'}])
    await settle(launch.pub)
    wire = '\n'.join(launch.endpoint.texts())
    record = inflight.read_record(out['delegation_id'])
    record_property('wire_calls',json.dumps(launch.endpoint.calls))
    record_property('observed_public_rows',json.dumps(record['tasks'][0].get('checklist')))
    assert forbidden not in wire, 'private native todo value reached serialized Telegram parameters'
    assert forbidden not in json.dumps(record['tasks'][0].get('checklist')), 'private value persisted as public snapshot'

@pytest.mark.asyncio
@pytest.mark.parametrize('row_count', [3, 6])
async def test_resume_equal_labels_and_copied_child_context_are_exact(launch, monkeypatch, record_property, row_count):
    import contextvars
    from gateway import delegation_resume as resume
    from gateway.config import Platform
    from gateway.session import SessionSource
    saved = [{'id':'a','content':'Check links','status':'completed'},
             {'id':'b','content':'Check links','status':'pending'},
             {'id':'c','content':'Check signup','status':'in_progress'}]
    if row_count == 6:
        saved = [{'id':chr(97+i),'content':'Check links',
                  'status':'completed' if i < 5 else 'in_progress'} for i in range(6)]
    first_renders = []
    first_records = []
    write_record = dt._record_inflight_dispatch
    def capture_record(delegation_id, **kwargs):
        write_record(delegation_id, **kwargs)
        first_records.append(inflight.read_record(delegation_id)['tasks'][0]['checklist'])
    monkeypatch.setattr(dt, '_record_inflight_dispatch', capture_record)
    render = ac.Checklist.render
    def capture_render(self, *args, **kwargs):
        first_renders.append(ac.checklist_todos(self.steps, self.step_ids))
        return render(self, *args, **kwargs)
    monkeypatch.setattr(ac.Checklist, 'render', capture_render)
    copied = []
    build = dt._build_child_agent
    def capture_context(**kw):
        copied.append(contextvars.copy_context())
        return build(**kw)
    monkeypatch.setattr(dt,'_build_child_agent',capture_context)
    record = {'delegation_id':'deleg_review_equal','session_key':'agent:main:telegram:dm:100',
              'tasks':[{'goal':'Check links and signup','checklist':saved}],
              'origin':{'origin_platform':'telegram','origin_chat_id':'100'}}
    result = resume._launch(None,record,launch.root,
                           SessionSource(platform=Platform.TELEGRAM,chat_id='100',chat_type='dm'),
                           launch.pub,resume.build_resume_tasks(record,[0]))
    assert result['status']=='dispatched'
    await settle(launch.pub)
    assert copied[0].run(inflight.take_resume_checklists)==[]
    cl=ac._PARTS[result['delegation_id']].checklist
    record_property('rendered_rows',json.dumps(ac.checklist_todos(cl.steps,cl.step_ids)))
    record_property('saved_rows',json.dumps(inflight.read_record(result['delegation_id'])['tasks'][0]['checklist']))
    record_property('wire_calls',json.dumps(launch.endpoint.calls))
    record_property('copied_context_snapshot_consumed',True)
    assert first_renders and first_renders[0] == saved
    assert first_records == [saved]
    # TodoStore intentionally orders active work before pending work. Id/status
    # equality is exact; card and durable public snapshots keep saved row order.
    assert {t['id']: t for t in launch.kids[-1]._todo_store.read()} == {t['id']: t for t in saved}
    assert inflight.read_record(result['delegation_id'])['tasks'][0]['checklist'] == saved
    assert ac.checklist_todos(cl.steps,cl.step_ids)==saved
    expected = [dict(t) for t in saved]
    expected[1]['status']='in_progress'
    child_todo(launch.kids[-1],list(reversed(expected)))
    await settle(launch.pub)
    assert ac.checklist_todos(cl.steps,cl.step_ids)==expected
    assert inflight.read_record(result['delegation_id'])['tasks'][0]['checklist'] == expected
    # Native Stop and late events must not overwrite or reopen a resumed card.
    ac.on_child_event('subagent.complete', delegation_id=result['delegation_id'],
                      task_index=0, subagent_id=launch.kids[-1]._subagent_id, status='cancelled')
    terminal = ac.checklist_todos(cl.steps, cl.step_ids)
    child_todo(launch.kids[-1], saved)
    assert ac.checklist_todos(cl.steps, cl.step_ids) == terminal

@pytest.mark.parametrize('name,label,forbidden', CASES, ids=[c[0] for c in CASES])
def test_dispatch_seed_secret_must_not_reach_public_rows(name,label,forbidden):
    assert forbidden not in ' '.join(ac.dispatch_steps('Check login form', [label]))


@pytest.mark.asyncio
@pytest.mark.parametrize('name,label,forbidden', CASES, ids=[c[0] for c in CASES])
async def test_dispatch_seed_wire_receipt_and_snapshot_are_public(launch, name, label, forbidden, record_property):
    from agent import foreground_time_limit as fl
    from agent import router_backstop as rb
    from dataclasses import asdict

    out = json.loads(dt.delegate_task(parent_agent=launch.root, goal='Check login form',
                                     tasks=None, handoff={'work': 'Check login', 'outcome': 'Login checked',
                                                         'steps': [label]}, background=True))
    assert out['status'] == 'dispatched'
    ack = rb._ack_text(fl.own_dispatch_receipts(launch.root), launch.root)
    assert (await launch.adapter.send('100', ack)).success
    await settle(launch.pub)
    record = inflight.read_record(out['delegation_id'])
    receipts = [asdict(r) for r in fl.own_dispatch_receipts(launch.root)]
    public = {'steps': record['tasks'][0]['steps'], 'checklist': record['tasks'][0]['checklist'],
              'receipts': receipts}
    record_property('public_fields', json.dumps(public, default=str))
    record_property('wire_calls', json.dumps(launch.endpoint.calls))
    assert forbidden not in json.dumps(public, default=str)
    assert launch.endpoint.texts()
    assert forbidden not in '\n'.join(launch.endpoint.texts())
    assert 'Verify' in ' '.join(record['tasks'][0]['steps']) or name in {
        'vendor_key', 'private_path', 'markup_link', 'quoted_secret'}


def test_safe_useful_public_labels_survive():
    labels = ['Check password reset form', 'Verify token expiry handling', 'Read API key setup guide']
    assert ac.clean_steps(labels) == tuple(labels)


@pytest.mark.asyncio
async def test_equal_label_resume_siblings_keep_exact_identity_before_events(launch, record_property):
    from gateway import delegation_resume as resume
    from gateway.config import Platform
    from gateway.session import SessionSource

    saved = [[{'id': f'{prefix}{i}', 'content': 'Check links',
               'status': 'completed' if i < 5 else 'pending'} for i in range(6)]
             for prefix in ('a', 'b')]
    record = {'delegation_id': 'deleg_equal_siblings',
              'session_key': 'agent:main:telegram:dm:100',
              'tasks': [{'goal': 'Check links', 'checklist': rows} for rows in saved],
              'origin': {'origin_platform': 'telegram', 'origin_chat_id': '100'}}
    result = resume._launch(None, record, launch.root,
                            SessionSource(platform=Platform.TELEGRAM, chat_id='100', chat_type='dm'),
                            launch.pub, resume.build_resume_tasks(record, [0, 1]))
    assert result['status'] == 'dispatched'
    await settle(launch.pub)
    cl = ac._PARTS[result['delegation_id']].checklist
    snapshots = lambda: [ac.checklist_todos(k.steps, k.step_ids) for k in cl.kids]
    assert snapshots() == saved
    assert [t['checklist'] for t in inflight.read_record(result['delegation_id'])['tasks']] == saved
    expected = [[dict(t) for t in rows] for rows in saved]
    expected[1][-1]['status'] = 'in_progress'
    child_todo(launch.kids[1], list(reversed(expected[1])))
    await settle(launch.pub)
    assert snapshots() == expected
    record_property('wire_calls', json.dumps(launch.endpoint.calls))
    record_property('saved_rows', json.dumps(snapshots()))


def test_reserved_resume_rows_precede_buffered_terminal_and_cannot_rehydrate():
    ac.reset_for_tests()
    try:
        saved = [{'id': 'a', 'content': 'Check links', 'status': 'completed'},
                 {'id': 'b', 'content': 'Check links', 'status': 'pending'}]
        assert ac.reserve('resume', checklists=[saved])
        ac.on_child_event('subagent.complete', delegation_id='resume', task_index=0,
                          status='cancelled')
        part = ac.open_part('resume', 'Check links', ['Check links', 'Check links'])
        assert part.checklist.state == ac.STOPPED
        terminal = ac.checklist_todos(part.checklist.steps, part.checklist.step_ids)
        assert [t['id'] for t in terminal] == ['a', 'b']
        assert terminal[0]['status'] == 'completed'
        assert ac.reserve('resume', checklists=[saved])
        assert ac.open_part('resume', 'Check links', []).checklist.state == ac.STOPPED
        assert ac.checklist_todos(part.checklist.steps, part.checklist.step_ids) == terminal
        ac.reserve('released', checklists=[saved])
        ac.release('released')
        assert 'released' not in ac._RESERVED_CHECKLISTS
    finally:
        ac.reset_for_tests()
