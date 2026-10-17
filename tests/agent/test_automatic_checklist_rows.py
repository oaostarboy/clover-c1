"""Privacy, identity and replay contracts for automatic public checklist rows."""
import pytest

from agent import ack_checklist as ac

pytestmark = pytest.mark.unified_card


@pytest.fixture(autouse=True)
def clean():
    ac.reset_for_tests()
    ac.set_style_for_tests('unified')
    yield
    ac.reset_for_tests()


@pytest.mark.parametrize('raw, forbidden', [
    ('Check sk-abcdefghijklmnopqrstuvwxyz0123456789', 'abcdefghijklmnopqrstuvwxyz'),
    ('Read /home/private-owner/project/secrets.txt', '/home/private-owner'),
    ('Read C:\\Users\\private-owner\\project\\secrets.txt', 'private-owner'),
    ('Inspect deleg_abcdef1234 links', 'deleg_abcdef1234'),
    ('Check **links** [report](https://private.example/token)', 'https://'),
    ('Check <b>links</b>\u202e\x00', '<'),
])
def test_public_rows_sanitize_before_truncating(raw, forbidden):
    labels = ac.clean_steps([raw])
    assert labels
    assert forbidden not in labels[0]
    assert all(c not in labels[0] for c in '*`[]<>\u202e\x00')
    assert len(labels[0]) <= ac.MAX_STEP_CHARS


@pytest.mark.parametrize('plan', [None, '', {}, [], [None, 7, '', '\x00']])
def test_old_schema_or_invalid_plan_gets_one_task_specific_row(plan):
    card = ac.open_dispatch_card('old', ['Audit checkout links'], steps=plan)
    assert card.parts[0].checklist.steps == [['Audit checkout links', 'in_progress']]
    assert '▸ Audit checkout links' in card.render()


def test_first_event_card_multi_uses_each_tasks_own_goal():
    card = ac.open_dispatch_card('multi', ['Audit checkout links', 'Check newsletter signup'])
    assert [k.steps for k in card.parts[0].checklist.kids] == [
        [['Audit checkout links', 'in_progress']], [['Check newsletter signup', 'in_progress']],
    ]


def test_late_todo_after_stop_does_not_enrich_or_reopen_card():
    card = ac.open_dispatch_card('stop', ['Repair checkout links'])
    ac.on_child_event('subagent.complete', delegation_id='stop', status='cancelled')
    before = card.render()
    assert not ac.on_child_event('subagent.todo', delegation_id='stop', todos=[
        {'id': 'late', 'content': 'Restart the task', 'status': 'in_progress'}])
    assert card.finished and card.render() == before


def test_empty_early_todos_enrich_only_their_child_and_replay_terminal_fence():
    ac.reserve('early')
    ac.on_child_event('subagent.todo', delegation_id='early', task_index=1, todos=[
        {'id': 'inspect', 'content': 'Read newsletter form', 'status': 'completed'}])
    ac.on_child_event('subagent.complete', delegation_id='early', task_index=1, status='cancelled')
    ac.on_child_event('subagent.todo', delegation_id='early', task_index=1, todos=[
        {'id': 'late', 'content': 'Do not restart', 'status': 'in_progress'}])
    card = ac.open_dispatch_card('early', ['Audit checkout links', 'Check newsletter signup'])
    cl = card.parts[0].checklist
    assert any(row[0] == 'Read newsletter form' for row in cl.kids[1].steps)
    assert not any(row[0] == 'Do not restart' for row in cl.kids[1].steps)
    assert not any(row[0] == 'Read newsletter form' for row in cl.kids[0].steps)


def test_ambiguous_child_todos_cannot_claim_an_unbound_sibling():
    card = ac.open_dispatch_card('owner', ['Audit checkout links', 'Check newsletter signup'])
    before = [k.steps[:] for k in card.parts[0].checklist.kids]
    assert not ac.on_child_event('subagent.todo', delegation_id='owner', subagent_id='unknown', todos=[
        {'id': 'x', 'content': 'Wrong child', 'status': 'completed'}])
    assert [k.steps for k in card.parts[0].checklist.kids] == before


