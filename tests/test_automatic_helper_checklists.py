"""Accepted dispatch -> real child todo relay -> publisher -> PTB request bytes.

Only credentials, child/model work and the network endpoint are fake. No live
Telegram messages or model requests. Tests use the actual launch and stores.
"""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio
from telegram.ext import Application
from telegram.request import BaseRequest

from agent import ack_checklist as ac
from agent.delegation_activity import bind_activity_sink, _ACTIVITY_SINK
from gateway import delegation_activity as da
from gateway.config import PlatformConfig
from gateway.session_context import clear_session_vars, set_session_vars
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_delegation_activity import _turn_runner
from tests.tools.test_delegate_child_steps import _root, _CREDS
from tools import async_delegation as ad
from tools import delegate_tool as dt
from tools.todo_tool import TodoStore

pytestmark = pytest.mark.unified_card


class Endpoint(BaseRequest):
    def __init__(self):
        self.calls = []
        self.next_id = 0

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        params = request_data.parameters if request_data else {}
        name = url.rsplit('/', 1)[-1]
        # Force a JSON roundtrip: assertions below inspect actual serialized
        # Bot API parameters, not a formatting helper's intermediate value.
        self.calls.append(json.loads(json.dumps({'method': name, **params})))
        if name == 'getMe':
            result = {'id': 123456, 'is_bot': True, 'first_name': 'Bot', 'username': 'testbot'}
        elif name == 'deleteMessage':
            result = True
        else:
            self.next_id += 1
            result = {'message_id': params.get('message_id', self.next_id), 'date': 1800000000,
                      'chat': {'id': 100, 'type': 'private'}, 'text': params.get('text', '')}
        return 200, json.dumps({'ok': True, 'result': result}).encode()

    def texts(self):
        return [c['text'] for c in self.calls if c['method'] in ('sendMessage', 'editMessageText')]


@pytest_asyncio.fixture
async def launch(monkeypatch):
    ad._reset_for_tests()
    ac.reset_for_tests()
    ac.set_style_for_tests('unified')
    da._ACK_EDITORS.clear()
    endpoint = Endpoint()
    app = Application.builder().token('123456:test-token').request(endpoint).build()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='123456:test-token'))
    adapter._bot = app.bot
    pub = da.DelegationActivityPublisher(
        adapter=adapter, chat_id='100', loop=asyncio.get_running_loop(),
        min_interval=0.0, auto_heartbeat=False, expandable=True,
    )
    token = bind_activity_sink(pub)
    session_tokens = set_session_vars(platform='telegram', chat_id='100', chat_type='dm',
                                      session_key='agent:main:telegram:dm:100', async_delivery=True)
    root = _root()
    root.tool_progress_callback = _turn_runner(pub).progress_callback
    kids = []
    release = threading.Event()

    def build(**kw):
        i = kw['task_index']
        child = SimpleNamespace(
            _delegate_role='leaf', _subagent_id=f'sa-{i}', session_id=f'cs-{i}',
            _todo_store=TodoStore(), close=lambda: None,
            get_activity_summary=lambda: {'api_call_count': 0, 'current_tool': None},
        )
        ref = {'title': '', 'provider': 'custom'}
        child._progress_identity_ref = ref
        child.tool_progress_callback = dt._build_child_progress_callback(
            i, kw['goal'], kw['parent_agent'], kw['task_count'],
            subagent_id=child._subagent_id, model='m', session_ref=ref,
        )
        kids.append(child)
        kw['parent_agent']._active_children.append(child)
        return child

    def work(*args, **kw):
        assert release.wait(20), 'test must release background children'
        return {'task_index': args[0], 'status': 'completed', 'summary': 'Plain summary: Done.',
                'api_calls': 0, 'duration_seconds': 0.0, 'model': 'm', 'exit_reason': 'completed'}

    monkeypatch.setattr(dt, '_build_child_agent', build)
    monkeypatch.setattr(dt, '_run_single_child', work)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **k: _CREDS)
    monkeypatch.setattr(dt, '_get_max_async_children', lambda: 3)
    # Read-only credential-free config: never inherited routing/account defaults.
    monkeypatch.setattr(dt, '_load_config', lambda: {})
    async with app:
        try:
            yield SimpleNamespace(root=root, kids=kids, pub=pub, endpoint=endpoint, adapter=adapter)
        finally:
            release.set()
            ad._reset_for_tests(drain_timeout=5)
            await pub.aclose()
            _ACTIVITY_SINK.reset(token)
            clear_session_vars(session_tokens)
            ac.reset_for_tests()
            da._ACK_EDITORS.clear()


