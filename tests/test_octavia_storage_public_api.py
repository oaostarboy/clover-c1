"""Real filesystem and public Stop exercises, on synthetic temporary state."""
import json
import logging
from pathlib import Path
import pytest
from agent.storage_recovery import _write_checkpoint
from tests.run_agent.test_storage_progress_recovery import _make_agent,_attach_real_session_db,_history,_hold_reader,ReleaseAfterFailure

@pytest.mark.parametrize('clear',[False,True])
def test_real_public_stop_prevents_persistence_retry_and_preserves_pending(tmp_path,clear):
    agent=_make_agent()
    db=_attach_real_session_db(agent,tmp_path/'state.db','worker')
    agent._current_turn_id='accepted-turn'
    agent._delegate_depth=1
    history=_history()
    assert agent._flush_messages_to_session_db(history[:2]) is True
    old_epoch=getattr(agent,'_orchestration_stop_epoch',0)
    before=[tuple(row) for row in db._conn.execute('SELECT * FROM messages ORDER BY id')]
    reader=_hold_reader(db)
    def stop():
        agent.interrupt(hard_cancel=True)
        if clear: agent.clear_interrupt()
    release=ReleaseAfterFailure(reader,stop)
    log=logging.getLogger('run_agent');log.addHandler(release)
    try:
        assert agent._flush_messages_to_session_db(history) is False
        assert release.seen
        assert agent._orchestration_stop_epoch>old_epoch
        assert [tuple(row) for row in db._conn.execute('SELECT * FROM messages ORDER BY id')]==before
        pending=json.loads(Path(agent._storage_recovery_checkpoint).read_text())
        assert pending['disposition']=='pending'
        assert pending['messages'][-1]['content']==history[-1]['content']
        assert pending['automatic_replay_allowed'] is False
    finally:
        log.removeHandler(release);reader.close();db.close()

@pytest.mark.parametrize('mode',['symlink-dir','public-dir'])
def test_unsafe_checkpoint_directory_is_refused(tmp_path,mode):
    directory=tmp_path/'storage-recovery'
    outside=tmp_path/'outside';outside.mkdir(mode=0o700)
    if mode=='symlink-dir':directory.symlink_to(outside,target_is_directory=True)
    else:directory.mkdir(mode=0o755);directory.chmod(0o755)
    with pytest.raises(OSError):_write_checkpoint(directory/'pending.json',{'private':'synthetic'})
    assert list(outside.iterdir())==[]

@pytest.mark.parametrize('link',['symlink','hardlink'])
def test_existing_target_link_is_replaced_without_writing_outside(tmp_path,link):
    directory=tmp_path/'storage-recovery';directory.mkdir(mode=0o700)
    outside=tmp_path/'outside';outside.write_text('retained')
    target=directory/'pending.json'
    if link=='symlink':target.symlink_to(outside)
    else:target.hardlink_to(outside)
    _write_checkpoint(target,{'private':'synthetic'})
    assert outside.read_text()=='retained'
    assert not target.is_symlink()
    assert json.loads(target.read_text())=={'private':'synthetic'}
    assert target.stat().st_nlink==1
