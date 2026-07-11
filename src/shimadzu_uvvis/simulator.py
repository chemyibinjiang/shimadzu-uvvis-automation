"""Small local simulator for exercising file exchange without an instrument."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .client import MODE_FILE_NAMES, LabSolutionsProtocolError, parse_exchange_text


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "sample"


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_simulated_export(export_dir: Path, sample: dict[str, str]) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    identity = sample.get("SampleID") or sample.get("SampleName") or "sample"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = export_dir / f"{_safe_name(identity)}_{timestamp}_SIMULATED.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["simulated", "sample_name", "sample_id"])
        writer.writerow(
            ["true", sample.get("SampleName", ""), sample.get("SampleID", "")]
        )
        writer.writerow([])
        writer.writerow(["wavelength_nm", "absorbance"])
        for wavelength, absorbance in (
            (300, 0.12),
            (400, 0.18),
            (500, 0.51),
            (550, 0.83),
            (600, 0.47),
            (700, 0.16),
            (800, 0.10),
            (900, 0.08),
        ):
            writer.writerow([wavelength, absorbance])
    return path


def _write_simulated_data_file(sample: dict[str, str]) -> Path | None:
    value = sample.get("DataFileName")
    if not value:
        return None
    path = Path(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "SIMULATED LabSolutions data placeholder - not instrument data\n",
        encoding="utf-8",
    )
    return path


def _append_command_log(path: Path | None, fields: dict[str, str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "fields": fields,
    }
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-dir", type=Path, required=True)
    parser.add_argument("--export-dir", type=Path)
    parser.add_argument("--mode", choices=sorted(MODE_FILE_NAMES), default="spectrum")
    parser.add_argument("--poll-interval", type=float, default=0.05)
    parser.add_argument("--response-delay", type=float, default=0.02)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--command-log", type=Path)
    args = parser.parse_args(argv)

    args.command_dir.mkdir(parents=True, exist_ok=True)
    command_name, response_name = MODE_FILE_NAMES[args.mode]
    command_path = args.command_dir / command_name
    response_path = args.command_dir / response_name
    sample: dict[str, str] = {}
    method_loaded = False
    response_path.unlink(missing_ok=True)
    if args.ready_file is not None:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(args.ready_file, "SIMULATOR READY\n")

    print(f"SIMULATOR ONLY: watching {command_path}", flush=True)
    try:
        while True:
            if not command_path.exists():
                time.sleep(args.poll_interval)
                continue
            try:
                fields = parse_exchange_text(
                    command_path.read_text(encoding="utf-8-sig")
                )
                command = int(fields["Command"])
                command_path.unlink()
                _append_command_log(args.command_log, fields)

                return_code = 0
                error = ""
                if command == 100:
                    method_loaded = bool(fields.get("ParameterFileName"))
                if command == 110:
                    sample = {key: value for key, value in fields.items() if key != "Command"}
                if command == 111:
                    if not method_loaded:
                        return_code = -1001
                        error = "No parameter file loaded (simulated)"
                    else:
                        data_path = _write_simulated_data_file(sample)
                        if data_path is not None:
                            print(f"SIMULATED data: {data_path}", flush=True)
                        if args.export_dir is not None:
                            export_path = _write_simulated_export(args.export_dir, sample)
                            print(f"SIMULATED export: {export_path}", flush=True)

                if args.response_delay > 0:
                    time.sleep(args.response_delay)

                _write_atomic(
                    response_path,
                    f'Command={command}\r\nReturn={return_code}\r\nError="{error}"\r\n',
                )
                print(
                    f"SIMULATED Command={command} Return={return_code}", flush=True
                )
            except (KeyError, ValueError, OSError, LabSolutionsProtocolError) as exc:
                command_path.unlink(missing_ok=True)
                escaped = str(exc).replace('"', "'").replace("\r", " ").replace("\n", " ")
                _write_atomic(
                    response_path,
                    f'Command=-1\r\nReturn=-1\r\nError="{escaped}"\r\n',
                )
    except KeyboardInterrupt:
        print("Simulator stopped", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
