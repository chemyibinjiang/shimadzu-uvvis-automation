"""Typed configuration for a Shimadzu UV-Vis control computer."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ScanProfile:
    name: str
    method_file: Path
    start_nm: float
    stop_nm: float
    step_nm: float


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
    scan_profiles: Mapping[str, ScanProfile]


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


def _scan_profiles(
    config: Mapping[str, Any], base_dir: Path
) -> Mapping[str, ScanProfile]:
    section = _section(config, "scan_profiles")
    profiles: dict[str, ScanProfile] = {}
    for name, raw_profile in section.items():
        if not isinstance(raw_profile, Mapping):
            raise ValueError(f"scan profile {name!r} must be a TOML table")
        method_file = _path(raw_profile, "method_file", base_dir)
        if method_file is None:
            raise ValueError(f"scan profile {name!r} requires method_file")
        start_nm = _float(raw_profile, "start_nm", float("nan"))
        stop_nm = _float(raw_profile, "stop_nm", float("nan"))
        step_nm = _float(raw_profile, "step_nm", float("nan"))
        values = (start_nm, stop_nm, step_nm)
        if any(not math.isfinite(value) for value in values):
            raise ValueError(
                f"scan profile {name!r} requires start_nm, stop_nm, and step_nm"
            )
        if start_nm == stop_nm:
            raise ValueError(f"scan profile {name!r} start_nm and stop_nm must differ")
        if start_nm <= 0 or stop_nm <= 0:
            raise ValueError(f"scan profile {name!r} wavelengths must be positive")
        if step_nm <= 0:
            raise ValueError(f"scan profile {name!r} step_nm must be greater than zero")
        interval_count = abs(stop_nm - start_nm) / step_nm
        if abs(interval_count - round(interval_count)) > 1e-6:
            raise ValueError(
                f"scan profile {name!r} range must be evenly divisible by step_nm"
            )
        profiles[name] = ScanProfile(
            name=name,
            method_file=method_file,
            start_nm=start_nm,
            stop_nm=stop_nm,
            step_nm=step_nm,
        )
    return MappingProxyType(profiles)


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
        scan_profiles=_scan_profiles(config, base_dir),
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
