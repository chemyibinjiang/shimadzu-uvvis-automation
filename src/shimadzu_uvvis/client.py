"""Client for the LabSolutions UV-Vis automatic-control text protocol."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Final, Iterator, Mapping, Sequence, TypeAlias

from .audit import AuditRecorder, write_json_atomic
from .locking import FileLockTimeoutError, InterProcessFileLock

ParameterValue: TypeAlias = str | int | float | bool | Path

MODE_FILE_NAMES: Final[dict[str, tuple[str, str]]] = {
    "spectrum": ("SPC_CMD.txt", "SPC_RES.txt"),
    "quantitation": ("QUA_CMD.txt", "QUA_RES.txt"),
    "photometric": ("PHO_CMD.txt", "PHO_RES.txt"),
    "time_course": ("TMC_CMD.txt", "TMC_RES.txt"),
}


def _workflow_locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapped(self: LabSolutionsClient, *args: Any, **kwargs: Any) -> Any:
        with self._workflow_lock():
            return method(self, *args, **kwargs)

    return wrapped


class LabSolutionsError(RuntimeError):
    """Base class for LabSolutions communication failures."""


class LabSolutionsBusyError(LabSolutionsError):
    """Raised when an unconsumed command already exists."""


class LabSolutionsRecoveryRequiredError(LabSolutionsBusyError):
    """Raised after an ambiguous command until an operator acknowledges it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            "A previous command did not reach a confirmed feedback state. "
            f"Inspect LabSolutions and run the recovery command before continuing: {path}"
        )


class LabSolutionsProtocolError(LabSolutionsError):
    """Raised when a command or feedback file is malformed."""


class LabSolutionsTimeoutError(LabSolutionsError):
    """Raised when LabSolutions or an export file does not arrive in time."""


class SpectrumScheduleOverrunError(LabSolutionsError):
    """Raised before a repeated Spectrum would start outside its tolerance."""

    def __init__(
        self,
        *,
        index: int,
        sample_id: str,
        scheduled_offset_seconds: float,
        actual_offset_seconds: float,
        tolerance_seconds: float,
    ) -> None:
        self.index = index
        self.sample_id = sample_id
        self.scheduled_offset_seconds = scheduled_offset_seconds
        self.actual_offset_seconds = actual_offset_seconds
        self.tolerance_seconds = tolerance_seconds
        lateness = actual_offset_seconds - scheduled_offset_seconds
        super().__init__(
            f"Spectrum series run {index} ({sample_id}) is {lateness:.3f}s late, "
            f"exceeding the {tolerance_seconds:.3f}s tolerance. The next "
            "measurement was not started. Increase --interval-seconds only after "
            "checking the completed scan and export duration."
        )


@dataclass(frozen=True, slots=True)
class Feedback:
    """Parsed LabSolutions feedback."""

    command: int
    return_code: int
    error: str
    fields: Mapping[str, str]

    @property
    def ok(self) -> bool:
        return self.return_code == 0

    def raise_for_error(self) -> None:
        if not self.ok:
            raise LabSolutionsCommandError(self)


class LabSolutionsCommandError(LabSolutionsError):
    """Raised when LabSolutions returns a non-zero result code."""

    def __init__(self, feedback: Feedback) -> None:
        self.feedback = feedback
        detail = feedback.error or "No error text was returned"
        super().__init__(
            f"LabSolutions command {feedback.command} failed with "
            f"Return={feedback.return_code}: {detail}"
        )


@dataclass(frozen=True, slots=True)
class SpectrumRunResult:
    """Result of a high-level Spectrum measurement sequence."""

    feedback: tuple[Feedback, ...]
    export_path: Path | None


@dataclass(frozen=True, slots=True)
class SpectrumSeriesSample:
    """One uniquely named acquisition in a repeated Spectrum series."""

    sample_name: str
    sample_id: str
    data_file: Path | None = None
    export_pattern: str = "*.csv"


