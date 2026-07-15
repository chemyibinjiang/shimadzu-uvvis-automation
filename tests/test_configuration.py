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

[scan_profiles.default]
method_file = "methods/test.vspm"
start_nm = 300.0
stop_nm = 900.0
step_nm = 1.0
scan_speed_nm_per_min = 600.0

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
            self.assertEqual(settings.scan_profiles["default"].start_nm, 300.0)
            self.assertEqual(settings.scan_profiles["default"].stop_nm, 900.0)
            self.assertEqual(
                settings.scan_profiles["default"].scan_speed_nm_per_min,
                600.0,
            )

    def test_invalid_measurement_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "invalid.toml"
            config_path.write_text(
                "[spectrum]\nmeasurement_mode = 3\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "measurement_mode"):
                load_settings(config_path)

    def test_method_template_extension_is_validated_by_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "invalid.toml"
            config_path.write_text(
                """
[method_templates.wrong]
mode = "photometric"
method_file = "template.vspm"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must use: .vphm"):
                load_settings(config_path)

    def test_time_course_accepts_installed_and_manual_method_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for extension in (".vtmm", ".vtcm"):
                with self.subTest(extension=extension):
                    config_path = root / f"time-course-{extension[1:]}.toml"
                    config_path.write_text(
                        f"""
[method_templates.time_course]
mode = "time_course"
method_file = "template{extension}"
""".strip(),
                        encoding="utf-8",
                    )
                    settings = load_settings(config_path)
                    self.assertEqual(
                        settings.method_templates["time_course"].method_file.suffix,
                        extension,
                    )


if __name__ == "__main__":
    unittest.main()
