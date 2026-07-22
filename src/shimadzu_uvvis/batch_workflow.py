"""Persistent state machine for manually exchanged UV-Vis sample batches."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .audit import write_json_atomic
from .client import Feedback, LabSolutionsClient, LabSolutionsCommandError
from .configuration import METHOD_FILE_EXTENSIONS, ControlSettings, MeasurementMode
from .locking import FileLockTimeoutError, InterProcessFileLock
from .results import (
    PhotometricResultError,
    SpectrumResultError,
    build_photometric_result,
    build_spectrum_result,
    normalize_photometric_data_file,
    normalize_spectrum_data_file,
)
from .runtime_manager import (
    LabSolutionsRuntimeManager,
    RuntimeReady,
    settings_for_mode,
)


_BATCH_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_WAITING_STATES = {"WAITING_FOR_BLANK", "WAITING_FOR_SAMPLE"}
_TERMINAL_STATES = {"COMPLETED", "ABORTED", "FAILED"}


class SpectrumBatchError(RuntimeError):
    """Raised when a batch action violates the persisted workflow state."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _file_metadata(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "sha256": _sha256(path),
        "modified_at_utc": datetime.fromtimestamp(
            stat.st_mtime, timezone.utc
        ).isoformat(timespec="milliseconds"),
    }


def _feedback_record(feedback: Feedback) -> dict[str, object]:
    return {
        "command": feedback.command,
        "return_code": feedback.return_code,
        "error": feedback.error,
        "fields": dict(feedback.fields),
        "completed_at_utc": _utc_now(),
    }


def _export_pattern(template: str, sample_id: str, sample_name: str) -> str:
    sample_token = re.sub(r"[^A-Za-z0-9_.-]+", "_", sample_name).strip("._")
    try:
        pattern = template.format(
            sample_id=sample_id,
            sample_name=sample_token,
        )
    except (KeyError, ValueError) as exc:
        raise SpectrumBatchError(
            "export pattern may only use {sample_id} and {sample_name} placeholders"
        ) from exc
    if not pattern or Path(pattern).name != pattern:
        raise SpectrumBatchError("export pattern must be a filename glob, not a path")
    return pattern


