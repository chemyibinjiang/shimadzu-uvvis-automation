"""Stable storage paths for student-owned UV-Vis batches."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path


_INVALID_WINDOWS_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class StoragePathError(ValueError):
    """Raised when a storage context cannot produce a safe directory path."""


def safe_storage_component(
    value: object,
    name: str,
    *,
    strip_student_prefix: bool = False,
) -> str:
    text = unicodedata.normalize("NFKC", value.strip() if isinstance(value, str) else "")
    if strip_student_prefix and text.lower().startswith("stu_") and len(text) > 4:
        text = text[4:]
    text = _INVALID_WINDOWS_PATH_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        raise StoragePathError(f"{name} must produce a non-empty storage directory name")
    if text.upper() in _WINDOWS_RESERVED_NAMES:
        text = f"_{text}"
    text = text[:120].rstrip(" .")
    if not text:
        raise StoragePathError(f"{name} must produce a non-empty storage directory name")
    return text


def student_batch_directory(
    data_dir: Path,
    batch_id: str,
    *,
    student_id: str | None = None,
    experiment_name: str | None = None,
) -> Path:
    has_student = bool(isinstance(student_id, str) and student_id.strip())
    has_experiment = bool(isinstance(experiment_name, str) and experiment_name.strip())
    if has_student != has_experiment:
        raise StoragePathError(
            "student_id and experiment_name must be provided together"
        )
    if not has_student:
        return data_dir / batch_id
    student_account = safe_storage_component(
        student_id,
        "student_id",
        strip_student_prefix=True,
    )
    experiment_directory = safe_storage_component(
        experiment_name,
        "experiment_name",
    )
    return data_dir / student_account / "uvvis" / experiment_directory / batch_id


def existing_batch_directories(data_dir: Path, batch_id: str) -> list[Path]:
    candidates = [data_dir / batch_id]
    if data_dir.is_dir():
        candidates.extend(data_dir.glob(f"*/uvvis/*/{batch_id}"))
    unique: dict[str, Path] = {}
    for candidate in candidates:
        if candidate.is_dir():
            unique[str(candidate.resolve())] = candidate
    return sorted(unique.values(), key=lambda path: str(path).casefold())
