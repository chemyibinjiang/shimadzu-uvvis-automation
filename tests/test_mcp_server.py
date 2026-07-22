from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from shimadzu_uvvis.configuration import load_settings
from shimadzu_uvvis.mcp_server import (
    build_uvvis_measurement_plan,
    build_uvvis_sample_batch_plan,
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
            "quantitation_absorbance": root
            / "methods"
            / "quantitation_absorbance.vqum",
            "time_course_absorbance": root / "methods" / "time_course_absorbance.vtmm",
        }
        for template in templates.values():
            template.write_text("simulated template", encoding="utf-8")
        config = root / "control.toml"
        config.write_text(
            f"""
[labsolutions]
command_dir = "{(root / "control").as_posix()}"

[export]
directory = "{(root / "export").as_posix()}"

[spectrum]
data_dir = "{(root / "data").as_posix()}"
measurement_mode = 2
discharge_after_measurement = false

[method_generation]
output_directory = "{(root / "methods" / "generated").as_posix()}"

[method_templates.spectrum_absorbance]
mode = "spectrum"
signal_type = "absorbance"
method_file = "{templates["spectrum_absorbance"].as_posix()}"

[method_templates.photometric_absorbance]
mode = "photometric"
signal_type = "absorbance"
method_file = "{templates["photometric_absorbance"].as_posix()}"

[method_templates.quantitation_absorbance]
mode = "quantitation"
signal_type = "absorbance"
method_file = "{templates["quantitation_absorbance"].as_posix()}"

[method_templates.time_course_absorbance]
mode = "time_course"
signal_type = "absorbance"
method_file = "{templates["time_course_absorbance"].as_posix()}"

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

            plan = build_uvvis_scan_plan(settings, start_nm=400, stop_nm=700, step_nm=1)

            self.assertEqual(plan["status"], "planned")
            self.assertTrue(plan["plan_only"])
            self.assertEqual(plan["request"]["point_count"], 301)
            self.assertEqual(plan["profile"]["name"], "visible")
            self.assertEqual(plan["profile"]["method_file"], str(method))
            self.assertTrue(plan["execution_readiness"]["ready"])
            self.assertEqual(plan["timing"]["nominal_scan_traverse_seconds"], 30.0)
            self.assertFalse(plan["safety"]["writes_command_file"])
            self.assertFalse((root / "control" / "SPC_CMD.txt").exists())

            commands = plan["labsolutions_command_plan"]
            self.assertEqual([item["command"] for item in commands], [0, 100, 110, 111])
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
                [
                    "plan_uvvis_measurement",
                    "generate_uvvis_method",
                    "plan_uvvis_sample_batch",
                    "plan_uvvis_scan",
                    "start_uvvis_batch",
                    "correct_uvvis_baseline",
                    "measure_next_uvvis_sample",
                    "recover_uvvis_spectrum_result",
                    "get_uvvis_batch_status",
                    "abort_uvvis_batch",
                ],
            )
            by_name = {tool.name: tool for tool in tools}
            for name in (
                "plan_uvvis_measurement",
                "plan_uvvis_sample_batch",
                "plan_uvvis_scan",
                "get_uvvis_batch_status",
            ):
                self.assertTrue(by_name[name].annotations.readOnlyHint)
                self.assertFalse(by_name[name].annotations.destructiveHint)
            self.assertFalse(by_name["generate_uvvis_method"].annotations.readOnlyHint)
            self.assertFalse(
                by_name["generate_uvvis_method"].annotations.destructiveHint
            )
            self.assertFalse(by_name["start_uvvis_batch"].annotations.readOnlyHint)
            self.assertFalse(by_name["start_uvvis_batch"].annotations.destructiveHint)
            self.assertFalse(
                by_name["recover_uvvis_spectrum_result"].annotations.readOnlyHint
            )
            self.assertFalse(
                by_name["recover_uvvis_spectrum_result"].annotations.destructiveHint
            )
            for name in (
                "correct_uvvis_baseline",
                "measure_next_uvvis_sample",
                "abort_uvvis_batch",
            ):
                self.assertFalse(by_name[name].annotations.readOnlyHint)
                self.assertTrue(by_name[name].annotations.destructiveHint)
            batch_tool = tools[2]
            self.assertEqual(
                batch_tool.inputSchema["$defs"]["SampleBatchItem"]["required"],
                ["sample_name", "sample_id"],
            )
            self.assertEqual(
                batch_tool.inputSchema["properties"]["baseline_policy"]["enum"],
                ["new", "reuse_valid"],
            )
            scan_tool = tools[3]
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

    def test_batch_status_mcp_tool_uses_persisted_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            calls: list[str] = []

            class StubController:
                def get_status(self, batch_id: str) -> dict[str, object]:
                    calls.append(batch_id)
                    return {
                        "batch_id": batch_id,
                        "state": "WAITING_FOR_SAMPLE",
                        "next_sample": {"sample_id": "001_sample_a"},
                    }

            stub = StubController()
            server = create_mcp_server(
                config,
                batch_controller_factory=lambda settings: stub,  # type: ignore[arg-type,return-value]
            )

            _, structured = asyncio.run(
                server.call_tool(
                    "get_uvvis_batch_status",
                    {"batch_id": "batch_001"},
                )
            )

            self.assertEqual(calls, ["batch_001"])
            self.assertEqual(structured["state"], "WAITING_FOR_SAMPLE")
            self.assertEqual(structured["next_sample"]["sample_id"], "001_sample_a")

    def test_recover_spectrum_result_mcp_tool_uses_controller_without_confirmation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            calls: list[str] = []

            class StubController:
                def recover_spectrum_result(
                    self, batch_id: str
                ) -> dict[str, object]:
                    calls.append(batch_id)
                    return {
                        "batch_id": batch_id,
                        "state": "COMPLETED",
                        "completed_sample_count": 1,
                    }

            stub = StubController()
            server = create_mcp_server(
                config,
                batch_controller_factory=lambda settings: stub,  # type: ignore[arg-type,return-value]
            )

            _, structured = asyncio.run(
                server.call_tool(
                    "recover_uvvis_spectrum_result",
                    {"batch_id": "batch_001"},
                )
            )

            self.assertEqual(calls, ["batch_001"])
            self.assertEqual(structured["state"], "COMPLETED")
            self.assertEqual(structured["completed_sample_count"], 1)

    def test_generate_method_mcp_tool_passes_normalized_spectrum_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            calls: list[object] = []

            class StubMethodManager:
                def generate(self, request, *, template_name=None):  # type: ignore[no-untyped-def]
                    calls.extend([request, template_name])
                    return {
                        "status": "generated",
                        "method_file": str(root / "generated.vspm"),
                        "request": dict(request.parameters),
                    }

            stub = StubMethodManager()
            server = create_mcp_server(
                config,
                method_manager_factory=lambda settings: stub,  # type: ignore[arg-type,return-value]
            )

            _, structured = asyncio.run(
                server.call_tool(
                    "generate_uvvis_method",
                    {
                        "mode": "spectrum",
                        "start_nm": 400,
                        "stop_nm": 700,
                        "step_nm": 5,
                        "template_name": "spectrum_absorbance",
                    },
                )
            )

            request = calls[0]
            self.assertEqual(request.parameters["lower_nm"], 400.0)
            self.assertEqual(request.parameters["upper_nm"], 700.0)
            self.assertEqual(request.parameters["step_nm"], 5.0)
            self.assertEqual(calls[1], "spectrum_absorbance")
            self.assertEqual(structured["status"], "generated")

    def test_ten_nm_spectrum_plan_reports_exact_photometric_alternative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)

            plan = build_uvvis_measurement_plan(
                load_settings(config),
                mode="spectrum",
                start_nm=400,
                stop_nm=700,
                step_nm=10,
            )

            generation = plan["method_generation"]
            self.assertFalse(generation["automatic_generation_supported"])
            self.assertIn("Photometric", generation["reason"])
            self.assertIn("5", generation["reason"])

    def test_auto_plan_routes_ten_nm_range_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)

            plan = build_uvvis_measurement_plan(
                load_settings(config),
                start_nm=400,
                stop_nm=700,
                step_nm=10,
            )

            self.assertEqual(plan["mode"], "photometric")
            self.assertEqual(plan["routing"]["requested_mode"], "auto")
            self.assertEqual(plan["routing"]["selected_mode"], "photometric")
            self.assertTrue(plan["routing"]["selected_before_labsolutions_start"])
            self.assertTrue(plan["routing"]["current_mcp_execution_supported"])
            self.assertEqual(
                plan["request"]["wavelengths_nm"],
                [float(value) for value in range(400, 701, 10)],
            )
            self.assertEqual(
                [command["command"] for command in plan["labsolutions_command_plan"]],
                [0, 300, 310, 311, 320, 321] * 4,
            )
            self.assertTrue(plan["method_generation"]["automatic_generation_supported"])
            self.assertEqual(len(plan["method_generation"]["segments"]), 4)
            self.assertEqual(
                [
                    len(segment["requested_parameters"]["wavelengths_nm"])
                    for segment in plan["method_generation"]["segments"]
                ],
                [10, 10, 10, 1],
            )

    def test_mcp_plan_defaults_to_auto_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            server = create_mcp_server(config)

            _, structured = asyncio.run(
                server.call_tool(
                    "plan_uvvis_measurement",
                    {"start_nm": 400, "stop_nm": 700, "step_nm": 10},
                )
            )

            self.assertEqual(structured["mode"], "photometric")
            self.assertEqual(structured["routing"]["requested_mode"], "auto")

    def test_sample_batch_assigns_unique_paths_and_operator_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            before = {path.relative_to(root) for path in root.rglob("*")}

            plan = build_uvvis_sample_batch_plan(
                settings,
                batch_id="experiment_20260716_001",
                mode="spectrum",
                samples=[
                    {"sample_name": "sample A", "sample_id": "sample_a"},
                    {"sample_name": "sample B", "sample_id": "sample_b"},
                ],
                reference_name="blank",
                start_nm=400,
                stop_nm=700,
                step_nm=1,
            )

            after = {path.relative_to(root) for path in root.rglob("*")}
            self.assertEqual(before, after)
            self.assertTrue(plan["plan_only"])
            self.assertEqual(plan["sample_count"], 2)
            self.assertEqual(plan["reference"]["name"], "blank")
            self.assertEqual(plan["batch_preparation"]["policy"], "new")
            self.assertEqual(
                plan["batch_preparation"]["operator_gate"]["type"],
                "place_blank_and_confirm",
            )
            self.assertEqual(
                plan["batch_preparation"]["labsolutions_command"],
                {
                    "command": 21,
                    "name": "automatic_correction",
                    "parameters": {"CorrectionType": 1},
                    "send_only_after_operator_gate": True,
                },
            )
            self.assertEqual(
                [sample["sequence_number"] for sample in plan["samples"]], [1, 2]
            )
            self.assertEqual(
                [sample["sample_id"] for sample in plan["samples"]],
                ["001_sample_a", "002_sample_b"],
            )
            self.assertEqual(
                {sample["operator_gate"]["type"] for sample in plan["samples"]},
                {"replace_sample_and_confirm"},
            )
            self.assertEqual(
                {sample["operator_gate"]["status"] for sample in plan["samples"]},
                {"required"},
            )
            first_paths = plan["samples"][0]["paths"]
            second_paths = plan["samples"][1]["paths"]
            self.assertNotEqual(
                first_paths["sample_directory"], second_paths["sample_directory"]
            )
            self.assertTrue(first_paths["raw_data_file"].endswith("001_sample_a.vspd"))
            self.assertTrue(second_paths["raw_data_file"].endswith("002_sample_b.vspd"))
            self.assertFalse(plan["safety"]["unattended_execution_supported"])

    def test_sample_batch_reuses_valid_baseline_without_operator_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)

            plan = build_uvvis_sample_batch_plan(
                load_settings(config),
                batch_id="batch_001",
                mode="spectrum",
                samples=[{"sample_name": "one", "sample_id": "one"}],
                reference_name="blank",
                baseline_policy="reuse_valid",
                start_nm=400,
                stop_nm=700,
                step_nm=1,
            )

            preparation = plan["batch_preparation"]
            self.assertEqual(preparation["policy"], "reuse_valid")
            self.assertIsNone(preparation["operator_gate"])
            self.assertIsNone(preparation["labsolutions_command"])
            self.assertTrue(preparation["automatic_reuse"])
            self.assertFalse(plan["safety"]["operator_baseline_confirmation_required"])

    def test_sample_batch_uses_mode_specific_data_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            cases = {
                "spectrum": (
                    {"start_nm": 400, "stop_nm": 700, "step_nm": 1},
                    ".vspd",
                ),
                "photometric": ({"wavelengths_nm": [450, 520]}, ".vphd"),
                "quantitation": ({"wavelength_nm": 520}, ".vqud"),
                "time_course": (
                    {
                        "wavelength_nm": 520,
                        "interval_seconds": 1,
                        "duration_seconds": 60,
                    },
                    ".vtmd",
                ),
            }
            for mode, (parameters, extension) in cases.items():
                with self.subTest(mode=mode):
                    plan = build_uvvis_sample_batch_plan(
                        settings,
                        batch_id=f"batch_{mode}",
                        mode=mode,
                        samples=[{"sample_name": "sample", "sample_id": "sample"}],
                        reference_name="blank",
                        **parameters,
                    )
                    self.assertTrue(
                        plan["samples"][0]["paths"]["raw_data_file"].endswith(extension)
                    )

    def test_sample_batch_rejects_duplicate_and_invalid_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            common = {
                "settings": settings,
                "mode": "spectrum",
                "reference_name": "blank",
                "start_nm": 400,
                "stop_nm": 700,
                "step_nm": 1,
            }

            with self.assertRaisesRegex(ValueError, "duplicate sample_id"):
                build_uvvis_sample_batch_plan(
                    batch_id="batch_001",
                    samples=[
                        {"sample_name": "one", "sample_id": "same"},
                        {"sample_name": "two", "sample_id": "same"},
                    ],
                    **common,
                )
            with self.assertRaisesRegex(ValueError, "batch_id must contain"):
                build_uvvis_sample_batch_plan(
                    batch_id="../batch",
                    samples=[{"sample_name": "one", "sample_id": "one"}],
                    **common,
                )
            with self.assertRaisesRegex(ValueError, "baseline_policy must be"):
                build_uvvis_sample_batch_plan(
                    batch_id="batch_001",
                    samples=[{"sample_name": "one", "sample_id": "one"}],
                    baseline_policy="skip",  # type: ignore[arg-type]
                    **common,
                )

    def test_sample_batch_reports_existing_batch_path_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            existing_batch = root / "data" / "batch_001"
            existing_batch.mkdir()

            plan = build_uvvis_sample_batch_plan(
                settings,
                batch_id="batch_001",
                mode="spectrum",
                samples=[{"sample_name": "one", "sample_id": "one"}],
                reference_name="blank",
                start_nm=400,
                stop_nm=700,
                step_nm=1,
            )

            self.assertEqual(plan["status"], "path_conflict")
            self.assertFalse(plan["execution_readiness"]["ready"])
            self.assertIn(
                "batch_and_sample_directories_are_new",
                plan["execution_readiness"]["blocking_reasons"],
            )
            self.assertIn(
                str(existing_batch), plan["execution_readiness"]["path_conflicts"]
            )

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
                    self.assertEqual(
                        plan["method_generation"]["automatic_generation_supported"],
                        mode in {"spectrum", "photometric"},
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

            self.assertEqual(plan["status"], "execution_not_supported")
            self.assertFalse(plan["execution_readiness"]["ready"])
            self.assertIn(
                "mcp_execution_supported",
                plan["execution_readiness"]["blocking_reasons"],
            )
            self.assertFalse(plan["method_generation"]["required"])

    def test_template_hash_mismatch_blocks_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            contents = config.read_text(encoding="utf-8")
            marker = f'method_file = "{(root / "methods" / "time_course_absorbance.vtmm").as_posix()}"'
            contents = contents.replace(
                marker,
                marker
                + "\nsha256 = "
                + '"0000000000000000000000000000000000000000000000000000000000000000"',
                1,
            )
            config.write_text(contents, encoding="utf-8")

            plan = build_uvvis_measurement_plan(
                load_settings(config),
                mode="time_course",
                wavelength_nm=520,
                interval_seconds=1,
                duration_seconds=600,
            )

            self.assertFalse(
                plan["execution_readiness"]["checks"]["template_sha256_matches"]
            )
            self.assertIn(
                "template_sha256_matches",
                plan["execution_readiness"]["blocking_reasons"],
            )


if __name__ == "__main__":
    unittest.main()
