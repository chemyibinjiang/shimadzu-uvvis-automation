import json
from pathlib import Path
from unittest.mock import patch

import pytest
from test_student_batch_isolation import fixture
from shimadzu_uvvis.batch_workflow import SpectrumBatchError
from shimadzu_uvvis.mcp_server import build_uvvis_sample_batch_plan
from shimadzu_uvvis.results import AbsorbancePoint, _write_csv


@pytest.mark.parametrize('fault', [None, 'owner', 'command', 'raw'])
def test_saved_result_recovery_never_sends_instrument_commands(tmp_path, fault):
    settings, client, runtime, controller = fixture(tmp_path)
    plan = build_uvvis_sample_batch_plan(settings, batch_id='recover', mode='photometric', wavelengths_nm=[417],
        samples=[{'sample_name':'sample', 'sample_id':'sample'}], reference_name='blank',
        student_id='student', experiment_name='experiment', session_id='session')
    controller.start(plan, execution_confirmed=True)
    controller.correct_baseline('recover', blank_loaded_confirmed=True)
    with patch('shimadzu_uvvis.batch_workflow.normalize_photometric_data_file', side_effect=FileNotFoundError('long temp')):
        with pytest.raises(FileNotFoundError):
            controller.measure_next('recover', sample_id='001_sample', sample_loaded_confirmed=True)
    before = list(client.commands)
    manifest_path = Path(plan['batch_directory']) / 'batch-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if fault == 'command':
        manifest['commands'] = [c for c in manifest['commands'] if c['command'] != 311]
        manifest_path.write_text(json.dumps(manifest))
    if fault == 'raw':
        Path(plan['samples'][0]['paths']['raw_data_file']).unlink()
    def normalize(**kwargs):
        _write_csv(kwargs['csv_file'], [AbsorbancePoint(417, 0.1417)])
    with patch('shimadzu_uvvis.results.parse_photometric_data_file', return_value=[AbsorbancePoint(417, 0.1417)]), patch('shimadzu_uvvis.batch_workflow.normalize_photometric_data_file', side_effect=normalize):
        kwargs = dict(student_id='wrong' if fault == 'owner' else 'student', experiment_name='experiment', session_id='session')
        if fault:
            with pytest.raises((SpectrumBatchError, FileNotFoundError)):
                controller.recover_photometric_saved_result('recover', **kwargs)
        else:
            result = controller.recover_photometric_saved_result('recover', **kwargs)
            assert result['state'] == 'COMPLETED'
            assert Path(plan['samples'][0]['paths']['merged_csv_file']).is_file()
            with pytest.raises(SpectrumBatchError):
                controller.recover_photometric_saved_result('recover', **kwargs)
    assert client.commands == before
