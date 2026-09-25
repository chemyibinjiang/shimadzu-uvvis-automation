"""Bound instrument paths before acquisition; support long paths in our writers."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Mapping


def local_io_path(path: Path) -> Path:
    """Extended paths are for our I/O only, never for LabSolutions commands."""
    if os.name != "nt":
        return path
    value = os.path.abspath(path)
    if len(value.encode("utf-16-le")) // 2 < 248:
        return path
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def atomic_temporary(path: Path) -> Path:
    return local_io_path(path).with_name(".t" + uuid.uuid4().hex[:15])


def windows_path_length(path: Path) -> int:
    return len(str(path.absolute()).encode("utf-16-le")) // 2


def validate_batch_paths(plan: Mapping[str, Any]) -> None:
    """Pure lexical, O(samples) check. Leave 19 characters below MAX_PATH."""
    paths = [Path(str(plan["batch_directory"])) / "preparation" / "baseline_preparation.vphd"]
    for sample in plan["samples"]:
        for key, value in sample["paths"].items():
            if key.endswith("_file"):
                paths.append(Path(str(value)))
        for segment in sample["segments"]:
            paths.append(Path(str(segment["raw_data_file"])))
            paths.append(Path(sample["paths"]["export_directory"]) / (segment["sample_id"] + ".csv"))
    for path in paths:
        for candidate in (path, path.with_name(".t" + "0" * 15)):
            if windows_path_length(candidate) > 240:
                raise ValueError(f"UV-Vis path exceeds safe Windows budget (240): {path}; shorten the data root or identifiers before starting; no measurement was sent")