def test_stopped_multi_part_fences_children_even_without_individual_end_events():
    card = ac.open_dispatch_card('stopped-multi', ['Audit checkout links', 'Check newsletter signup'])
    cl = card.parts[0].checklist
    assert cl.finish(ac.STOPPED)
    before = [k.steps[:] for k in cl.kids]
    assert not ac.on_child_event('subagent.todo', delegation_id='stopped-multi', task_index=0, todos=[
        {'id': 'late', 'content': 'Must stay stopped', 'status': 'completed'}])
    assert [k.steps for k in cl.kids] == before


def test_ids_not_positions_match_and_unknown_status_cannot_create_a_row():
    cl = ac.Checklist.start('header', ['Inspect links', 'Repair links'])
    assert cl.apply_todos([
        {'id': 's2', 'content': 'Repair checkout links', 'status': 'in_progress'},
        {'id': 's1', 'content': 'Inspect links', 'status': 'completed'},
        {'id': 'bad', 'content': 'Not real progress', 'status': 'magic'},
    ])
    assert cl.steps == [['Inspect links', 'completed'], ['Repair checkout links', 'in_progress']]
    assert not cl.apply_todos([{'id': 'unknown', 'status': 'completed'}])


def test_matching_labels_can_bind_changed_ids_without_duplicate_rows():
    cl = ac.Checklist.start('header', ['Inspect links'])
    cl.apply_todos([{'id': 'inspect', 'content': 'Inspect links', 'status': 'completed'}])
    assert cl.steps == [['Inspect links', 'completed']]
    cl.apply_todos([{'id': 'inspect', 'content': 'Inspect checkout links', 'status': 'completed'}])
    assert cl.steps == [['Inspect checkout links', 'completed']]


def test_distinct_ids_with_equal_labels_keep_their_own_status_when_reordered():
    cl = ac.Checklist.start('header', [])
    cl.apply_todos([{'id': 'a', 'content': 'Check links', 'status': 'completed'},
                    {'id': 'b', 'content': 'Check links', 'status': 'pending'}])
    assert cl.step_ids == ['a', 'b']
    assert cl.steps == [['Check links', 'completed'], ['Check links', 'pending']]
    cl.apply_todos([{'id': 'b', 'content': 'Check second set of links', 'status': 'in_progress'},
                    {'id': 'a', 'content': 'Check links', 'status': 'completed'}])
    assert cl.step_ids == ['a', 'b']
    assert cl.steps == [['Check links', 'completed'], ['Check second set of links', 'in_progress']]


def test_early_status_only_legacy_event_merges_with_persisted_seed_without_losing_labels():
    from tools import delegation_inflight as inflight

    assert inflight.write_dispatch_record('legacy-early',
        tasks=[{'goal': 'Audit checkout links', 'steps': ['Inspect links', 'Repair links']}],
        session_key='session', parent_session_id='parent',
        origin={'origin_platform': 'telegram', 'origin_chat_id': '100'})
    ac.reserve('legacy-early')
    ac.on_child_event('subagent.todo', delegation_id='legacy-early', task_index=0, todos=[
        {'id': 's2', 'status': 'in_progress'}, {'id': 's1', 'status': 'completed'}])
    assert inflight.read_record('legacy-early')['tasks'][0]['checklist'] == [
        {'id': 's1', 'content': 'Inspect links', 'status': 'completed'},
        {'id': 's2', 'content': 'Repair links', 'status': 'in_progress'}]


def test_rows_and_labels_are_bounded_even_on_empty_card():
    cl = ac.Checklist.start('header', [])
    cl.apply_todos([{'id': str(i), 'content': f'Public row {i} ' + 'word ' * 100,
                     'status': 'pending'} for i in range(1000)])
    assert len(cl.steps) == ac.MAX_STEPS
    assert all(len(text) <= ac.MAX_STEP_CHARS for text, _ in cl.steps)
