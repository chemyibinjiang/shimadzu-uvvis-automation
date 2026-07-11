"""Client for the LabSolutions UV-Vis automatic-control text protocol."""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping, TypeAlias

ParameterValue: TypeAlias = str | int | float | bool | Path

MODE_FILE_NAMES: Final[dict[str, tuple[str, str]]] = {
    "spectrum": ("SPC_CMD.txt", "SPC_RES.txt"),
    "quantitation": ("QUA_CMD.txt", "QUA_RES.txt"),
    "photometric": ("PHO_CMD.txt", "PHO_RES.txt"),
    "time_course": ("TMC_CMD.txt", "TMC_RES.txt"),
}


class LabSolutionsError(RuntimeError):
    """Base class for LabSolutions communication failures."""


class LabSolutionsBusyError(LabSolutionsError):
    """Raised when an unconsumed command already exists."""


class LabSolutionsProtocolError(LabSolutionsError):
    """Raised when a command or feedback file is malformed."""


class LabSolutionsTimeoutError(LabSolutionsError):
    """Raised when LabSolutions or an export file does not arrive in time."""


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
        timeout: float = 120.0,
        poll_interval: float = 0.2,
        encoding: str = "utf-8",
    ) -> None:
        if mode not in MODE_FILE_NAMES:
            choices = ", ".join(sorted(MODE_FILE_NAMES))
            raise ValueError(f"Unsupported mode {mode!r}; choose one of: {choices}")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")

        self.command_dir = Path(command_dir)
        self.mode = mode
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.encoding = encoding
        command_name, feedback_name = MODE_FILE_NAMES[mode]
        self.command_path = self.command_dir / command_name
        self.feedback_path = self.command_dir / feedback_name
        self._lock = threading.Lock()

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

        with self._lock:
            if self.command_path.exists():
                raise LabSolutionsBusyError(
                    f"A command is already waiting at {self.command_path}. "
                    "Do not overwrite it until its state has been checked."
                )

            try:
                self.feedback_path.unlink(missing_ok=True)
            except OSError as exc:
                raise LabSolutionsBusyError(
                    f"Cannot clear stale feedback file {self.feedback_path}: {exc}"
                ) from exc

            temporary_path = self.command_dir / (
                f".{self.command_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary_path.write_text(
                    payload, encoding=self.encoding, errors="strict", newline=""
                )
                os.replace(temporary_path, self.command_path)
            finally:
                temporary_path.unlink(missing_ok=True)

            feedback = self._wait_for_feedback(command, wait_timeout)
            if raise_on_error:
                feedback.raise_for_error()
            return feedback

    def _wait_for_feedback(self, command: int, timeout: float) -> Feedback:
        deadline = time.monotonic() + timeout
        last_protocol_error: LabSolutionsProtocolError | None = None
        read_encoding = (
            "utf-8-sig" if self.encoding.lower().replace("_", "-") == "utf-8" else self.encoding
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

    def run_spectrum(
        self,
        *,
        method_file: str | Path,
        sample_name: str,
        sample_id: str | None = None,
        data_file: str | Path | None = None,
        measurement_mode: int = 1,
        correction: Mapping[str, ParameterValue] | None = None,
        connect: bool = False,
        disconnect: bool = False,
        export_dir: str | Path | None = None,
        export_pattern: str = "*.csv",
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
        feedback.append(self.send_command(111, MeasurementMode=measurement_mode))

        export_path: Path | None = None
        if export_dir is not None:
            export_path = self.wait_for_export(
                export_dir,
                pattern=export_pattern,
                since=measurement_started_at,
                stable_seconds=stable_seconds,
            )

        if disconnect:
            feedback.append(self.send_command(2))

        return SpectrumRunResult(tuple(feedback), export_path)
