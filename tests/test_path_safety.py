import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from shimadzu_uvvis.audit import write_json_atomic
from shimadzu_uvvis.path_safety import atomic_temporary, local_io_path, validate_batch_paths
from shimadzu_uvvis.results import AbsorbancePoint, _write_csv, _write_time_course_csv, TimeCoursePoint


def test_short_unique_temporary_names(tmp_path):
    target = tmp_path / ('very_long_result_name_' * 5 + '.csv')
    names = {atomic_temporary(target).name for _ in range(1000)}
    assert len(names) == 1000
    assert all(len(name) == 17 for name in names)


@pytest.mark.parametrize('length', [222, 260, 290])
def test_atomic_writers_handle_deep_existing_paths(tmp_path, length):
    parent = tmp_path / ('d' * (length - len(str(tmp_path)) - 1 - 60)) / ('e' * 50)
    path = parent / 'test.csv'
    local_io_path(parent).mkdir(parents=True)
    _write_csv(path, [AbsorbancePoint(777, -0.006)])
    assert '-0.006' in local_io_path(path).read_text()
    write_json_atomic(parent / 'test.json', {'value': '0.0060'})
    assert json.loads(local_io_path(parent / 'test.json').read_text())['value'] == '0.0060'
    assert not list(local_io_path(parent).glob('.t*'))


def test_path_budget_checked_without_disk_or_instrument(tmp_path):
    plan = {'batch_directory': str(tmp_path / 'batch'), 'samples': [{
        'paths': {'raw_data_file': str(tmp_path / ('x' * 220) / 's.vphd'), 'export_directory': str(tmp_path)},
        'segments': [{'raw_data_file': str(tmp_path / 's.vphd'), 'sample_id': 's'}]}]}
    with patch.object(Path, 'exists', side_effect=AssertionError('no filesystem scan')):
        with pytest.raises(ValueError, match='no measurement was sent'):
            validate_batch_paths(plan)


def test_preflight_is_lightweight(tmp_path):
    plan = {'batch_directory': str(tmp_path / 'batch'), 'samples': []}
    started = time.perf_counter()
    for _ in range(1000):
        validate_batch_paths(plan)
    assert time.perf_counter() - started < 2
