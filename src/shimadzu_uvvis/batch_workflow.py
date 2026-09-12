"""Persistent state machine for manually exchanged UV-Vis sample batches."""

from __future__ import annotations

import copy
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
    build_time_course_result,
    normalize_photometric_data_file,
    normalize_spectrum_data_file,
)
from .runtime_manager import (
    LabSolutionsRuntimeManager,
    RuntimeReady,
    settings_for_mode,
)
from .storage_paths import existing_batch_directories, student_batch_directory


_BATCH_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_WAITING_STATES = {"WAITING_FOR_BLANK", "WAITING_FOR_SAMPLE"}
_TERMINAL_STATES = {"COMPLETED", "ABORTED", "FAILED"}
_MODE_SWITCH_SOURCE_STATES = {"COMPLETED", "ABORTED"}


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
        runtime_manager_factory: Callable[[], LabSolutionsRuntimeManager] | None = None,
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
        self.runtime_mode_path = self.data_dir / ".uvvis_runtime_mode.json"

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
        normalized_batch_id = self._batch_id(batch_id)
        matches = existing_batch_directories(self.data_dir, normalized_batch_id)
        if len(matches) > 1:
            raise SpectrumBatchError(
                f"multiple UV-Vis batch directories use batch_id {normalized_batch_id!r}"
            )
        return matches[0] if matches else self.data_dir / normalized_batch_id

    def _manifest_path(self, batch_id: str) -> Path:
        normalized_batch_id = self._batch_id(batch_id)
        if self.active_batch_path.is_file():
            active = self._read_json(self.active_batch_path)
            if active.get("batch_id") == normalized_batch_id:
                manifest_path = Path(str(active.get("manifest_path") or "")).resolve()
                try:
                    manifest_path.relative_to(self.data_dir.resolve())
                except ValueError as exc:
                    raise SpectrumBatchError(
                        "active UV-Vis manifest path is outside spectrum.data_dir"
                    ) from exc
                if (
                    manifest_path.name != "batch-manifest.json"
                    or manifest_path.parent.name != normalized_batch_id
                ):
                    raise SpectrumBatchError("active UV-Vis manifest path is invalid")
                return manifest_path
        return self._batch_directory(normalized_batch_id) / "batch-manifest.json"

    def _manifest_path_for_record(self, manifest: Mapping[str, Any]) -> Path:
        batch_id = self._batch_id(str(manifest.get("batch_id") or ""))
        raw_directory = str(manifest.get("batch_directory") or "").strip()
        batch_directory = (
            Path(raw_directory).resolve()
            if raw_directory
            else self._batch_directory(batch_id).resolve()
        )
        try:
            batch_directory.relative_to(self.data_dir.resolve())
        except ValueError as exc:
            raise SpectrumBatchError(
                "batch manifest directory is outside spectrum.data_dir"
            ) from exc
        if batch_directory.name != batch_id:
            raise SpectrumBatchError("batch manifest directory does not match batch_id")
        return batch_directory / "batch-manifest.json"

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
        manifest_path = self._manifest_path_for_record(manifest)
        write_json_atomic(manifest_path, manifest)
        write_json_atomic(
            self.runtime_mode_path,
            {
                "schema_version": 1,
                "mode": manifest.get("mode"),
                "batch_id": manifest.get("batch_id"),
                "manifest_path": str(manifest_path),
                "batch_state": manifest.get("state"),
                "automatic_control_state": "WAITING",
                "pending_target_mode": None,
                "updated_at_utc": _utc_now(),
            },
        )

    def _runtime_manifest(self, record: Mapping[str, Any]) -> dict[str, Any]:
        batch_id = self._batch_id(str(record.get("batch_id") or ""))
        raw_path = str(record.get("manifest_path") or "").strip()
        if not raw_path:
            raise SpectrumBatchError("UV-Vis runtime mode record has no manifest path")
        manifest_path = Path(raw_path).resolve()
        try:
            manifest_path.relative_to(self.data_dir.resolve())
        except ValueError as exc:
            raise SpectrumBatchError(
                "UV-Vis runtime mode manifest is outside spectrum.data_dir"
            ) from exc
        if (
            manifest_path.name != "batch-manifest.json"
            or manifest_path.parent.name != batch_id
        ):
            raise SpectrumBatchError("UV-Vis runtime mode manifest path is invalid")
        manifest = self._read_json(manifest_path)
        if manifest.get("batch_id") != batch_id:
            raise SpectrumBatchError(
                "UV-Vis runtime mode record does not match its batch manifest"
            )
        return manifest

    def _prepare_mode_transition(
        self, target_mode: MeasurementMode
    ) -> dict[str, Any] | None:
        if not self.runtime_mode_path.is_file():
            return None
        record = self._read_json(self.runtime_mode_path)
        previous_mode = record.get("mode")
        if previous_mode not in {
            "spectrum",
            "photometric",
            "quantitation",
            "time_course",
        }:
            raise SpectrumBatchError("UV-Vis runtime mode record has an invalid mode")

        automatic_control_state = record.get("automatic_control_state")
        if automatic_control_state == "RELEASED":
            pending_target_mode = record.get("pending_target_mode")
            if pending_target_mode != target_mode:
                raise SpectrumBatchError(
                    "UV-Vis runtime was released for mode "
                    f"{pending_target_mode!r}; finish that transition before starting "
                    f"{target_mode!r}"
                )
            transition = record.get("transition")
            if not isinstance(transition, Mapping):
                raise SpectrumBatchError(
                    "released UV-Vis runtime record has no transition details"
                )
            resumed = dict(transition)
            resumed["release_reused"] = True
            return resumed

        if previous_mode == target_mode:
            return None

        batch_state = record.get("batch_state")
        if batch_state not in _MODE_SWITCH_SOURCE_STATES:
            raise SpectrumBatchError(
                "cannot switch UV-Vis mode until the previous batch is exactly "
                "COMPLETED or ABORTED; "
                f"current state is {batch_state!r}"
            )
        previous_manifest = self._runtime_manifest(record)
        if previous_manifest.get("mode") != previous_mode:
            raise SpectrumBatchError(
                "UV-Vis runtime mode does not match the previous batch manifest"
            )
        manifest_state = previous_manifest.get("state")
        if (
            manifest_state != batch_state
            or manifest_state not in _MODE_SWITCH_SOURCE_STATES
        ):
            raise SpectrumBatchError(
                "previous UV-Vis batch manifest must be exactly COMPLETED or ABORTED "
                "before switching modes"
            )

        released_at = _utc_now()
        release = self._runtime_manager(previous_mode).release_for_mode_switch()
        transition = {
            "from_mode": previous_mode,
            "to_mode": target_mode,
            "source_batch_id": record.get("batch_id"),
            "source_batch_state": batch_state,
            "released_at_utc": released_at,
            "release": release,
            "release_reused": False,
        }
        write_json_atomic(
            self.runtime_mode_path,
            {
                **record,
                "automatic_control_state": "RELEASED",
                "pending_target_mode": target_mode,
                "transition": transition,
                "updated_at_utc": released_at,
            },
        )
        return transition

    def _set_active(self, manifest: Mapping[str, Any]) -> None:
        write_json_atomic(
            self.active_batch_path,
            {
                "schema_version": 1,
                "batch_id": manifest["batch_id"],
                "manifest_path": str(self._manifest_path_for_record(manifest)),
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

    @staticmethod
    def _validate_batch_owner(
        manifest: Mapping[str, Any],
        *,
        student_id: str,
        experiment_name: str,
        session_id: str,
    ) -> None:
        supplied = {
            "student_id": student_id.strip() if isinstance(student_id, str) else "",
            "experiment_name": (
                experiment_name.strip() if isinstance(experiment_name, str) else ""
            ),
            "session_id": session_id.strip() if isinstance(session_id, str) else "",
        }
        missing = [name for name, value in supplied.items() if not value]
        if missing:
            raise SpectrumBatchError(
                "UV-Vis batch ownership requires non-empty " + ", ".join(missing)
            )
        mismatches = [
            name
            for name, value in supplied.items()
            if str(manifest.get(name) or "").strip() != value
        ]
        if mismatches:
            raise SpectrumBatchError(
                "UV-Vis batch does not belong to the current student experiment "
                "session; mismatched fields: "
                + ", ".join(mismatches)
            )

    def _validate_optional_batch_owner(
        self,
        manifest: Mapping[str, Any],
        *,
        student_id: str | None,
        experiment_name: str | None,
        session_id: str | None,
    ) -> None:
        if student_id is None and experiment_name is None and session_id is None:
            return
        self._validate_batch_owner(
            manifest,
            student_id=student_id or "",
            experiment_name=experiment_name or "",
            session_id=session_id or "",
        )

    def _ensure_no_active_batch(self, replacement_plan: Mapping[str, Any]) -> None:
        if not self.active_batch_path.exists():
            return
        active = self._read_json(self.active_batch_path)
        active_id = active.get("batch_id")
        if not isinstance(active_id, str):
            raise SpectrumBatchError(
                f"invalid active batch marker: {self.active_batch_path}"
            )
        # Read the marker directly here.  _manifest_path intentionally rejects
        # paths outside data_dir, but a stale marker can contain precisely such
        # a path; it must be treated as missing and repaired below.
        raw_manifest_path = str(active.get("manifest_path") or "").strip()
        manifest_path = Path(raw_manifest_path).resolve() if raw_manifest_path else None
        if manifest_path is not None:
            try:
                manifest_path.relative_to(self.data_dir.resolve())
            except ValueError:
                manifest_path = None
        if manifest_path is None:
            manifest_path = self._batch_directory(active_id) / "batch-manifest.json"
        if manifest_path.is_file():
            manifest = self._read_json(manifest_path)
            if manifest.get("state") in _TERMINAL_STATES:
                self.active_batch_path.unlink(missing_ok=True)
                return
            old_identity = {
                name: str(manifest.get(name) or "").strip()
                for name in ("student_id", "experiment_name", "session_id")
            }
            new_identity = {
                name: str(replacement_plan.get(name) or "").strip()
                for name in ("student_id", "experiment_name", "session_id")
            }
            same_student_experiment = (
                old_identity["student_id"]
                and old_identity["student_id"] == new_identity["student_id"]
                and old_identity["experiment_name"]
                and old_identity["experiment_name"] == new_identity["experiment_name"]
            )
            is_new_session = (
                old_identity["session_id"]
                and new_identity["session_id"]
                and old_identity["session_id"] != new_identity["session_id"]
            )
            state = str(manifest.get("state") or "")
            if same_student_experiment and is_new_session and state in {
                "WAITING_FOR_BLANK",
                "WAITING_FOR_SAMPLE",
                "RECOVERY_REQUIRED",
            }:
                aborted_at = _utc_now()
                manifest["state"] = "ABORTED"
                manifest["aborted_at_utc"] = aborted_at
                manifest["abort_reason"] = "superseded_by_new_experiment_session"
                manifest.setdefault("events", []).append(
                    {
                        "type": "batch_aborted_for_new_session",
                        "replacement_session_id": new_identity["session_id"],
                        "at_utc": aborted_at,
                    }
                )
                self._write_manifest(manifest)
                self._clear_active(active_id)
                return
        else:
            # A crash or a manual data cleanup can leave only the global marker
            # behind.  Do not treat that marker as a live instrument batch: first
            # search the configured data root for a surviving manifest with the
            # same id, and only clear the marker when no such manifest exists.
            surviving_manifests = []
            for candidate in self.data_dir.rglob("batch-manifest.json"):
                try:
                    candidate_payload = self._read_json(candidate)
                except SpectrumBatchError:
                    continue
                if candidate_payload.get("batch_id") == active_id:
                    surviving_manifests.append(candidate)
            if not surviving_manifests:
                self.active_batch_path.unlink(missing_ok=True)
                if self.runtime_mode_path.is_file():
                    try:
                        runtime = self._read_json(self.runtime_mode_path)
                    except SpectrumBatchError:
                        runtime = {}
                    if runtime.get("batch_id") == active_id:
                        self.runtime_mode_path.unlink(missing_ok=True)
                return
            raise SpectrumBatchError(
                f"UV-Vis active marker for {active_id!r} points to a missing "
                "manifest, but a surviving manifest exists: "
                + ", ".join(str(path) for path in surviving_manifests)
            )
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

    def _auto_recover_photometric_pre_acquisition(
        self,
        *,
        manifest: dict[str, Any],
        sample: dict[str, Any],
        error: Exception,
    ) -> dict[str, Any] | None:
        """Return to the same sample after a manifest error before Command 311."""

        if manifest.get("mode") != "photometric" or not isinstance(
            error, PermissionError
        ):
            return None
        if "batch-manifest.json" not in str(error):
            return None

        sample_id = str(sample.get("sample_id") or "")
        completed_segments = list(sample.get("completed_segments", []))
        segment_index = len(completed_segments) + 1
        segments = list(sample.get("segments", []))
        if not sample_id or segment_index > len(segments):
            return None
        phase = f"sample:{sample_id}:segment:{segment_index}"
        phase_commands = [
            command
            for command in manifest.get("commands", [])
            if isinstance(command, Mapping) and command.get("phase") == phase
        ]
        if not any(
            command.get("command") == 310 and command.get("return_code") == 0
            for command in phase_commands
        ):
            return None
        if any(
            command.get("command") in {311, 320, 321}
            for command in phase_commands
        ):
            return None

        segment = segments[segment_index - 1]
        raw_path = Path(str(segment["raw_data_file"]))
        if raw_path.exists():
            return None

        recovered_at = _utc_now()
        recovery = {
            "operation": f"measure:{sample_id}",
            "type": type(error).__name__,
            "message": str(error),
            "reason": "manifest_access_error_before_command_311",
            "automatic": True,
            "at_utc": recovered_at,
        }
        manifest.setdefault("recovery_history", []).append(recovery)
        manifest["last_recovery"] = recovery
        manifest["last_error"] = None
        manifest["state"] = "WAITING_FOR_SAMPLE"
        sample["status"] = "PENDING"
        sample.pop("started_at_utc", None)
        manifest["events"].append(
            {
                "type": "photometric_pre_acquisition_auto_recovered",
                "sample_id": sample_id,
                "segment_index": segment_index,
                "reason": recovery["reason"],
                "at_utc": recovered_at,
            }
        )
        self._write_manifest(manifest)
        self._set_active(manifest)
        return self._status(manifest)

    def _validate_start_plan(
        self, plan: Mapping[str, Any]
    ) -> tuple[str, MeasurementMode, list[Path], Path]:
        if plan.get("tool") != "plan_uvvis_sample_batch":
            raise SpectrumBatchError("start requires a plan_uvvis_sample_batch result")
        mode = plan.get("mode")
        if mode not in {"spectrum", "photometric", "time_course"}:
            raise SpectrumBatchError(
                "execution currently supports Spectrum, Photometric, and Time Course batches"
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
        try:
            expected_directory = student_batch_directory(
                self.data_dir,
                batch_id,
                student_id=str(plan.get("student_id") or "") or None,
                experiment_name=str(plan.get("experiment_name") or "") or None,
                session_id=str(plan.get("session_id") or "") or None,
            ).resolve()
        except ValueError as exc:
            raise SpectrumBatchError(str(exc)) from exc
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
        return batch_id, mode, methods, expected_directory

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

    def _validate_sample_baseline(self, manifest: Mapping[str, Any], methods: list[Path]) -> None:
        """Every sample must use the completed baseline for this exact method/reference."""
        baseline = manifest.get("baseline") or {}
        record = baseline.get("record") or {}
        if baseline.get("status") not in {"COMPLETED", "REUSED"} or not record.get("completed_at_utc"):
            raise SpectrumBatchError("sample measurement requires a completed baseline record")
        current = self._reusable_baseline(mode=manifest["mode"], methods=methods,
            method_sha256s=list(manifest["method_sha256s"]), reference_name=manifest["reference_name"])
        for field in ("mode", "method_files", "method_sha256s", "reference_name", "completed_at_utc"):
            if record.get(field) != current.get(field):
                raise SpectrumBatchError("baseline changed after this batch was prepared; start a new batch and correct the blank")

    def start(
        self, plan: Mapping[str, Any], *, execution_confirmed: bool
    ) -> dict[str, Any]:
        """Create a batch, verify LabSolutions, and enter the placement gate."""

        if execution_confirmed is not True:
            raise SpectrumBatchError("execution_confirmed must be true")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock():
                self._ensure_no_active_batch(plan)
                batch_id, mode, methods, batch_directory = self._validate_start_plan(
                    plan
                )
                existing_directories = existing_batch_directories(
                    self.data_dir, batch_id
                )
                if existing_directories:
                    raise SpectrumBatchError(
                        f"batch directory already exists: {existing_directories[0]}"
                    )
                mode_transition = self._prepare_mode_transition(mode)
                runtime_manager = self._runtime_manager(mode)
                runtime_ready = runtime_manager.ensure_ready(allow_reconfigure=True)

                method_sha256s = [_sha256(method) for method in methods]
                requested_baseline_policy = str(
                    plan["batch_preparation"]["policy"]
                )
                baseline_policy = requested_baseline_policy
                reference_name = str(plan["reference"]["name"])
                reused_baseline: dict[str, Any] | None = None
                baseline_reuse_rejected_reason: str | None = None
                baseline_reuse_rejected_detail: str | None = None
                if mode_transition is not None and baseline_policy == "reuse_valid":
                    baseline_policy = "new"
                    baseline_reuse_rejected_reason = "measurement_mode_changed"
                if baseline_policy == "reuse_valid":
                    try:
                        reused_baseline = self._reusable_baseline(
                            mode=mode,
                            methods=methods,
                            method_sha256s=method_sha256s,
                            reference_name=reference_name,
                        )
                    except SpectrumBatchError as exc:
                        baseline_policy = "new"
                        baseline_reuse_rejected_reason = "baseline_context_changed"
                        baseline_reuse_rejected_detail = str(exc)

                batch_directory.mkdir(parents=True, exist_ok=False)
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
                    sample_directory = Path(str(paths["sample_directory"]))
                    sample_directory.mkdir(parents=True, exist_ok=False)
                    child_directories = {
                        Path(str(paths[key]))
                        for key in (
                            "raw_directory",
                            "export_directory",
                            "plot_directory",
                        )
                        if Path(str(paths[key])) != sample_directory
                    }
                    for directory in child_directories:
                        directory.mkdir(parents=True, exist_ok=True)
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
                    "batch_directory": str(batch_directory),
                    "student_id": str(plan.get("student_id") or ""),
                    "student_account": str(plan.get("student_account") or ""),
                    "experiment_name": str(plan.get("experiment_name") or ""),
                    "experiment_directory": str(plan.get("experiment_directory") or ""),
                    "session_id": str(plan.get("session_id") or ""),
                    "session_directory": str(plan.get("session_directory") or ""),
                    "results_directory": str(plan.get("results_directory") or ""),
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
                        **(
                            {
                                "requested_policy": requested_baseline_policy,
                                "reuse_rejected_reason": baseline_reuse_rejected_reason,
                                "reuse_rejected_detail": baseline_reuse_rejected_detail,
                            }
                            if baseline_reuse_rejected_reason is not None
                            else {}
                        ),
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
                    "mode_transition": mode_transition,
                    "commands": [
                        {
                            **_feedback_record(runtime_ready.feedback),
                            "phase": "runtime_ready:start",
                        }
                    ],
                    "events": [
                        {"type": "batch_created", "at_utc": now},
                        {"type": "runtime_ready", "at_utc": now},
                        *(
                            [
                                {
                                    "type": "measurement_mode_switched",
                                    "from_mode": mode_transition["from_mode"],
                                    "to_mode": mode_transition["to_mode"],
                                    "at_utc": now,
                                }
                            ]
                            if mode_transition is not None
                            else []
                        ),
                        *(
                            [
                                {
                                    "type": "baseline_reuse_rejected",
                                    "reason": baseline_reuse_rejected_reason,
                                    "at_utc": now,
                                }
                            ]
                            if baseline_reuse_rejected_reason is not None
                            else []
                        ),
                    ],
                    "last_error": None,
                }
                self._write_manifest(manifest)
                self._set_active(manifest)

                client = self._client(mode)
                try:
                    with client.workflow_session():
                        if (
                            self.settings.connect_before_run
                            or mode_transition is not None
                        ):
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
                        elif mode == "photometric":
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
                        else:
                            self._append_feedback(
                                manifest,
                                client.send_command(400, ParameterFileName=methods[0]),
                                phase="start:time_course",
                            )

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
        student_id: str | None = None,
        experiment_name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Run Command 21 after the operator confirms blank placement."""

        if blank_loaded_confirmed is not True:
            raise SpectrumBatchError("blank_loaded_confirmed must be true")
        try:
            with self._lock():
                manifest = self._read_json(self._manifest_path(batch_id))
                self._validate_optional_batch_owner(
                    manifest,
                    student_id=student_id,
                    experiment_name=experiment_name,
                    session_id=session_id,
                )
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
                    "runtime_process_id": runtime_ready.process_id,
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
        destination_directory.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source, destination)
        except OSError:
            temporary = destination.with_name(
                f".{destination.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, destination)
                source.unlink()
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
        normalized = Path(str(sample["paths"]["export_directory"])) / f"{sample_id}.csv"
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
            raise SpectrumBatchError(
                "completed Photometric segment records are invalid"
            )
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

    def _measure_time_course_sample(
        self,
        *,
        client: LabSolutionsClient,
        manifest: dict[str, Any],
        sample: dict[str, Any],
        sample_id: str,
    ) -> list[dict[str, Any]]:
        raw_path = Path(str(sample["paths"]["raw_data_file"]))
        started_at = time.time()
        self._append_feedback(
            manifest,
            client.send_command(
                410,
                DataFileName=raw_path,
                SampleName=str(sample["sample_name"]),
                SampleID=sample_id,
            ),
            phase=f"sample:{sample_id}",
        )
        duration_seconds = float(manifest["request"]["duration_seconds"])
        measurement_timeout = max(
            self.settings.timeout_seconds,
            duration_seconds + max(120.0, self.settings.stable_seconds + 30.0),
        )
        self._append_feedback(
            manifest,
            client.send_command(
                411,
                MeasurementMode=self.settings.measurement_mode,
                Discharge=self.settings.discharge_after_measurement,
                timeout=measurement_timeout,
            ),
            phase=f"sample:{sample_id}",
        )
        self._wait_for_stable_file(raw_path)
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
            export_source,
            Path(str(sample["paths"]["export_directory"])),
        )
        return [
            {
                "segment_index": 1,
                "sample_id": sample_id,
                "raw_data": _file_metadata(raw_path),
                "export": _file_metadata(archived),
                "export_source": str(export_source),
                "result_source_kind": "labsolutions_time_course_export",
                "measurement_timeout_seconds": measurement_timeout,
            }
        ]

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
        if mode == "photometric":
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
            return

        if mode != "time_course":
            raise SpectrumBatchError(f"unsupported result mode: {mode}")
        sample["raw_data"] = segment_records[0]["raw_data"]
        sample["export"] = segment_records[0]["export"]
        sample["export_source"] = segment_records[0]["export_source"]
        request = manifest["request"]
        sample["result"] = build_time_course_result(
            export_file=Path(str(segment_records[0]["export"]["path"])),
            wavelength_nm=float(request["wavelength_nm"]),
            interval_seconds=float(request["interval_seconds"]),
            duration_seconds=float(request["duration_seconds"]),
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

        pending_remeasurement = manifest.get("pending_remeasurement")
        if (
            isinstance(pending_remeasurement, Mapping)
            and pending_remeasurement.get("sample_id") == sample_id
        ):
            original_state = str(
                pending_remeasurement.get("original_state") or "WAITING_FOR_SAMPLE"
            )
            original_next_sample_index = int(
                pending_remeasurement.get("original_next_sample_index", index + 1)
            )
            attempt_number = int(pending_remeasurement.get("attempt_number", 2))
            completed_at = _utc_now()
            history = sample.setdefault("remeasurement_history", [])
            history.append(
                {
                    "attempt_number": attempt_number,
                    "archive_directory": pending_remeasurement.get(
                        "archive_directory"
                    ),
                    "completed_at_utc": completed_at,
                }
            )
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
            manifest["next_sample_index"] = original_next_sample_index
            manifest["state"] = original_state
            manifest.pop("pending_remeasurement", None)
            manifest["last_remeasurement"] = {
                "sample_id": sample_id,
                "attempt_number": attempt_number,
                "completed_at_utc": completed_at,
            }
            if original_state == "COMPLETED":
                manifest["completed_at_utc"] = completed_at
            self._write_manifest(manifest)
            if original_state in _TERMINAL_STATES:
                self._clear_active(str(manifest["batch_id"]))
            else:
                self._set_active(manifest)
            status = self._status(manifest)
            status["remeasurement"] = dict(manifest["last_remeasurement"])
            return status

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
        student_id: str | None = None,
        experiment_name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Measure exactly the next planned sample and archive its outputs."""

        if sample_loaded_confirmed is not True:
            raise SpectrumBatchError("sample_loaded_confirmed must be true")
        try:
            with self._lock():
                manifest = self._read_json(self._manifest_path(batch_id))
                self._validate_optional_batch_owner(
                    manifest,
                    student_id=student_id,
                    experiment_name=experiment_name,
                    session_id=session_id,
                )
                self._require_active(manifest)
                if manifest.get("state") != "WAITING_FOR_SAMPLE":
                    raise SpectrumBatchError(
                        "sample measurement requires state WAITING_FOR_SAMPLE; "
                        f"current state is {manifest.get('state')}"
                    )
                methods = self._validate_methods(manifest)
                self._validate_sample_baseline(manifest, methods)
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
                baseline_pid = manifest["baseline"]["record"].get("runtime_process_id")
                if baseline_pid is None:
                    baseline_pid = (manifest.get("runtime") or {}).get("process_id")
                if baseline_pid is not None and baseline_pid != runtime_ready.process_id:
                    raise SpectrumBatchError("LabSolutions restarted after baseline correction; start a new batch and correct the blank before measuring")
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
                        elif mode == "time_course":
                            segment_records = self._measure_time_course_sample(
                                client=client,
                                manifest=manifest,
                                sample=sample,
                                sample_id=expected_id,
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
                    automatically_recovered = (
                        self._auto_recover_photometric_pre_acquisition(
                            manifest=manifest,
                            sample=sample,
                            error=exc,
                        )
                    )
                    if automatically_recovered is not None:
                        return automatically_recovered
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
                    event_type=(
                        "sample_remeasured"
                        if isinstance(manifest.get("pending_remeasurement"), Mapping)
                        else "sample_completed"
                    ),
                )
        except FileLockTimeoutError as exc:
            raise SpectrumBatchError(
                "another process is changing the UV-Vis batch state"
            ) from exc

    def recover_spectrum_result(
        self,
        batch_id: str,
        *,
        student_id: str | None = None,
        experiment_name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Complete a measured Spectrum sample from its saved .vspd without remeasure."""

        try:
            with self._lock():
                manifest = self._read_json(self._manifest_path(batch_id))
                self._validate_optional_batch_owner(
                    manifest,
                    student_id=student_id,
                    experiment_name=experiment_name,
                    session_id=session_id,
                )
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
                    or "waiting for a stable export"
                    not in str(last_error.get("message", ""))
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
                    Path(str(sample["paths"]["export_directory"])) / f"{sample_id}.csv"
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

    def recover_photometric_pre_acquisition(
        self,
        batch_id: str,
        *,
        student_id: str | None = None,
        experiment_name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore a Photometric sample when persistence failed before Command 311."""

        try:
            with self._lock():
                manifest = self._read_json(self._manifest_path(batch_id))
                self._validate_optional_batch_owner(
                    manifest,
                    student_id=student_id,
                    experiment_name=experiment_name,
                    session_id=session_id,
                )
                self._require_active(manifest)
                if manifest.get("state") != "RECOVERY_REQUIRED":
                    raise SpectrumBatchError(
                        "Photometric pre-acquisition recovery requires state "
                        f"RECOVERY_REQUIRED; current state is {manifest.get('state')}"
                    )
                if manifest.get("mode") != "photometric":
                    raise SpectrumBatchError(
                        "pre-acquisition recovery is only supported for Photometric"
                    )
                self._validate_methods(manifest)

                index = int(manifest["next_sample_index"])
                samples = manifest["samples"]
                if index >= len(samples):
                    raise SpectrumBatchError("batch has no sample awaiting recovery")
                sample = samples[index]
                sample_id = str(sample["sample_id"])
                if sample.get("status") != "RECOVERY_REQUIRED":
                    raise SpectrumBatchError(
                        "next Photometric sample is not marked RECOVERY_REQUIRED"
                    )

                last_error = manifest.get("last_error")
                error_message = (
                    str(last_error.get("message", ""))
                    if isinstance(last_error, Mapping)
                    else ""
                )
                if (
                    not isinstance(last_error, Mapping)
                    or last_error.get("operation") != f"measure:{sample_id}"
                    or last_error.get("type") != "PermissionError"
                    or "batch-manifest.json" not in error_message
                ):
                    raise SpectrumBatchError(
                        "pre-acquisition recovery is allowed only after a Photometric "
                        "batch-manifest access error"
                    )

                segments = list(sample.get("segments", []))
                completed_segments = list(sample.get("completed_segments", []))
                segment_index = len(completed_segments) + 1
                if not segments or segment_index > len(segments):
                    raise SpectrumBatchError(
                        "Photometric sample has no unmeasured segment to recover"
                    )
                if any(
                    record.get("segment_index") != position
                    for position, record in enumerate(completed_segments, start=1)
                ):
                    raise SpectrumBatchError(
                        "completed Photometric segment records are invalid"
                    )

                phase = f"sample:{sample_id}:segment:{segment_index}"
                phase_commands = [
                    command
                    for command in manifest.get("commands", [])
                    if isinstance(command, Mapping) and command.get("phase") == phase
                ]
                successful_310 = any(
                    command.get("command") == 310
                    and command.get("return_code") == 0
                    for command in phase_commands
                )
                acquisition_was_sent = any(
                    command.get("command") in {311, 320, 321}
                    for command in phase_commands
                )
                if not successful_310 or acquisition_was_sent:
                    raise SpectrumBatchError(
                        "cannot prove the Photometric failure occurred after Command "
                        "310 and before acquisition Command 311"
                    )

                segment = segments[segment_index - 1]
                raw_path = Path(str(segment["raw_data_file"]))
                result_paths = [
                    raw_path,
                    Path(str(sample["paths"]["merged_csv_file"])),
                    Path(str(sample["paths"]["result_json_file"])),
                    Path(str(sample["paths"]["plot_file"])),
                    Path(str(sample["paths"]["manifest_file"])),
                ]
                segment_csv = (
                    Path(str(sample["paths"]["export_directory"]))
                    / f"{segment['sample_id']}.csv"
                )
                result_paths.append(segment_csv)
                existing_results = [path for path in result_paths if path.exists()]
                if existing_results or any(
                    sample.get(key) is not None
                    for key in ("raw_data", "export", "result")
                ):
                    raise SpectrumBatchError(
                        "Photometric sample has acquisition artifacts; refusing "
                        "pre-acquisition recovery: "
                        + ", ".join(str(path) for path in existing_results)
                    )

                recovered_at = _utc_now()
                manifest.setdefault("recovery_history", []).append(dict(last_error))
                manifest["last_recovery"] = {
                    **dict(last_error),
                    "reason": "manifest_access_error_before_command_311",
                    "automatic": False,
                    "recovered_at_utc": recovered_at,
                }
                manifest["last_error"] = None
                manifest["state"] = "WAITING_FOR_SAMPLE"
                sample["status"] = "PENDING"
                sample.pop("started_at_utc", None)
                manifest["events"].append(
                    {
                        "type": "photometric_pre_acquisition_recovered",
                        "sample_id": sample_id,
                        "segment_index": segment_index,
                        "reason": "manifest_access_error_before_command_311",
                        "at_utc": recovered_at,
                    }
                )
                self._write_manifest(manifest)
                self._set_active(manifest)
                return self._status(manifest)
        except FileLockTimeoutError as exc:
            raise SpectrumBatchError(
                "another process is changing the UV-Vis batch state"
            ) from exc

    def _archive_sample_for_remeasurement(
        self,
        *,
        manifest: dict[str, Any],
        sample: dict[str, Any],
        attempt_number: int,
    ) -> Path:
        sample_directory = Path(str(sample["paths"]["sample_directory"])).resolve()
        archive_directory = (
            sample_directory
            / "remeasurements"
            / f"attempt_{attempt_number:02d}_previous"
        )
        if archive_directory.exists():
            raise SpectrumBatchError(
                f"remeasurement archive already exists: {archive_directory}"
            )

        paths: set[Path] = set()
        sample_paths = sample.get("paths")
        if isinstance(sample_paths, Mapping):
            for key in (
                "raw_data_file",
                "merged_csv_file",
                "result_json_file",
                "plot_file",
                "manifest_file",
            ):
                value = sample_paths.get(key)
                if value:
                    paths.add(Path(str(value)).resolve())
            raw_files = sample_paths.get("raw_data_files")
            for value in raw_files if isinstance(raw_files, list) else []:
                paths.add(Path(str(value)).resolve())
            export_directory = sample_paths.get("export_directory")
            if export_directory:
                export_path = Path(str(export_directory)).resolve()
                if export_path.is_dir():
                    paths.update(path.resolve() for path in export_path.rglob("*") if path.is_file())

        result = sample.get("result")
        published = result.get("published") if isinstance(result, Mapping) else None
        if isinstance(published, Mapping):
            for value in published.values():
                if value:
                    paths.add(Path(str(value)).resolve())

        archive_directory.mkdir(parents=True, exist_ok=False)
        write_json_atomic(
            archive_directory / "previous-sample-record.json",
            {
                "schema_version": 1,
                "batch_id": manifest.get("batch_id"),
                "sample": sample,
                "archived_at_utc": _utc_now(),
            },
        )
        for source in sorted(paths, key=lambda path: str(path).lower()):
            if not source.is_file():
                continue
            try:
                relative = source.relative_to(sample_directory)
                if relative.parts and relative.parts[0] == "remeasurements":
                    continue
                destination = archive_directory / "sample" / relative
            except ValueError:
                destination = archive_directory / "published" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise SpectrumBatchError(
                    f"duplicate artifact in remeasurement archive: {destination}"
                )
            try:
                os.replace(source, destination)
            except OSError:
                shutil.copy2(source, destination)
                source.unlink()
        return archive_directory

    def remeasure(
        self,
        batch_id: str,
        *,
        sample_id: str,
        sample_loaded_confirmed: bool,
        student_id: str,
        experiment_name: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Remeasure one completed sample while retaining the batch baseline."""

        if sample_loaded_confirmed is not True:
            raise SpectrumBatchError("sample_loaded_confirmed must be true")
        try:
            with self._lock():
                manifest = self._read_json(self._manifest_path(batch_id))
                self._validate_batch_owner(
                    manifest,
                    student_id=student_id,
                    experiment_name=experiment_name,
                    session_id=session_id,
                )
                state = str(manifest.get("state") or "")
                if state not in {"WAITING_FOR_SAMPLE", "COMPLETED"}:
                    raise SpectrumBatchError(
                        "sample remeasurement requires a stable waiting or completed "
                        f"batch; current state is {state}"
                    )
                if state == "WAITING_FOR_SAMPLE":
                    self._require_active(manifest)
                if isinstance(manifest.get("pending_remeasurement"), Mapping):
                    raise SpectrumBatchError(
                        "the batch already has a pending sample remeasurement"
                    )

                samples = manifest.get("samples")
                if not isinstance(samples, list):
                    raise SpectrumBatchError("batch sample records are invalid")
                matches = [
                    (index, sample)
                    for index, sample in enumerate(samples)
                    if isinstance(sample, dict) and sample.get("sample_id") == sample_id
                ]
                if len(matches) != 1:
                    raise SpectrumBatchError(
                        f"batch does not contain exactly one sample {sample_id!r}"
                    )
                index, sample = matches[0]
                if sample.get("status") != "COMPLETED":
                    raise SpectrumBatchError(
                        f"sample {sample_id!r} is not completed and cannot be remeasured"
                    )

                attempt_number = int(sample.get("measurement_attempt", 1)) + 1
                reconstructed_segments: list[dict[str, Any]] | None = None
                if manifest.get("mode") == "photometric":
                    old_segments = list(sample.get("segments", []))
                    raw_files = list(sample.get("paths", {}).get("raw_data_files", []))
                    if len(old_segments) != len(raw_files):
                        raise SpectrumBatchError(
                            "Photometric remeasurement cannot reconstruct planned segments"
                        )
                    reconstructed_segments = [
                        {
                            "segment_index": segment.get("segment_index", position),
                            "sample_id": segment.get("sample_id"),
                            "wavelengths_nm": segment.get("wavelengths_nm"),
                            "raw_data_file": raw_files[position - 1],
                        }
                        for position, segment in enumerate(old_segments, start=1)
                    ]
                archive_directory = self._archive_sample_for_remeasurement(
                    manifest=manifest,
                    sample=sample,
                    attempt_number=attempt_number,
                )
                if reconstructed_segments is not None:
                    sample["segments"] = reconstructed_segments
                for key in (
                    "raw_data",
                    "export",
                    "export_source",
                    "result",
                    "completed_segments",
                    "started_at_utc",
                    "completed_at_utc",
                ):
                    sample.pop(key, None)
                sample["status"] = "PENDING"
                sample["measurement_attempt"] = attempt_number

                manifest["pending_remeasurement"] = {
                    "sample_id": sample_id,
                    "attempt_number": attempt_number,
                    "archive_directory": str(archive_directory),
                    "original_state": state,
                    "original_next_sample_index": int(
                        manifest.get("next_sample_index", len(samples))
                    ),
                    "requested_at_utc": _utc_now(),
                }
                manifest["next_sample_index"] = index
                manifest["state"] = "WAITING_FOR_SAMPLE"
                manifest["events"].append(
                    {
                        "type": "sample_remeasurement_requested",
                        "sample_id": sample_id,
                        "attempt_number": attempt_number,
                        "at_utc": _utc_now(),
                    }
                )
                self._write_manifest(manifest)
                self._set_active(manifest)
        except FileLockTimeoutError as exc:
            raise SpectrumBatchError(
                "another process is changing the UV-Vis batch state"
            ) from exc

        return self.measure_next(
            batch_id,
            sample_id=sample_id,
            sample_loaded_confirmed=True,
            student_id=student_id,
            experiment_name=experiment_name,
            session_id=session_id,
        )

    def restart(
        self,
        batch_id: str,
        plan: Mapping[str, Any],
        *,
        execution_confirmed: bool,
        student_id: str,
        experiment_name: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Close a stable old batch and start a fresh batch with a new baseline."""

        restart_plan = copy.deepcopy(dict(plan))
        new_batch_id = self._batch_id(str(restart_plan.get("batch_id") or ""))
        if new_batch_id == self._batch_id(batch_id):
            raise SpectrumBatchError("restart requires a new batch_id")
        preparation = restart_plan.get("batch_preparation")
        if not isinstance(preparation, Mapping) or preparation.get("policy") != "new":
            raise SpectrumBatchError("restart requires baseline_policy='new'")
        for name, supplied in (
            ("student_id", student_id),
            ("experiment_name", experiment_name),
            ("session_id", session_id),
        ):
            if str(restart_plan.get(name) or "").strip() != str(supplied or "").strip():
                raise SpectrumBatchError(
                    f"restart plan {name} does not match the current session"
                )

        readiness = restart_plan.get("execution_readiness")
        path_conflicts = (
            readiness.get("path_conflicts")
            if isinstance(readiness, Mapping)
            else None
        )
        if isinstance(path_conflicts, list) and path_conflicts:
            batch_directory = Path(str(restart_plan["batch_directory"]))
            for sample in restart_plan.get("samples", []):
                if not isinstance(sample, dict):
                    continue
                sample_id = str(sample.get("sample_id") or "")
                sample_directory = batch_directory / sample_id
                raw_directory = sample_directory / "raw"
                export_directory = sample_directory / "export"
                plot_directory = sample_directory / "plot"
                segments = sample.get("segments")
                segments = segments if isinstance(segments, list) else []
                for segment in segments:
                    if not isinstance(segment, dict):
                        continue
                    original = Path(str(segment.get("raw_data_file") or ""))
                    segment["raw_data_file"] = str(raw_directory / original.name)
                raw_files = [
                    str(segment["raw_data_file"])
                    for segment in segments
                    if isinstance(segment, dict) and segment.get("raw_data_file")
                ]
                sample["paths"] = {
                    "sample_directory": str(sample_directory),
                    "raw_directory": str(raw_directory),
                    "raw_data_file": raw_files[0] if raw_files else "",
                    "raw_data_files": raw_files,
                    "export_directory": str(export_directory),
                    "plot_directory": str(plot_directory),
                    "plot_file": str(plot_directory / "result.png"),
                    "merged_csv_file": str(export_directory / "result.csv"),
                    "result_json_file": str(export_directory / "result.json"),
                    "manifest_file": str(sample_directory / "manifest.json"),
                }
                run_inputs = sample.get("labsolutions_run_inputs")
                if isinstance(run_inputs, dict) and raw_files:
                    run_inputs["data_file"] = raw_files[0]
            measurement_readiness = restart_plan.get("measurement_plan", {}).get(
                "execution_readiness", {}
            )
            measurement_ready = (
                isinstance(measurement_readiness, Mapping)
                and measurement_readiness.get("ready") is True
            )
            restart_plan["status"] = (
                "planned" if measurement_ready else restart_plan.get("status")
            )
            restart_plan["execution_readiness"] = {
                "ready": measurement_ready,
                "checks": {
                    "measurement_plan_ready": measurement_ready,
                    "batch_and_sample_directories_are_new": True,
                },
                "blocking_reasons": (
                    [] if measurement_ready else ["measurement_plan_ready"]
                ),
                "path_conflicts": [],
                "note": "Restart results use the replacement batch directory.",
            }

        try:
            with self._lock():
                manifest = self._read_json(self._manifest_path(batch_id))
                self._validate_batch_owner(
                    manifest,
                    student_id=student_id,
                    experiment_name=experiment_name,
                    session_id=session_id,
                )
                state = str(manifest.get("state") or "")
                if state in {
                    "STARTING",
                    "BASELINE_CORRECTING",
                    "MEASURING_SAMPLE",
                    "FINALIZING",
                }:
                    raise SpectrumBatchError(
                        "cannot restart while an instrument command is in flight; "
                        f"current state is {state}"
                    )
                if state not in _TERMINAL_STATES:
                    self._require_active(manifest)
                    manifest["state"] = "ABORTED"
                    manifest["aborted_at_utc"] = _utc_now()
                    manifest["abort_reason"] = "superseded_by_batch_restart"
                    manifest["events"].append(
                        {
                            "type": "batch_aborted_for_restart",
                            "replacement_batch_id": new_batch_id,
                            "at_utc": manifest["aborted_at_utc"],
                        }
                    )
                    self._write_manifest(manifest)
                    self._clear_active(str(manifest["batch_id"]))
        except FileLockTimeoutError as exc:
            raise SpectrumBatchError(
                "another process is changing the UV-Vis batch state"
            ) from exc

        restarted = self.start(restart_plan, execution_confirmed=execution_confirmed)
        try:
            with self._lock():
                new_manifest = self._read_json(self._manifest_path(new_batch_id))
                new_manifest["restarted_from_batch_id"] = batch_id
                new_manifest["events"].append(
                    {
                        "type": "batch_restarted",
                        "source_batch_id": batch_id,
                        "at_utc": _utc_now(),
                    }
                )
                self._write_manifest(new_manifest)
                self._set_active(new_manifest)
                restarted = self._status(new_manifest)
        except FileLockTimeoutError as exc:
            raise SpectrumBatchError(
                "new batch started but restart linkage could not be persisted"
            ) from exc
        restarted["restarted_from_batch_id"] = batch_id
        return restarted

    def abort(
        self,
        batch_id: str,
        *,
        reason: str,
        abort_confirmed: bool,
        student_id: str | None = None,
        experiment_name: str | None = None,
        session_id: str | None = None,
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
                self._validate_optional_batch_owner(
                    manifest,
                    student_id=student_id,
                    experiment_name=experiment_name,
                    session_id=session_id,
                )
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
        completed_sample = samples[index - 1] if 0 < index <= len(samples) else None
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
        operator_instruction: dict[str, Any] | None = None
        if state == "WAITING_FOR_SAMPLE" and next_sample is not None:
            next_name = str(next_sample.get("sample_name") or next_sample.get("sample_id") or "").strip()
            completed_name = (
                str(
                    completed_sample.get("sample_name")
                    or completed_sample.get("sample_id")
                    or ""
                ).strip()
                if isinstance(completed_sample, Mapping)
                else ""
            )
            operator_instruction = {
                "requires_confirmation": True,
                "completed_sample": (
                    {
                        "sequence_number": completed_sample.get("sequence_number"),
                        "sample_name": completed_sample.get("sample_name"),
                        "sample_id": completed_sample.get("sample_id"),
                    }
                    if isinstance(completed_sample, Mapping)
                    else None
                ),
                "next_sample": {
                    "sequence_number": next_sample.get("sequence_number"),
                    "sample_name": next_sample.get("sample_name"),
                    "sample_id": next_sample.get("sample_id"),
                },
                "message_zh": (
                    f"{completed_name}已测量完成，请取出。请放入{next_name}，放好后告诉我。"
                    if completed_name
                    else f"请放入{next_name}，放好后告诉我。"
                ),
                "message_en": (
                    f"{completed_name} is complete. Remove it, place {next_name}, and tell me when it is ready."
                    if completed_name
                    else f"Place {next_name} and tell me when it is ready."
                ),
            }
        elif state == "COMPLETED" and isinstance(completed_sample, Mapping):
            completed_name = str(
                completed_sample.get("sample_name")
                or completed_sample.get("sample_id")
                or ""
            ).strip()
            operator_instruction = {
                "requires_confirmation": False,
                "completed_sample": {
                    "sequence_number": completed_sample.get("sequence_number"),
                    "sample_name": completed_sample.get("sample_name"),
                    "sample_id": completed_sample.get("sample_id"),
                },
                "next_sample": None,
                "message_zh": f"{completed_name}已测量完成，本批次全部样品测量结束。",
                "message_en": f"{completed_name} is complete. All samples in this batch are finished.",
            }
        return {
            "batch_id": manifest.get("batch_id"),
            "batch_directory": str(self._manifest_path_for_record(manifest).parent),
            "student_id": manifest.get("student_id"),
            "student_account": manifest.get("student_account"),
            "experiment_name": manifest.get("experiment_name"),
            "session_id": manifest.get("session_id"),
            "results_directory": manifest.get("results_directory"),
            "mode": manifest.get("mode"),
            "state": state,
            "next_action": next_action,
            "operator_instruction": operator_instruction,
            "reference_name": manifest.get("reference_name"),
            "baseline": manifest.get("baseline"),
            "method_file": manifest.get("method_file"),
            "method_sha256": manifest.get("method_sha256"),
            "runtime": manifest.get("runtime"),
            "mode_transition": manifest.get("mode_transition"),
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
                    "result": sample.get("result"),
                }
                for sample in samples
            ],
            "last_error": manifest.get("last_error"),
            "last_recovery": manifest.get("last_recovery"),
            "manifest_path": str(self._manifest_path_for_record(manifest)),
            "updated_at_utc": manifest.get("updated_at_utc"),
        }

    def get_status(
        self,
        batch_id: str,
        *,
        student_id: str | None = None,
        experiment_name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Read one atomically persisted batch status without changing files."""

        manifest = self._read_json(self._manifest_path(batch_id))
        self._validate_optional_batch_owner(
            manifest,
            student_id=student_id,
            experiment_name=experiment_name,
            session_id=session_id,
        )
        return self._status(manifest)
