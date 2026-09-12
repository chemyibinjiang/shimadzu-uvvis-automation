from pathlib import Path
from shimadzu_uvvis.storage_paths import student_storage_key

from shimadzu_uvvis.storage_paths import student_batch_directory, student_uvvis_directory


def test_student_uvvis_directory_matches_gateway_student_hash(tmp_path: Path) -> None:
    uvvis = student_uvvis_directory(
        tmp_path / "data",
        student_id="stu_123456",
        experiment_name="碘酸铜溶度积的测定",
        session_id="sess_000412",
    )

    assert uvvis == (
        tmp_path
        / "data"
        / student_storage_key("stu_123456")
        / "碘酸铜溶度积的测定"
        / "sess_000412"
        / "uvvis"
    )


def test_student_batch_directory_uses_the_same_uvvis_owner_directory(tmp_path: Path) -> None:
    batch = student_batch_directory(
        tmp_path / "data",
        "batch_001",
        student_id="stu_123456",
        experiment_name="experiment",
        session_id="sess_001",
    )

    assert batch == (
        tmp_path / "data" / student_storage_key("stu_123456") / "experiment" / "sess_001" / "uvvis" / ".batches" / "batch_001"
    )