async def settle(pub):
    for _ in range(25):
        await asyncio.sleep(0)
    await pub.drain()
    for editor in list(da._ACK_EDITORS.values()):
        # Exercise the real editor, without its normal network-rate delay.
        editor._min_interval = 0.0
        editor.notify()
    for _ in range(25):
        await asyncio.sleep(0)
    tasks = [e._task for e in da._ACK_EDITORS.values() if e._task is not None]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


@pytest.mark.asyncio
async def test_omitted_steps_seed_real_launch_store_and_first_telegram_send(launch):
    goal = 'Audit the checkout links'
    launch.root._todo_store.write([{'id': 'old', 'content': 'Unrelated private billing task', 'status': 'in_progress'}])
    out = json.loads(dt.delegate_task(parent_agent=launch.root, goal=goal,
                                     context='PRIVATE: internal credentials and working paths', background=True))
    assert out['status'] == 'dispatched', out
    await settle(launch.pub)
    rows = launch.kids[0]._todo_store.read()
    assert rows == [{'id': 's1', 'content': goal, 'status': 'in_progress'}]
    assert launch.endpoint.texts(), launch.endpoint.calls
    wire = launch.endpoint.texts()[0]
    assert '▸ ' + goal in wire
    assert 'PRIVATE' not in wire and 'billing' not in wire
    assert ac._PARTS[out['delegation_id']].checklist.steps == [[goal, 'in_progress']]


def child_todo(child, todos, *, merge=False):
    from tools.todo_tool import todo_tool

    result = todo_tool(todos=todos, merge=merge, store=child._todo_store)
    child.tool_progress_callback('tool.started', 'todo', '', {'todos': todos})
    child.tool_progress_callback('tool.completed', 'todo', result=result, is_error=False)
    return json.loads(result)


@pytest.mark.asyncio
async def test_real_todo_result_populates_empty_card_then_adds_and_matches_ids(launch):
    out = json.loads(dt.delegate_task(parent_agent=launch.root, goal='Repair checkout links', background=True))
    assert out['status'] == 'dispatched'
    cl = ac._PARTS[out['delegation_id']].checklist
    # An old accepted card restored without seed rows must be repairable too.
    cl.steps.clear()
    child_todo(launch.kids[0], [
        {'id': 'inspect', 'content': 'Find broken links', 'status': 'completed'},
        {'id': 'repair', 'content': 'Repair links', 'status': 'in_progress'},
    ])
    await settle(launch.pub)
    assert cl.steps == [['Find broken links', 'completed'], ['Repair links', 'in_progress']]
    child_todo(launch.kids[0], [
        {'id': 'verify', 'content': 'Check checkout navigation', 'status': 'in_progress'},
        {'id': 'repair', 'content': 'Repair checkout links', 'status': 'completed'},
        {'id': 'inspect', 'content': 'Find broken links', 'status': 'completed'},
    ])
    await settle(launch.pub)
    assert cl.steps == [
        ['Find broken links', 'completed'], ['Repair checkout links', 'completed'],
        ['Check checkout navigation', 'in_progress'],
    ], 'renaming/reordering must never tick a different item by position'
    wire = launch.endpoint.texts()[-1]
    assert '✓ Find broken links' in wire and '▸ Check checkout navigation' in wire
    assert cl.calls == 0, 'todo bookkeeping does not inflate native tool counts'


