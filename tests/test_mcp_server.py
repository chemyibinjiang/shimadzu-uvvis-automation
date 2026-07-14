from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from shimadzu_uvvis.configuration import load_settings
from shimadzu_uvvis.mcp_server import build_uvvis_scan_plan, create_mcp_server


class McpServerTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        for name in ("control", "export", "data", "methods"):
            (root / name).mkdir()
        method = root / "methods" / "visible.vspm"
        method.write_text("simulated method", encoding="utf-8")
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
            self.assertEqual([tool.name for tool in tools], ["plan_uvvis_scan"])
            tool = tools[0]
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)
            direction_schema = tool.inputSchema["properties"]["direction"]
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


if __name__ == "__main__":
    unittest.main()
