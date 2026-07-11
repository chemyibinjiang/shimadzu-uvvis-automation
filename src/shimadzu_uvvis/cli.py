"""Command-line interface for Shimadzu LabSolutions UV-Vis automation."""

from __future__ import annotations

import argparse
import hashlib
import json
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
)
from .configuration import ControlSettings, load_settings
from .diagnostics import run_diagnostics


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
    commands: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


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
    spectrum.add_argument("--method", type=Path)
    spectrum.add_argument("--sample-name", required=True)
    spectrum.add_argument("--sample-id")
    spectrum.add_argument("--data-file", type=Path)
    spectrum.add_argument("--measurement-mode", type=int, choices=(1, 2))
    spectrum.add_argument(
        "--connect", action=argparse.BooleanOptionalAction, default=None
    )
    spectrum.add_argument(
        "--disconnect", action=argparse.BooleanOptionalAction, default=None
    )
    spectrum.add_argument(
        "--discharge", action=argparse.BooleanOptionalAction, default=None
    )
    spectrum.add_argument(
        "--correction", choices=("none", "auto", "baseline", "zero")
    )
    spectrum.add_argument("--start-wl", type=float)
    spectrum.add_argument("--end-wl", type=float)
    spectrum.add_argument("--wavelength", type=float)
    spectrum.add_argument("--export-dir", type=Path)
    spectrum.add_argument("--export-pattern")
    spectrum.add_argument("--export-timeout", type=float)
    spectrum.add_argument("--stable-seconds", type=float)
    spectrum.add_argument(
        "--no-wait-export",
        action="store_true",
        help="Do not wait for the LabSolutions automatic export",
    )
    spectrum.add_argument(
        "--allow-unicode-identifiers",
        action="store_true",
        help="Allow non-ASCII sample names after instrument-PC validation",
    )
    spectrum.add_argument(
        "--execute",
        action="store_true",
        help="Actually measure; otherwise only print the exact command plan",
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


def _resolve_spectrum_run(
    args: argparse.Namespace, settings: ControlSettings
) -> ResolvedSpectrumRun:
    sample_id = args.sample_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    allow_unicode = (
        args.allow_unicode_identifiers or settings.allow_unicode_identifiers
    )
    _validate_identifiers(args.sample_name, sample_id, allow_unicode=allow_unicode)

    method_file = args.method or settings.method_file
    if method_file is None:
        raise ValueError("no Spectrum method file configured; use --method or [spectrum]")

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
        commands=tuple(commands),
        warnings=tuple(warnings),
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
        "commands": list(run.commands),
        "warnings": list(run.warnings),
        "next_step": "Review the plan, then repeat with --execute",
    }


def _run_manifest_path(settings: ControlSettings, run_id: str) -> Path | None:
    if settings.audit_dir is None:
        return None
    return settings.audit_dir / "runs" / f"{run_id}.json"


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
