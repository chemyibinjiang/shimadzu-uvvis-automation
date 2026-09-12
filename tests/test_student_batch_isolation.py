import json
import hashlib
from pathlib import Path
import pytest
from shimadzu_uvvis.configuration import load_settings
from shimadzu_uvvis.batch_workflow import SpectrumBatchController, SpectrumBatchError
from shimadzu_uvvis.mcp_server import build_uvvis_sample_batch_plan
import test_batch_workflow as fixtures


def fixture(tmp_path):
    config,_=fixtures.SpectrumBatchControllerTests()._fixture(tmp_path)
    settings=load_settings(config)
    client=fixtures.FakeSpectrumClient(tmp_path/'export')
    runtime=fixtures.FakeRuntimeManager(tmp_path/'control')
    controller=SpectrumBatchController(settings,client_factory=lambda:client,runtime_manager_factory=lambda:runtime)
    return settings,client,runtime,controller


def test_two_students_use_distinct_hashed_sample_paths_without_software_restart(tmp_path):
    settings,client,runtime,controller=fixture(tmp_path)
    paths=[]
    for number in (1,2):
        owner=f'stu_student_{number}';session=f'session_{number}';batch=f'batch_{number}'
        plan=build_uvvis_sample_batch_plan(settings,batch_id=batch,mode='spectrum',
            samples=[{'sample_name':'standard 1','sample_id':'standard_1'}], reference_name='blank',
            start_nm=400,stop_nm=700,step_nm=1,student_id=owner,experiment_name='experiment',session_id=session)
        controller.start(plan,execution_confirmed=True)
        controller.correct_baseline(batch,blank_loaded_confirmed=True)
        result=controller.measure_next(batch,sample_id='001_standard_1',sample_loaded_confirmed=True)
        assert result['state']=='COMPLETED'
        path=Path(plan['samples'][0]['paths']['raw_data_file'])
        assert path.is_relative_to(tmp_path/'data'/hashlib.sha256(owner.encode()).hexdigest()/'experiment'/session/'uvvis')
        assert path.is_file()
        paths.append(path)
    assert paths[0]!=paths[1]
    actual=[Path(args['DataFileName']) for cmd,args in client.commands if cmd==110]
    assert actual==paths
    assert sum(cmd==21 for cmd,_ in client.commands)==2
    with pytest.raises(SpectrumBatchError):
        controller.get_status('batch_1',student_id='stu_student_2',experiment_name='experiment',session_id='session_2')


@pytest.mark.parametrize('corruption',['missing','reference','timestamp'])
def test_corrupt_baseline_blocks_a_sample_before_any_measurement_command(tmp_path,corruption):
    settings,client,runtime,controller=fixture(tmp_path)
    plan=build_uvvis_sample_batch_plan(settings,batch_id='batch',mode='spectrum',
        samples=[{'sample_name':'sample','sample_id':'sample'}],reference_name='blank',start_nm=400,stop_nm=700,step_nm=1)
    controller.start(plan,execution_confirmed=True)
    controller.correct_baseline('batch',blank_loaded_confirmed=True)
    path=Path(plan['batch_directory'])/'batch-manifest.json'
    record=json.loads(path.read_text())
    if corruption=='missing':record['baseline']['status']='PENDING'
    elif corruption=='reference':record['baseline']['record']['reference_name']='different blank'
    else:record['baseline']['record']['completed_at_utc']='old baseline'
    path.write_text(json.dumps(record))
    before=len(client.commands)
    with pytest.raises(SpectrumBatchError):controller.measure_next('batch',sample_id='001_sample',sample_loaded_confirmed=True)
    assert client.commands[before:]==[]


def test_six_fixed_wavelength_samples_each_return_one_instrument_absorbance_after_baseline(tmp_path):
    settings,client,runtime,controller=fixture(tmp_path)
    plan=build_uvvis_sample_batch_plan(settings,batch_id='six',mode='photometric',wavelengths_nm=[417],
        samples=[{'sample_name':f'standard {n}','sample_id':f'standard_{n}'} for n in range(1,7)],
        reference_name='blank',student_id='stu_student',experiment_name='experiment',session_id='session')
    controller.start(plan,execution_confirmed=True)
    with pytest.raises(SpectrumBatchError):controller.measure_next('six',sample_id='001_standard_1',sample_loaded_confirmed=True)
    controller.correct_baseline('six',blank_loaded_confirmed=True)
    after_baseline=len(client.commands)
    for n in range(1,7):
        result=controller.measure_next('six',sample_id=f'{n:03d}_standard_{n}',sample_loaded_confirmed=True)
        sample=result['samples'][n-1]
        assert sample['status']=='COMPLETED'
        assert sample['result']['point_count']==1
        assert sample['result']['points'][0]['wavelength_nm']==417
        assert sample['result']['points'][0]['absorbance']==pytest.approx(0.1417)
    assert result['state']=='COMPLETED'
    assert sum(cmd==311 for cmd,_ in client.commands[after_baseline:])==6
    assert sum(cmd==21 for cmd,_ in client.commands)==1


