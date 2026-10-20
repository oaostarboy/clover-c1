"""Exact-row current-turn sidecar writes preserve cache replay identity."""
from clover_state import SessionDB


def test_current_sidecar_cannot_stamp_a_newer_identical_user_row(tmp_path):
    db = SessionDB(db_path=tmp_path/'state.db')
    try:
        db.create_session('s', source='telegram')
        first = {'role':'user','content':'same human text'}
        second = {'role':'user','content':'same human text'}
        db.append_messages_batch('s', [first, {'role':'assistant','content':'ack'}, second])
        assert db.set_latest_user_api_content('s', first['content'], 'private current context',
                                              expected_row_id=first['_row_id']) == 0
        assert not db.get_messages('s')[-1].get('api_content')
        assert db.set_latest_user_api_content('s', second['content'], 'current exact bytes',
                                              expected_row_id=second['_row_id']) == 1
        assert db.get_messages('s')[-1]['api_content'] == 'current exact bytes'
    finally:
        db.close()