class SpectrumBatchController:
    """Execute one Spectrum or Photometric batch across guarded MCP calls."""

    def __init__(
        self,
        settings: ControlSettings,
        *,
        client_factory: Callable[[], LabSolutionsClient] | None = None,
        runtime_manager_factory: (
            Callable[[], LabSolutionsRuntimeManager] | None
        ) = None,
    ) -> None:
        if settings.data_dir is None:
            raise SpectrumBatchError("spectrum.data_dir must be configured")
        self.settings = settings
        self.data_dir = settings.data_dir
        self._client_factory = client_factory
        self._runtime_manager_factory = runtime_manager_factory
        self.controller_lock_path = self.data_dir / ".spectrum_batch_controller.lock"
        self.active_batch_path = self.data_dir / ".active_spectrum_batch.json"
        self.baseline_path = self.data_dir / ".spectrum_baseline.json"

    def _default_client(self, mode: MeasurementMode) -> LabSolutionsClient:
        settings = settings_for_mode(self.settings, mode)
        return LabSolutionsClient(
            command_dir=settings.command_dir,
            mode=mode,
            timeout=settings.timeout_seconds,
            poll_interval=settings.poll_interval_seconds,
            lock_timeout=settings.lock_timeout_seconds,
            encoding=settings.encoding,
            audit_dir=settings.audit_dir,
        )

    def _client(self, mode: MeasurementMode) -> LabSolutionsClient:
        if self._client_factory is not None:
            return self._client_factory()
        return self._default_client(mode)

    def _runtime_manager(self, mode: MeasurementMode) -> LabSolutionsRuntimeManager:
        if self._runtime_manager_factory is not None:
            return self._runtime_manager_factory()
        return LabSolutionsRuntimeManager(settings_for_mode(self.settings, mode))

    @staticmethod
    def _runtime_record(ready: RuntimeReady) -> dict[str, object]:
        return ready.as_dict()

    def _lock(self) -> InterProcessFileLock:
        return InterProcessFileLock(
            self.controller_lock_path,
            timeout=self.settings.lock_timeout_seconds,
            poll_interval=min(self.settings.poll_interval_seconds, 0.1),
        )

    def _batch_id(self, batch_id: str) -> str:
        normalized = batch_id.strip() if isinstance(batch_id, str) else ""
        if not _BATCH_ID_PATTERN.fullmatch(normalized):
            raise SpectrumBatchError(
                "batch_id must contain only ASCII letters, digits, underscores, "
                "or hyphens"
            )
        return normalized

    def _batch_directory(self, batch_id: str) -> Path:
        return self.data_dir / self._batch_id(batch_id)

    def _manifest_path(self, batch_id: str) -> Path:
        return self._batch_directory(batch_id) / "batch-manifest.json"

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SpectrumBatchError(f"batch record does not exist: {path}") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SpectrumBatchError(f"cannot read batch record {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SpectrumBatchError(f"batch record is not a JSON object: {path}")
        return payload

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at_utc"] = _utc_now()
        write_json_atomic(
            self._manifest_path(str(manifest["batch_id"])),
            manifest,
        )

    def _set_active(self, manifest: Mapping[str, Any]) -> None:
        write_json_atomic(
            self.active_batch_path,
            {
                "schema_version": 1,
                "batch_id": manifest["batch_id"],
                "manifest_path": str(self._manifest_path(str(manifest["batch_id"]))),
                "state": manifest["state"],
                "updated_at_utc": _utc_now(),
            },
        )

    def _clear_active(self, batch_id: str) -> None:
        if not self.active_batch_path.exists():
            return
        active = self._read_json(self.active_batch_path)
        if active.get("batch_id") == batch_id:
            self.active_batch_path.unlink(missing_ok=True)

    def _require_active(self, manifest: Mapping[str, Any]) -> None:
        active = self._read_json(self.active_batch_path)
        if active.get("batch_id") != manifest.get("batch_id"):
            raise SpectrumBatchError(
                f"batch {manifest.get('batch_id')!r} is not the active UV-Vis batch"
            )

    def _ensure_no_active_batch(self) -> None:
        if not self.active_batch_path.exists():
            return
        active = self._read_json(self.active_batch_path)
        active_id = active.get("batch_id")
        if not isinstance(active_id, str):
            raise SpectrumBatchError(
                f"invalid active batch marker: {self.active_batch_path}"
            )
        manifest_path = self._manifest_path(active_id)
        if manifest_path.is_file():
            manifest = self._read_json(manifest_path)
            if manifest.get("state") in _TERMINAL_STATES:
                self.active_batch_path.unlink(missing_ok=True)
                return
        raise SpectrumBatchError(
            f"UV-Vis batch {active_id!r} is already active; finish or abort it first"
        )

    def _validate_methods(self, manifest: Mapping[str, Any]) -> list[Path]:
        methods = [Path(str(value)) for value in manifest.get("method_files", [])]
        hashes = list(manifest.get("method_sha256s", []))
        if not methods and manifest.get("method_file"):
            methods = [Path(str(manifest["method_file"]))]
            hashes = [manifest.get("method_sha256")]
        if len(methods) != len(hashes) or not methods:
            raise SpectrumBatchError("batch method records are incomplete")
        for method, expected_hash in zip(methods, hashes, strict=True):
            if not method.is_file():
                raise SpectrumBatchError(
                    f"generated method file does not exist: {method}"
                )
            if _sha256(method) != expected_hash:
                raise SpectrumBatchError(
                    "generated method changed after the batch started; start a new batch"
                )
        return methods

    def _record_failure(
        self,
        manifest: dict[str, Any],
        *,
        operation: str,
        error: Exception,
        terminal_rejection: bool = False,
    ) -> None:
        manifest["state"] = "FAILED" if terminal_rejection else "RECOVERY_REQUIRED"
        manifest["last_error"] = {
            "operation": operation,
            "type": type(error).__name__,
            "message": str(error),
            "at_utc": _utc_now(),
        }
        self._write_manifest(manifest)
        if terminal_rejection:
            self._clear_active(str(manifest["batch_id"]))
        else:
            self._set_active(manifest)

    def _append_feedback(
        self,
        manifest: dict[str, Any],
        feedback: Feedback,
        *,
        phase: str,
    ) -> None:
        record = _feedback_record(feedback)
        record["phase"] = phase
        manifest["commands"].append(record)
        self._write_manifest(manifest)

    def _validate_start_plan(
        self, plan: Mapping[str, Any]
    ) -> tuple[str, MeasurementMode, list[Path]]:
        if plan.get("tool") != "plan_uvvis_sample_batch":
            raise SpectrumBatchError("start requires a plan_uvvis_sample_batch result")
        mode = plan.get("mode")
        if mode not in {"spectrum", "photometric"}:
            raise SpectrumBatchError(
                "execution currently supports Spectrum and Photometric batches"
            )
        if plan.get("status") != "planned":
            raise SpectrumBatchError(
                f"batch plan is not executable: status={plan.get('status')!r}"
            )
        readiness = plan.get("execution_readiness", {})
        if not isinstance(readiness, Mapping) or readiness.get("ready") is not True:
            reasons = (
                readiness.get("blocking_reasons", [])
                if isinstance(readiness, Mapping)
                else []
            )
            raise SpectrumBatchError(
                "batch plan execution paths are not ready: "
                + (", ".join(str(reason) for reason in reasons) or "unknown reason")
            )
        batch_id = self._batch_id(str(plan.get("batch_id", "")))
        expected_directory = self._batch_directory(batch_id).resolve()
        planned_directory = Path(str(plan.get("batch_directory", ""))).resolve()
        if planned_directory != expected_directory:
            raise SpectrumBatchError("batch plan data directory does not match config")

        method_generation = plan.get("measurement_plan", {}).get(
            "method_generation", {}
        )
        raw_methods = method_generation.get("target_method_files") or [
            method_generation.get("target_method_file", "")
        ]
        methods = [Path(str(value)).resolve() for value in raw_methods]
        generated_root = self.settings.generated_method_dir.resolve()
        expected_extensions = METHOD_FILE_EXTENSIONS[mode]
        for method in methods:
            if (
                method.parent != generated_root
                or method.suffix.lower() not in expected_extensions
            ):
                raise SpectrumBatchError(
                    f"{mode} execution methods must be in generated_method_dir and use "
                    f"one of: {', '.join(expected_extensions)}"
                )
            if not method.is_file():
                raise SpectrumBatchError(
                    f"generated method file does not exist: {method}"
                )
        return batch_id, mode, methods

    def _reusable_baseline(
        self,
        *,
        mode: MeasurementMode,
        methods: list[Path],
        method_sha256s: list[str],
        reference_name: str,
    ) -> dict[str, Any]:
        baseline = self._read_json(self.baseline_path)
        expected = {
            "mode": mode,
            "method_files": [str(method) for method in methods],
            "method_sha256s": method_sha256s,
            "reference_name": reference_name,
        }
        mismatches = [
            name for name, value in expected.items() if baseline.get(name) != value
        ]
        if mismatches:
            raise SpectrumBatchError(
                "stored baseline cannot be reused because these fields changed: "
                + ", ".join(mismatches)
            )
        return baseline

    def start(
        self, plan: Mapping[str, Any], *, execution_confirmed: bool
    ) -> dict[str, Any]:
        """Create a batch, verify LabSolutions, and enter the placement gate."""

        if execution_confirmed is not True:
            raise SpectrumBatchError("execution_confirmed must be true")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock():
                self._ensure_no_active_batch()
                batch_id, mode, methods = self._validate_start_plan(plan)
                runtime_manager = self._runtime_manager(mode)
                runtime_ready = runtime_manager.ensure_ready(allow_reconfigure=True)
                batch_directory = self._batch_directory(batch_id)
                if batch_directory.exists():
                    raise SpectrumBatchError(
                        f"batch directory already exists: {batch_directory}"
                    )

                method_sha256s = [_sha256(method) for method in methods]
                baseline_policy = str(plan["batch_preparation"]["policy"])
                reference_name = str(plan["reference"]["name"])
                reused_baseline: dict[str, Any] | None = None
                if baseline_policy == "reuse_valid":
                    reused_baseline = self._reusable_baseline(
                        mode=mode,
                        methods=methods,
                        method_sha256s=method_sha256s,
                        reference_name=reference_name,
                    )

                batch_directory.mkdir(parents=False)
                preparation: dict[str, Any] | None = None
                if mode == "photometric":
                    preparation_directory = batch_directory / "preparation"
                    preparation_directory.mkdir(parents=False)
                    preparation = {
                        "method_file": str(methods[0]),
                        "data_file": str(
                            preparation_directory / "baseline_preparation.vphd"
                        ),
                        "status": "PENDING",
                    }
                samples: list[dict[str, Any]] = []
                for planned_sample in plan["samples"]:
                    paths = dict(planned_sample["paths"])
                    for key in ("raw_directory", "export_directory", "plot_directory"):
                        Path(str(paths[key])).mkdir(parents=True, exist_ok=False)
                    samples.append(
                        {
                            "sequence_number": planned_sample["sequence_number"],
                            "sample_name": planned_sample["sample_name"],
                            "source_sample_id": planned_sample["source_sample_id"],
                            "sample_id": planned_sample["sample_id"],
                            "status": "PENDING",
                            "paths": paths,
                            "segments": planned_sample.get("segments", []),
                        }
                    )

                now = _utc_now()
                manifest: dict[str, Any] = {
                    "schema_version": 1,
                    "batch_id": batch_id,
                    "mode": mode,
                    "state": "STARTING",
                    "created_at_utc": now,
                    "updated_at_utc": now,
                    "reference_name": reference_name,
                    "baseline": {
                        "policy": baseline_policy,
                        "status": (
                            "REUSED" if reused_baseline is not None else "PENDING"
                        ),
                        "record": reused_baseline,
                    },
                    "preparation": preparation,
                    "method_file": str(methods[0]),
                    "method_sha256": method_sha256s[0],
                    "method_files": [str(method) for method in methods],
                    "method_sha256s": method_sha256s,
                    "request": plan["measurement_plan"]["request"],
                    "next_sample_index": 0,
                    "samples": samples,
                    "runtime": self._runtime_record(runtime_ready),
                    "commands": [
                        {
                            **_feedback_record(runtime_ready.feedback),
                            "phase": "runtime_ready:start",
                        }
                    ],
                    "events": [
                        {"type": "batch_created", "at_utc": now},
                        {"type": "runtime_ready", "at_utc": now},
                    ],
                    "last_error": None,
                }
                self._write_manifest(manifest)
                self._set_active(manifest)

                client = self._client(mode)
                try:
                    with client.workflow_session():
                        if self.settings.connect_before_run:
                            try:
                                connect_feedback = client.send_command(1)
                            except LabSolutionsCommandError as exc:
                                if exc.feedback.return_code != -3002:
                                    raise
                                connect_feedback = exc.feedback
                                connect_phase = "start:already_connected"
                                manifest["events"].append(
                                    {
                                        "type": "instrument_already_connected",
                                        "at_utc": _utc_now(),
                                    }
                                )
                            else:
                                connect_phase = "start"
                            self._append_feedback(
                                manifest,
                                connect_feedback,
                                phase=connect_phase,
                            )
                            if (
                                baseline_policy == "reuse_valid"
                                and connect_feedback.return_code == 0
                            ):
                                baseline_policy = "new"
                                manifest["baseline"] = {
                                    "policy": "new",
                                    "status": "PENDING",
                                    "record": None,
                                    "requested_policy": "reuse_valid",
                                    "reuse_rejected_reason": (
                                        "instrument_connection_reestablished"
                                    ),
                                }
                                manifest["events"].append(
                                    {
                                        "type": (
                                            "baseline_reuse_rejected_new_connection"
                                        ),
                                        "at_utc": _utc_now(),
                                    }
                                )
                                self._write_manifest(manifest)
                        if mode == "spectrum":
                            self._append_feedback(
                                manifest,
                                client.send_command(100, ParameterFileName=methods[0]),
                                phase="start",
                            )
                        else:
                            assert preparation is not None
                            self._append_feedback(
                                manifest,
                                client.send_command(
                                    300,
                                    ParameterFileName=methods[0],
                                    DataFileName=Path(str(preparation["data_file"])),
                                ),
                                phase="start:photometric_preparation",
                            )
                            preparation["status"] = "OPEN"
                            self._write_manifest(manifest)

                    prompt_dismissed = (
                        runtime_manager.dismiss_parameter_change_baseline_prompt(
                            wait_seconds=min(
                                2.0,
                                self.settings.runtime.ui_timeout_seconds,
                            )
                        )
                    )
                except Exception as exc:
                    self._record_failure(
                        manifest,
                        operation="start",
                        error=exc,
                        terminal_rejection=isinstance(exc, LabSolutionsCommandError),
                    )
                    raise

                if prompt_dismissed:
                    manifest["events"].append(
                        {
                            "type": "parameter_change_baseline_prompt_declined",
                            "at_utc": _utc_now(),
                        }
                    )

                manifest["state"] = (
                    "WAITING_FOR_SAMPLE"
                    if baseline_policy == "reuse_valid"
                    else "WAITING_FOR_BLANK"
                )
                manifest["events"].append(
                    {"type": "methods_ready", "at_utc": _utc_now()}
                )
                self._write_manifest(manifest)
                self._set_active(manifest)
                return self._status(manifest)
        except FileLockTimeoutError as exc:
            raise SpectrumBatchError(
                "another process is changing the UV-Vis batch state"
            ) from exc

    def correct_baseline(
        self,
        batch_id: str,
        *,
        blank_loaded_confirmed: bool,
    ) -> dict[str, Any]:
        """Run Command 21 after the operator confirms blank placement."""

        if blank_loaded_confirmed is not True:
            raise SpectrumBatchError("blank_loaded_confirmed must be true")
        try:
            with self._lock():
                manifest = self._read_json(self._manifest_path(batch_id))
                self._require_active(manifest)
                if manifest.get("state") != "WAITING_FOR_BLANK":
                    raise SpectrumBatchError(
                        "baseline correction requires state WAITING_FOR_BLANK; "
                        f"current state is {manifest.get('state')}"
                    )
                methods = self._validate_methods(manifest)
                mode = str(manifest["mode"])
                runtime_ready = self._runtime_manager(mode).ensure_ready(
                    allow_reconfigure=False
                )
                manifest["runtime"] = self._runtime_record(runtime_ready)
                self._append_feedback(
                    manifest,
                    runtime_ready.feedback,
                    phase="runtime_ready:baseline_correction",
                )
                manifest["state"] = "BASELINE_CORRECTING"
                manifest["events"].append(
                    {"type": "blank_loaded_confirmed", "at_utc": _utc_now()}
                )
                self._write_manifest(manifest)
                self._set_active(manifest)

                client = self._client(mode)
                correction_completed = False
                try:
                    with client.workflow_session():
                        feedback = client.send_command(21, CorrectionType=1)
                        correction_completed = True
                        self._append_feedback(
                            manifest, feedback, phase="baseline_correction"
                        )
                        if mode == "photometric":
                            self._append_feedback(
                                manifest,
                                client.send_command(321),
                                phase="baseline_preparation_close",
                            )
                            if isinstance(manifest.get("preparation"), dict):
                                manifest["preparation"]["status"] = "CLOSED"
                                self._write_manifest(manifest)
                except Exception as exc:
                    self._record_failure(
                        manifest,
                        operation="correct_baseline",
                        error=exc,
                        terminal_rejection=(
                            isinstance(exc, LabSolutionsCommandError)
                            and not correction_completed
                        ),
                    )
                    raise

                baseline = {
                    "schema_version": 1,
                    "mode": mode,
                    "method_file": str(methods[0]),
                    "method_sha256": manifest["method_sha256s"][0],
                    "method_files": [str(method) for method in methods],
                    "method_sha256s": manifest["method_sha256s"],
                    "reference_name": manifest["reference_name"],
                    "correction_type": 1,
                    "completed_at_utc": _utc_now(),
                }
                write_json_atomic(self.baseline_path, baseline)
                manifest["baseline"] = {
                    "policy": "new",
                    "status": "COMPLETED",
                    "record": baseline,
                }
                manifest["state"] = "WAITING_FOR_SAMPLE"
                manifest["events"].append(
                    {"type": "baseline_completed", "at_utc": _utc_now()}
                )
                self._write_manifest(manifest)
                self._set_active(manifest)
                return self._status(manifest)
        except FileLockTimeoutError as exc:
            raise SpectrumBatchError(
                "another process is changing the UV-Vis batch state"
            ) from exc

    def _wait_for_stable_file(self, path: Path) -> Path:
        deadline = time.monotonic() + self.settings.export_timeout_seconds
        previous: tuple[int, int] | None = None
        stable_since: float | None = None
        while time.monotonic() < deadline:
            try:
                stat = path.stat()
            except OSError:
                previous = None
                stable_since = None
                time.sleep(self.settings.poll_interval_seconds)
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            now = time.monotonic()
            if stat.st_size <= 0 or signature != previous:
                previous = signature
                stable_since = now
            elif stable_since is not None and (
                now - stable_since >= self.settings.stable_seconds
            ):
                return path
            time.sleep(self.settings.poll_interval_seconds)
        raise SpectrumBatchError(
            f"timed out waiting for stable raw UV-Vis data file: {path}"
        )

    def _archive_export(self, source: Path, destination_directory: Path) -> Path:
        destination = destination_directory / source.name
        if destination.exists():
            raise SpectrumBatchError(f"archived export already exists: {destination}")
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _measure_spectrum_sample(
        self,
        *,
        client: LabSolutionsClient,
        manifest: dict[str, Any],
        sample: dict[str, Any],
        sample_id: str,
    ) -> list[dict[str, Any]]:
        raw_path = Path(str(sample["paths"]["raw_data_file"]))
        started_at = time.time()
        feedback = client.send_command(
            110,
            DataFileName=raw_path,
            SampleName=str(sample["sample_name"]),
            SampleID=sample_id,
        )
        self._append_feedback(manifest, feedback, phase=f"sample:{sample_id}")
        feedback = client.send_command(
            111,
            MeasurementMode=self.settings.measurement_mode,
            Discharge=self.settings.discharge_after_measurement,
        )
        self._append_feedback(manifest, feedback, phase=f"sample:{sample_id}")
        self._wait_for_stable_file(raw_path)
        request = manifest["request"]
        normalized = (
            Path(str(sample["paths"]["export_directory"])) / f"{sample_id}.csv"
        )
        try:
            normalize_spectrum_data_file(
                data_file=raw_path,
                lower_nm=float(request["lower_nm"]),
                upper_nm=float(request["upper_nm"]),
                step_nm=float(request["step_nm"]),
                csv_file=normalized,
            )
            archived = normalized
            export_source = raw_path
            result_source_kind = "labsolutions_vspd"
        except SpectrumResultError:
            assert self.settings.export_dir is not None
            export_source = client.wait_for_export(
                self.settings.export_dir,
                pattern=_export_pattern(
                    self.settings.export_pattern,
                    sample_id,
                    str(sample["sample_name"]),
                ),
                since=started_at,
                timeout=self.settings.export_timeout_seconds,
                stable_seconds=self.settings.stable_seconds,
            )
            archived = self._archive_export(
                export_source, Path(str(sample["paths"]["export_directory"]))
            )
            result_source_kind = "labsolutions_export"
        return [
            {
                "segment_index": 1,
                "sample_id": sample_id,
                "raw_data": _file_metadata(raw_path),
                "export": _file_metadata(archived),
                "export_source": str(export_source),
                "result_source_kind": result_source_kind,
            }
        ]

    def _measure_photometric_sample(
        self,
        *,
        client: LabSolutionsClient,
        manifest: dict[str, Any],
        sample: dict[str, Any],
        methods: list[Path],
    ) -> list[dict[str, Any]]:
        segments = list(sample.get("segments", []))
        if len(segments) != len(methods):
            raise SpectrumBatchError(
                "Photometric sample segments do not match generated methods"
            )
        assert self.settings.export_dir is not None
        records = list(sample.get("completed_segments", []))
        if len(records) > len(segments) or any(
            record.get("segment_index") != index
            for index, record in enumerate(records, start=1)
        ):
            raise SpectrumBatchError("completed Photometric segment records are invalid")
        for index, (segment, method) in enumerate(
            zip(segments, methods, strict=True), start=1
        ):
            if index <= len(records):
                continue
            raw_path = Path(str(segment["raw_data_file"]))
            segment_sample_id = str(segment["sample_id"])
            started_at = time.time()
            phase = f"sample:{sample['sample_id']}:segment:{index}"
            self._append_feedback(
                manifest,
                client.send_command(
                    300,
                    ParameterFileName=method,
                    DataFileName=raw_path,
                ),
                phase=phase,
            )
            self._append_feedback(
                manifest,
                client.send_command(
                    310,
                    SampleName=str(sample["sample_name"]),
                    SampleID=segment_sample_id,
                    SampleType=1,
                ),
                phase=phase,
            )
            self._append_feedback(
                manifest,
                client.send_command(
                    311,
                    MeasurementMode=self.settings.measurement_mode,
                    Discharge=self.settings.discharge_after_measurement,
                ),
                phase=phase,
            )
            self._append_feedback(manifest, client.send_command(320), phase=phase)
            self._append_feedback(manifest, client.send_command(321), phase=phase)
            self._wait_for_stable_file(raw_path)
            normalized = (
                Path(str(sample["paths"]["export_directory"]))
                / f"{segment_sample_id}.csv"
            )
            try:
                normalize_photometric_data_file(
                    data_file=raw_path,
                    expected_wavelengths_nm=list(segment["wavelengths_nm"]),
                    csv_file=normalized,
                )
                archived = normalized
                export_source = raw_path
                result_source_kind = "labsolutions_vphd"
            except PhotometricResultError:
                export_source = client.wait_for_export(
                    self.settings.export_dir,
                    pattern=_export_pattern(
                        self.settings.export_pattern,
                        segment_sample_id,
                        str(sample["sample_name"]),
                    ),
                    since=started_at,
                    timeout=self.settings.export_timeout_seconds,
                    stable_seconds=self.settings.stable_seconds,
                )
                archived = self._archive_export(
                    export_source,
                    Path(str(sample["paths"]["export_directory"])),
                )
                result_source_kind = "labsolutions_export"
            records.append(
                {
                    "segment_index": index,
                    "sample_id": segment_sample_id,
                    "wavelengths_nm": segment["wavelengths_nm"],
                    "method_file": str(method),
                    "raw_data": _file_metadata(raw_path),
                    "export": _file_metadata(archived),
                    "export_source": str(export_source),
                    "result_source_kind": result_source_kind,
                }
            )
            sample["completed_segments"] = records
            self._write_manifest(manifest)
        return records

    def _build_sample_result(
        self,
        *,
        manifest: dict[str, Any],
        sample: dict[str, Any],
        segment_records: list[dict[str, Any]],
        mode: str,
        sample_id: str,
    ) -> None:
        sample["segments"] = segment_records
        if mode == "spectrum":
            sample["raw_data"] = segment_records[0]["raw_data"]
            sample["export"] = segment_records[0]["export"]
            sample["export_source"] = segment_records[0]["export_source"]
            request = manifest["request"]
            sample["result"] = build_spectrum_result(
                export_file=Path(str(segment_records[0]["export"]["path"])),
                lower_nm=float(request["lower_nm"]),
                upper_nm=float(request["upper_nm"]),
                step_nm=float(request["step_nm"]),
                csv_file=Path(str(sample["paths"]["merged_csv_file"])),
                json_file=Path(str(sample["paths"]["result_json_file"])),
                png_file=Path(str(sample["paths"]["plot_file"])),
                batch_id=str(manifest["batch_id"]),
                sample_id=sample_id,
                publish_root=self.settings.result_dir,
            )
            return

        sample["raw_data"] = [segment["raw_data"] for segment in segment_records]
        sample["export"] = [segment["export"] for segment in segment_records]
        sample["export_source"] = [
            segment["export_source"] for segment in segment_records
        ]
        sample["result"] = build_photometric_result(
            export_files=[
                Path(str(segment["export"]["path"])) for segment in segment_records
            ],
            expected_segments=[
                list(segment["wavelengths_nm"]) for segment in segment_records
            ],
            csv_file=Path(str(sample["paths"]["merged_csv_file"])),
            json_file=Path(str(sample["paths"]["result_json_file"])),
            png_file=Path(str(sample["paths"]["plot_file"])),
            batch_id=str(manifest["batch_id"]),
            sample_id=sample_id,
            publish_root=self.settings.result_dir,
        )

    def _complete_sample(
        self,
        *,
        manifest: dict[str, Any],
        sample: dict[str, Any],
        index: int,
        sample_id: str,
        event_type: str = "sample_completed",
    ) -> dict[str, Any]:
        sample["status"] = "COMPLETED"
        sample["completed_at_utc"] = _utc_now()
        write_json_atomic(
            Path(str(sample["paths"]["manifest_file"])),
            {
                "schema_version": 1,
                "batch_id": manifest["batch_id"],
                "method_file": manifest["method_file"],
                "method_sha256": manifest["method_sha256"],
                "method_files": manifest["method_files"],
                "method_sha256s": manifest["method_sha256s"],
                "baseline": manifest["baseline"],
                "sample": sample,
            },
        )
        manifest["next_sample_index"] = index + 1
        manifest["events"].append(
            {
                "type": event_type,
                "sample_id": sample_id,
                "at_utc": _utc_now(),
            }
        )

        if manifest["next_sample_index"] < len(manifest["samples"]):
            manifest["state"] = "WAITING_FOR_SAMPLE"
            self._write_manifest(manifest)
            self._set_active(manifest)
            return self._status(manifest)

        manifest["state"] = "FINALIZING"
        self._write_manifest(manifest)
        self._set_active(manifest)
        manifest["state"] = "COMPLETED"
        manifest["completed_at_utc"] = _utc_now()
        self._write_manifest(manifest)
        self._clear_active(str(manifest["batch_id"]))
        return self._status(manifest)

    def measure_next(
        self,
        batch_id: str,
        *,
        sample_id: str,
        sample_loaded_confirmed: bool,
    ) -> dict[str, Any]:
        """Measure exactly the next planned sample and archive its outputs."""

        if sample_loaded_confirmed is not True:
            raise SpectrumBatchError("sample_loaded_confirmed must be true")
        try:
            with self._lock():
                manifest = self._read_json(self._manifest_path(batch_id))
                self._require_active(manifest)
                if manifest.get("state") != "WAITING_FOR_SAMPLE":
                    raise SpectrumBatchError(
                        "sample measurement requires state WAITING_FOR_SAMPLE; "
                        f"current state is {manifest.get('state')}"
                    )
                methods = self._validate_methods(manifest)
                mode = str(manifest["mode"])
                index = int(manifest["next_sample_index"])
                samples = manifest["samples"]
                if index >= len(samples):
                    raise SpectrumBatchError("batch has no remaining samples")
                sample = samples[index]
                expected_id = str(sample["sample_id"])
                if sample_id != expected_id:
                    raise SpectrumBatchError(
                        f"next sample is {expected_id!r}, not {sample_id!r}"
                    )

                raw_paths = [
                    Path(str(value))
                    for value in sample["paths"].get(
                        "raw_data_files", [sample["paths"]["raw_data_file"]]
                    )
                ]
                completed_segment_count = (
                    len(sample.get("completed_segments", []))
                    if mode == "photometric"
                    else 0
                )
                existing_raw = [
                    path
                    for path in raw_paths[completed_segment_count:]
                    if path.exists()
                ]
                if existing_raw:
                    raise SpectrumBatchError(
                        "raw data file already exists; refusing overwrite: "
                        + ", ".join(str(path) for path in existing_raw)
                    )
                if self.settings.export_dir is None:
                    raise SpectrumBatchError("export.directory must be configured")

                runtime_ready = self._runtime_manager(mode).ensure_ready(
                    allow_reconfigure=False
                )
                manifest["runtime"] = self._runtime_record(runtime_ready)
                self._append_feedback(
                    manifest,
                    runtime_ready.feedback,
                    phase=f"runtime_ready:sample:{expected_id}",
                )

                sample["status"] = "MEASURING"
                sample.setdefault("started_at_utc", _utc_now())
                manifest["state"] = "MEASURING_SAMPLE"
                manifest["events"].append(
                    {
                        "type": "sample_loaded_confirmed",
                        "sample_id": expected_id,
                        "at_utc": _utc_now(),
                    }
                )
                self._write_manifest(manifest)
                self._set_active(manifest)

                client = self._client(mode)
                try:
                    with client.workflow_session():
                        if mode == "spectrum":
                            segment_records = self._measure_spectrum_sample(
                                client=client,
                                manifest=manifest,
                                sample=sample,
                                sample_id=expected_id,
                            )
                        elif mode == "photometric":
                            segment_records = self._measure_photometric_sample(
                                client=client,
                                manifest=manifest,
                                sample=sample,
                                methods=methods,
                            )
                        else:
                            raise SpectrumBatchError(f"unsupported batch mode: {mode}")
                        if (
                            index + 1 == len(samples)
                            and self.settings.disconnect_after_run
                        ):
                            self._append_feedback(
                                manifest,
                                client.send_command(2),
                                phase="finalize",
                            )
                except Exception as exc:
                    sample["status"] = (
                        "FAILED"
                        if isinstance(exc, LabSolutionsCommandError)
                        else "RECOVERY_REQUIRED"
                    )
                    self._record_failure(
                        manifest, operation=f"measure:{expected_id}", error=exc
                    )
                    raise

                try:
                    self._build_sample_result(
                        manifest=manifest,
                        sample=sample,
                        segment_records=segment_records,
                        mode=mode,
                        sample_id=expected_id,
                    )
                except Exception as exc:
                    sample["status"] = "RECOVERY_REQUIRED"
                    self._record_failure(
                        manifest,
                        operation=f"process_result:{expected_id}",
                        error=exc,
                    )
                    raise

                return self._complete_sample(
                    manifest=manifest,
                    sample=sample,
                    index=index,
                    sample_id=expected_id,
                )
        except FileLockTimeoutError as exc:
            raise SpectrumBatchError(
                "another process is changing the UV-Vis batch state"
            ) from exc

    def recover_spectrum_result(self, batch_id: str) -> dict[str, Any]:
        """Complete a measured Spectrum sample from its saved .vspd without remeasure."""

        try:
            with self._lock():
                manifest = self._read_json(self._manifest_path(batch_id))
                self._require_active(manifest)
                if manifest.get("state") != "RECOVERY_REQUIRED":
                    raise SpectrumBatchError(
                        "Spectrum result recovery requires state RECOVERY_REQUIRED; "
                        f"current state is {manifest.get('state')}"
                    )
                if manifest.get("mode") != "spectrum":
                    raise SpectrumBatchError(
                        "direct .vspd result recovery is only supported for Spectrum"
                    )
                self._validate_methods(manifest)
                index = int(manifest["next_sample_index"])
                samples = manifest["samples"]
                if index >= len(samples):
                    raise SpectrumBatchError("batch has no sample awaiting recovery")
                sample = samples[index]
                sample_id = str(sample["sample_id"])
                last_error = manifest.get("last_error")
                expected_operation = f"measure:{sample_id}"
                if (
                    not isinstance(last_error, Mapping)
                    or last_error.get("operation") != expected_operation
                    or last_error.get("type") != "LabSolutionsTimeoutError"
                    or "waiting for a stable export" not in str(
                        last_error.get("message", "")
                    )
                ):
                    raise SpectrumBatchError(
                        "result recovery is allowed only after a confirmed Spectrum "
                        "measurement timed out waiting for its automatic export"
                    )
                command_completed = any(
                    command.get("command") == 111
                    and command.get("return_code") == 0
                    and command.get("phase") == f"sample:{sample_id}"
                    for command in manifest.get("commands", [])
                    if isinstance(command, Mapping)
                )
                if not command_completed:
                    raise SpectrumBatchError(
                        "cannot recover result because Command=111 success is not recorded"
                    )

                raw_path = Path(str(sample["paths"]["raw_data_file"]))
                self._wait_for_stable_file(raw_path)
                normalized = (
                    Path(str(sample["paths"]["export_directory"]))
                    / f"{sample_id}.csv"
                )
                request = manifest["request"]
                try:
                    normalize_spectrum_data_file(
                        data_file=raw_path,
                        lower_nm=float(request["lower_nm"]),
                        upper_nm=float(request["upper_nm"]),
                        step_nm=float(request["step_nm"]),
                        csv_file=normalized,
                    )
                    segment_records = [
                        {
                            "segment_index": 1,
                            "sample_id": sample_id,
                            "raw_data": _file_metadata(raw_path),
                            "export": _file_metadata(normalized),
                            "export_source": str(raw_path),
                            "result_source_kind": "labsolutions_vspd",
                        }
                    ]
                    self._build_sample_result(
                        manifest=manifest,
                        sample=sample,
                        segment_records=segment_records,
                        mode="spectrum",
                        sample_id=sample_id,
                    )
                except Exception as exc:
                    sample["status"] = "RECOVERY_REQUIRED"
                    self._record_failure(
                        manifest,
                        operation=f"recover_result:{sample_id}",
                        error=exc,
                    )
                    raise

                manifest.setdefault("recovery_history", []).append(dict(last_error))
                manifest["last_error"] = None
                return self._complete_sample(
                    manifest=manifest,
                    sample=sample,
                    index=index,
                    sample_id=sample_id,
                    event_type="sample_result_recovered_from_vspd",
                )
        except FileLockTimeoutError as exc:
            raise SpectrumBatchError(
                "another process is changing the UV-Vis batch state"
            ) from exc

    def abort(
        self,
        batch_id: str,
        *,
        reason: str,
        abort_confirmed: bool,
    ) -> dict[str, Any]:
        """Stop future batch actions while no LabSolutions command is running."""

        if abort_confirmed is not True:
            raise SpectrumBatchError("abort_confirmed must be true")
        normalized_reason = reason.strip() if isinstance(reason, str) else ""
        if (
            not normalized_reason
            or "\r" in normalized_reason
            or "\n" in normalized_reason
        ):
            raise SpectrumBatchError("abort reason must be non-empty and single-line")
        try:
            with self._lock():
                manifest = self._read_json(self._manifest_path(batch_id))
                state = str(manifest.get("state"))
                if state == "ABORTED":
                    return self._status(manifest)
                if state == "COMPLETED":
                    raise SpectrumBatchError("completed batch cannot be aborted")
                if state not in _WAITING_STATES:
                    raise SpectrumBatchError(
                        "batch can only be aborted while waiting for blank or sample; "
                        f"current state is {state}"
                    )
                self._require_active(manifest)
                manifest["state"] = "ABORTED"
                manifest["aborted_at_utc"] = _utc_now()
                manifest["abort_reason"] = normalized_reason
                manifest["events"].append(
                    {
                        "type": "batch_aborted",
                        "reason": normalized_reason,
                        "at_utc": manifest["aborted_at_utc"],
                    }
                )
                self._write_manifest(manifest)
                self._clear_active(str(manifest["batch_id"]))
                return self._status(manifest)
        except FileLockTimeoutError as exc:
            raise SpectrumBatchError(
                "another process is changing the UV-Vis batch state"
            ) from exc

    def _status(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        state = str(manifest.get("state"))
        samples = manifest.get("samples", [])
        index = int(manifest.get("next_sample_index", 0))
        next_sample = samples[index] if index < len(samples) else None
        if state == "WAITING_FOR_BLANK":
            next_action = "place_blank_then_call_correct_uvvis_baseline"
        elif state == "WAITING_FOR_SAMPLE" and next_sample is not None:
            next_action = "place_next_sample_then_call_measure_next_uvvis_sample"
        elif state == "RECOVERY_REQUIRED":
            next_action = "inspect_labsolutions_and_recovery_records"
        elif state == "COMPLETED":
            next_action = "none_batch_completed"
        elif state == "ABORTED":
            next_action = "none_batch_aborted"
        elif state == "FAILED":
            next_action = "fix_rejected_command_then_start_new_batch"
        else:
            next_action = "wait_for_current_operation"
        return {
            "batch_id": manifest.get("batch_id"),
            "mode": manifest.get("mode"),
            "state": state,
            "next_action": next_action,
            "reference_name": manifest.get("reference_name"),
            "baseline": manifest.get("baseline"),
            "method_file": manifest.get("method_file"),
            "method_sha256": manifest.get("method_sha256"),
            "runtime": manifest.get("runtime"),
            "sample_count": len(samples),
            "completed_sample_count": sum(
                1 for sample in samples if sample.get("status") == "COMPLETED"
            ),
            "next_sample": (
                {
                    "sequence_number": next_sample.get("sequence_number"),
                    "sample_name": next_sample.get("sample_name"),
                    "sample_id": next_sample.get("sample_id"),
                }
                if next_sample is not None
                else None
            ),
            "samples": [
                {
                    "sequence_number": sample.get("sequence_number"),
                    "sample_name": sample.get("sample_name"),
                    "sample_id": sample.get("sample_id"),
                    "status": sample.get("status"),
                    "raw_data": sample.get("raw_data"),
                    "export": sample.get("export"),
                }
                for sample in samples
            ],
            "last_error": manifest.get("last_error"),
            "manifest_path": str(self._manifest_path(str(manifest["batch_id"]))),
            "updated_at_utc": manifest.get("updated_at_utc"),
        }

    def get_status(self, batch_id: str) -> dict[str, Any]:
        """Read one atomically persisted batch status without changing files."""

        manifest = self._read_json(self._manifest_path(batch_id))
        return self._status(manifest)