@pytest.mark.asyncio
async def test_observed_rows_and_ids_survive_record_restart_and_real_relaunch(launch):
    from gateway import delegation_resume as resume
    from gateway.config import Platform
    from gateway.session import SessionSource
    from tools import delegation_inflight as inflight

    out = json.loads(dt.delegate_task(parent_agent=launch.root, goal='Repair checkout links', background=True))
    did = out['delegation_id']
    child_todo(launch.kids[0], [
        {'id': 's1', 'content': 'Inspect checkout links', 'status': 'completed'},
        {'id': 'fix', 'content': 'Repair checkout links', 'status': 'in_progress'},
        {'id': 'verify', 'content': 'Check navigation', 'status': 'pending'},
    ])
    await settle(launch.pub)
    record = inflight.read_record(did)
    assert record is not None
    assert record['tasks'][0]['checklist'] == launch.kids[0]._todo_store.read()
    # Lose in-memory card state just as a new gateway does. Re-enter its actual
    # resume launch function, using the saved per-task record and real delegate.
    ac.reset_for_tests()
    ac.set_style_for_tests('unified')
    restored = resume._launch(SimpleNamespace(), record, launch.root,
                             SessionSource(platform=Platform.TELEGRAM, chat_id='100', chat_type='dm'),
                             launch.pub, resume.build_resume_tasks(record, [0]))
    assert restored['status'] == 'dispatched', restored
    cl = ac._PARTS[restored['delegation_id']].checklist
    assert cl.steps == [['Inspect checkout links', 'completed'], ['Repair checkout links', 'in_progress'],
                        ['Check navigation', 'pending']]
    assert launch.kids[-1]._todo_store.read() == record['tasks'][0]['checklist']
    child_todo(launch.kids[-1], [{'id': 'fix', 'content': 'Repair all checkout links', 'status': 'completed'}], merge=True)
    await settle(launch.pub)
    assert cl.steps[0][1] == cl.steps[1][1] == 'completed'
    assert cl.steps[1][0] == 'Repair all checkout links'


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 2])
@pytest.mark.parametrize('with_handoff', [False, True])
@pytest.mark.parametrize('with_plan', [False, True])
async def test_accepted_launch_matrix_renders_public_steps_and_tracks_each_child(launch, count, with_handoff, with_plan, record_property):
    from agent import foreground_time_limit as fl
    from agent import router_backstop as rb
    from tools import delegation_inflight as inflight

    goals = ['Audit checkout links', 'Check newsletter signup'][:count]
    tasks = [{'goal': g, 'title': g, 'context': 'PRIVATE_CONTEXT never a UI source'} for g in goals]
    if with_plan:
        tasks[0]['steps'] = ['Inspect A & B <links>', 'Repair broken links', 'Check navigation']
    handoff = {'work': 'Checking checkout links', 'outcome': 'Working navigation', 'say': 'I am checking the links.'} if with_handoff else None
    if with_plan and handoff:
        handoff['steps'] = ['Read link list', 'Fix targets']
    out = json.loads(dt.delegate_task(parent_agent=launch.root, tasks=tasks, handoff=handoff, background=True))
    assert out['status'] == 'dispatched', out
    if with_handoff:
        # This is the accepted acknowledgement path, including BaseAdapter's
        # real opener/card binding wrapper, not manual card/editor construction.
        text = rb._ack_text(fl.own_dispatch_receipts(launch.root), launch.root)
        result = await launch.adapter.send('100', text)
        assert result.success
    await settle(launch.pub)
    part = ac._PARTS[out['delegation_id']]
    objects = part.checklist.kids or [part.checklist]
    expected = [ac.dispatch_steps(g, tasks[i].get('steps')) for i, g in enumerate(goals)]
    if with_plan and handoff and count == 1:
        expected[0] = tuple(handoff['steps'])
    assert [c._todo_store.read() for c in launch.kids] == [ac.seed_todos(s) for s in expected]
    assert [o.steps for o in objects] == [[[label, 'in_progress' if i == 0 else 'pending']
                                          for i, label in enumerate(s)] for s in expected]
    wire = launch.endpoint.texts()[-1]
    assert ('▸ ' if count == 1 else '○ ') + expected[0][0] in wire
    assert 'PRIVATE_CONTEXT' not in wire and '<links>' not in wire
    assert len(inflight.read_record(out['delegation_id'])['tasks']) == count
    if count == 2:
        before = [r[:] for r in objects[0].steps]
        child_todo(launch.kids[1], [
            {'id': 's1', 'content': goals[1], 'status': 'completed'},
            {'id': 'new', 'content': 'Check signup confirmation', 'status': 'pending'},
        ])
        await settle(launch.pub)
        assert objects[0].steps == before
        assert objects[1].steps == [[goals[1], 'completed'], ['Check signup confirmation', 'pending']]
        assert '○ Check signup confirmation' in launch.endpoint.texts()[-1]
    else:
        cb = launch.kids[0].tool_progress_callback
        cb('tool.started', 'read_file', 'page.html', {'path': 'page.html'}, tool_call_id='read')
        cb('tool.completed', 'read_file', result='PRIVATE OUTPUT', tool_call_id='read', is_error=False)
        await settle(launch.pub)
        assert part.checklist.calls == 1
        assert 'read\\_file page' in launch.endpoint.texts()[-1]
        assert 'PRIVATE OUTPUT' not in launch.endpoint.texts()[-1]
        cb('subagent.complete', status='completed', summary='Result: done\nPlain summary: The links work.')
        await settle(launch.pub)
        final = launch.endpoint.texts()[-1]
        assert part.card.finished and 'done' in final
        assert '▸' not in final and 'tap to watch' not in final
        snapshot = [r[:] for r in part.checklist.steps]
        child_todo(launch.kids[0], [{'id': 'late', 'content': 'Must not restart', 'status': 'in_progress'}])
        assert part.checklist.steps == snapshot
    record_property('telegram_wire', json.dumps(launch.endpoint.calls, ensure_ascii=False))


