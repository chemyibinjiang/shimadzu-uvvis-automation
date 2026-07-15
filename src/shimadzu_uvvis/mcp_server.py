"""Read-only MCP planning tools for Shimadzu UV-Vis automation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .configuration import ControlSettings, MeasurementMode, load_settings
from .measurements import (
    DATA_FILE_EXTENSIONS,
    build_measurement_request,
    resolve_method_template,
)
from .profiles import ScanDirection, resolve_scan_profile


CONFIG_ENVIRONMENT_VARIABLE = "SHIMADZU_UVVIS_CONFIG"


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
    """Plan any supported UV-Vis mode without generating or executing a method."""

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
    resolved = resolve_method_template(
        settings.method_templates,
        settings.generated_method_dir,
        request,
        template_name=template_name,
    )
    template_file = resolved.template.method_file
    generated_file = resolved.generated_method_file
    checks = {
        "template_file_exists": template_file.is_file(),
        "generated_method_file_exists": generated_file.is_file(),
        "command_directory_exists": settings.command_dir.is_dir(),
        "data_directory_exists": (
            settings.data_dir is not None and settings.data_dir.is_dir()
        ),
        "export_directory_exists": (
            settings.export_dir is not None and settings.export_dir.is_dir()
        ),
    }
    blocking_reasons = [name for name, passed in checks.items() if not passed]
    generation_required = not generated_file.is_file()
    return {
        "tool": "plan_uvvis_measurement",
        "status": "method_generation_required" if generation_required else "planned",
        "plan_only": True,
        "mode": mode,
        "request": {
            "signal_type": request.signal_type,
            **dict(request.parameters),
        },
        "method_template": {
            "name": resolved.template.name,
            "mode": resolved.template.mode,
            "signal_type": resolved.template.signal_type,
            "method_file": str(template_file),
        },
        "method_generation": {
            "required": generation_required,
            "automatic_generation_supported": False,
            "source_template": str(template_file),
            "target_method_file": str(generated_file),
            "requested_parameters": dict(request.parameters),
            "reason": (
                "The automatic-control text protocol can load and run method files "
                "but does not expose commands for changing these method parameters."
            ),
        },
        "data_file_extension": DATA_FILE_EXTENSIONS[mode],
        "execution_readiness": {
            "ready": not blocking_reasons,
            "checks": checks,
            "blocking_reasons": blocking_reasons,
            "note": (
                "A generated method must be created and verified in LabSolutions "
                "before any execution tool may use this plan."
            ),
        },
        "labsolutions_command_plan": _measurement_command_plan(
            settings, mode=mode, method_file=generated_file
        ),
        "safety": {
            "writes_command_file": False,
            "edits_method_file": False,
            "connects_instrument": False,
            "starts_measurement": False,
            "template_is_never_overwritten": True,
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
            resolved.request.upper_nm - resolved.request.lower_nm
        ) * 60.0 / profile.scan_speed_nm_per_min

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


def create_mcp_server(config_path: str | Path) -> FastMCP:
    """Create an MCP server bound to one operator-controlled TOML config file."""

    resolved_config = Path(config_path).expanduser().resolve()
    server = FastMCP(
        "shimadzu-uvvis",
        instructions=(
            "Plan Shimadzu UV-Vis measurements using registered LabSolutions methods. "
            "Planning tools are read-only and never start a measurement."
        ),
    )

    @server.tool(
        name="plan_uvvis_measurement",
        title="Plan UV-Vis measurement",
        description=(
            "Validate a Spectrum, Photometric, Quantitation, or Time Course request; "
            "select a registered LabSolutions method template; and return the target "
            "method file and command sequence. This tool never edits a method, writes "
            "a command file, or starts a measurement."
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
        """Plan one of the four LabSolutions UV-Vis measurement modes."""

        settings = load_settings(resolved_config)
        return build_uvvis_measurement_plan(
            settings,
            mode=mode,
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

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the read-only Shimadzu UV-Vis MCP planning server."
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