@dataclass(frozen=True, slots=True)
class SpectrumSeriesPointResult:
    """Timing and output for one completed repeated Spectrum acquisition."""

    index: int
    sample_id: str
    scheduled_offset_seconds: float
    actual_start_offset_seconds: float
    start_lateness_seconds: float
    started_at_utc: datetime
    completed_at_utc: datetime
    elapsed_seconds: float
    feedback: tuple[Feedback, ...]
    export_path: Path | None


@dataclass(frozen=True, slots=True)
class SpectrumSeriesResult:
    """Result of a start-to-start scheduled Spectrum series."""

    preparation_feedback: tuple[Feedback, ...]
    runs: tuple[SpectrumSeriesPointResult, ...]
    finalization_feedback: tuple[Feedback, ...]


def parse_exchange_text(text: str) -> dict[str, str]:
    """Parse a LabSolutions key-value command or feedback file."""

    fields: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            raise LabSolutionsProtocolError(
                f"Line {line_number} is not a key-value pair: {raw_line!r}"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise LabSolutionsProtocolError(f"Line {line_number} has an empty key")
        fields[key] = value.strip()
    return fields


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def _format_value(value: ParameterValue) -> str:
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    rendered = str(value)
    if "\r" in rendered or "\n" in rendered:
        raise ValueError("LabSolutions parameter values cannot contain newlines")
    return rendered


def _serialize_command(
    command: int, parameters: Mapping[str, ParameterValue | None]
) -> str:
    if isinstance(command, bool) or not isinstance(command, int) or command < 0:
        raise ValueError("command must be a non-negative integer")

    lines = [f"Command={command}"]
    for key, value in parameters.items():
        if value is None:
            continue
        if not key or "=" in key or "\r" in key or "\n" in key:
            raise ValueError(f"Invalid LabSolutions parameter name: {key!r}")
        lines.append(f"{key}={_format_value(value)}")
    return "\r\n".join(lines) + "\r\n"


def _parse_feedback(text: str) -> Feedback:
    fields = parse_exchange_text(text)
    try:
        command = int(fields["Command"])
        return_code = int(fields["Return"])
    except KeyError as exc:
        raise LabSolutionsProtocolError(
            f"Feedback is missing required field {exc.args[0]!r}"
        ) from exc
    except ValueError as exc:
        raise LabSolutionsProtocolError(
            "Feedback Command and Return fields must be integers"
        ) from exc

    return Feedback(
        command=command,
        return_code=return_code,
        error=_unquote(fields.get("Error", "")),
        fields=MappingProxyType(dict(fields)),
    )


class LabSolutionsClient:
    """Synchronous client for one LabSolutions automatic-control mode."""

    def __init__(
        self,
        command_dir: str | Path = r"C:\UVVisControl",
        *,
        mode: str = "spectrum",
        timeout: float = 600.0,
        poll_interval: float = 0.2,
        lock_timeout: float = 5.0,
        encoding: str = "utf-8",
        audit_dir: str | Path | None = None,
    ) -> None:
        if mode not in MODE_FILE_NAMES:
            choices = ", ".join(sorted(MODE_FILE_NAMES))
            raise ValueError(f"Unsupported mode {mode!r}; choose one of: {choices}")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        if lock_timeout <= 0:
            raise ValueError("lock_timeout must be greater than zero")

        self.command_dir = Path(command_dir)
        self.mode = mode
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.lock_timeout = float(lock_timeout)
        self.encoding = encoding
        command_name, feedback_name = MODE_FILE_NAMES[mode]
        self.command_path = self.command_dir / command_name
        self.feedback_path = self.command_dir / feedback_name
        self.lock_path = self.command_dir / f".shimadzu_uvvis_{mode}.lock"
        self.workflow_lock_path = (
            self.command_dir / f".shimadzu_uvvis_{mode}.workflow.lock"
        )
        self.recovery_path = (
            self.command_dir / f".shimadzu_uvvis_{mode}.recovery.json"
        )
        self._lock = threading.Lock()
        self._workflow_state_lock = threading.Lock()
        self._workflow_owner: int | None = None
        self._workflow_depth = 0
        self.audit_recorder = AuditRecorder(audit_dir) if audit_dir is not None else None
        self.audit_warnings: list[str] = []

    @contextmanager
    def _workflow_lock(self) -> Iterator[None]:
        """Reserve this LabSolutions mode across a complete high-level workflow."""

        thread_id = threading.get_ident()
        with self._workflow_state_lock:
            reentrant = self._workflow_owner == thread_id
            if reentrant:
                self._workflow_depth += 1

        if reentrant:
            try:
                yield
            finally:
                with self._workflow_state_lock:
                    self._workflow_depth -= 1
            return

        process_lock = InterProcessFileLock(
            self.workflow_lock_path,
            timeout=self.lock_timeout,
            poll_interval=min(self.poll_interval, 0.1),
        )
        try:
            process_lock.acquire()
        except FileLockTimeoutError as exc:
            raise LabSolutionsBusyError(
                "Another controller process owns the LabSolutions workflow for "
                f"this mode: {self.command_dir}"
            ) from exc

        try:
            with self._workflow_state_lock:
                self._workflow_owner = thread_id
                self._workflow_depth = 1
            yield
        finally:
            with self._workflow_state_lock:
                self._workflow_owner = None
                self._workflow_depth = 0
            process_lock.release()

    @_workflow_locked
    def send_command(
        self,
        command: int,
        /,
        *,
        raise_on_error: bool = True,
        timeout: float | None = None,
        **parameters: ParameterValue | None,
    ) -> Feedback:
        """Write one command atomically and wait for its matching feedback."""

        wait_timeout = self.timeout if timeout is None else float(timeout)
        if wait_timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if not self.command_dir.is_dir():
            raise LabSolutionsError(
                f"Command directory does not exist: {self.command_dir}. "
                "Configure the same folder in LabSolutions Automatic Control first."
            )

        payload = _serialize_command(command, parameters)
        request_id = uuid.uuid4().hex
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        feedback: Feedback | None = None
        stale_feedback: str | None = None
        command_written = False
        failure: Exception | None = None

        try:
            with self._lock:
                try:
                    process_lock = InterProcessFileLock(
                        self.lock_path,
                        timeout=self.lock_timeout,
                        poll_interval=min(self.poll_interval, 0.1),
                    )
                    with process_lock:
                        if self.recovery_path.exists():
                            raise LabSolutionsRecoveryRequiredError(
                                self.recovery_path
                            )
                        if self.command_path.exists():
                            raise LabSolutionsBusyError(
                                f"A command is already waiting at {self.command_path}. "
                                "Do not overwrite it until its state has been checked."
                            )

                        if self.feedback_path.exists():
                            try:
                                stale_feedback = self.feedback_path.read_text(
                                    encoding="utf-8-sig", errors="replace"
                                )
                            except OSError:
                                stale_feedback = "<unreadable>"
                        try:
                            self.feedback_path.unlink(missing_ok=True)
                        except OSError as exc:
                            raise LabSolutionsBusyError(
                                f"Cannot clear stale feedback file "
                                f"{self.feedback_path}: {exc}"
                            ) from exc

                        recovery_record = {
                            "schema_version": 1,
                            "request_id": request_id,
                            "mode": self.mode,
                            "command": command,
                            "parameters": {
                                key: _format_value(value)
                                for key, value in parameters.items()
                                if value is not None
                            },
                            "created_at_utc": started_at.isoformat(
                                timespec="milliseconds"
                            ),
                            "command_path": str(self.command_path),
                            "feedback_path": str(self.feedback_path),
                        }
                        write_json_atomic(self.recovery_path, recovery_record)

                        temporary_path = self.command_dir / (
                            f".{self.command_path.name}.{uuid.uuid4().hex}.tmp"
                        )
                        try:
                            temporary_path.write_text(
                                payload,
                                encoding=self.encoding,
                                errors="strict",
                                newline="",
                            )
                            os.replace(temporary_path, self.command_path)
                            command_written = True
                        except Exception:
                            self.recovery_path.unlink(missing_ok=True)
                            raise
                        finally:
                            temporary_path.unlink(missing_ok=True)

                        feedback = self._wait_for_feedback(command, wait_timeout)
                        try:
                            self.recovery_path.unlink(missing_ok=True)
                        except OSError as exc:
                            self.audit_warnings.append(
                                "Feedback was confirmed, but the recovery marker "
                                f"could not be cleared: {exc}"
                            )
                except FileLockTimeoutError as exc:
                    raise LabSolutionsBusyError(
                        "Another controller process is using the LabSolutions "
                        f"command folder: {self.command_dir}"
                    ) from exc

            if feedback is None:
                raise LabSolutionsProtocolError(
                    f"Command {command} completed without parsed feedback"
                )
            if raise_on_error:
                feedback.raise_for_error()
            return feedback
        except Exception as exc:
            failure = exc
            raise
        finally:
            self._record_transaction(
                request_id=request_id,
                command=command,
                parameters=parameters,
                payload=payload,
                started_at=started_at,
                elapsed_seconds=time.monotonic() - started_monotonic,
                command_written=command_written,
                stale_feedback=stale_feedback,
                feedback=feedback,
                failure=failure,
            )

    def _record_transaction(
        self,
        *,
        request_id: str,
        command: int,
        parameters: Mapping[str, ParameterValue | None],
        payload: str,
        started_at: datetime,
        elapsed_seconds: float,
        command_written: bool,
        stale_feedback: str | None,
        feedback: Feedback | None,
        failure: Exception | None,
    ) -> None:
        if self.audit_recorder is None:
            return
        status = "ok"
        if feedback is not None and not feedback.ok:
            status = "labsolutions_error"
        if failure is not None and feedback is None:
            status = "transport_error"
        record: dict[str, object] = {
            "schema_version": 1,
            "request_id": request_id,
            "status": status,
            "started_at_utc": started_at.isoformat(timespec="milliseconds"),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "elapsed_seconds": round(elapsed_seconds, 6),
            "mode": self.mode,
            "command": command,
            "parameters": {
                key: _format_value(value)
                for key, value in parameters.items()
                if value is not None
            },
            "command_payload": payload,
            "command_path": str(self.command_path),
            "feedback_path": str(self.feedback_path),
            "command_written": command_written,
            "command_file_exists_at_record_time": self.command_path.exists(),
            "stale_feedback_removed": stale_feedback,
        }
        if feedback is not None:
            record["feedback"] = {
                "command": feedback.command,
                "return_code": feedback.return_code,
                "error": feedback.error,
                "fields": dict(feedback.fields),
            }
        if failure is not None:
            record["exception"] = {
                "type": type(failure).__name__,
                "message": str(failure),
            }
        try:
            self.audit_recorder.record(record)
        except OSError as exc:
            self.audit_warnings.append(f"Could not write command audit record: {exc}")

    def recovery_snapshot(self) -> dict[str, object]:
        """Describe an ambiguous prior command without changing any files."""

        marker: object = None
        if self.recovery_path.exists():
            try:
                marker = json.loads(
                    self.recovery_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                marker = {"unreadable": str(exc)}

        feedback: object = None
        if self.feedback_path.exists():
            try:
                parsed_feedback = _parse_feedback(
                    self.feedback_path.read_text(encoding="utf-8-sig")
                )
                feedback = {
                    "command": parsed_feedback.command,
                    "return_code": parsed_feedback.return_code,
                    "error": parsed_feedback.error,
                    "fields": dict(parsed_feedback.fields),
                }
            except (OSError, UnicodeError, LabSolutionsProtocolError) as exc:
                feedback = {"unreadable": str(exc)}
        return {
            "recovery_required": self.recovery_path.exists(),
            "marker_path": str(self.recovery_path),
            "marker": marker,
            "command_file_exists": self.command_path.exists(),
            "command_path": str(self.command_path),
            "feedback_file_exists": self.feedback_path.exists(),
            "feedback_path": str(self.feedback_path),
            "feedback": feedback,
        }

    def clear_recovery(self, *, force: bool = False) -> dict[str, object]:
        """Clear an ambiguous-command marker after operator review."""

        with self._lock:
            try:
                process_lock = InterProcessFileLock(
                    self.lock_path,
                    timeout=self.lock_timeout,
                    poll_interval=min(self.poll_interval, 0.1),
                )
                with process_lock:
                    snapshot = self.recovery_snapshot()
                    if not snapshot["recovery_required"]:
                        return snapshot
                    if snapshot["command_file_exists"] and not force:
                        raise LabSolutionsRecoveryRequiredError(self.recovery_path)

                    marker = snapshot.get("marker")
                    feedback = snapshot.get("feedback")
                    marker_command = (
                        marker.get("command") if isinstance(marker, dict) else None
                    )
                    feedback_command = (
                        feedback.get("command")
                        if isinstance(feedback, dict)
                        else None
                    )
                    if marker_command != feedback_command and not force:
                        raise LabSolutionsRecoveryRequiredError(self.recovery_path)
                    self.recovery_path.unlink(missing_ok=True)
                    return self.recovery_snapshot()
            except FileLockTimeoutError as exc:
                raise LabSolutionsBusyError(
                    "Another controller process is using the LabSolutions "
                    f"command folder: {self.command_dir}"
                ) from exc

    def _wait_for_feedback(self, command: int, timeout: float) -> Feedback:
        deadline = time.monotonic() + timeout
        last_protocol_error: LabSolutionsProtocolError | None = None
        read_encoding = (
            "utf-8-sig"
            if self.encoding.lower().replace("_", "-") == "utf-8"
            else self.encoding
        )

        while time.monotonic() < deadline:
            if self.feedback_path.exists():
                try:
                    text = self.feedback_path.read_text(
                        encoding=read_encoding, errors="strict"
                    )
                    feedback = _parse_feedback(text)
                    if feedback.command != command:
                        raise LabSolutionsProtocolError(
                            f"Expected feedback for command {command}, got "
                            f"command {feedback.command}"
                        )
                    return feedback
                except (OSError, UnicodeError):
                    # LabSolutions may still have the file open or partially written.
                    pass
                except LabSolutionsProtocolError as exc:
                    last_protocol_error = exc
            time.sleep(self.poll_interval)

        detail = ""
        if last_protocol_error is not None:
            detail = f" Last feedback parse error: {last_protocol_error}."
        pending = "still exists" if self.command_path.exists() else "was consumed"
        raise LabSolutionsTimeoutError(
            f"Timed out after {timeout:.1f}s waiting for {self.feedback_path.name}; "
            f"the command file {pending}.{detail}"
        )

    def wait_for_export(
        self,
        export_dir: str | Path,
        *,
        pattern: str = "*.csv",
        since: float | None = None,
        timeout: float | None = None,
        stable_seconds: float = 2.0,
        require_nonempty: bool = True,
    ) -> Path:
        """Wait until a recent exported file stops changing."""

        directory = Path(export_dir)
        wait_timeout = self.timeout if timeout is None else float(timeout)
        if wait_timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if stable_seconds < 0:
            raise ValueError("stable_seconds cannot be negative")
        if not directory.is_dir():
            raise LabSolutionsError(f"Export directory does not exist: {directory}")

        earliest_mtime = (time.time() - 1.0) if since is None else since - 1.0
        deadline = time.monotonic() + wait_timeout
        observations: dict[Path, tuple[tuple[int, int], float]] = {}

        while time.monotonic() < deadline:
            now = time.monotonic()
            candidates: list[tuple[int, Path, os.stat_result]] = []
            for path in directory.glob(pattern):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < earliest_mtime:
                    continue
                candidates.append((stat.st_mtime_ns, path, stat))

            for _, path, stat in sorted(candidates, reverse=True):
                if require_nonempty and stat.st_size == 0:
                    continue
                signature = (stat.st_size, stat.st_mtime_ns)
                previous = observations.get(path)
                if previous is None or previous[0] != signature:
                    observations[path] = (signature, now)
                    if stable_seconds == 0:
                        return path
                    continue
                if now - previous[1] >= stable_seconds:
                    return path

            time.sleep(self.poll_interval)

        raise LabSolutionsTimeoutError(
            f"Timed out after {wait_timeout:.1f}s waiting for a stable export "
            f"matching {pattern!r} in {directory}"
        )

    @_workflow_locked
    def run_spectrum(
        self,
        *,
        method_file: str | Path,
        sample_name: str,
        sample_id: str | None = None,
        data_file: str | Path | None = None,
        measurement_mode: int = 2,
        discharge: bool = False,
        correction: Mapping[str, ParameterValue] | None = None,
        connect: bool = False,
        disconnect: bool = False,
        export_dir: str | Path | None = None,
        export_pattern: str = "*.csv",
        export_timeout: float | None = None,
        stable_seconds: float = 2.0,
    ) -> SpectrumRunResult:
        """Run the standard Hello/load/sample/measure Spectrum sequence."""

        if self.mode != "spectrum":
            raise LabSolutionsProtocolError(
                "run_spectrum requires a client configured with mode='spectrum'"
            )
        if measurement_mode not in (1, 2):
            raise ValueError("measurement_mode must be 1 or 2")
        if not sample_name:
            raise ValueError("sample_name cannot be empty")

        feedback: list[Feedback] = [self.send_command(0)]
        if connect:
            feedback.append(self.send_command(1))

        feedback.append(
            self.send_command(100, ParameterFileName=Path(method_file))
        )
        if correction:
            feedback.append(self.send_command(21, **dict(correction)))

        sample_parameters: dict[str, ParameterValue | None] = {
            "DataFileName": Path(data_file) if data_file is not None else None,
            "SampleName": sample_name,
            "SampleID": sample_id,
        }
        feedback.append(self.send_command(110, **sample_parameters))

        measurement_started_at = time.time()
        feedback.append(
            self.send_command(
                111,
                MeasurementMode=measurement_mode,
                Discharge=discharge,
            )
        )

        export_path: Path | None = None
        if export_dir is not None:
            export_path = self.wait_for_export(
                export_dir,
                pattern=export_pattern,
                since=measurement_started_at,
                timeout=export_timeout,
                stable_seconds=stable_seconds,
            )

        if disconnect:
            feedback.append(self.send_command(2))

        return SpectrumRunResult(tuple(feedback), export_path)

    @_workflow_locked
    def run_spectrum_series(
        self,
        *,
        method_file: str | Path,
        samples: Sequence[SpectrumSeriesSample],
        interval_seconds: float,
        overrun_tolerance_seconds: float = 1.0,
        measurement_mode: int = 2,
        discharge: bool = False,
        correction: Mapping[str, ParameterValue] | None = None,
        connect: bool = False,
        disconnect: bool = False,
        export_dir: str | Path | None = None,
        export_timeout: float | None = None,
        stable_seconds: float = 2.0,
    ) -> SpectrumSeriesResult:
        """Run full spectra at a monotonic Command=111 start-to-start cadence."""

        if self.mode != "spectrum":
            raise LabSolutionsProtocolError(
                "run_spectrum_series requires a client configured with mode='spectrum'"
            )
        if measurement_mode not in (1, 2):
            raise ValueError("measurement_mode must be 1 or 2")
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be a finite number greater than zero")
        if (
            not math.isfinite(overrun_tolerance_seconds)
            or overrun_tolerance_seconds < 0
        ):
            raise ValueError(
                "overrun_tolerance_seconds must be a finite non-negative number"
            )

        scheduled_samples = tuple(samples)
        if not scheduled_samples:
            raise ValueError("samples cannot be empty")
        sample_ids = [sample.sample_id for sample in scheduled_samples]
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("series sample IDs must be unique")
        for sample in scheduled_samples:
            if not sample.sample_name:
                raise ValueError("series sample names cannot be empty")
            if not sample.sample_id:
                raise ValueError("series sample IDs cannot be empty")
            if not sample.export_pattern:
                raise ValueError("series export patterns cannot be empty")

        preparation: list[Feedback] = [self.send_command(0)]
        if connect:
            preparation.append(self.send_command(1))
        preparation.append(
            self.send_command(100, ParameterFileName=Path(method_file))
        )
        if correction:
            preparation.append(self.send_command(21, **dict(correction)))

        run_results: list[SpectrumSeriesPointResult] = []
        series_epoch: float | None = None

        def ensure_on_schedule(
            *, index: int, sample_id: str, target: float, epoch: float
        ) -> None:
            actual = time.monotonic()
            if actual - target > overrun_tolerance_seconds:
                raise SpectrumScheduleOverrunError(
                    index=index,
                    sample_id=sample_id,
                    scheduled_offset_seconds=target - epoch,
                    actual_offset_seconds=actual - epoch,
                    tolerance_seconds=overrun_tolerance_seconds,
                )

        for offset_index, sample in enumerate(scheduled_samples):
            index = offset_index + 1
            if series_epoch is not None:
                target = series_epoch + offset_index * interval_seconds
                ensure_on_schedule(
                    index=index,
                    sample_id=sample.sample_id,
                    target=target,
                    epoch=series_epoch,
                )

            sample_feedback = self.send_command(
                110,
                DataFileName=sample.data_file,
                SampleName=sample.sample_name,
                SampleID=sample.sample_id,
            )

            if series_epoch is None:
                series_epoch = time.monotonic()
                target = series_epoch
            else:
                target = series_epoch + offset_index * interval_seconds
                ensure_on_schedule(
                    index=index,
                    sample_id=sample.sample_id,
                    target=target,
                    epoch=series_epoch,
                )
                remaining = target - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                ensure_on_schedule(
                    index=index,
                    sample_id=sample.sample_id,
                    target=target,
                    epoch=series_epoch,
                )

            started_monotonic = time.monotonic()
            started_at_utc = datetime.now(timezone.utc)
            measurement_started_at = time.time()
            measurement_feedback = self.send_command(
                111,
                MeasurementMode=measurement_mode,
                Discharge=discharge,
            )

            export_path: Path | None = None
            if export_dir is not None:
                export_path = self.wait_for_export(
                    export_dir,
                    pattern=sample.export_pattern,
                    since=measurement_started_at,
                    timeout=export_timeout,
                    stable_seconds=stable_seconds,
                )

            completed_at_utc = datetime.now(timezone.utc)
            actual_offset = started_monotonic - series_epoch
            scheduled_offset = offset_index * interval_seconds
            run_results.append(
                SpectrumSeriesPointResult(
                    index=index,
                    sample_id=sample.sample_id,
                    scheduled_offset_seconds=scheduled_offset,
                    actual_start_offset_seconds=actual_offset,
                    start_lateness_seconds=max(0.0, actual_offset - scheduled_offset),
                    started_at_utc=started_at_utc,
                    completed_at_utc=completed_at_utc,
                    elapsed_seconds=time.monotonic() - started_monotonic,
                    feedback=(sample_feedback, measurement_feedback),
                    export_path=export_path,
                )
            )

        finalization: list[Feedback] = []
        if disconnect:
            finalization.append(self.send_command(2))

        return SpectrumSeriesResult(
            preparation_feedback=tuple(preparation),
            runs=tuple(run_results),
            finalization_feedback=tuple(finalization),
        )
