from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from shimadzu_uvvis.batch_workflow import (
    SpectrumBatchController,
    SpectrumBatchError,
)
from shimadzu_uvvis.client import (
    Feedback,
    LabSolutionsCommandError,
    LabSolutionsTimeoutError,
)
from shimadzu_uvvis.configuration import load_settings
from shimadzu_uvvis.mcp_server import build_uvvis_sample_batch_plan
from shimadzu_uvvis.results import SpectrumResultError
from shimadzu_uvvis.runtime_manager import RuntimeReady


class FakeSpectrumClient:
    def __init__(self, export_dir: Path) -> None:
        self.export_dir = export_dir
        self.commands: list[tuple[int, dict[str, object]]] = []
        self.data_file: Path | None = None
        self.sample_id: str | None = None
        self.wavelengths: list[float] = [500.0]
        self.fail_command: int | None = None
        self.reject_command: int | None = None
        self.already_connected = False
        self.fail_export = False

    def workflow_session(self):
        return nullcontext()

    def send_command(self, command: int, **parameters: object) -> Feedback:
        self.commands.append((command, dict(parameters)))
        if command == self.fail_command:
            raise LabSolutionsTimeoutError(f"simulated timeout for command {command}")
        if command == 1 and self.already_connected:
            raise LabSolutionsCommandError(
                Feedback(
                    command=command,
                    return_code=-3002,
                    error="already connected",
                    fields=MappingProxyType(
                        {
                            "Command": str(command),
                            "Return": "-3002",
                            "Error": "already connected",
                        }
                    ),
                )
            )
        if command == self.reject_command:
            raise LabSolutionsCommandError(
                Feedback(
                    command=command,
                    return_code=-3001,
                    error="instrument is not connected",
                    fields=MappingProxyType(
                        {
                            "Command": str(command),
                            "Return": "-3001",
                            "Error": "instrument is not connected",
                        }
                    ),
                )
            )
        if command == 110:
            self.data_file = Path(str(parameters["DataFileName"]))
            self.sample_id = str(parameters["SampleID"])
        if command == 100:
            stem = Path(str(parameters["ParameterFileName"])).stem
            range_text = stem.removeprefix("spectrum_").removesuffix(
                "nm_absorbance"
            )
            lower_text, upper_text, step_text = range_text.split("_")
            lower = float(lower_text.replace("p", "."))
            upper = float(upper_text.replace("p", "."))
            step = float(step_text.replace("p", "."))
            self.wavelengths = [
                lower + index * step
                for index in range(round((upper - lower) / step) + 1)
            ]
        if command == 300:
            self.data_file = Path(str(parameters["DataFileName"]))
            stem = Path(str(parameters["ParameterFileName"])).stem
            wavelength_text = stem.removeprefix("photometric_").removesuffix(
                "nm_absorbance"
            )
            self.wavelengths = [
                float(value.replace("p", ".")) for value in wavelength_text.split("_")
            ]
        if command == 310:
            self.sample_id = str(parameters["SampleID"])
        if command == 111:
            assert self.data_file is not None
            assert self.sample_id is not None
            self.data_file.write_bytes(b"SIMULATED VSPD")
            (self.export_dir / f"{self.sample_id}_result.csv").write_text(
                "wavelength,absorbance\n"
                + "".join(
                    f"{wavelength:g},{0.1 + wavelength / 10000:.6f}\n"
                    for wavelength in reversed(self.wavelengths)
                ),
                encoding="utf-8",
            )
        if command == 320:
            assert self.data_file is not None
            assert self.sample_id is not None
            self.data_file.write_bytes(b"SIMULATED VPHD")
            (self.export_dir / f"{self.sample_id}_result.csv").write_text(
                "wavelength_nm,absorbance\n"
                + "".join(
                    f"{wavelength:g},{0.1 + wavelength / 10000:.6f}\n"
                    for wavelength in self.wavelengths
                ),
                encoding="utf-8",
            )
        return Feedback(
            command=command,
            return_code=0,
            error="",
            fields=MappingProxyType(
                {"Command": str(command), "Return": "0", "Error": ""}
            ),
        )

    def wait_for_export(
        self,
        export_dir: str | Path,
        *,
        pattern: str,
        since: float,
        timeout: float,
        stable_seconds: float,
    ) -> Path:
        if self.fail_export:
            raise LabSolutionsTimeoutError(
                f"Timed out after {timeout}s waiting for a stable export "
                f"matching {pattern!r} in {export_dir}"
            )
        matches = list(Path(export_dir).glob(pattern))
        if len(matches) != 1:
            raise LabSolutionsTimeoutError(
                f"expected one simulated export for {pattern!r}, got {matches}"
            )
        return matches[0]


