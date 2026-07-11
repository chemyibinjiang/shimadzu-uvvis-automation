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
            self.assertFalse((root / "control" / "SPC_CMD.txt").exists())

    def test_generic_send_is_plan_only_without_execute(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(["send", "12", "CellPosition=3"])
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["executed"])


if __name__ == "__main__":
    unittest.main()