@pytest.mark.asyncio
async def test_old_record_without_steps_or_snapshot_relaunches_with_automatic_goal_row(launch):
    from gateway import delegation_resume as resume
    from gateway.config import Platform
    from gateway.session import SessionSource

    record = {'delegation_id': 'deleg_oldrecord', 'session_key': 'agent:main:telegram:dm:100',
              'tasks': [{'goal': 'Check archived checkout links', 'context': 'PRIVATE OLD CONTEXT'}],
              'origin': {'origin_platform': 'telegram', 'origin_chat_id': '100'}}
    tasks = resume.build_resume_tasks(record, [0])
    assert tasks[0].get('steps') is None
    result = resume._launch(SimpleNamespace(), record, launch.root,
                            SessionSource(platform=Platform.TELEGRAM, chat_id='100', chat_type='dm'),
                            launch.pub, tasks)
    assert result['status'] == 'dispatched', result
    await settle(launch.pub)
    assert launch.kids[-1]._todo_store.read() == ac.seed_todos(['Check archived checkout links'])
    assert '▸ Check archived checkout links' in launch.endpoint.texts()[-1]


@pytest.mark.asyncio
async def test_partial_multi_restart_keeps_the_selected_childs_snapshot_not_its_siblings(launch):
    from gateway import delegation_resume as resume
    from gateway.config import Platform
    from gateway.session import SessionSource

    selected = [{'id': 'inspect', 'content': 'Read newsletter form', 'status': 'completed'},
                {'id': 'verify', 'content': 'Check newsletter confirmation', 'status': 'in_progress'}]
    record = {'delegation_id': 'deleg_oldmulti', 'session_key': 'agent:main:telegram:dm:100',
              'tasks': [{'goal': 'Check checkout links', 'checklist': [{'id': 'other', 'content': 'Sibling task', 'status': 'completed'}]},
                        {'goal': 'Check newsletter signup', 'checklist': selected}],
              'origin': {'origin_platform': 'telegram', 'origin_chat_id': '100'}}
    result = resume._launch(SimpleNamespace(), record, launch.root,
                            SessionSource(platform=Platform.TELEGRAM, chat_id='100', chat_type='dm'),
                            launch.pub, resume.build_resume_tasks(record, [1]))
    assert result['status'] == 'dispatched', result
    await settle(launch.pub)
    assert launch.kids[-1]._todo_store.read() == selected
    wire = launch.endpoint.texts()[-1]
    assert '✓ Read newsletter form' in wire and '▸ Check newsletter confirmation' in wire
    assert 'Sibling task' not in wire


@pytest.mark.asyncio
async def test_resume_snapshot_is_consumed_only_by_its_launch_not_a_later_dispatch(launch):
    from tools import delegation_inflight as inflight

    saved = [{'id': 'inspect', 'content': 'Inspect restored links', 'status': 'completed'}]
    with inflight.resume_context(1, 'deleg_previous', checklists=[saved]):
        first = json.loads(dt.delegate_task(parent_agent=launch.root, goal='Repair restored checkout links', background=True))
        second = json.loads(dt.delegate_task(parent_agent=launch.root, goal='Check a different signup form', background=True))
    assert first['status'] == second['status'] == 'dispatched'
    assert launch.kids[0]._todo_store.read() == saved
    assert launch.kids[1]._todo_store.read() == ac.seed_todos(['Check a different signup form'])
