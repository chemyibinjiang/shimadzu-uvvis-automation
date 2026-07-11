from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from shimadzu_uvvis.cli import main


class CliTests(unittest.TestCase):
    def test_spectrum_defaults_to_safe_plan_without_writing_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name in ("control", "export", "data", "audit", "methods"):
                (root / name).mkdir()
            method = root / "methods" / "test.vspm"
            method.write_text("simulated", encoding="utf-8")
            config = root / "control.toml"
            config.write_text(
                f"""
[labsolutions]
command_dir = "{(root / 'control').as_posix()}"

[export]
directory = "{(root / 'export').as_posix()}"
pattern = "{{sample_id}}*.csv"

[spectrum]
method_file = "{method.as_posix()}"
data_dir = "{(root / 'data').as_posix()}"

[scan_profiles.default]
method_file = "{method.as_posix()}"
start_nm = 300.0
stop_nm = 900.0
step_nm = 1.0

[audit]
directory = "{(root / 'audit').as_posix()}"
""".strip(),
                encoding="utf-8",
            )
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                exit_code = main(
                    [
                        "--config",
                        str(config),
                        "spectrum",
                        "--sample-name",
                        "validation_sample",
                        "--sample-id",
                        "validation_001",
                        "--start",
                        "300",
                        "--stop",
                        "900",
                        "--step",
                        "1",
                        "--wavelengths",
                        "450",
                        "550",
                        "650",
                    ]
                )

            payload = json.loads(output.getvalue())
            measurement = next(
                item for item in payload["commands"] if item["command"] == 111
            )
            self.assertEqual(exit_code, 0)
            self.assertFalse(payload["executed"])
            self.assertEqual(measurement["parameters"]["MeasurementMode"], 2)
            self.assertEqual(measurement["parameters"]["Discharge"], "OFF")
            self.assertEqual(payload["wavelength_control"]["profile"], "default")
            self.assertEqual(
                payload["wavelength_control"]["requested_wavelengths_nm"],
                [450.0, 550.0, 650.0],
            )
            self.assertFalse((root / "control" / "SPC_CMD.txt").exists())

            error_output = io.StringIO()
            with contextlib.redirect_stderr(error_output):
                mismatch_exit = main(
                    [
                        "--config",
                        str(config),
                        "spectrum",
                        "--sample-name",
                        "validation_sample",
                        "--sample-id",
                        "validation_002",
                        "--start",
                        "310",
                        "--stop",
                        "900",
                        "--step",
                        "1",
                    ]
                )
            mismatch = json.loads(error_output.getvalue())
            self.assertEqual(mismatch_exit, 1)
            self.assertIn("no registered LabSolutions method", mismatch["error"])

            off_grid_output = io.StringIO()
            with contextlib.redirect_stderr(off_grid_output):
                off_grid_exit = main(
                    [
                        "--config",
                        str(config),
                        "spectrum",
                        "--sample-name",
                        "validation_sample",
                        "--sample-id",
                        "validation_003",
                        "--profile",
                        "default",
                        "--wavelengths",
                        "450.5",
                    ]
                )
            off_grid = json.loads(off_grid_output.getvalue())
            self.assertEqual(off_grid_exit, 1)
            self.assertIn("not on profile", off_grid["error"])

    def test_generic_send_is_plan_only_without_execute(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(["send", "12", "CellPosition=3"])
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["executed"])


if __name__ == "__main__":
    unittest.main()
