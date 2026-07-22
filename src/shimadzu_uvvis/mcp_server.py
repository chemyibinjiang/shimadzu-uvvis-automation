"""Read-only MCP planning tools for Shimadzu UV-Vis automation."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, TypedDict

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .batch_workflow import SpectrumBatchController
from .configuration import ControlSettings, MeasurementMode, load_settings
from .measurements import (
    DATA_FILE_EXTENSIONS,
    MeasurementPurpose,
    MeasurementPlanError,
    PlanningMode,
    build_measurement_request,
    method_generation_requests,
    resolve_method_template,
    route_measurement_request,
)
from .method_manager import UVVisMethodManager, method_generation_support
from .profiles import ScanDirection, resolve_scan_profile


CONFIG_ENVIRONMENT_VARIABLE = "SHIMADZU_UVVIS_CONFIG"
_BATCH_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
BaselinePolicy = Literal["new", "reuse_valid"]


class SampleBatchItem(TypedDict):
    """One operator-loaded sample in a sequential batch."""

    sample_name: str
    sample_id: str


def _batch_identifier(value: object, name: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not _BATCH_IDENTIFIER_PATTERN.fullmatch(normalized):
        raise MeasurementPlanError(
            f"{name} must contain only ASCII letters, digits, underscores, or hyphens"
        )
    return normalized


def _sample_name(value: object, index: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or "\r" in normalized or "\n" in normalized:
        raise MeasurementPlanError(
            f"samples[{index}].sample_name must be non-empty and single-line"
        )
    return normalized


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _execution_readiness(
    settings: ControlSettings, method_file: Path
) -> dict[str, object]:
    checks = {
        "method_file_exists": method_file.is_file(),
        "command_directory_exists": settings.command_dir.is_dir(),
        "data_directory_exists": (
            settings.data_dir is not None and settings.data_dir.is_dir()
        ),
        "export_directory_exists": (
            settings.export_dir is not None and settings.export_dir.is_dir()
        ),
    }
    blocking_reasons = [name for name, passed in checks.items() if not passed]
    return {
        "ready": not blocking_reasons,
        "checks": checks,
        "blocking_reasons": blocking_reasons,
        "note": (
            "Readiness only covers configured paths. LabSolutions, the instrument, "
            "the sample, and the saved method must still be checked before execution."
        ),
    }


def _command_plan(
    settings: ControlSettings, method_file: Path
) -> list[dict[str, object]]:
    commands: list[dict[str, object]] = [
        {"command": 0, "name": "hello", "parameters": {}},
    ]
    if settings.connect_before_run:
        commands.append({"command": 1, "name": "connect", "parameters": {}})
    commands.extend(
        [
            {
                "command": 100,
                "name": "load_method",
                "parameters": {"ParameterFileName": str(method_file)},
            },
            {
                "command": 110,
                "name": "sample_information",
                "deferred": True,
                "required_run_inputs": ["sample_name", "sample_id", "data_file"],
            },
            {
                "command": 111,
                "name": "spectrum_measurement",
                "parameters": {
                    "MeasurementMode": settings.measurement_mode,
                    "Discharge": (
                        "ON" if settings.discharge_after_measurement else "OFF"
                    ),
                },
            },
        ]
    )
    if settings.disconnect_after_run:
        commands.append({"command": 2, "name": "disconnect", "parameters": {}})
    return commands


def _measurement_command_plan(
    settings: ControlSettings,
    *,
    mode: MeasurementMode,
    method_file: Path,
) -> list[dict[str, object]]:
    commands: list[dict[str, object]] = [
        {"command": 0, "name": "hello", "parameters": {}}
    ]
    if settings.connect_before_run:
        commands.append({"command": 1, "name": "connect", "parameters": {}})

    measurement_parameters = {
        "MeasurementMode": settings.measurement_mode,
        "Discharge": "ON" if settings.discharge_after_measurement else "OFF",
    }
    if mode == "spectrum":
        commands.extend(
            [
                {
                    "command": 100,
                    "name": "load_spectrum_method",
                    "parameters": {"ParameterFileName": str(method_file)},
                },
                {
                    "command": 110,
                    "name": "spectrum_sample_information",
                    "deferred": True,
                    "required_run_inputs": ["sample_name", "sample_id", "data_file"],
                },
                {
                    "command": 111,
                    "name": "spectrum_measurement",
                    "parameters": measurement_parameters,
                },
            ]
        )
    elif mode == "quantitation":
        commands.extend(
            [
                {
                    "command": 200,
                    "name": "prepare_quantitation",
                    "parameters": {"ParameterFileName": str(method_file)},
                    "deferred": True,
                    "required_run_inputs": ["data_file"],
                },
                {
                    "command": 210,
                    "name": "quantitation_sample_information",
                    "deferred": True,
                    "required_run_inputs": ["sample_name", "sample_id", "sample_type"],
                },
                {
                    "command": 211,
                    "name": "quantitation_measurement",
                    "parameters": measurement_parameters,
                },
                {"command": 220, "name": "save_quantitation_data", "parameters": {}},
                {"command": 221, "name": "close_quantitation_data", "parameters": {}},
            ]
        )
    elif mode == "photometric":
        commands.extend(
            [
                {
                    "command": 300,
                    "name": "prepare_photometric",
                    "parameters": {"ParameterFileName": str(method_file)},
                    "deferred": True,
                    "required_run_inputs": ["data_file"],
                },
                {
                    "command": 310,
                    "name": "photometric_sample_information",
                    "deferred": True,
                    "required_run_inputs": ["sample_name", "sample_id", "sample_type"],
                },
                {
                    "command": 311,
                    "name": "photometric_measurement",
                    "parameters": measurement_parameters,
                },
                {"command": 320, "name": "save_photometric_data", "parameters": {}},
                {"command": 321, "name": "close_photometric_data", "parameters": {}},
            ]
        )
    else:
        commands.extend(
            [
                {
                    "command": 400,
                    "name": "load_time_course_method",
                    "parameters": {"ParameterFileName": str(method_file)},
                },
                {
                    "command": 410,
                    "name": "time_course_sample_information",
                    "deferred": True,
                    "required_run_inputs": ["sample_name", "sample_id", "data_file"],
                },
                {
                    "command": 411,
                    "name": "time_course_measurement",
                    "parameters": measurement_parameters,
                },
            ]
        )
    if settings.disconnect_after_run:
        commands.append({"command": 2, "name": "disconnect", "parameters": {}})
    return commands


def build_uvvis_measurement_plan(
    settings: ControlSettings,
    *,
    mode: PlanningMode = "auto",
    measurement_purpose: MeasurementPurpose = "measurement",
    signal_type: str = "absorbance",
    template_name: str | None = None,
    start_nm: float | None = None,
    stop_nm: float | None = None,
    step_nm: float | None = None,
    direction: ScanDirection | None = None,
    wavelength_nm: float | None = None,
    wavelengths_nm: list[float] | None = None,
    interval_seconds: float | None = None,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Plan any supported UV-Vis mode without generating or executing a method."""

    route = route_measurement_request(
        mode=mode,
        measurement_purpose=measurement_purpose,
        signal_type=signal_type,
        start_nm=start_nm,
        stop_nm=stop_nm,
        step_nm=step_nm,
        direction=direction,
        wavelength_nm=wavelength_nm,
        wavelengths_nm=wavelengths_nm,
        interval_seconds=interval_seconds,
        duration_seconds=duration_seconds,
    )
    request = route.request
    selected_mode = request.mode
    generation_requests = method_generation_requests(request)
    resolved_segments = [
        resolve_method_template(
            settings.method_templates,
            settings.generated_method_dir,
            segment,
            template_name=template_name,
        )
        for segment in generation_requests
    ]
    resolved = resolved_segments[0]
    template_file = resolved.template.method_file
    generated_files = [item.generated_method_file for item in resolved_segments]
    generated_file = generated_files[0]
    template_sha256 = _file_sha256(template_file)
    expected_template_sha256 = resolved.template.sha256
    automatic_generation_supported, generation_reason = method_generation_support(
        request
    )
    mcp_execution_supported = (
        selected_mode
        in {
            "spectrum",
            "photometric",
        }
        and automatic_generation_supported
    )
    checks = {
        "template_file_exists": template_file.is_file(),
        "template_sha256_matches": (
            expected_template_sha256 is None
            or template_sha256 == expected_template_sha256
        ),
        "generated_method_file_exists": all(path.is_file() for path in generated_files),
        "command_directory_exists": settings.command_dir.is_dir(),
        "data_directory_exists": (
            settings.data_dir is not None and settings.data_dir.is_dir()
        ),
        "export_directory_exists": (
            settings.export_dir is not None and settings.export_dir.is_dir()
        ),
        "mcp_execution_supported": mcp_execution_supported,
    }
    blocking_reasons = [name for name, passed in checks.items() if not passed]
    generation_required = not all(path.is_file() for path in generated_files)
    status = "method_generation_required" if generation_required else "planned"
    if not generation_required and not mcp_execution_supported:
        status = "execution_not_supported"
    return {
        "tool": "plan_uvvis_measurement",
        "status": status,
        "plan_only": True,
        "mode": selected_mode,
        "routing": {
            **route.as_dict(),
            "current_mcp_execution_supported": mcp_execution_supported,
        },
        "request": {
            "signal_type": request.signal_type,
            **dict(request.parameters),
        },
        "method_template": {
            "name": resolved.template.name,
            "mode": resolved.template.mode,
            "signal_type": resolved.template.signal_type,
            "method_file": str(template_file),
            "sha256": template_sha256,
            "expected_sha256": expected_template_sha256,
        },
        "method_generation": {
            "required": generation_required,
            "automatic_generation_supported": automatic_generation_supported,
            "source_template": str(template_file),
            "target_method_file": str(generated_file),
            "target_method_files": [str(path) for path in generated_files],
            "segments": [
                {
                    "segment_index": index,
                    "segment_count": len(generation_requests),
                    "target_method_file": str(item.generated_method_file),
                    "requested_parameters": dict(segment.parameters),
                    "exists": item.generated_method_file.is_file(),
                }
                for index, (segment, item) in enumerate(
                    zip(generation_requests, resolved_segments, strict=True), start=1
                )
            ],
            "requested_parameters": dict(request.parameters),
            "reason": generation_reason,
        },
        "data_file_extension": DATA_FILE_EXTENSIONS[selected_mode],
        "execution_readiness": {
            "ready": not blocking_reasons,
            "checks": checks,
            "blocking_reasons": blocking_reasons,
            "note": (
                "A generated method must be created and verified in LabSolutions "
                "before any execution tool may use this plan."
            ),
        },
        "labsolutions_command_plan": [
            {
                **command,
                "segment_index": segment_index,
                "segment_count": len(generated_files),
            }
            for segment_index, method_file in enumerate(generated_files, start=1)
            for command in _measurement_command_plan(
                settings, mode=selected_mode, method_file=method_file
            )
        ],
        "safety": {
            "writes_command_file": False,
            "edits_method_file": False,
            "connects_instrument": False,
            "starts_measurement": False,
            "template_is_never_overwritten": True,
        },
    }


