import pytest
from shimadzu_uvvis import access_queue as queue, instrument_lock as lease


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv('AI_TUTOR_DATA_ROOT',str(tmp_path))
    monkeypatch.setenv('SHIMADZU_UVVIS_INSTRUMENT_ID','TEST-UVVIS')
    monkeypatch.setenv('SHIMADZU_UVVIS_ENFORCE_INSTRUMENT_LOCK','true')

def creds(record):
    return {k:record[k] for k in ('instrument_job_id','lease_id','fencing_token')} | {'request_id':'request','idempotency_key':'request'}

def test_entire_experiment_keeps_lock_and_hands_off_fifo_only_after_last_uvvis_step():
    a=queue.request_access(student_id='a',session_id='sa',required_step_ids=['scan','standards','unknown'],current_step_id='scan')
    cap=creds(a)
    b=queue.request_access(student_id='b',session_id='sb',required_step_ids=['scan'])
    assert b['queue_position']==1
    c=queue.request_access(student_id='c',session_id='sc',required_step_ids=['scan'])
    assert c['queue_position']==2
    assert queue.request_access(student_id='b',session_id='sb')['queue_position']==1
    for i,step in enumerate(('scan','standards','unknown')):
        queue.request_access(student_id='a',session_id='sa',current_step_id=step,preparing=True)
        batch=f'batch-{i}'
        lease.guard_physical_action(student_id='a',session_id='sa',batch_id=batch,**cap)
        with pytest.raises(RuntimeError):lease.guard_physical_action(student_id='b',session_id='sb',batch_id='foreign',**cap)
        lease.update_lease_state(student_id='a',session_id='sa',batch_id=batch,batch_state='COMPLETED',mode='spectrum',**cap)
        result=lease.mark_results_persisted(student_id='a',session_id='sa',batch_id=batch,completed_step_id=step,**cap)
        if i<2:
            assert result['remaining_step_ids']
            with pytest.raises(RuntimeError,match='unfinished UV-Vis'):
                lease.release_lease(student_id='a',session_id='sa',batch_id=batch,**cap)
            assert queue.request_access(student_id='b',session_id='sb',join=False)['queue_position']==1
    handoff=lease.release_lease(student_id='a',session_id='sa',batch_id='batch-2',**cap)
    assert handoff['released_session_id']=='sa'
    assert handoff['student_id']=='b' and handoff['session_id']=='sb'
    assert handoff['lease_id']!=a['lease_id']
    assert queue.request_access(student_id='b',session_id='sb',join=False)['owned_by_current_session']
    with pytest.raises(RuntimeError):lease.guard_physical_action(student_id='a',session_id='sa',batch_id='late',**cap)
    lease.guard_physical_action(student_id='b',session_id='sb',batch_id='b-first',**creds(handoff))
    assert queue.request_access(student_id='c',session_id='sc',join=False)['queue_position']==1

def test_incomplete_or_unpersisted_batch_cannot_be_rebound_or_released():
    a=queue.request_access(student_id='a',session_id='sa',required_step_ids=['scan'])
    cap=creds(a)
    lease.guard_physical_action(student_id='a',session_id='sa',batch_id='one',**cap)
    with pytest.raises(RuntimeError):lease.guard_physical_action(student_id='a',session_id='sa',batch_id='two',**cap)
    lease.update_lease_state(student_id='a',session_id='sa',batch_id='one',batch_state='COMPLETED',mode='spectrum',**cap)
    with pytest.raises(RuntimeError,match='not persisted'):lease.release_lease(student_id='a',session_id='sa',batch_id='one',**cap)

def test_cancel_and_expiry_only_affect_waiters_or_unused_reservations(monkeypatch):
    now=[1000.]
    monkeypatch.setattr(queue.time,'time',lambda:now[0])
    a=queue.request_access(student_id='a',session_id='sa',required_step_ids=['scan'])
    lease.guard_physical_action(student_id='a',session_id='sa',batch_id='active',**creds(a))
    queue.request_access(student_id='b',session_id='sb')
    queue.request_access(student_id='c',session_id='sc')
    queue.cancel_wait(student_id='b',session_id='sb')
    assert queue.request_access(student_id='c',session_id='sc',join=False)['queue_position']==1
    now[0]+=10000
    queue.request_access(student_id='d',session_id='sd')
    state=queue.request_access(student_id='a',session_id='sa',join=False)
    assert state['owned_by_current_session'] and state['batch_id']=='active'
    assert [q['student_id'] for q in state['queue']]==['d']

def test_polling_does_not_join_queue_or_steal_a_reservation():
    queue.request_access(student_id='a',session_id='sa',required_step_ids=['scan'])
    other=queue.request_access(student_id='b',session_id='sb',join=False)
    assert other['queue_position']==0 and not other['owned_by_current_session']
    assert other['queue']==[]


def test_method_generation_requires_owner_and_cannot_change_an_active_baseline():
    a=queue.request_access(student_id='a',session_id='sa',required_step_ids=['scan','standards'],current_step_id='scan')
    cap=creds(a)
    lease.guard_method_generation(student_id='a',session_id='sa',**cap)
    with pytest.raises(RuntimeError):lease.guard_method_generation(student_id='b',session_id='sb',**cap)
    lease.guard_physical_action(student_id='a',session_id='sa',batch_id='scan',**cap)
    with pytest.raises(RuntimeError,match='Cannot change a method'):
        lease.guard_method_generation(student_id='a',session_id='sa',**cap)
    lease.update_lease_state(student_id='a',session_id='sa',batch_id='scan',batch_state='COMPLETED',mode='spectrum',**cap)
    with pytest.raises(RuntimeError,match='Cannot change a method'):
        lease.guard_method_generation(student_id='a',session_id='sa',**cap)
    lease.mark_results_persisted(student_id='a',session_id='sa',batch_id='scan',completed_step_id='scan',**cap)
    lease.guard_method_generation(student_id='a',session_id='sa',**cap)
