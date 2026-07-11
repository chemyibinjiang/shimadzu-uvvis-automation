"""Readiness checks for a Windows LabSolutions control computer."""

from __future__ import annotations

import os
import platform
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .client import MODE_FILE_NAMES
from .configuration import ControlSettings
from .locking import FileLockTimeoutError, InterProcessFileLock

CheckStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    status: CheckStatus
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "message": self.message}


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    checks: tuple[DiagnosticCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
        }


def _ascii_only(value: Path | str) -> bool:
    try:
        str(value).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _write_probe(directory: Path) -> str | None:
    probe = directory / f".shimadzu_uvvis_probe_{uuid.uuid4().hex}.tmp"
    try:
        probe.write_text("probe", encoding="ascii")
        if probe.read_text(encoding="ascii") != "probe":
            return "probe content could not be read back"
    except OSError as exc:
        return str(exc)
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return None


def _check_directory(
    checks: list[DiagnosticCheck],
    name: str,
    directory: Path | None,
    *,
    write_check: bool,
    required: bool = True,
) -> None:
    if directory is None:
        status: CheckStatus = "fail" if required else "warn"
        checks.append(DiagnosticCheck(name, status, "not configured"))
        return
    if not directory.is_dir():
        checks.append(
            DiagnosticCheck(name, "fail", f"directory does not exist: {directory}")
        )
        return
    checks.append(DiagnosticCheck(name, "pass", str(directory)))
    if write_check:
        problem = _write_probe(directory)
        if problem is None:
            checks.append(
                DiagnosticCheck(f"{name}_write", "pass", "write/read/delete probe passed")
            )
        else:
            checks.append(DiagnosticCheck(f"{name}_write", "fail", problem))


def run_diagnostics(
    settings: ControlSettings, *, write_check: bool = False
) -> DiagnosticReport:
    checks: list[DiagnosticCheck] = []
    version = ".".join(str(part) for part in sys.version_info[:3])
    checks.append(
        DiagnosticCheck(
            "python",
            "pass" if sys.version_info >= (3, 11) else "fail",
            f"Python {version}",
        )
    )
    checks.append(
        DiagnosticCheck(
            "operating_system",
            "pass" if os.name == "nt" else "warn",
            platform.platform(),
        )
    )
    checks.append(
        DiagnosticCheck(
            "config",
            "pass" if settings.config_path is not None else "warn",
            str(settings.config_path) if settings.config_path else "using defaults/CLI only",
        )
    )

    _check_directory(
        checks, "command_directory", settings.command_dir, write_check=write_check
    )
    command_name, feedback_name = MODE_FILE_NAMES[settings.mode]
    command_path = settings.command_dir / command_name
    feedback_path = settings.command_dir / feedback_name
    recovery_path = (
        settings.command_dir / f".shimadzu_uvvis_{settings.mode}.recovery.json"
    )
    if settings.command_dir.is_dir():
        checks.append(
            DiagnosticCheck(
                "pending_command",
                "fail" if command_path.exists() else "pass",
                f"pending command exists: {command_path}"
                if command_path.exists()
                else "no unconsumed command file",
            )
        )
        if feedback_path.exists():
            checks.append(
                DiagnosticCheck(
                    "stale_feedback",
                    "warn",
                    f"feedback from a previous command exists: {feedback_path}",
                )
            )
        checks.append(
            DiagnosticCheck(
                "recovery_state",
                "fail" if recovery_path.exists() else "pass",
                f"operator recovery is required: {recovery_path}"
                if recovery_path.exists()
                else "no ambiguous prior command",
            )
        )
        lock_path = settings.command_dir / f".shimadzu_uvvis_{settings.mode}.lock"
        try:
            with InterProcessFileLock(
                lock_path,
                timeout=min(settings.lock_timeout_seconds, 0.2),
                poll_interval=0.02,
            ):
                pass
            checks.append(
                DiagnosticCheck(
                    "controller_lock", "pass", "no other controller holds the lock"
                )
            )
        except FileLockTimeoutError:
            checks.append(
                DiagnosticCheck(
                    "controller_lock",
                    "fail",
                    "another controller process currently holds the command lock",
                )
            )

    _check_directory(
        checks, "export_directory", settings.export_dir, write_check=write_check
    )
    _check_directory(
        checks, "data_directory", settings.data_dir, write_check=write_check
    )
    _check_directory(
        checks, "audit_directory", settings.audit_dir, write_check=write_check
    )

    if settings.method_file is None:
        checks.append(DiagnosticCheck("method_file", "fail", "not configured"))
    elif not settings.method_file.is_file():
        checks.append(
            DiagnosticCheck(
                "method_file", "fail", f"file does not exist: {settings.method_file}"
            )
        )
    else:
        suffix_status: CheckStatus = (
            "pass" if settings.method_file.suffix.lower() == ".vspm" else "warn"
        )
        checks.append(
            DiagnosticCheck(
                "method_file", suffix_status, str(settings.method_file)
            )
        )

    if not settings.scan_profiles:
        checks.append(
            DiagnosticCheck(
                "scan_profiles",
                "warn",
                "none configured; start/stop/step compatibility is unavailable",
            )
        )
    else:
        for name, profile in settings.scan_profiles.items():
            status: CheckStatus = (
                "pass"
                if profile.method_file.is_file()
                and profile.method_file.suffix.lower() == ".vspm"
                else "fail"
            )
            checks.append(
                DiagnosticCheck(
                    f"scan_profile_{name}",
                    status,
                    f"{profile.start_nm:g}:{profile.stop_nm:g}:{profile.step_nm:g} "
                    f"-> {profile.method_file}",
                )
            )

    path_values = [
        settings.command_dir,
        settings.export_dir,
        settings.data_dir,
        settings.method_file,
        settings.audit_dir,
        *(profile.method_file for profile in settings.scan_profiles.values()),
    ]
    non_ascii = [str(value) for value in path_values if value and not _ascii_only(value)]
    checks.append(
        DiagnosticCheck(
            "ascii_paths",
            "warn" if non_ascii else "pass",
            "non-ASCII paths need instrument-PC validation: " + ", ".join(non_ascii)
            if non_ascii
            else "all configured paths are ASCII-only",
        )
    )
    checks.append(
        DiagnosticCheck(
            "measurement_mode",
            "pass" if settings.measurement_mode == 2 else "warn",
            "2: current cell only"
            if settings.measurement_mode == 2
            else "1: all configured cells will be measured",
        )
    )
    checks.append(
        DiagnosticCheck(
            "discharge",
            "warn" if settings.discharge_after_measurement else "pass",
            "ON: aspiration accessory may discharge after measurement"
            if settings.discharge_after_measurement
            else "OFF: no post-measurement discharge requested",
        )
    )
    checks.append(
        DiagnosticCheck(
            "export_correlation",
            "pass" if "{sample_id}" in settings.export_pattern else "warn",
            settings.export_pattern,
        )
    )
    checks.append(
        DiagnosticCheck(
            "automatic_control",
            "warn",
            "filesystem readiness only; run 'ping' after LabSolutions enters Automatic Control",
        )
    )
    return DiagnosticReport(tuple(checks))
