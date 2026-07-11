"""Small local simulator for exercising file exchange without an instrument."""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
import uuid
from datetime import datetime
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-dir", type=Path, required=True)
    parser.add_argument("--export-dir", type=Path)
    parser.add_argument("--mode", choices=sorted(MODE_FILE_NAMES), default="spectrum")
    parser.add_argument("--poll-interval", type=float, default=0.05)
    args = parser.parse_args(argv)

    args.command_dir.mkdir(parents=True, exist_ok=True)
    command_name, response_name = MODE_FILE_NAMES[args.mode]
    command_path = args.command_dir / command_name
    response_path = args.command_dir / response_name
    sample: dict[str, str] = {}

    print(f"SIMULATOR ONLY: watching {command_path}")
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

                if command == 110:
                    sample = {key: value for key, value in fields.items() if key != "Command"}
                if command == 111 and args.export_dir is not None:
                    export_path = _write_simulated_export(args.export_dir, sample)
                    print(f"SIMULATED export: {export_path}")

                _write_atomic(
                    response_path,
                    f'Command={command}\r\nReturn=0\r\nError=""\r\n',
                )
                print(f"SIMULATED Command={command} Return=0")
            except (KeyError, ValueError, OSError, LabSolutionsProtocolError) as exc:
                command_path.unlink(missing_ok=True)
                _write_atomic(
                    response_path,
                    f'Command=-1\r\nReturn=-1\r\nError="{str(exc)}"\r\n',
                )
    except KeyboardInterrupt:
        print("Simulator stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
