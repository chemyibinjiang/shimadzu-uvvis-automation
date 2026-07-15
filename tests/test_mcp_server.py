from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from shimadzu_uvvis.configuration import load_settings
from shimadzu_uvvis.mcp_server import (
    build_uvvis_measurement_plan,
    build_uvvis_scan_plan,
    create_mcp_server,
)


class McpServerTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        for name in ("control", "export", "data", "methods"):
            (root / name).mkdir()
        method = root / "methods" / "visible.vspm"
        method.write_text("simulated method", encoding="utf-8")
        templates = {
            "spectrum_absorbance": root / "methods" / "spectrum_absorbance.vspm",
            "photometric_absorbance": root / "methods" / "photometric_absorbance.vphm",
            "quantitation_absorbance": root / "methods" / "quantitation_absorbance.vqum",
            "time_course_absorbance": root / "methods" / "time_course_absorbance.vtmm",
        }
        for template in templates.values():
            template.write_text("simulated template", encoding="utf-8")
        config = root / "control.toml"
        config.write_text(
            f"""
[labsolutions]
command_dir = "{(root / 'control').as_posix()}"

[export]
directory = "{(root / 'export').as_posix()}"

[spectrum]
data_dir = "{(root / 'data').as_posix()}"
measurement_mode = 2
discharge_after_measurement = false

[method_generation]
output_directory = "{(root / 'methods' / 'generated').as_posix()}"

[method_templates.spectrum_absorbance]
mode = "spectrum"
signal_type = "absorbance"
method_file = "{templates['spectrum_absorbance'].as_posix()}"

[method_templates.photometric_absorbance]
mode = "photometric"
signal_type = "absorbance"
method_file = "{templates['photometric_absorbance'].as_posix()}"

[method_templates.quantitation_absorbance]
mode = "quantitation"
signal_type = "absorbance"
method_file = "{templates['quantitation_absorbance'].as_posix()}"

[method_templates.time_course_absorbance]
mode = "time_course"
signal_type = "absorbance"
method_file = "{templates['time_course_absorbance'].as_posix()}"

[scan_profiles.visible]
method_file = "{method.as_posix()}"
start_nm = 400.0
stop_nm = 700.0
step_nm = 1.0
scan_speed_nm_per_min = 600.0
""".strip(),
            encoding="utf-8",
        )
        return config, method

    def test_build_plan_is_read_only_and_reports_exact_method(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, method = self._fixture(root)
            settings = load_settings(config)

            plan = build_uvvis_scan_plan(
                settings, start_nm=400, stop_nm=700, step_nm=1
            )

            self.assertEqual(plan["status"], "planned")
            self.assertTrue(plan["plan_only"])
            self.assertEqual(plan["request"]["point_count"], 301)
            self.assertEqual(plan["profile"]["name"], "visible")
            self.assertEqual(plan["profile"]["method_file"], str(method))
            self.assertTrue(plan["execution_readiness"]["ready"])
            self.assertEqual(
                plan["timing"]["nominal_scan_traverse_seconds"], 30.0
            )
            self.assertFalse(plan["safety"]["writes_command_file"])
            self.assertFalse((root / "control" / "SPC_CMD.txt").exists())

            commands = plan["labsolutions_command_plan"]
            self.assertEqual(
                [item["command"] for item in commands], [0, 100, 110, 111]
            )
            self.assertEqual(
                commands[1]["parameters"]["ParameterFileName"], str(method)
            )

    def test_registered_mcp_tool_has_read_only_schema_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            server = create_mcp_server(config)

            tools = asyncio.run(server.list_tools())
            self.assertEqual(
                [tool.name for tool in tools],
                ["plan_uvvis_measurement", "plan_uvvis_scan"],
            )
            for tool in tools:
                self.assertTrue(tool.annotations.readOnlyHint)
                self.assertFalse(tool.annotations.destructiveHint)
            scan_tool = tools[1]
            direction_schema = scan_tool.inputSchema["properties"]["direction"]
            self.assertIn(
                {"enum": ["ascending", "descending"], "type": "string"},
                direction_schema["anyOf"],
            )

            _, structured = asyncio.run(
                server.call_tool(
                    "plan_uvvis_scan",
                    {"start_nm": 400, "stop_nm": 700, "step_nm": 1},
                )
            )
            self.assertEqual(structured["profile"]["name"], "visible")
            self.assertTrue(structured["plan_only"])

    def test_all_measurement_modes_return_official_command_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            cases = {
                "spectrum": (
                    {"start_nm": 400, "stop_nm": 700, "step_nm": 1},
                    [0, 100, 110, 111],
                    ".vspd",
                ),
                "photometric": (
                    {"wavelengths_nm": [450, 520, 650]},
                    [0, 300, 310, 311, 320, 321],
                    ".vphd",
                ),
                "quantitation": (
                    {"wavelength_nm": 520},
                    [0, 200, 210, 211, 220, 221],
                    ".vqud",
                ),
                "time_course": (
                    {
                        "wavelength_nm": 520,
                        "interval_seconds": 1,
                        "duration_seconds": 600,
                    },
                    [0, 400, 410, 411],
                    ".vtmd",
                ),
            }
            for mode, (parameters, command_numbers, data_extension) in cases.items():
                with self.subTest(mode=mode):
                    plan = build_uvvis_measurement_plan(
                        settings, mode=mode, **parameters
                    )
                    self.assertEqual(plan["status"], "method_generation_required")
                    self.assertEqual(plan["data_file_extension"], data_extension)
                    self.assertTrue(plan["method_generation"]["required"])
                    self.assertFalse(
                        plan["method_generation"]["automatic_generation_supported"]
                    )
                    self.assertFalse(plan["safety"]["edits_method_file"])
                    self.assertEqual(
                        [
                            command["command"]
                            for command in plan["labsolutions_command_plan"]
                        ],
                        command_numbers,
                    )

    def test_existing_generated_method_makes_path_checks_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            generated = root / "methods" / "generated"
            generated.mkdir()
            target = generated / "time_course_520nm_1s_600s_absorbance.vtmm"
            target.write_text("operator verified", encoding="utf-8")

            plan = build_uvvis_measurement_plan(
                load_settings(config),
                mode="time_course",
                wavelength_nm=520,
                interval_seconds=1,
                duration_seconds=600,
            )

            self.assertEqual(plan["status"], "planned")
            self.assertTrue(plan["execution_readiness"]["ready"])
            self.assertFalse(plan["method_generation"]["required"])


if __name__ == "__main__":
    unittest.main()
