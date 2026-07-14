"""Command-line interface for Shimadzu LabSolutions UV-Vis automation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .audit import write_json_atomic
from .client import (
    MODE_FILE_NAMES,
    Feedback,
    LabSolutionsClient,
    LabSolutionsError,
    ParameterValue,
    SpectrumSeriesSample,
)
from .configuration import ControlSettings, ScanProfile, load_settings
from .diagnostics import run_diagnostics
from .profiles import ScanProfileRegistry, SpectrumScanRequest


@dataclass(frozen=True, slots=True)
class ResolvedSpectrumRun:
    sample_name: str
    sample_id: str
    method_file: Path
    data_file: Path
    measurement_mode: int
    discharge: bool
    correction: Mapping[str, ParameterValue] | None
    connect: bool
    disconnect: bool
    export_dir: Path | None
    export_pattern: str
    export_timeout: float
    stable_seconds: float
    scan_profile: ScanProfile | None
    requested_wavelengths: tuple[float, ...]
    commands: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedSpectrumSeries:
    series_id: str
    interval_seconds: float
    overrun_tolerance_seconds: float
    runs: tuple[ResolvedSpectrumRun, ...]
    warnings: tuple[str, ...]


def _add_spectrum_options(
    command: argparse.ArgumentParser, *, single_run: bool
) -> None:
    command.add_argument("--method", type=Path)
    command.add_argument(
        "--profile", help="Registered LabSolutions Spectrum method profile"
    )
    command.add_argument(
        "--start", type=float, help="Old-control-compatible start wavelength in nm"
    )
    command.add_argument(
        "--stop", type=float, help="Old-control-compatible stop wavelength in nm"
    )
    command.add_argument(
        "--step", type=float, help="Old-control-compatible data interval in nm"
    )
    command.add_argument(
        "--wavelengths",
        type=float,
        nargs="+",
        help="Target points to validate against the selected Spectrum profile",
    )
    command.add_argument("--sample-name", required=True)
    if single_run:
        command.add_argument("--sample-id")
        command.add_argument("--data-file", type=Path)
    else:
        command.set_defaults(sample_id=None, data_file=None)
    command.add_argument("--measurement-mode", type=int, choices=(1, 2))
    command.add_argument(
        "--connect", action=argparse.BooleanOptionalAction, default=None
    )
    command.add_argument(
        "--disconnect", action=argparse.BooleanOptionalAction, default=None
    )
    command.add_argument(
        "--discharge", action=argparse.BooleanOptionalAction, default=None
    )
    command.add_argument(
        "--correction", choices=("none", "auto", "baseline", "zero")
    )
    command.add_argument("--start-wl", type=float)
    command.add_argument("--end-wl", type=float)
    command.add_argument("--wavelength", type=float)
    command.add_argument("--export-dir", type=Path)
    command.add_argument("--export-pattern")
    command.add_argument("--export-timeout", type=float)
    command.add_argument("--stable-seconds", type=float)
    command.add_argument(
        "--no-wait-export",
        action="store_true",
        help="Do not wait for the LabSolutions automatic export",
    )
    command.add_argument(
        "--allow-unicode-identifiers",
        action="store_true",
        help="Allow non-ASCII sample names after instrument-PC validation",
    )
    command.add_argument(
        "--execute",
        action="store_true",
        help="Actually measure; otherwise only print the exact command plan",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shimadzu-uvvis",
        description="Control Shimadzu LabSolutions UV-Vis through text exchange.",
    )
    parser.add_argument("--config", type=Path, help="TOML configuration file")
    parser.add_argument("--command-dir", type=Path, help="LabSolutions command folder")
    parser.add_argument("--mode", choices=sorted(MODE_FILE_NAMES))
    parser.add_argument("--timeout", type=float, help="Command timeout in seconds")
    parser.add_argument(
        "--poll-interval", type=float, help="File polling interval in seconds"
    )
    parser.add_argument(
        "--lock-timeout", type=float, help="Cross-process lock timeout in seconds"
    )
    parser.add_argument("--audit-dir", type=Path, help="Command audit directory")

    subparsers = parser.add_subparsers(dest="action", required=True)
    doctor = subparsers.add_parser(
        "doctor", help="Check control-PC paths and safety settings without measuring"
    )
    doctor.add_argument(
        "--write-check",
        action="store_true",
        help="Write harmless probe files to configured directories",
    )

    recover = subparsers.add_parser(
        "recover", help="Inspect or acknowledge an ambiguous prior command"
    )
    recover.add_argument(
        "--clear",
        action="store_true",
        help="Clear the recovery marker after inspecting instrument state",
    )
    recover.add_argument(
        "--force",
        action="store_true",
        help="Clear even without matching feedback or while a command file exists",
    )
    recover.add_argument(
        "--execute",
        action="store_true",
        help="Actually clear; otherwise only show the recovery plan",
    )

    subparsers.add_parser("ping", help="Send Command=0 and check automatic control")

    send = subparsers.add_parser("send", help="Plan or send one manual command")
    send.add_argument("command", type=int)
    send.add_argument("parameters", nargs="*", metavar="KEY=VALUE")
    send.add_argument(
        "--allow-error",
        action="store_true",
        help="Return non-zero LabSolutions feedback instead of raising",
    )
    send.add_argument(
        "--execute",
        action="store_true",
        help="Actually write the command; otherwise only print a plan",
    )

    spectrum = subparsers.add_parser(
        "spectrum", help="Plan or execute a complete Spectrum measurement"
    )
    _add_spectrum_options(spectrum, single_run=True)

    series = subparsers.add_parser(
        "series", help="Plan or execute start-to-start repeated full spectra"
    )
    _add_spectrum_options(series, single_run=False)
    series.add_argument(
        "--series-id",
        help="ASCII base ID; each acquisition receives a numbered suffix",
    )
    series.add_argument(
        "--count", type=int, required=True, help="Number of full spectra (minimum 2)"
    )
    series.add_argument(
        "--interval-seconds",
        type=float,
        required=True,
        help="Target seconds between consecutive Command=111 start times",
    )
    series.add_argument(
        "--overrun-tolerance-seconds",
        type=float,
        default=1.0,
        help="Allowed start lateness before the next measurement is stopped",
    )
    return parser


def _print_json(payload: Mapping[str, Any], *, stream: Any = None) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, indent=2),
        file=stream if stream is not None else sys.stdout,
    )


def _parse_parameters(items: list[str]) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Expected KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError(f"Parameter name cannot be empty: {item!r}")
        parameters[key] = value
    return parameters


def _correction_parameters(
    kind: str,
    *,
    start_wl: float | None,
    end_wl: float | None,
    wavelength: float | None,
) -> dict[str, ParameterValue] | None:
    if kind == "none":
        return None
    if kind == "auto":
        return {"CorrectionType": 1}
    if kind == "baseline":
        if start_wl is None or end_wl is None:
            raise ValueError("baseline correction requires --start-wl and --end-wl")
        if start_wl == end_wl:
            raise ValueError("baseline correction wavelengths must differ")
        return {"CorrectionType": 2, "StartWL": start_wl, "EndWL": end_wl}
    if wavelength is None:
        raise ValueError("zero correction requires --wavelength")
    return {"CorrectionType": 3, "WL": wavelength}


def _feedback_dict(feedback: Feedback) -> dict[str, Any]:
    return {
        "command": feedback.command,
        "return_code": feedback.return_code,
        "error": feedback.error,
        "fields": dict(feedback.fields),
    }


def _apply_global_overrides(
    settings: ControlSettings, args: argparse.Namespace
) -> ControlSettings:
    return replace(
        settings,
        command_dir=args.command_dir or settings.command_dir,
        mode=args.mode or settings.mode,
        timeout_seconds=(
            args.timeout if args.timeout is not None else settings.timeout_seconds
        ),
        poll_interval_seconds=(
            args.poll_interval
            if args.poll_interval is not None
            else settings.poll_interval_seconds
        ),
        lock_timeout_seconds=(
            args.lock_timeout
            if args.lock_timeout is not None
            else settings.lock_timeout_seconds
        ),
        audit_dir=args.audit_dir or settings.audit_dir,
    )


def _build_client(
    settings: ControlSettings, *, enable_audit: bool = True
) -> LabSolutionsClient:
    return LabSolutionsClient(
        command_dir=settings.command_dir,
        mode=settings.mode,
        timeout=settings.timeout_seconds,
        poll_interval=settings.poll_interval_seconds,
        lock_timeout=settings.lock_timeout_seconds,
        encoding=settings.encoding,
        audit_dir=settings.audit_dir if enable_audit else None,
    )


def _resolve_pattern(template: str, sample_id: str, sample_name: str) -> str:
    sample_token = re.sub(r"[^A-Za-z0-9_.-]+", "_", sample_name).strip("._")
    try:
        return template.format(sample_id=sample_id, sample_name=sample_token)
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "export pattern may only use {sample_id} and {sample_name} placeholders"
        ) from exc


def _validate_identifiers(
    sample_name: str, sample_id: str, *, allow_unicode: bool
) -> None:
    if not sample_name or "\r" in sample_name or "\n" in sample_name:
        raise ValueError("sample name must be non-empty and single-line")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", sample_id):
        raise ValueError(
            "sample ID must contain only ASCII letters, digits, underscore, dot, or hyphen"
        )
    if not allow_unicode:
        try:
            sample_name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "sample name must be ASCII until Unicode is validated on the "
                "instrument PC; use --allow-unicode-identifiers to override"
            ) from exc


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def _select_scan_profile(
    args: argparse.Namespace, settings: ControlSettings
) -> tuple[Path, ScanProfile | None]:
    range_values = (args.start, args.stop, args.step)
    if any(value is not None for value in range_values) and not all(
        value is not None for value in range_values
    ):
        raise ValueError("--start, --stop, and --step must be supplied together")
    if all(value is not None for value in range_values):
        if not all(math.isfinite(value) for value in range_values):
            raise ValueError("scan range values must be finite numbers")
        if args.step == 0:
            raise ValueError("--step must not be zero")

    registry = ScanProfileRegistry(settings.scan_profiles)
    profile: ScanProfile | None = None
    if all(value is not None for value in range_values):
        direction = "ascending" if args.stop > args.start else "descending"
        request = SpectrumScanRequest.from_boundaries(
            args.start,
            args.stop,
            abs(args.step),
            direction=direction,
            profile_name=args.profile,
        )
        profile = registry.resolve(request).profile
    elif args.profile:
        profile = registry.get(args.profile)

    requested_method = Path(args.method) if args.method is not None else None
    if profile is not None and requested_method is not None:
        if not _same_path(requested_method, profile.method_file):
            raise ValueError(
                f"--method does not match scan profile {profile.name!r}: "
                f"{profile.method_file}"
            )

    method_file = (
        requested_method
        or (profile.method_file if profile is not None else None)
        or settings.method_file
    )
    if method_file is None:
        raise ValueError("no Spectrum method file configured; use --method or [spectrum]")

    if profile is None:
        matching_method = [
            candidate
            for candidate in settings.scan_profiles.values()
            if _same_path(candidate.method_file, Path(method_file))
        ]
        if len(matching_method) == 1:
            profile = matching_method[0]
    return Path(method_file), profile


def _requested_wavelengths(
    values: list[float] | None, profile: ScanProfile | None
) -> tuple[float, ...]:
    if not values:
        return ()
    if profile is None:
        raise ValueError(
            "--wavelengths requires a registered scan profile so the points can be verified"
        )
    if any(not math.isfinite(value) for value in values):
        raise ValueError("requested wavelengths must be finite numbers")
    if any(value <= 0 for value in values):
        raise ValueError("requested wavelengths must be positive")
    if len(set(values)) != len(values):
        raise ValueError("requested wavelengths must not contain duplicates")

    low = min(profile.start_nm, profile.stop_nm)
    high = max(profile.start_nm, profile.stop_nm)
    tolerance = max(1e-6, profile.step_nm * 1e-6)
    for wavelength in values:
        if wavelength < low - tolerance or wavelength > high + tolerance:
            raise ValueError(
                f"wavelength {wavelength:g} nm is outside profile {profile.name!r} "
                f"range {low:g}-{high:g} nm"
            )
        interval = abs(wavelength - profile.start_nm) / profile.step_nm
        if not math.isclose(interval, round(interval), abs_tol=1e-6):
            raise ValueError(
                f"wavelength {wavelength:g} nm is not on profile {profile.name!r} "
                f"{profile.step_nm:g} nm data grid"
            )
    return tuple(values)


def _within_scan_timing(profile: ScanProfile | None) -> dict[str, Any]:
    if profile is None:
        return {
            "source": "labsolutions_method_file",
            "scan_speed_nm_per_min": None,
            "nominal_point_interval_seconds": None,
            "nominal_scan_traverse_seconds": None,
            "note": (
                "Point dwell/settling is controlled inside the LabSolutions method; "
                "Command=111 has no time-step parameter."
            ),
        }
    speed = profile.scan_speed_nm_per_min
    if speed is None:
        return {
            "source": "registered_labsolutions_method",
            "scan_speed_nm_per_min": None,
            "nominal_point_interval_seconds": None,
            "nominal_scan_traverse_seconds": None,
            "note": (
                "Register scan_speed_nm_per_min after verifying the saved .vspm "
                "method to calculate nominal timing."
            ),
        }
    return {
        "source": "registered_labsolutions_method",
        "scan_speed_nm_per_min": speed,
        "nominal_point_interval_seconds": profile.step_nm * 60.0 / speed,
        "nominal_scan_traverse_seconds": (
            abs(profile.stop_nm - profile.start_nm) * 60.0 / speed
        ),
        "note": (
            "Nominal values come from wavelength interval / scan speed and exclude "
            "instrument response, command, accessory, save, and export overhead."
        ),
    }


def _wavelength_control(run: ResolvedSpectrumRun) -> dict[str, Any]:
    if run.scan_profile is None:
        return {
            "source": "method_file_only",
            "profile_verified": False,
            "method_file": str(run.method_file),
            "requested_wavelengths_nm": list(run.requested_wavelengths),
            "within_scan_timing": _within_scan_timing(None),
            "note": "Wavelength settings are inside the LabSolutions method file.",
        }
    profile = run.scan_profile
    return {
        "source": "registered_labsolutions_method",
        "profile_verified": True,
        "profile": profile.name,
        "method_file": str(profile.method_file),
        "start_nm": profile.start_nm,
        "stop_nm": profile.stop_nm,
        "step_nm": profile.step_nm,
        "requested_wavelengths_nm": list(run.requested_wavelengths),
        "acquisition": "continuous_spectrum",
        "within_scan_timing": _within_scan_timing(profile),
        "note": "The full registered range is acquired; requested points are metadata targets.",
    }


def _resolve_spectrum_run(
    args: argparse.Namespace, settings: ControlSettings
) -> ResolvedSpectrumRun:
    sample_id = args.sample_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    allow_unicode = (
        args.allow_unicode_identifiers or settings.allow_unicode_identifiers
    )
    _validate_identifiers(args.sample_name, sample_id, allow_unicode=allow_unicode)

    method_file, scan_profile = _select_scan_profile(args, settings)
    requested_wavelengths = _requested_wavelengths(
        args.wavelengths, scan_profile
    )

    data_file = args.data_file
    if data_file is None:
        if settings.data_dir is None:
            raise ValueError("no data directory configured; use --data-file or [spectrum]")
        data_file = settings.data_dir / f"{sample_id}.vspd"

    measurement_mode = (
        args.measurement_mode
        if args.measurement_mode is not None
        else settings.measurement_mode
    )
    discharge = (
        args.discharge
        if args.discharge is not None
        else settings.discharge_after_measurement
    )
    connect = (
        args.connect if args.connect is not None else settings.connect_before_run
    )
    disconnect = (
        args.disconnect
        if args.disconnect is not None
        else settings.disconnect_after_run
    )
    correction_kind = args.correction or settings.correction
    correction = _correction_parameters(
        correction_kind,
        start_wl=args.start_wl,
        end_wl=args.end_wl,
        wavelength=args.wavelength,
    )

    export_dir = None if args.no_wait_export else (args.export_dir or settings.export_dir)
    if export_dir is None and not args.no_wait_export:
        raise ValueError(
            "no export directory configured; configure [export] or use --no-wait-export"
        )
    export_template = args.export_pattern or settings.export_pattern
    export_pattern = _resolve_pattern(
        export_template, sample_id=sample_id, sample_name=args.sample_name
    )
    export_timeout = (
        args.export_timeout
        if args.export_timeout is not None
        else settings.export_timeout_seconds
    )
    stable_seconds = (
        args.stable_seconds
        if args.stable_seconds is not None
        else settings.stable_seconds
    )

    commands: list[dict[str, Any]] = [
        {"command": 0, "name": "hello", "parameters": {}}
    ]
    if connect:
        commands.append({"command": 1, "name": "connect", "parameters": {}})
    commands.append(
        {
            "command": 100,
            "name": "load_method",
            "parameters": {"ParameterFileName": str(method_file)},
        }
    )
    if correction:
        commands.append(
            {"command": 21, "name": "correction", "parameters": dict(correction)}
        )
    commands.append(
        {
            "command": 110,
            "name": "sample_information",
            "parameters": {
                "DataFileName": str(data_file),
                "SampleName": args.sample_name,
                "SampleID": sample_id,
            },
        }
    )
    commands.append(
        {
            "command": 111,
            "name": "spectrum_measurement",
            "parameters": {
                "MeasurementMode": measurement_mode,
                "Discharge": "ON" if discharge else "OFF",
            },
        }
    )
    if disconnect:
        commands.append({"command": 2, "name": "disconnect", "parameters": {}})

    warnings: list[str] = []
    if measurement_mode == 1:
        warnings.append("MeasurementMode=1 measures all configured cells")
    if discharge:
        warnings.append("Discharge=ON may discharge an aspiration accessory")
    if correction_kind != "none":
        warnings.append(f"correction={correction_kind} is a physical instrument action")
    if args.no_wait_export:
        warnings.append("automatic export will not be verified")
    if settings.audit_dir is None:
        warnings.append("command audit logging is not configured")
    if scan_profile is None:
        warnings.append(
            "method wavelength settings are not registered and cannot be verified by the CLI"
        )
    if requested_wavelengths:
        warnings.append(
            "requested wavelengths are validated targets; Spectrum still acquires the full range"
        )

    return ResolvedSpectrumRun(
        sample_name=args.sample_name,
        sample_id=sample_id,
        method_file=Path(method_file),
        data_file=Path(data_file),
        measurement_mode=measurement_mode,
        discharge=discharge,
        correction=correction,
        connect=connect,
        disconnect=disconnect,
        export_dir=Path(export_dir) if export_dir is not None else None,
        export_pattern=export_pattern,
        export_timeout=float(export_timeout),
        stable_seconds=float(stable_seconds),
        scan_profile=scan_profile,
        requested_wavelengths=requested_wavelengths,
        commands=tuple(commands),
        warnings=tuple(warnings),
    )


def _resolve_spectrum_series(
    args: argparse.Namespace, settings: ControlSettings
) -> ResolvedSpectrumSeries:
    if args.count < 2:
        raise ValueError("--count must be at least 2 for a Spectrum series")
    if args.count > 10_000:
        raise ValueError("--count cannot exceed 10000 in one Spectrum series")
    if not math.isfinite(args.interval_seconds) or args.interval_seconds <= 0:
        raise ValueError("--interval-seconds must be a finite number greater than zero")
    if (
        not math.isfinite(args.overrun_tolerance_seconds)
        or args.overrun_tolerance_seconds < 0
    ):
        raise ValueError(
            "--overrun-tolerance-seconds must be a finite non-negative number"
        )

    series_id = args.series_id or datetime.now().strftime("series_%Y%m%d_%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", series_id):
        raise ValueError(
            "series ID must contain only ASCII letters, digits, underscore, dot, or hyphen"
        )

    runs: list[ResolvedSpectrumRun] = []
    for index in range(1, args.count + 1):
        run_args = argparse.Namespace(**vars(args))
        run_args.sample_id = f"{series_id}_{index:04d}"
        run_args.data_file = None
        runs.append(_resolve_spectrum_run(run_args, settings))

    if runs[0].export_dir is not None:
        export_patterns = [run.export_pattern for run in runs]
        if len(set(export_patterns)) != len(export_patterns):
            raise ValueError(
                "Spectrum series export patterns must be unique per run; include "
                "{sample_id} in [export].pattern or use --no-wait-export"
            )

    warnings = list(runs[0].warnings)
    warnings.extend(
        (
            "series interval controls Command=111 start-to-start timing, not "
            "per-wavelength settling inside a scan",
            "the series stops before the next measurement if scan/export work "
            "exceeds the configured interval and tolerance",
        )
    )
    timing = _within_scan_timing(runs[0].scan_profile)
    nominal_scan_seconds = timing["nominal_scan_traverse_seconds"]
    if (
        isinstance(nominal_scan_seconds, (int, float))
        and args.interval_seconds <= nominal_scan_seconds
    ):
        warnings.append(
            f"interval {args.interval_seconds:g}s is not longer than the nominal "
            f"{nominal_scan_seconds:g}s wavelength traverse; the series is likely "
            "to stop on overrun before command/export overhead is included"
        )

    return ResolvedSpectrumSeries(
        series_id=series_id,
        interval_seconds=float(args.interval_seconds),
        overrun_tolerance_seconds=float(args.overrun_tolerance_seconds),
        runs=tuple(runs),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _validate_execution_paths(run: ResolvedSpectrumRun) -> None:
    if not run.method_file.is_file():
        raise ValueError(f"Spectrum method file does not exist: {run.method_file}")
    if run.method_file.suffix.lower() != ".vspm":
        raise ValueError(f"Spectrum method file should end in .vspm: {run.method_file}")
    if not run.data_file.parent.is_dir():
        raise ValueError(f"data directory does not exist: {run.data_file.parent}")
    if run.data_file.exists():
        raise ValueError(
            f"data file already exists; choose a new sample ID: {run.data_file}"
        )
    if run.export_dir is not None and not run.export_dir.is_dir():
        raise ValueError(f"export directory does not exist: {run.export_dir}")


def _export_summary(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "sha256": digest.hexdigest(),
        "modified_at_utc": datetime.fromtimestamp(
            stat.st_mtime, timezone.utc
        ).isoformat(timespec="milliseconds"),
        "suffix": path.suffix.lower(),
    }


def _plan_dict(run: ResolvedSpectrumRun) -> dict[str, Any]:
    return {
        "ok": True,
        "executed": False,
        "run_id": run.sample_id,
        "sample_name": run.sample_name,
        "method_file": str(run.method_file),
        "data_file": str(run.data_file),
        "export_directory": str(run.export_dir) if run.export_dir else None,
        "export_pattern": run.export_pattern,
        "wavelength_control": _wavelength_control(run),
        "commands": list(run.commands),
        "warnings": list(run.warnings),
        "next_step": "Review the plan, then repeat with --execute",
    }


def _series_timing_control(series: ResolvedSpectrumSeries) -> dict[str, Any]:
    first = series.runs[0]
    return {
        "mode": "repeated_full_spectra",
        "clock": "monotonic",
        "interval_seconds": series.interval_seconds,
        "interval_reference": "Command=111 start-to-start",
        "count": len(series.runs),
        "scheduled_duration_seconds": (
            (len(series.runs) - 1) * series.interval_seconds
        ),
        "overrun_tolerance_seconds": series.overrun_tolerance_seconds,
        "overrun_policy": "stop_before_next_measurement",
        "wait_for_each_export": first.export_dir is not None,
        "within_scan": _within_scan_timing(first.scan_profile),
        "note": (
            "This interval does not replace LabSolutions method scan speed, "
            "response time, or data interval."
        ),
    }


def _series_commands(series: ResolvedSpectrumSeries) -> list[dict[str, Any]]:
    first = series.runs[0]
    commands = [
        dict(command)
        for command in first.commands
        if command["command"] in {0, 1, 21, 100}
    ]
    for index, run in enumerate(series.runs):
        for command in run.commands:
            if command["command"] not in {110, 111}:
                continue
            planned = dict(command)
            planned["series_index"] = index + 1
            if command["command"] == 111:
                planned["scheduled_offset_seconds"] = (
                    index * series.interval_seconds
                )
            commands.append(planned)
    if first.disconnect:
        commands.append({"command": 2, "name": "disconnect", "parameters": {}})
    return commands


def _series_plan_dict(series: ResolvedSpectrumSeries) -> dict[str, Any]:
    first = series.runs[0]
    return {
        "ok": True,
        "executed": False,
        "series_id": series.series_id,
        "sample_name": first.sample_name,
        "method_file": str(first.method_file),
        "wavelength_control": _wavelength_control(first),
        "timing_control": _series_timing_control(series),
        "runs": [
            {
                "index": index,
                "run_id": run.sample_id,
                "scheduled_offset_seconds": (index - 1) * series.interval_seconds,
                "data_file": str(run.data_file),
                "export_directory": (
                    str(run.export_dir) if run.export_dir is not None else None
                ),
                "export_pattern": run.export_pattern,
            }
            for index, run in enumerate(series.runs, start=1)
        ],
        "commands": _series_commands(series),
        "warnings": list(series.warnings),
        "next_step": "Review the series plan, then repeat with --execute",
    }


def _run_manifest_path(settings: ControlSettings, run_id: str) -> Path | None:
    if settings.audit_dir is None:
        return None
    return settings.audit_dir / "runs" / f"{run_id}.json"


def _series_manifest_path(
    settings: ControlSettings, series_id: str
) -> Path | None:
    if settings.audit_dir is None:
        return None
    return settings.audit_dir / "series" / f"{series_id}.json"


def _execute_spectrum_series(
    settings: ControlSettings,
    client: LabSolutionsClient,
    series: ResolvedSpectrumSeries,
) -> dict[str, Any]:
    for run in series.runs:
        _validate_execution_paths(run)

    first = series.runs[0]
    started_at = datetime.now(timezone.utc)
    result = client.run_spectrum_series(
        method_file=first.method_file,
        samples=tuple(
            SpectrumSeriesSample(
                sample_name=run.sample_name,
                sample_id=run.sample_id,
                data_file=run.data_file,
                export_pattern=run.export_pattern,
            )
            for run in series.runs
        ),
        interval_seconds=series.interval_seconds,
        overrun_tolerance_seconds=series.overrun_tolerance_seconds,
        measurement_mode=first.measurement_mode,
        discharge=first.discharge,
        correction=first.correction,
        connect=first.connect,
        disconnect=first.disconnect,
        export_dir=first.export_dir,
        export_timeout=first.export_timeout,
        stable_seconds=first.stable_seconds,
    )

    run_outputs: list[dict[str, Any]] = []
    for run, point in zip(series.runs, result.runs, strict=True):
        export_metadata: dict[str, Any] | None = None
        if point.export_path is not None:
            try:
                export_metadata = _export_summary(point.export_path)
            except OSError as exc:
                export_metadata = {
                    "path": str(point.export_path),
                    "metadata_error": str(exc),
                }
                client.audit_warnings.append(
                    f"Measurement {run.sample_id} completed but export metadata "
                    f"failed: {exc}"
                )

        run_output: dict[str, Any] = {
            "ok": True,
            "executed": True,
            "run_id": run.sample_id,
            "series_id": series.series_id,
            "series_index": point.index,
            "sample_name": run.sample_name,
            "started_at_utc": point.started_at_utc.isoformat(
                timespec="milliseconds"
            ),
            "completed_at_utc": point.completed_at_utc.isoformat(
                timespec="milliseconds"
            ),
            "elapsed_seconds": round(point.elapsed_seconds, 6),
            "scheduled_offset_seconds": point.scheduled_offset_seconds,
            "actual_start_offset_seconds": round(
                point.actual_start_offset_seconds, 6
            ),
            "start_lateness_seconds": round(point.start_lateness_seconds, 6),
            "method_file": str(run.method_file),
            "data_file": str(run.data_file),
            "wavelength_control": _wavelength_control(run),
            "commands": [_feedback_dict(item) for item in point.feedback],
            "export": export_metadata,
            "warnings": list(run.warnings),
        }
        manifest_path = _run_manifest_path(settings, run.sample_id)
        if manifest_path is not None:
            try:
                write_json_atomic(manifest_path, run_output)
                run_output["manifest_path"] = str(manifest_path)
            except OSError as exc:
                client.audit_warnings.append(
                    f"Measurement {run.sample_id} completed but its run manifest "
                    f"could not be written: {exc}"
                )
        run_outputs.append(run_output)

    output: dict[str, Any] = {
        "ok": True,
        "executed": True,
        "series_id": series.series_id,
        "sample_name": first.sample_name,
        "started_at_utc": started_at.isoformat(timespec="milliseconds"),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ),
        "method_file": str(first.method_file),
        "wavelength_control": _wavelength_control(first),
        "timing_control": _series_timing_control(series),
        "preparation_commands": [
            _feedback_dict(item) for item in result.preparation_feedback
        ],
        "runs": run_outputs,
        "finalization_commands": [
            _feedback_dict(item) for item in result.finalization_feedback
        ],
        "warnings": list(series.warnings),
        "audit_warnings": list(client.audit_warnings),
    }
    manifest_path = _series_manifest_path(settings, series.series_id)
    if manifest_path is not None:
        try:
            write_json_atomic(manifest_path, output)
            output["manifest_path"] = str(manifest_path)
        except OSError as exc:
            output["audit_warnings"].append(
                f"Series completed but its manifest could not be written: {exc}"
            )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        settings = _apply_global_overrides(load_settings(args.config), args)

        if args.action == "doctor":
            report = run_diagnostics(settings, write_check=args.write_check)
            _print_json(report.as_dict())
            return 0 if report.ok else 1

        client = _build_client(settings, enable_audit=args.action != "recover")
        if args.action == "recover":
            before = client.recovery_snapshot()
            if args.force and not args.clear:
                raise ValueError("--force requires --clear")
            if not args.clear:
                _print_json({"ok": not before["recovery_required"], **before})
                return 2 if before["recovery_required"] else 0
            if not args.execute:
                _print_json(
                    {
                        "ok": True,
                        "executed": False,
                        "action": "clear_recovery",
                        "force": args.force,
                        "current_state": before,
                        "next_step": "Verify LabSolutions, then repeat with --execute",
                    }
                )
                return 0
            after = client.clear_recovery(force=args.force)
            recovery_record = {
                "schema_version": 1,
                "event": "operator_recovery_cleared",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                ),
                "forced": args.force,
                "before": before,
                "after": after,
            }
            recovery_audit: str | None = None
            if settings.audit_dir is not None:
                timestamp = datetime.now(timezone.utc).strftime(
                    "%Y%m%dT%H%M%S_%fZ"
                )
                path = settings.audit_dir / "recovery" / f"{timestamp}.json"
                try:
                    write_json_atomic(path, recovery_record)
                    recovery_audit = str(path)
                except OSError as exc:
                    client.audit_warnings.append(
                        f"Recovery cleared but audit record failed: {exc}"
                    )
            _print_json(
                {
                    "ok": True,
                    "executed": True,
                    "before": before,
                    "after": after,
                    "recovery_audit": recovery_audit,
                    "audit_warnings": client.audit_warnings,
                }
            )
            return 0

        if args.action == "ping":
            feedback = client.send_command(0)
            _print_json(
                {
                    "ok": feedback.ok,
                    "executed": True,
                    "feedback": _feedback_dict(feedback),
                    "audit_warnings": client.audit_warnings,
                }
            )
            return 0

        if args.action == "send":
            parameters = _parse_parameters(args.parameters)
            if not args.execute:
                _print_json(
                    {
                        "ok": True,
                        "executed": False,
                        "command": args.command,
                        "parameters": parameters,
                        "next_step": "Review the command, then repeat with --execute",
                    }
                )
                return 0
            result = client.send_command(
                args.command,
                raise_on_error=not args.allow_error,
                **parameters,
            )
            _print_json(
                {
                    "ok": result.ok,
                    "executed": True,
                    "feedback": _feedback_dict(result),
                    "audit_warnings": client.audit_warnings,
                }
            )
            return 0 if result.ok else 2

        if args.action == "series":
            series = _resolve_spectrum_series(args, settings)
            if not args.execute:
                _print_json(_series_plan_dict(series))
                return 0
            _print_json(_execute_spectrum_series(settings, client, series))
            return 0

        run = _resolve_spectrum_run(args, settings)
        if not args.execute:
            _print_json(_plan_dict(run))
            return 0

        _validate_execution_paths(run)
        started_at = datetime.now(timezone.utc)
        result = client.run_spectrum(
            method_file=run.method_file,
            sample_name=run.sample_name,
            sample_id=run.sample_id,
            data_file=run.data_file,
            measurement_mode=run.measurement_mode,
            discharge=run.discharge,
            correction=run.correction,
            connect=run.connect,
            disconnect=run.disconnect,
            export_dir=run.export_dir,
            export_pattern=run.export_pattern,
            export_timeout=run.export_timeout,
            stable_seconds=run.stable_seconds,
        )
        export_metadata: dict[str, Any] | None = None
        if result.export_path is not None:
            try:
                export_metadata = _export_summary(result.export_path)
            except OSError as exc:
                export_metadata = {
                    "path": str(result.export_path),
                    "metadata_error": str(exc),
                }
                client.audit_warnings.append(
                    f"Measurement completed but export metadata failed: {exc}"
                )
        output: dict[str, Any] = {
            "ok": True,
            "executed": True,
            "run_id": run.sample_id,
            "sample_name": run.sample_name,
            "started_at_utc": started_at.isoformat(timespec="milliseconds"),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "method_file": str(run.method_file),
            "data_file": str(run.data_file),
            "wavelength_control": _wavelength_control(run),
            "commands": [_feedback_dict(item) for item in result.feedback],
            "export": export_metadata,
            "warnings": list(run.warnings),
            "audit_warnings": client.audit_warnings,
        }
        manifest_path = _run_manifest_path(settings, run.sample_id)
        if manifest_path is not None:
            try:
                write_json_atomic(manifest_path, output)
                output["manifest_path"] = str(manifest_path)
            except OSError as exc:
                output["audit_warnings"].append(
                    f"Measurement completed but run manifest could not be written: {exc}"
                )
        _print_json(output)
        return 0
    except (LabSolutionsError, OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        _print_json(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
