from __future__ import annotations

from pathlib import Path

import pytest

from shimadzu_uvvis.instrument_lock import (
    guard_physical_action,
    mark_results_persisted,
    release_lease,
    rollback_failed_preparation,
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


def test_failed_preparation_can_be_unbound_without_releasing_reservation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AI_TUTOR_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SHIMADZU_UVVIS_INSTRUMENT_ID", "PC1-SHIMADZU-UVVIS-01")
    monkeypatch.setenv("SHIMADZU_UVVIS_ENFORCE_INSTRUMENT_LOCK", "true")
    credentials = _credentials()
    guard_physical_action(
        student_id="stu-1",
        session_id="sess-1",
        batch_id="batch-failed",
        **credentials,
    )

    rolled_back = rollback_failed_preparation(
        student_id="stu-1",
        session_id="sess-1",
        batch_id="batch-failed",
        **credentials,
    )

    assert rolled_back["state"] == "HELD"
    assert rolled_back["phase"] == "RESERVED"
    assert rolled_back["batch_id"] == ""
    assert rolled_back["last_failed_preparation"]["batch_id"] == "batch-failed"
    guard_physical_action(
        student_id="stu-1",
        session_id="sess-1",
        batch_id="batch-retry",
        **credentials,
    )