class FakeRuntimeManager:
    def __init__(self, command_dir: Path) -> None:
        self.command_dir = command_dir
        self.calls: list[bool] = []
        self.prompt_dismissal_calls: list[float] = []
        self.dismiss_prompt = False
        self.prompt_error: Exception | None = None

    def ensure_ready(self, *, allow_reconfigure: bool) -> RuntimeReady:
        self.calls.append(allow_reconfigure)
        return RuntimeReady(
            process_id=1234,
            window_handle=5678,
            launched=False,
            command_directory=self.command_dir,
            command_directory_changed=False,
            waiting_status="Automatic Control - Waiting",
            feedback=Feedback(
                command=0,
                return_code=0,
                error="",
                fields=MappingProxyType({"Command": "0", "Return": "0", "Error": ""}),
            ),
        )

    def dismiss_parameter_change_baseline_prompt(
        self, *, wait_seconds: float = 0.0
    ) -> bool:
        self.prompt_dismissal_calls.append(wait_seconds)
        if self.prompt_error is not None:
            raise self.prompt_error
        return self.dismiss_prompt


class SpectrumBatchControllerTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        for name in ("control", "export", "data", "templates", "generated"):
            (root / name).mkdir()
        template = root / "templates" / "spectrum_absorbance.vspm"
        template.write_bytes(b"TEMPLATE")
        generated = root / "generated" / "spectrum_400_700_1nm_absorbance.vspm"
        generated.write_bytes(b"VERIFIED GENERATED METHOD")
        config = root / "control.toml"
        config.write_text(
            f"""
[labsolutions]
command_dir = "{(root / "control").as_posix()}"
mode = "spectrum"
timeout_seconds = 1.0
poll_interval_seconds = 0.01
lock_timeout_seconds = 1.0

[export]
directory = "{(root / "export").as_posix()}"
pattern = "{{sample_id}}*.csv"
timeout_seconds = 1.0
stable_seconds = 0.0

[spectrum]
data_dir = "{(root / "data").as_posix()}"
measurement_mode = 2
connect_before_run = false
disconnect_after_run = false
discharge_after_measurement = false

[audit]
directory = "{(root / "audit").as_posix()}"

[method_generation]
output_directory = "{(root / "generated").as_posix()}"

[method_templates.spectrum_absorbance]
mode = "spectrum"
signal_type = "absorbance"
method_file = "{template.as_posix()}"

[results]
directory = "{(root / "outputs").as_posix()}"
""".strip(),
            encoding="utf-8",
        )
        return config, generated

    def _plan(self, config: Path, batch_id: str, *, baseline_policy: str = "new"):
        return build_uvvis_sample_batch_plan(
            load_settings(config),
            batch_id=batch_id,
            mode="spectrum",
            samples=[
                {"sample_name": "sample A", "sample_id": "sample_a"},
                {"sample_name": "sample B", "sample_id": "sample_b"},
            ],
            reference_name="blank",
            baseline_policy=baseline_policy,  # type: ignore[arg-type]
            start_nm=400,
            stop_nm=700,
            step_nm=1,
        )

    def test_full_two_sample_state_flow_and_archival(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )

            started = controller.start(
                self._plan(config, "batch_001"), execution_confirmed=True
            )
            self.assertEqual(started["state"], "WAITING_FOR_BLANK")
            self.assertEqual([item[0] for item in fake.commands], [100])
            self.assertEqual(runtime.prompt_dismissal_calls, [2.0])

            corrected = controller.correct_baseline(
                "batch_001", blank_loaded_confirmed=True
            )
            self.assertEqual(corrected["state"], "WAITING_FOR_SAMPLE")
            self.assertEqual(corrected["next_sample"]["sample_id"], "001_sample_a")
            self.assertEqual(fake.commands[-1][0], 21)
            self.assertEqual(fake.commands[-1][1]["CorrectionType"], 1)

            with self.assertRaisesRegex(SpectrumBatchError, "next sample is"):
                controller.measure_next(
                    "batch_001",
                    sample_id="002_sample_b",
                    sample_loaded_confirmed=True,
                )

            first = controller.measure_next(
                "batch_001",
                sample_id="001_sample_a",
                sample_loaded_confirmed=True,
            )
            self.assertEqual(first["state"], "WAITING_FOR_SAMPLE")
            self.assertEqual(first["completed_sample_count"], 1)
            self.assertEqual(first["next_sample"]["sample_id"], "002_sample_b")

            completed = controller.measure_next(
                "batch_001",
                sample_id="002_sample_b",
                sample_loaded_confirmed=True,
            )
            self.assertEqual(completed["state"], "COMPLETED")
            self.assertEqual(completed["completed_sample_count"], 2)
            self.assertFalse(controller.active_batch_path.exists())
            self.assertEqual(
                [item[0] for item in fake.commands],
                [100, 21, 110, 111, 110, 111],
            )
            self.assertEqual(runtime.calls, [True, False, False, False])
            for sample_id in ("001_sample_a", "002_sample_b"):
                sample_dir = root / "data" / "batch_001" / sample_id
                self.assertTrue((sample_dir / "raw" / f"{sample_id}.vspd").is_file())
                self.assertTrue((sample_dir / "manifest.json").is_file())
                self.assertEqual(len(list((sample_dir / "export").glob("*.csv"))), 2)
                result = json.loads(
                    (sample_dir / "export" / "result.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(result["mode"], "spectrum")
                self.assertEqual(result["point_count"], 301)
                self.assertEqual(
                    result["maximum_absorbance"]["wavelength_nm"], 700.0
                )
                self.assertTrue((sample_dir / "plot" / "result.png").is_file())
                published = root / "outputs" / "batch_001" / sample_id
                self.assertTrue((published / "result.csv").is_file())
                self.assertTrue((published / "result.json").is_file())
                self.assertTrue((published / "result.png").is_file())

            reloaded = SpectrumBatchController(settings)
            self.assertEqual(reloaded.get_status("batch_001")["state"], "COMPLETED")

    def test_confirmations_and_abort_are_state_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )
            plan = self._plan(config, "batch_abort")

            with self.assertRaisesRegex(SpectrumBatchError, "must be true"):
                controller.start(plan, execution_confirmed=False)
            self.assertFalse((root / "data" / "batch_abort").exists())

            controller.start(plan, execution_confirmed=True)
            with self.assertRaisesRegex(SpectrumBatchError, "must be true"):
                controller.correct_baseline("batch_abort", blank_loaded_confirmed=False)
            with self.assertRaisesRegex(SpectrumBatchError, "must be true"):
                controller.abort(
                    "batch_abort", reason="student stopped", abort_confirmed=False
                )

            aborted = controller.abort(
                "batch_abort", reason="student stopped", abort_confirmed=True
            )
            self.assertEqual(aborted["state"], "ABORTED")
            self.assertFalse(controller.active_batch_path.exists())
            self.assertEqual([item[0] for item in fake.commands], [100])

    def test_start_accepts_command_1_already_connected_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "connect_before_run = false",
                    "connect_before_run = true",
                ),
                encoding="utf-8",
            )
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            fake.already_connected = True
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )

            started = controller.start(
                self._plan(config, "batch_connected"), execution_confirmed=True
            )

            self.assertEqual(started["state"], "WAITING_FOR_BLANK")
            self.assertEqual([item[0] for item in fake.commands], [1, 100])
            manifest = json.loads(
                (root / "data" / "batch_connected" / "batch-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["commands"][1]["return_code"], -3002)
            self.assertEqual(
                manifest["commands"][1]["phase"], "start:already_connected"
            )
            self.assertEqual(
                manifest["events"][-2]["type"], "instrument_already_connected"
            )

    def test_prompt_dismissal_failure_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            runtime.prompt_error = RuntimeError("cannot close prompt")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )

            with self.assertRaisesRegex(RuntimeError, "cannot close prompt"):
                controller.start(
                    self._plan(config, "batch_prompt_failure"),
                    execution_confirmed=True,
                )

            status = controller.get_status("batch_prompt_failure")
            self.assertEqual(status["state"], "RECOVERY_REQUIRED")
            self.assertEqual(status["last_error"]["operation"], "start")

    def test_timeout_enters_recovery_and_blocks_abort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )
            controller.start(
                self._plan(config, "batch_recovery"), execution_confirmed=True
            )
            fake.fail_command = 21

            with self.assertRaises(LabSolutionsTimeoutError):
                controller.correct_baseline(
                    "batch_recovery", blank_loaded_confirmed=True
                )

            status = controller.get_status("batch_recovery")
            self.assertEqual(status["state"], "RECOVERY_REQUIRED")
            self.assertEqual(status["last_error"]["operation"], "correct_baseline")
            with self.assertRaisesRegex(SpectrumBatchError, "can only be aborted"):
                controller.abort(
                    "batch_recovery",
                    reason="cannot continue",
                    abort_confirmed=True,
                )

    def test_invalid_spectrum_export_requires_result_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )
            controller.start(
                self._plan(config, "batch_bad_spectrum"), execution_confirmed=True
            )
            controller.correct_baseline(
                "batch_bad_spectrum", blank_loaded_confirmed=True
            )
            fake.wavelengths = [400.0, 700.0]

            with self.assertRaises(SpectrumResultError):
                controller.measure_next(
                    "batch_bad_spectrum",
                    sample_id="001_sample_a",
                    sample_loaded_confirmed=True,
                )

            status = controller.get_status("batch_bad_spectrum")
            self.assertEqual(status["state"], "RECOVERY_REQUIRED")
            self.assertEqual(
                status["last_error"]["operation"],
                "process_result:001_sample_a",
            )

    def test_export_timeout_recovers_from_saved_vspd_without_remeasurement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            fake.fail_export = True
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )
            controller.start(
                self._plan(config, "batch_vspd_recovery"),
                execution_confirmed=True,
            )
            controller.correct_baseline(
                "batch_vspd_recovery", blank_loaded_confirmed=True
            )
            with self.assertRaises(LabSolutionsTimeoutError):
                controller.measure_next(
                    "batch_vspd_recovery",
                    sample_id="001_sample_a",
                    sample_loaded_confirmed=True,
                )
            commands_before_recovery = list(fake.commands)

            def normalize_saved_vspd(**kwargs: object) -> list[object]:
                csv_file = Path(str(kwargs["csv_file"]))
                csv_file.write_text(
                    "wavelength_nm,absorbance\n"
                    + "".join(
                        f"{wavelength},{0.1 + wavelength / 10000:.6f}\n"
                        for wavelength in range(400, 701)
                    ),
                    encoding="utf-8",
                )
                return []

            with patch(
                "shimadzu_uvvis.batch_workflow.normalize_spectrum_data_file",
                side_effect=normalize_saved_vspd,
            ):
                recovered = controller.recover_spectrum_result(
                    "batch_vspd_recovery"
                )

            self.assertEqual(recovered["state"], "WAITING_FOR_SAMPLE")
            self.assertEqual(recovered["completed_sample_count"], 1)
            self.assertEqual(fake.commands, commands_before_recovery)
            sample_dir = root / "data" / "batch_vspd_recovery" / "001_sample_a"
            self.assertTrue((sample_dir / "export" / "result.csv").is_file())
            self.assertTrue((sample_dir / "export" / "result.json").is_file())
            self.assertTrue((sample_dir / "plot" / "result.png").is_file())

    def test_definite_command_rejection_ends_batch_without_recovery_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )
            controller.start(
                self._plan(config, "batch_rejected"), execution_confirmed=True
            )
            fake.reject_command = 21

            with self.assertRaises(LabSolutionsCommandError):
                controller.correct_baseline(
                    "batch_rejected", blank_loaded_confirmed=True
                )

            status = controller.get_status("batch_rejected")
            self.assertEqual(status["state"], "FAILED")
            self.assertFalse(controller.active_batch_path.exists())

    def test_valid_baseline_is_reused_without_command_21(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )
            controller.start(
                self._plan(config, "batch_first"), execution_confirmed=True
            )
            controller.correct_baseline("batch_first", blank_loaded_confirmed=True)
            controller.abort(
                "batch_first", reason="baseline validation only", abort_confirmed=True
            )

            before = len(fake.commands)
            reused = controller.start(
                self._plan(
                    config,
                    "batch_reuse",
                    baseline_policy="reuse_valid",
                ),
                execution_confirmed=True,
            )
            self.assertEqual(reused["state"], "WAITING_FOR_SAMPLE")
            self.assertEqual(reused["baseline"]["status"], "REUSED")
            self.assertEqual(
                [item[0] for item in fake.commands[before:]],
                [100],
            )

    def test_connected_session_reuses_baseline_with_connect_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )
            controller.start(
                self._plan(config, "batch_first"), execution_confirmed=True
            )
            controller.correct_baseline("batch_first", blank_loaded_confirmed=True)
            controller.abort(
                "batch_first", reason="baseline validation only", abort_confirmed=True
            )

            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "connect_before_run = false",
                    "connect_before_run = true",
                ),
                encoding="utf-8",
            )
            connected_settings = load_settings(config)
            fake.already_connected = True
            connected_controller = SpectrumBatchController(
                connected_settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )

            before = len(fake.commands)
            reused = connected_controller.start(
                self._plan(
                    config,
                    "batch_reuse_connected",
                    baseline_policy="reuse_valid",
                ),
                execution_confirmed=True,
            )

            self.assertEqual(reused["state"], "WAITING_FOR_SAMPLE")
            self.assertEqual(reused["baseline"]["status"], "REUSED")
            self.assertEqual(
                [item[0] for item in fake.commands[before:]],
                [1, 100],
            )

    def test_new_connection_rejects_reuse_and_waits_for_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config, _ = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )
            controller.start(
                self._plan(config, "batch_first"), execution_confirmed=True
            )
            controller.correct_baseline("batch_first", blank_loaded_confirmed=True)
            controller.abort(
                "batch_first", reason="baseline validation only", abort_confirmed=True
            )

            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "connect_before_run = false",
                    "connect_before_run = true",
                ),
                encoding="utf-8",
            )
            connected_settings = load_settings(config)
            connected_controller = SpectrumBatchController(
                connected_settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )

            before = len(fake.commands)
            started = connected_controller.start(
                self._plan(
                    config,
                    "batch_reuse_new_connection",
                    baseline_policy="reuse_valid",
                ),
                execution_confirmed=True,
            )

            self.assertEqual(started["state"], "WAITING_FOR_BLANK")
            self.assertEqual(started["baseline"]["policy"], "new")
            self.assertEqual(started["baseline"]["status"], "PENDING")
            self.assertEqual(
                started["baseline"]["reuse_rejected_reason"],
                "instrument_connection_reestablished",
            )
            self.assertEqual(
                [item[0] for item in fake.commands[before:]],
                [1, 100],
            )
            manifest = json.loads(
                (
                    root
                    / "data"
                    / "batch_reuse_new_connection"
                    / "batch-manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn(
                "baseline_reuse_rejected_new_connection",
                [event["type"] for event in manifest["events"]],
            )


class PhotometricBatchControllerTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        for name in ("control", "export", "data", "templates", "generated"):
            (root / name).mkdir()
        template = root / "templates" / "photometric_absorbance.vphm"
        template.write_bytes(b"TEMPLATE")
        groups = [
            list(range(400, 500, 10)),
            list(range(500, 600, 10)),
            list(range(600, 700, 10)),
            [700],
        ]
        for group in groups:
            joined = "_".join(str(value) for value in group)
            (
                root / "generated" / f"photometric_{joined}nm_absorbance.vphm"
            ).write_bytes(b"VERIFIED PHOTOMETRIC METHOD")
        config = root / "control.toml"
        config.write_text(
            f"""
[labsolutions]
command_dir = "{(root / "control").as_posix()}"
mode = "spectrum"
timeout_seconds = 1.0
poll_interval_seconds = 0.01
lock_timeout_seconds = 1.0

[export]
directory = "{(root / "export").as_posix()}"
pattern = "{{sample_id}}*.csv"
timeout_seconds = 1.0
stable_seconds = 0.0

[spectrum]
data_dir = "{(root / "data").as_posix()}"
measurement_mode = 2
connect_before_run = false
disconnect_after_run = false
discharge_after_measurement = false

[method_generation]
output_directory = "{(root / "generated").as_posix()}"

[method_templates.photometric_absorbance]
mode = "photometric"
signal_type = "absorbance"
method_file = "{template.as_posix()}"

[results]
directory = "{(root / "outputs").as_posix()}"
""".strip(),
            encoding="utf-8",
        )
        return config

    def test_one_confirmation_runs_all_four_photometric_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )
            plan = build_uvvis_sample_batch_plan(
                settings,
                batch_id="photo_batch",
                samples=[{"sample_name": "sample A", "sample_id": "sample_a"}],
                reference_name="blank",
                start_nm=400,
                stop_nm=700,
                step_nm=10,
            )

            started = controller.start(plan, execution_confirmed=True)
            self.assertEqual(started["state"], "WAITING_FOR_BLANK")
            self.assertEqual([command for command, _ in fake.commands], [300])

            corrected = controller.correct_baseline(
                "photo_batch", blank_loaded_confirmed=True
            )
            self.assertEqual(corrected["state"], "WAITING_FOR_SAMPLE")
            self.assertEqual([command for command, _ in fake.commands], [300, 21, 321])

            completed = controller.measure_next(
                "photo_batch",
                sample_id="001_sample_a",
                sample_loaded_confirmed=True,
            )
            self.assertEqual(completed["state"], "COMPLETED")
            self.assertEqual(
                [command for command, _ in fake.commands],
                [300, 21, 321] + [300, 310, 311, 320, 321] * 4,
            )
            sample_dir = root / "data" / "photo_batch" / "001_sample_a"
            self.assertEqual(len(list((sample_dir / "raw").glob("*.vphd"))), 4)
            self.assertEqual(len(list((sample_dir / "export").glob("*.csv"))), 5)
            result = sample_dir / "export" / "result.csv"
            self.assertEqual(len(result.read_text(encoding="utf-8").splitlines()), 32)
            self.assertTrue((sample_dir / "plot" / "result.png").is_file())
            published = root / "outputs" / "photo_batch" / "001_sample_a"
            self.assertTrue((published / "result.csv").is_file())
            self.assertTrue((published / "result.json").is_file())
            self.assertTrue((published / "result.png").is_file())

    def test_completed_photometric_segment_is_not_measured_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._fixture(root)
            settings = load_settings(config)
            fake = FakeSpectrumClient(root / "export")
            runtime = FakeRuntimeManager(root / "control")
            controller = SpectrumBatchController(
                settings,
                client_factory=lambda: fake,  # type: ignore[arg-type]
                runtime_manager_factory=lambda: runtime,  # type: ignore[arg-type]
            )
            plan = build_uvvis_sample_batch_plan(
                settings,
                batch_id="photo_resume",
                samples=[{"sample_name": "sample A", "sample_id": "sample_a"}],
                reference_name="blank",
                start_nm=400,
                stop_nm=700,
                step_nm=10,
            )
            controller.start(plan, execution_confirmed=True)
            controller.correct_baseline("photo_resume", blank_loaded_confirmed=True)

            manifest_path = root / "data" / "photo_resume" / "batch-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            sample = manifest["samples"][0]
            first_segment = sample["segments"][0]
            raw_path = Path(first_segment["raw_data_file"])
            raw_path.write_bytes(b"ALREADY COMPLETED")
            normalized = Path(sample["paths"]["export_directory"]) / "segment_01.csv"
            normalized.write_text(
                "wavelength_nm,absorbance\n"
                + "".join(
                    f"{wavelength:g},0.1\n"
                    for wavelength in first_segment["wavelengths_nm"]
                ),
                encoding="utf-8",
            )
            sample["completed_segments"] = [
                {
                    "segment_index": 1,
                    "sample_id": first_segment["sample_id"],
                    "wavelengths_nm": first_segment["wavelengths_nm"],
                    "method_file": first_segment["method_file"],
                    "raw_data": {"path": str(raw_path)},
                    "export": {"path": str(normalized)},
                    "export_source": str(raw_path),
                    "result_source_kind": "labsolutions_vphd",
                }
            ]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            before = len(fake.commands)

            completed = controller.measure_next(
                "photo_resume",
                sample_id="001_sample_a",
                sample_loaded_confirmed=True,
            )

            self.assertEqual(completed["state"], "COMPLETED")
            self.assertEqual(
                [command for command, _ in fake.commands[before:]],
                [300, 310, 311, 320, 321] * 3,
            )
            result = root / "data" / "photo_resume" / "001_sample_a" / "export"
            self.assertEqual(len((result / "result.csv").read_text().splitlines()), 32)


if __name__ == "__main__":
    unittest.main()
