from __future__ import annotations

from pathlib import Path

import pytest

from shimadzu_uvvis.instrument_lock import (
    guard_physical_action,
    mark_results_persisted,
    release_lease,
    update_lease_state,
)


def _credentials() -> dict[str, str]:
    return {
        "instrument_job_id": "job-1",
        "lease_id": "lease-1",
        "fencing_token": "fence-1",
        "request_id": "request-1",
        "idempotency_key": "idem-1",
    }


def test_shimadzu_is_the_only_persistent_lease_writer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_TUTOR_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SHIMADZU_UVVIS_INSTRUMENT_ID", "PC1-SHIMADZU-UVVIS-01")
    monkeypatch.setenv("SHIMADZU_UVVIS_ENFORCE_INSTRUMENT_LOCK", "true")
    credentials = _credentials()
    guard_physical_action(
        student_id="stu-1",
        session_id="sess-1",
        batch_id="batch-1",
        **credentials,
    )
    with pytest.raises(RuntimeError, match="使用"):
        guard_physical_action(
            student_id="stu-2",
            session_id="sess-2",
            batch_id="batch-2",
            **credentials,
        )
    update_lease_state(
        student_id="stu-1",
        session_id="sess-1",
        batch_id="batch-1",
        batch_state="COMPLETED",
        mode="spectrum",
        **credentials,
    )
    mark_results_persisted(
        student_id="stu-1",
        session_id="sess-1",
        batch_id="batch-1",
        **credentials,
    )
    released = release_lease(
        student_id="stu-1",
        session_id="sess-1",
        batch_id="batch-1",
        **credentials,
    )
    assert released["state"] == "FREE"
