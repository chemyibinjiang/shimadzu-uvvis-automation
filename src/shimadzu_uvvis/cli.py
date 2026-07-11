"""Command-line interface for Shimadzu LabSolutions UV-Vis automation."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from .client import (
    MODE_FILE_NAMES,
    Feedback,
    LabSolutionsClient,
    LabSolutionsError,
    ParameterValue,
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

    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("ping", help="Send Command=0 and check automatic control")

    send = subparsers.add_parser("send", help="Send one command from the manual")
    send.add_argument("command", type=int)
    send.add_argument("parameters", nargs="*", metavar="KEY=VALUE")
    send.add_argument(
        "--allow-error",
        action="store_true",
        help="Print non-zero feedback instead of exiting with an error",
    )

    spectrum = subparsers.add_parser(
        "spectrum", help="Run a complete Spectrum measurement sequence"
    )
    spectrum.add_argument("--method", type=Path, required=True)
    spectrum.add_argument("--sample-name", required=True)
    spectrum.add_argument("--sample-id")
    spectrum.add_argument("--data-file", type=Path)
    spectrum.add_argument("--measurement-mode", type=int, choices=(1, 2), default=1)
    spectrum.add_argument("--connect", action="store_true")
    spectrum.add_argument("--disconnect", action="store_true")
    spectrum.add_argument(
        "--correction",
        choices=("none", "auto", "baseline", "zero"),
        default="none",
    )
    spectrum.add_argument("--start-wl", type=float)
    spectrum.add_argument("--end-wl", type=float)
    spectrum.add_argument("--wavelength", type=float)
    spectrum.add_argument("--export-dir", type=Path)
    spectrum.add_argument("--export-pattern")
    spectrum.add_argument("--stable-seconds", type=float)
    return parser


def _load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


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


def _correction_parameters(args: argparse.Namespace) -> dict[str, ParameterValue] | None:
    if args.correction == "none":
        return None
    if args.correction == "auto":
        return {"CorrectionType": 1}
    if args.correction == "baseline":
        if args.start_wl is None or args.end_wl is None:
            raise ValueError("baseline correction requires --start-wl and --end-wl")
        return {
            "CorrectionType": 2,
            "StartWL": args.start_wl,
            "EndWL": args.end_wl,
        }
    if args.wavelength is None:
        raise ValueError("zero correction requires --wavelength")
    return {"CorrectionType": 3, "WL": args.wavelength}


def _feedback_dict(feedback: Feedback) -> dict[str, Any]:
    return {
        "command": feedback.command,
        "return_code": feedback.return_code,
        "error": feedback.error,
        "fields": dict(feedback.fields),
    }


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = _load_config(args.config)
        lab_config = config.get("labsolutions", {})
        export_config = config.get("export", {})
        client = LabSolutionsClient(
            command_dir=args.command_dir
            or lab_config.get("command_dir", r"C:\UVVisControl"),
            mode=args.mode or lab_config.get("mode", "spectrum"),
            timeout=args.timeout or lab_config.get("timeout_seconds", 120.0),
            poll_interval=args.poll_interval
            or lab_config.get("poll_interval_seconds", 0.2),
        )

        if args.action == "ping":
            print(json.dumps(_feedback_dict(client.send_command(0)), indent=2))
            return 0

        if args.action == "send":
            result = client.send_command(
                args.command,
                raise_on_error=not args.allow_error,
                **_parse_parameters(args.parameters),
            )
            print(json.dumps(_feedback_dict(result), indent=2))
            return 0 if result.ok else 2

        export_dir = args.export_dir or export_config.get("directory")
        export_pattern = args.export_pattern or export_config.get("pattern", "*.csv")
        stable_seconds = (
            args.stable_seconds
            if args.stable_seconds is not None
            else export_config.get("stable_seconds", 2.0)
        )
        result = client.run_spectrum(
            method_file=args.method,
            sample_name=args.sample_name,
            sample_id=args.sample_id,
            data_file=args.data_file,
            measurement_mode=args.measurement_mode,
            correction=_correction_parameters(args),
            connect=args.connect,
            disconnect=args.disconnect,
            export_dir=export_dir,
            export_pattern=export_pattern,
            stable_seconds=stable_seconds,
        )
        output = {
            "ok": True,
            "commands": [_feedback_dict(item) for item in result.feedback],
            "export_path": str(result.export_path) if result.export_path else None,
        }
        print(json.dumps(output, indent=2))
        return 0
    except (LabSolutionsError, OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
