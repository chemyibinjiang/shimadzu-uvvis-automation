"""Shimadzu-owned FIFO access queue and experiment-scoped reservations."""
from __future__ import annotations
import hashlib,os,time,uuid
from typing import Any
from . import instrument_lock as lease

WAIT_TTL_SECONDS=300
MAX_QUEUE=128

def _same(item,student,session):
    return item.get('student_id')==student and item.get('session_id')==session

def _hold(record,item,now):
    token=uuid.uuid4().hex
    record.update(schema_version=2,instrument_id=lease._lock_path().stem,
        node_id=os.getenv('AI_TUTOR_NODE_ID',''),state='HELD',student_id=item['student_id'],
        session_id=item['session_id'],device_id=item.get('device_id',''),owner_label=item.get('owner_label') or item['student_id'],
        batch_id='',batch_state='',mode='',results_persisted=False,phase='RESERVED',
        acquired_at=now,last_seen_at=now,lease_id=token,instrument_job_id=uuid.uuid4().hex,
        fencing_token=hashlib.sha256(token.encode()).hexdigest(),
        required_step_ids=list(item.get('required_step_ids') or []),
        completed_step_ids=list(item.get('completed_step_ids') or []),current_step_id=item.get('current_step_id',''),
        version=int(record.get('version') or 0)+1)

def promote(record,now=None):
    now=time.time() if now is None else now
    queue=[q for q in record.get('queue',[]) if now-float(q.get('last_seen_at') or 0)<=WAIT_TTL_SECONDS]
    record['queue']=queue
    if record.get('state','FREE')=='FREE' and queue:
        item=queue.pop(0);_hold(record,item,now)
    return record

def _view(record,student,session):
    result=dict(record)
    own=_same(record,student,session) and record.get('state')!='FREE'
    position=next((i+1 for i,q in enumerate(record.get('queue',[])) if _same(q,student,session)),0)
    result.update(owned_by_current_session=own,queue_position=position,queue_length=len(record.get('queue',[])),
        remaining_step_ids=[s for s in record.get('required_step_ids',[]) if s not in record.get('completed_step_ids',[])])
    return result

def request_access(*,student_id:str,session_id:str,device_id:str='',owner_label:str='',
    required_step_ids:list[str]|None=None,completed_step_ids:list[str]|None=None,current_step_id:str='',
    join:bool=True,preparing:bool=False)->dict[str,Any]:
    student=student_id.strip();session=session_id.strip()
    if not student or not session:raise RuntimeError('Student and session are required')
    required=list(dict.fromkeys(s.strip() for s in required_step_ids or [] if s.strip()))
    completed=[s for s in completed_step_ids or [] if s in required]
    if len(required)>128:raise RuntimeError('Experiment UV-Vis scope is too large')
    now=time.time();path=lease._lock_path();path.parent.mkdir(parents=True,exist_ok=True)
    with lease._file_mutex(path):
        record=lease._read(path) or {'state':'FREE','queue':[]}
        if record.get('state')=='HELD' and not record.get('batch_id') and record.get('phase')=='RESERVED' and now-float(record.get('last_seen_at') or 0)>WAIT_TTL_SECONDS:
            record.update(state='FREE',student_id='',session_id='',device_id='')
        promote(record,now)
        if _same(record,student,session) and record.get('state')!='FREE':
            record['last_seen_at']=now
            record['required_step_ids']=list(dict.fromkeys([*record.get('required_step_ids',[]),*required]))
            record['completed_step_ids']=list(dict.fromkeys([*record.get('completed_step_ids',[]),*completed]))
            previous=record.get('current_step_id','')
            if current_step_id and current_step_id!=previous:
                if record.get('batch_id') and not (record.get('batch_state') == 'COMPLETED' and record.get('results_persisted')):
                    raise RuntimeError('Previous UV-Vis batch is not complete and persisted')
                record['current_step_id']=current_step_id
        else:
            item=next((q for q in record.get('queue',[]) if _same(q,student,session)),None)
            if item is not None:item['last_seen_at']=now
            elif join:
                item={'student_id':student,'session_id':session,'device_id':device_id,'owner_label':owner_label,
                    'required_step_ids':required,'completed_step_ids':completed,'current_step_id':current_step_id,
                    'requested_at':now,'last_seen_at':now}
                if record.get('state','FREE')=='FREE' and not record.get('queue'):
                    _hold(record,item,now)
                else:
                    queue=record.setdefault('queue',[])
                    if len(queue)>=MAX_QUEUE:raise RuntimeError('UV-Vis waiting queue is full')
                    queue.append(item)
        lease._write(path,record)
        return _view(record,student,session)

def cancel_wait(*,student_id:str,session_id:str)->dict[str,Any]:
    path=lease._lock_path();path.parent.mkdir(parents=True,exist_ok=True)
    with lease._file_mutex(path):
        record=lease._read(path) or {'state':'FREE','queue':[]}
        record['queue']=[q for q in record.get('queue',[]) if not _same(q,student_id,session_id)]
        if _same(record,student_id,session_id) and not record.get('batch_id') and record.get('phase')=='RESERVED':
            record.update(state='FREE',student_id='',session_id='',device_id='')
            promote(record)
        lease._write(path,record)
        return _view(record,student_id,session_id)