def build_uvvis_sample_batch_plan(
    settings: ControlSettings,
    *,
    batch_id: str,
    samples: list[Mapping[str, str]],
    reference_name: str,
    mode: PlanningMode = "auto",
    measurement_purpose: MeasurementPurpose = "measurement",
    baseline_policy: BaselinePolicy = "new",
    signal_type: str = "absorbance",
    template_name: str | None = None,
    start_nm: float | None = None,
    stop_nm: float | None = None,
    step_nm: float | None = None,
    direction: ScanDirection | None = None,
    wavelength_nm: float | None = None,
    wavelengths_nm: list[float] | None = None,
    interval_seconds: float | None = None,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Plan sequential measurements that require manual sample replacement."""

    normalized_batch_id = _batch_identifier(batch_id, "batch_id")
    normalized_reference = _batch_identifier(reference_name, "reference_name")
    if baseline_policy not in ("new", "reuse_valid"):
        raise MeasurementPlanError("baseline_policy must be 'new' or 'reuse_valid'")
    if not samples:
        raise MeasurementPlanError("samples is required and cannot be empty")
    if settings.data_dir is None:
        raise MeasurementPlanError(
            "a configured spectrum.data_dir is required for sample batch planning"
        )

    normalized_samples: list[tuple[str, str]] = []
    seen_sample_ids: set[str] = set()
    for index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            raise MeasurementPlanError(f"samples[{index}] must be an object")
        unexpected = sorted(set(sample) - {"sample_name", "sample_id"})
        if unexpected:
            raise MeasurementPlanError(
                f"samples[{index}] contains unsupported fields: {', '.join(unexpected)}"
            )
        sample_name = _sample_name(sample.get("sample_name"), index)
        sample_id = _batch_identifier(
            sample.get("sample_id"), f"samples[{index}].sample_id"
        )
        if sample_id in seen_sample_ids:
            raise MeasurementPlanError(f"duplicate sample_id: {sample_id!r}")
        seen_sample_ids.add(sample_id)
        normalized_samples.append((sample_name, sample_id))

    measurement = build_uvvis_measurement_plan(
        settings,
        mode=mode,
        measurement_purpose=measurement_purpose,
        signal_type=signal_type,
        template_name=template_name,
        start_nm=start_nm,
        stop_nm=stop_nm,
        step_nm=step_nm,
        direction=direction,
        wavelength_nm=wavelength_nm,
        wavelengths_nm=wavelengths_nm,
        interval_seconds=interval_seconds,
        duration_seconds=duration_seconds,
    )
    selected_mode = measurement["mode"]
    batch_directory = settings.data_dir / normalized_batch_id
    extension = DATA_FILE_EXTENSIONS[selected_mode]
    measurement_command = {
        "spectrum": 111,
        "photometric": 311,
        "quantitation": 211,
        "time_course": 411,
    }[selected_mode]
    if baseline_policy == "new":
        baseline_preparation: dict[str, Any] = {
            "policy": "new",
            "scope": "once_before_batch",
            "operator_gate": {
                "type": "place_blank_and_confirm",
                "status": "required",
                "instructions": (
                    f"Place the batch blank in the sample position, keep reference "
                    f"{normalized_reference} in the reference position, and confirm "
                    "blank identity, cuvette orientation, liquid level, and absence "
                    "of bubbles."
                ),
            },
            "labsolutions_command": {
                "command": 21,
                "name": "automatic_correction",
                "parameters": {"CorrectionType": 1},
                "send_only_after_operator_gate": True,
            },
        }
    else:
        baseline_preparation = {
            "policy": "reuse_valid",
            "scope": "once_before_batch",
            "operator_gate": None,
            "labsolutions_command": None,
            "automatic_reuse": True,
            "reuse_conditions": [
                "same_method",
                "same_blank",
                "same_reference",
                "same_instrument_session",
            ],
        }
    sample_plans: list[dict[str, Any]] = []
    path_conflicts: list[str] = []
    if batch_directory.exists():
        path_conflicts.append(str(batch_directory))
    for sequence_number, (sample_name, source_sample_id) in enumerate(
        normalized_samples, start=1
    ):
        run_sample_id = f"{sequence_number:03d}_{source_sample_id}"
        sample_directory = batch_directory / run_sample_id
        raw_directory = sample_directory / "raw"
        export_directory = sample_directory / "export"
        plot_directory = sample_directory / "plot"
        method_segments = measurement["method_generation"]["segments"]
        if selected_mode == "photometric":
            sample_segments = [
                {
                    "segment_index": segment["segment_index"],
                    "segment_count": segment["segment_count"],
                    "method_file": segment["target_method_file"],
                    "wavelengths_nm": segment["requested_parameters"]["wavelengths_nm"],
                    "sample_id": (
                        f"{run_sample_id}_s{int(segment['segment_index']):02d}"
                    ),
                    "raw_data_file": str(
                        raw_directory
                        / (
                            f"{run_sample_id}_s{int(segment['segment_index']):02d}"
                            f"{extension}"
                        )
                    ),
                }
                for segment in method_segments
            ]
        else:
            sample_segments = [
                {
                    "segment_index": 1,
                    "segment_count": 1,
                    "method_file": method_segments[0]["target_method_file"],
                    "wavelengths_nm": None,
                    "sample_id": run_sample_id,
                    "raw_data_file": str(raw_directory / f"{run_sample_id}{extension}"),
                }
            ]
        raw_data_file = Path(sample_segments[0]["raw_data_file"])
        manifest_file = sample_directory / "manifest.json"
        if sample_directory.exists():
            path_conflicts.append(str(sample_directory))
        sample_plans.append(
            {
                "sequence_number": sequence_number,
                "sample_name": sample_name,
                "source_sample_id": source_sample_id,
                "sample_id": run_sample_id,
                "reference_name": normalized_reference,
                "operator_gate": {
                    "type": "replace_sample_and_confirm",
                    "status": "required",
                    "required_before_command": measurement_command,
                    "instructions": (
                        f"Place sample {run_sample_id} in the sample position, keep "
                        f"reference {normalized_reference} in the reference position, "
                        "then confirm identity and placement."
                    ),
                },
                "paths": {
                    "sample_directory": str(sample_directory),
                    "raw_directory": str(raw_directory),
                    "raw_data_file": str(raw_data_file),
                    "raw_data_files": [
                        segment["raw_data_file"] for segment in sample_segments
                    ],
                    "export_directory": str(export_directory),
                    "plot_directory": str(plot_directory),
                    "plot_file": str(plot_directory / "result.png"),
                    "merged_csv_file": str(export_directory / "result.csv"),
                    "result_json_file": str(export_directory / "result.json"),
                    "manifest_file": str(manifest_file),
                },
                "segments": sample_segments,
                "labsolutions_run_inputs": {
                    "sample_name": sample_name,
                    "sample_id": run_sample_id,
                    "data_file": str(raw_data_file),
                    "sample_type": "unknown",
                },
            }
        )

    measurement_ready = measurement["execution_readiness"]["ready"]
    batch_checks = {
        "measurement_plan_ready": measurement_ready,
        "batch_and_sample_directories_are_new": not path_conflicts,
    }
    blocking_reasons = [name for name, passed in batch_checks.items() if not passed]
    if path_conflicts:
        status = "path_conflict"
    else:
        status = measurement["status"]
    return {
        "tool": "plan_uvvis_sample_batch",
        "status": status,
        "plan_only": True,
        "batch_id": normalized_batch_id,
        "mode": selected_mode,
        "routing": measurement["routing"],
        "sample_count": len(sample_plans),
        "reference": {
            "name": normalized_reference,
            "policy": "keep_installed_for_entire_batch",
        },
        "batch_preparation": baseline_preparation,
        "measurement_plan": measurement,
        "batch_directory": str(batch_directory),
        "samples": sample_plans,
        "execution_readiness": {
            "ready": not blocking_reasons,
            "checks": batch_checks,
            "blocking_reasons": blocking_reasons,
            "path_conflicts": path_conflicts,
            "note": (
                "Every operator gate must be confirmed at run time. Planning does not "
                "count as confirmation."
            ),
        },
        "safety": {
            "writes_files_or_directories": False,
            "writes_command_file": False,
            "connects_instrument": False,
            "starts_measurement": False,
            "manual_sample_exchange_required": True,
            "operator_baseline_confirmation_required": baseline_policy == "new",
            "unattended_execution_supported": False,
        },
    }


def build_uvvis_scan_plan(
    settings: ControlSettings,
    *,
    start_nm: float,
    stop_nm: float,
    step_nm: float,
    direction: ScanDirection | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    """Build a scan plan without writing files or contacting LabSolutions."""

    resolved = resolve_scan_profile(
        settings.scan_profiles,
        start_nm=start_nm,
        stop_nm=stop_nm,
        step_nm=step_nm,
        direction=direction,
        profile_name=profile_name,
    )
    profile = resolved.profile
    traverse_seconds = None
    if profile.scan_speed_nm_per_min is not None:
        traverse_seconds = (
            (resolved.request.upper_nm - resolved.request.lower_nm)
            * 60.0
            / profile.scan_speed_nm_per_min
        )

    return {
        "tool": "plan_uvvis_scan",
        "status": "planned",
        "plan_only": True,
        "request": resolved.request.as_dict(),
        "profile": resolved.as_dict()["profile"],
        "timing": {
            "point_count": resolved.request.point_count,
            "scan_speed_nm_per_min": profile.scan_speed_nm_per_min,
            "nominal_scan_traverse_seconds": traverse_seconds,
        },
        "execution_readiness": _execution_readiness(settings, profile.method_file),
        "labsolutions_command_plan": _command_plan(settings, profile.method_file),
        "safety": {
            "writes_command_file": False,
            "connects_instrument": False,
            "starts_measurement": False,
            "method_parameters_source": "registered_labsolutions_profile",
        },
    }


def create_mcp_server(
    config_path: str | Path,
    *,
    batch_controller_factory: (
        Callable[[ControlSettings], SpectrumBatchController] | None
    ) = None,
    method_manager_factory: Callable[[ControlSettings], Any] | None = None,
) -> FastMCP:
    """Create an MCP server bound to one operator-controlled TOML config file."""

    resolved_config = Path(config_path).expanduser().resolve()
    server = FastMCP(
        "shimadzu-uvvis",
        instructions=(
            "Route requests to a compatible LabSolutions measurement mode before "
            "starting an application, generating a method, or touching hardware. "
            "Plan and execute guarded Shimadzu UV-Vis workflows using registered "
            "methods. Spectrum and Photometric batch actions automatically require "
            "the LabSolutions runtime manager to verify Automatic Control Waiting, "
            "the command directory, and Command=0/Return=0. Planning and status "
            "tools are read-only. Physical actions require explicit placement or "
            "execution confirmations."
        ),
    )

    def batch_controller(settings: ControlSettings) -> SpectrumBatchController:
        if batch_controller_factory is not None:
            return batch_controller_factory(settings)
        return SpectrumBatchController(settings)

    def method_manager(settings: ControlSettings) -> Any:
        if method_manager_factory is not None:
            return method_manager_factory(settings)
        return UVVisMethodManager(settings)

    @server.tool(
        name="plan_uvvis_measurement",
        title="Plan UV-Vis measurement",
        description=(
            "Select a compatible Spectrum, Photometric, Quantitation, or Time Course "
            "mode before LabSolutions starts; validate the request; select a method "
            "template; and return the target method and command sequence. With "
            "mode=auto, an unsupported Spectrum range interval is represented as an "
            "exact Photometric wavelength list. This tool never edits a method, "
            "writes a command file, or starts a measurement."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def plan_uvvis_measurement(
        mode: PlanningMode = "auto",
        measurement_purpose: MeasurementPurpose = "measurement",
        signal_type: str = "absorbance",
        template_name: str | None = None,
        start_nm: float | None = None,
        stop_nm: float | None = None,
        step_nm: float | None = None,
        direction: ScanDirection | None = None,
        wavelength_nm: float | None = None,
        wavelengths_nm: list[float] | None = None,
        interval_seconds: float | None = None,
        duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Route and plan one of the four LabSolutions UV-Vis modes."""

        settings = load_settings(resolved_config)
        return build_uvvis_measurement_plan(
            settings,
            mode=mode,
            measurement_purpose=measurement_purpose,
            signal_type=signal_type,
            template_name=template_name,
            start_nm=start_nm,
            stop_nm=stop_nm,
            step_nm=step_nm,
            direction=direction,
            wavelength_nm=wavelength_nm,
            wavelengths_nm=wavelengths_nm,
            interval_seconds=interval_seconds,
            duration_seconds=duration_seconds,
        )

    @server.tool(
        name="generate_uvvis_method",
        title="Generate verified UV-Vis method",
        description=(
            "Generate parameterized LabSolutions Spectrum or Photometric methods "
            "from a registered immutable absorbance template. Photometric requests "
            "over the verified 10-wavelength method limit are split automatically. "
            "The tool temporarily leaves "
            "Automatic Control, connects through the installed Shimadzu driver, "
            "uses stable Win32 control IDs to Save As, reopens every generated method "
            "and reads back every requested field, then restores Waiting and "
            "Command=0/Return=0. It never performs baseline correction or starts a "
            "measurement."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def generate_uvvis_method(
        mode: MeasurementMode,
        signal_type: str = "absorbance",
        template_name: str | None = None,
        start_nm: float | None = None,
        stop_nm: float | None = None,
        step_nm: float | None = None,
        direction: ScanDirection | None = None,
        wavelength_nm: float | None = None,
        wavelengths_nm: list[float] | None = None,
        interval_seconds: float | None = None,
        duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Generate and attest the methods required by one logical request."""

        settings = load_settings(resolved_config)
        request = build_measurement_request(
            mode=mode,
            signal_type=signal_type,
            start_nm=start_nm,
            stop_nm=stop_nm,
            step_nm=step_nm,
            direction=direction,
            wavelength_nm=wavelength_nm,
            wavelengths_nm=wavelengths_nm,
            interval_seconds=interval_seconds,
            duration_seconds=duration_seconds,
        )
        return method_manager(settings).generate(
            request,
            template_name=template_name,
        )

    @server.tool(
        name="plan_uvvis_sample_batch",
        title="Plan sequential UV-Vis sample batch",
        description=(
            "Select a compatible LabSolutions mode, then plan multiple samples for "
            "an instrument with one sample and one reference position. Assign unique "
            "sequence-prefixed data folders and require sample-replacement "
            "confirmation before every measurement. This tool is read-only."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def plan_uvvis_sample_batch(
        batch_id: str,
        samples: list[SampleBatchItem],
        reference_name: str,
        mode: PlanningMode = "auto",
        measurement_purpose: MeasurementPurpose = "measurement",
        baseline_policy: BaselinePolicy = "new",
        signal_type: str = "absorbance",
        template_name: str | None = None,
        start_nm: float | None = None,
        stop_nm: float | None = None,
        step_nm: float | None = None,
        direction: ScanDirection | None = None,
        wavelength_nm: float | None = None,
        wavelengths_nm: list[float] | None = None,
        interval_seconds: float | None = None,
        duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Plan a manually exchanged sample batch in deterministic order."""

        settings = load_settings(resolved_config)
        return build_uvvis_sample_batch_plan(
            settings,
            batch_id=batch_id,
            mode=mode,
            measurement_purpose=measurement_purpose,
            samples=samples,
            reference_name=reference_name,
            baseline_policy=baseline_policy,
            signal_type=signal_type,
            template_name=template_name,
            start_nm=start_nm,
            stop_nm=stop_nm,
            step_nm=step_nm,
            direction=direction,
            wavelength_nm=wavelength_nm,
            wavelengths_nm=wavelengths_nm,
            interval_seconds=interval_seconds,
            duration_seconds=duration_seconds,
        )

    @server.tool(
        name="plan_uvvis_scan",
        title="Plan UV-Vis spectrum scan",
        description=(
            "Match a wavelength range and data interval to one registered "
            "LabSolutions Spectrum profile. This tool is read-only: it does not "
            "write SPC_CMD.txt, connect the instrument, or start a measurement."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def plan_uvvis_scan(
        start_nm: float,
        stop_nm: float,
        step_nm: float,
        direction: ScanDirection | None = None,
        profile_name: str | None = None,
    ) -> dict[str, Any]:
        """Plan a Spectrum scan from structured wavelength parameters."""

        settings = load_settings(resolved_config)
        return build_uvvis_scan_plan(
            settings,
            start_nm=start_nm,
            stop_nm=stop_nm,
            step_nm=step_nm,
            direction=direction,
            profile_name=profile_name,
        )

    @server.tool(
        name="start_uvvis_batch",
        title="Start UV-Vis sample batch",
        description=(
            "Route the range request before starting LabSolutions, require all "
            "generated Spectrum or Photometric methods, and verify Automatic "
            "Control. This does not correct a baseline or measure a sample."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def start_uvvis_batch(
        batch_id: str,
        samples: list[SampleBatchItem],
        reference_name: str,
        start_nm: float,
        stop_nm: float,
        step_nm: float,
        execution_confirmed: bool,
        baseline_policy: BaselinePolicy = "new",
        signal_type: str = "absorbance",
        template_name: str | None = None,
        direction: ScanDirection | None = None,
    ) -> dict[str, Any]:
        """Start one persisted Spectrum or Photometric manual sample batch."""

        settings = load_settings(resolved_config)
        plan = build_uvvis_sample_batch_plan(
            settings,
            batch_id=batch_id,
            mode="auto",
            samples=samples,
            reference_name=reference_name,
            baseline_policy=baseline_policy,
            signal_type=signal_type,
            template_name=template_name,
            start_nm=start_nm,
            stop_nm=stop_nm,
            step_nm=step_nm,
            direction=direction,
        )
        return batch_controller(settings).start(
            plan,
            execution_confirmed=execution_confirmed,
        )

    @server.tool(
        name="correct_uvvis_baseline",
        title="Correct UV-Vis batch baseline",
        description=(
            "Send Command=21 CorrectionType=1 for the active UV-Vis batch. "
            "Before the physical action, require Automatic Control Waiting and a "
            "fresh Command=0/Return=0 handshake without changing runtime settings. "
            "This is allowed only while the batch waits for a confirmed blank."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def correct_uvvis_baseline(
        batch_id: str,
        blank_loaded_confirmed: bool,
    ) -> dict[str, Any]:
        """Correct the active UV-Vis batch baseline exactly once."""

        settings = load_settings(resolved_config)
        return batch_controller(settings).correct_baseline(
            batch_id,
            blank_loaded_confirmed=blank_loaded_confirmed,
        )

    @server.tool(
        name="measure_next_uvvis_sample",
        title="Measure next Spectrum sample",
        description=(
            "Measure exactly the next planned Spectrum sample with Commands 110 "
            "and 111 after a fresh runtime Waiting and Hello readiness gate, wait "
            "for raw data, parse Spectrum .vspd directly with automatic CSV export "
            "as a compatibility fallback, and archive outputs. The supplied sample_id "
            "must match batch status and placement must be confirmed."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def measure_next_uvvis_sample(
        batch_id: str,
        sample_id: str,
        sample_loaded_confirmed: bool,
    ) -> dict[str, Any]:
        """Measure and archive the exact next sample in an active batch."""

        settings = load_settings(resolved_config)
        return batch_controller(settings).measure_next(
            batch_id,
            sample_id=sample_id,
            sample_loaded_confirmed=sample_loaded_confirmed,
        )

    @server.tool(
        name="recover_uvvis_spectrum_result",
        title="Recover UV-Vis Spectrum result",
        description=(
            "Recover a Spectrum sample result from an existing .vspd after the "
            "measurement completed but automatic CSV export timed out. This writes "
            "derived CSV, JSON, PNG, and batch state only; it never sends a "
            "LabSolutions or instrument command and never repeats the measurement."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def recover_uvvis_spectrum_result(batch_id: str) -> dict[str, Any]:
        """Recover and publish a measured Spectrum result without remeasurement."""

        settings = load_settings(resolved_config)
        return batch_controller(settings).recover_spectrum_result(batch_id)

    @server.tool(
        name="get_uvvis_batch_status",
        title="Get UV-Vis batch status",
        description=(
            "Read the persisted UV-Vis batch state, completed samples, next "
            "sample, required action, and recovery information. This tool never "
            "writes files or sends LabSolutions commands."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def get_uvvis_batch_status(batch_id: str) -> dict[str, Any]:
        """Read one UV-Vis batch manifest without changing it."""

        settings = load_settings(resolved_config)
        return batch_controller(settings).get_status(batch_id)

    @server.tool(
        name="abort_uvvis_batch",
        title="Abort waiting UV-Vis batch",
        description=(
            "Mark an active UV-Vis batch aborted and prevent future samples. "
            "This does not send a LabSolutions stop command and is allowed only "
            "while waiting for blank or sample placement, never during an "
            "in-flight or recovery-required command."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def abort_uvvis_batch(
        batch_id: str,
        reason: str,
        abort_confirmed: bool,
    ) -> dict[str, Any]:
        """Abort future actions for a waiting UV-Vis batch."""

        settings = load_settings(resolved_config)
        return batch_controller(settings).abort(
            batch_id,
            reason=reason,
            abort_confirmed=abort_confirmed,
        )

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the guarded Shimadzu UV-Vis MCP planning and batch server."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=os.environ.get(CONFIG_ENVIRONMENT_VARIABLE),
        help=(
            "Control TOML file. Defaults to the SHIMADZU_UVVIS_CONFIG "
            "environment variable."
        ),
    )
    args = parser.parse_args(argv)
    if args.config is None:
        parser.error(f"--config or {CONFIG_ENVIRONMENT_VARIABLE} is required")
    config_path = args.config.expanduser().resolve()
    if not config_path.is_file():
        parser.error(f"config file does not exist: {config_path}")

    create_mcp_server(config_path).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
