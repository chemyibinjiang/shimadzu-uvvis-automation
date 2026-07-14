"""Read-only MCP planning tools for Shimadzu UV-Vis automation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .configuration import ControlSettings, load_settings
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
            "Plan Shimadzu UV-Vis scans using registered LabSolutions methods. "
            "Planning tools are read-only and never start a measurement."
        ),
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
