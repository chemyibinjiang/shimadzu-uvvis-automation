"""Typed configuration for a Shimadzu UV-Vis control computer."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ControlSettings:
    config_path: Path | None
    command_dir: Path
    mode: str
    timeout_seconds: float
    poll_interval_seconds: float
    lock_timeout_seconds: float
    encoding: str
    export_dir: Path | None
    export_pattern: str
    export_timeout_seconds: float
    stable_seconds: float
    method_file: Path | None
    data_dir: Path | None
    measurement_mode: int
    connect_before_run: bool
    disconnect_after_run: bool
    correction: str
    discharge_after_measurement: bool
    allow_unicode_identifiers: bool
    audit_dir: Path | None


def _section(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"TOML section [{name}] must be a table")
    return value


def _float(section: Mapping[str, Any], key: str, default: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    return float(value)


def _int(section: Mapping[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _bool(section: Mapping[str, Any], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _text(section: Mapping[str, Any], key: str, default: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _path(
    section: Mapping[str, Any], key: str, base_dir: Path, default: str | None = None
) -> Path | None:
    value = section.get(key, default)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def load_settings(path: str | Path | None = None) -> ControlSettings:
    config_path = Path(path).resolve() if path is not None else None
    config: dict[str, Any] = {}
    if config_path is not None:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    base_dir = config_path.parent if config_path is not None else Path.cwd()

    lab = _section(config, "labsolutions")
    export = _section(config, "export")
    spectrum = _section(config, "spectrum")
    audit = _section(config, "audit")

    mode = _text(lab, "mode", "spectrum")
    if mode not in {"spectrum", "quantitation", "photometric", "time_course"}:
        raise ValueError(f"Unsupported LabSolutions mode: {mode!r}")
    measurement_mode = _int(spectrum, "measurement_mode", 2)
    if measurement_mode not in (1, 2):
        raise ValueError("measurement_mode must be 1 or 2")
    correction = _text(spectrum, "correction", "none")
    if correction not in {"none", "auto"}:
        raise ValueError("configured correction must be 'none' or 'auto'")

    settings = ControlSettings(
        config_path=config_path,
        command_dir=_path(
            lab, "command_dir", base_dir, r"C:\UVVisControl"
        )
        or Path(r"C:\UVVisControl"),
        mode=mode,
        timeout_seconds=_float(lab, "timeout_seconds", 600.0),
        poll_interval_seconds=_float(lab, "poll_interval_seconds", 0.2),
        lock_timeout_seconds=_float(lab, "lock_timeout_seconds", 5.0),
        encoding=_text(lab, "encoding", "utf-8"),
        export_dir=_path(export, "directory", base_dir),
        export_pattern=_text(export, "pattern", "{sample_id}*.csv"),
        export_timeout_seconds=_float(export, "timeout_seconds", 120.0),
        stable_seconds=_float(export, "stable_seconds", 2.0),
        method_file=_path(spectrum, "method_file", base_dir),
        data_dir=_path(spectrum, "data_dir", base_dir),
        measurement_mode=measurement_mode,
        connect_before_run=_bool(spectrum, "connect_before_run", False),
        disconnect_after_run=_bool(spectrum, "disconnect_after_run", False),
        correction=correction,
        discharge_after_measurement=_bool(
            spectrum, "discharge_after_measurement", False
        ),
        allow_unicode_identifiers=_bool(
            spectrum, "allow_unicode_identifiers", False
        ),
        audit_dir=_path(audit, "directory", base_dir),
    )
    positive_values = {
        "timeout_seconds": settings.timeout_seconds,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "lock_timeout_seconds": settings.lock_timeout_seconds,
        "export timeout_seconds": settings.export_timeout_seconds,
    }
    for name, value in positive_values.items():
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero")
    if settings.stable_seconds < 0:
        raise ValueError("stable_seconds cannot be negative")
    if not settings.export_pattern:
        raise ValueError("export pattern cannot be empty")
    if settings.encoding.lower().replace("_", "-") != "utf-8":
        raise ValueError("LabSolutions command and feedback encoding must be UTF-8")
    return settings
