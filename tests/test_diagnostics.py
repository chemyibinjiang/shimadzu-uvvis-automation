from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shimadzu_uvvis.configuration import load_settings
from shimadzu_uvvis.diagnostics import run_diagnostics


class DiagnosticsTests(unittest.TestCase):
    def _ready_config(self, root: Path) -> Path:
        for name in ("control", "export", "data", "audit", "methods"):
            (root / name).mkdir()
        (root / "methods" / "test.vspm").write_text(
            "simulated", encoding="utf-8"
        )
        config_path = root / "control.toml"
        config_path.write_text(
            """
[labsolutions]
command_dir = "control"

[export]
directory = "export"
pattern = "{sample_id}*.csv"

[spectrum]
method_file = "methods/test.vspm"
data_dir = "data"
measurement_mode = 2
discharge_after_measurement = false

[scan_profiles.default]
method_file = "methods/test.vspm"
start_nm = 300.0
stop_nm = 900.0
step_nm = 1.0

[audit]
directory = "audit"
""".strip(),
            encoding="utf-8",
        )
        return config_path

    def test_ready_control_pc_passes_write_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = load_settings(self._ready_config(root))

            report = run_diagnostics(settings, write_check=True)

            self.assertTrue(report.ok)
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_pending_command_fails_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = load_settings(self._ready_config(root))
            (settings.command_dir / "SPC_CMD.txt").write_text(
                "Command=0\n", encoding="utf-8"
            )

            report = run_diagnostics(settings)

            self.assertFalse(report.ok)
            pending = next(
                check for check in report.checks if check.name == "pending_command"
            )
            self.assertEqual(pending.status, "fail")


if __name__ == "__main__":
    unittest.main()