def test_queued_student_handoff_after_all_three_real_controller_batches(tmp_path,monkeypatch):
    """Use production persistence/locking/controllers; fake only the instrument IO."""
    from shimadzu_uvvis import access_queue, instrument_lock as lease
    monkeypatch.setenv('AI_TUTOR_DATA_ROOT',str(tmp_path))
    monkeypatch.setenv('SHIMADZU_UVVIS_INSTRUMENT_ID','TEST-UVVIS')
    monkeypatch.setenv('SHIMADZU_UVVIS_ENFORCE_INSTRUMENT_LOCK','true')
    settings,client,runtime,controller=fixture(tmp_path)
    steps=['spectrum','standards','unknown']
    a=access_queue.request_access(student_id='stu_A',session_id='sess_A',required_step_ids=steps,current_step_id=steps[0])
    assert access_queue.request_access(student_id='stu_B',session_id='sess_B',required_step_ids=steps)['queue_position']==1
    def credentials(record):
        return {k:record[k] for k in ('lease_id','instrument_job_id','fencing_token')} | {'request_id':'test','idempotency_key':'test'}
    cap=credentials(a)
    files_a={}
    for step,count in [('spectrum',1),('standards',6),('unknown',2)]:
        access_queue.request_access(student_id='stu_A',session_id='sess_A',current_step_id=step,preparing=True)
        batch='batch_'+step
        lease.guard_physical_action(student_id='stu_A',session_id='sess_A',batch_id=batch,**cap)
        plan=build_uvvis_sample_batch_plan(settings,batch_id=batch,mode='spectrum' if step=='spectrum' else 'photometric',
            **({'start_nm':400,'stop_nm':700,'step_nm':1} if step=='spectrum' else {'wavelengths_nm':[417]}),
            samples=[{'sample_id':f'{step}_{n}','sample_name':f'{step} {n}'} for n in range(1,count+1)],
            reference_name='blank',student_id='stu_A',session_id='sess_A',experiment_name='experiment')
        controller.start(plan,execution_confirmed=True)
        controller.correct_baseline(batch,blank_loaded_confirmed=True)
        for n in range(1,count+1):
            with pytest.raises(RuntimeError):
                lease.guard_physical_action(student_id='stu_B',session_id='sess_B',batch_id='B',**cap)
            result=controller.measure_next(batch,sample_id=f'{n:03d}_{step}_{n}',sample_loaded_confirmed=True)
            if step!='spectrum':assert result['samples'][n-1]['result']['point_count']==1
        assert result['state']=='COMPLETED'
        lease.update_lease_state(student_id='stu_A',session_id='sess_A',batch_id=batch,batch_state='COMPLETED',mode=plan['mode'],**cap)
        with pytest.raises(RuntimeError,match='not persisted'):
            lease.release_lease(student_id='stu_A',session_id='sess_A',batch_id=batch,**cap)
        lease.mark_results_persisted(student_id='stu_A',session_id='sess_A',batch_id=batch,completed_step_id=step,**cap)
        if step!='unknown':
            with pytest.raises(RuntimeError,match='unfinished UV-Vis'):
                lease.release_lease(student_id='stu_A',session_id='sess_A',batch_id=batch,**cap)
        for sample in plan['samples']:
            path=Path(sample['paths']['raw_data_file'])
            files_a[path]=hashlib.sha256(path.read_bytes()).hexdigest()
        # Recreate controllers between steps, as independent MCP processes do.
        controller=SpectrumBatchController(settings,client_factory=lambda:client,runtime_manager_factory=lambda:runtime)
    b=lease.release_lease(student_id='stu_A',session_id='sess_A',batch_id='batch_unknown',**cap)
    assert b['session_id']=='sess_B' and b['lease_id']!=a['lease_id']
    with pytest.raises(RuntimeError):
        lease.release_lease(student_id='stu_A',session_id='sess_A',batch_id='batch_unknown',**cap)
    lease.guard_physical_action(student_id='stu_B',session_id='sess_B',batch_id='batch_B',**credentials(b))
    plan=build_uvvis_sample_batch_plan(settings,batch_id='batch_B',mode='photometric',wavelengths_nm=[417],
        samples=[{'sample_id':'sample_1','sample_name':'B sample'}],reference_name='blank_B',
        student_id='stu_B',session_id='sess_B',experiment_name='experiment')
    controller.start(plan,execution_confirmed=True)
    with pytest.raises(SpectrumBatchError):controller.measure_next('batch_B',sample_id='001_sample_1',sample_loaded_confirmed=True)
    controller.correct_baseline('batch_B',blank_loaded_confirmed=True)
    result=controller.measure_next('batch_B',sample_id='001_sample_1',sample_loaded_confirmed=True)
    assert result['state']=='COMPLETED'
    path_b=Path(plan['samples'][0]['paths']['raw_data_file'])
    assert path_b.is_relative_to(tmp_path/'data'/hashlib.sha256(b'stu_B').hexdigest()/'experiment/sess_B/uvvis')
    assert path_b not in files_a
    assert all(hashlib.sha256(path.read_bytes()).hexdigest()==digest for path,digest in files_a.items())
    assert sum(cmd==21 for cmd,_ in client.commands)==4
