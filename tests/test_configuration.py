from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shimadzu_uvvis.configuration import load_settings


class ConfigurationTests(unittest.TestCase):
    def test_relative_paths_are_resolved_from_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "control.toml"
            config_path.write_text(
                """
[labsolutions]
command_dir = "control"

[export]
directory = "export"

[spectrum]
method_file = "methods/test.vspm"
data_dir = "data"

[audit]
directory = "audit"
""".strip(),
                encoding="utf-8",
            )

            settings = load_settings(config_path)

            self.assertEqual(settings.command_dir, (root / "control").resolve())
            self.assertEqual(settings.export_dir, (root / "export").resolve())
            self.assertEqual(settings.measurement_mode, 2)
            self.assertFalse(settings.discharge_after_measurement)

    def test_invalid_measurement_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "invalid.toml"
            config_path.write_text(
                "[spectrum]\nmeasurement_mode = 3\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "measurement_mode"):
                load_settings(config_path)


if __name__ == "__main__":
    unittest.main()
