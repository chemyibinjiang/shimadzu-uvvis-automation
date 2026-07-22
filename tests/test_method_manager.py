from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from shimadzu_uvvis.client import Feedback
from shimadzu_uvvis.configuration import load_settings
from shimadzu_uvvis.measurements import MeasurementPlanError, build_measurement_request
from shimadzu_uvvis.method_manager import (
    PhotometricMethodManager,
    PhotometricMethodReadback,
    SpectrumMethodGenerationError,
    SpectrumMethodManager,
    SpectrumMethodReadback,
    _is_baseline_confirmation_text,
    spectrum_generation_support,
)
from shimadzu_uvvis.runtime_manager import RuntimeReady


class FakeRuntimeManager:
    def __init__(self, calls: list[bool]) -> None:
        self.calls = calls

    def ensure_ready(self, *, allow_reconfigure: bool) -> RuntimeReady:
        self.calls.append(allow_reconfigure)
        return RuntimeReady(
            process_id=123,
            window_handle=456,
            launched=False,
            command_directory=Path("D:/control"),
            command_directory_changed=False,
            waiting_status="Automatic Control - Waiting",
            feedback=Feedback(
                command=0,
                return_code=0,
                error="",
                fields=MappingProxyType({"Command": "0", "Return": "0", "Error": ""}),
            ),
        )


class FakeMethodBackend:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def generate_and_verify(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        target = Path(kwargs["target_file"])
        target.write_bytes(b"generated method")
        return SpectrumMethodReadback(
            start_nm=float(kwargs["upper_nm"]),
            end_nm=float(kwargs["lower_nm"]),
            data_interval_nm=float(kwargs["step_nm"]),
            signal_type=str(kwargs["signal_type"]),
            signal_label="Absorbance",
        )


class FakePhotometricBackend:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def generate_and_verify(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        target = Path(kwargs["target_file"])
        target.write_bytes(b"generated photometric method")
        return PhotometricMethodReadback(
            wavelengths_nm=tuple(float(value) for value in kwargs["wavelengths_nm"]),
            signal_type=str(kwargs["signal_type"]),
            signal_label="Absorbance",
            measurement_method_label="Point",
        )


class SpectrumMethodManagerTests(unittest.TestCase):
    def test_baseline_confirmation_text_is_detected_without_localized_buttons(self) -> None:
        self.assertTrue(_is_baseline_confirmation_text("是否执行基线校正?"))
        self.assertTrue(
            _is_baseline_confirmation_text("Perform baseline correction now?")
        )
        self.assertFalse(_is_baseline_confirmation_text("Save parameter file?"))

    def _fixture(self, root: Path):
        for name in ("control", "data", "export", "generated"):
            (root / name).mkdir()
        template = root / "spectrum_absorbance.vspm"
        template.write_bytes(b"immutable spectrum template")
        digest = hashlib.sha256(template.read_bytes()).hexdigest().upper()
        config = root / "control.toml"
        config.write_text(
            f"""
[labsolutions]
command_dir = "{(root / "control").as_posix()}"
mode = "spectrum"
poll_interval_seconds = 0.01
lock_timeout_seconds = 1.0

[runtime]
enabled = true
executable = "D:/UVNavi.exe"

[export]
directory = "{(root / "export").as_posix()}"

[spectrum]
data_dir = "{(root / "data").as_posix()}"

[method_generation]
output_directory = "{(root / "generated").as_posix()}"

[method_templates.spectrum_absorbance]
mode = "spectrum"
signal_type = "absorbance"
method_file = "{template.as_posix()}"
sha256 = "{digest}"
""".strip(),
            encoding="utf-8",
        )
        return load_settings(config), template

    def test_supported_intervals_are_explicit(self) -> None:
        supported = build_measurement_request(
            mode="spectrum", start_nm=400, stop_nm=700, step_nm=5
        )
        unsupported = build_measurement_request(
            mode="spectrum", start_nm=400, stop_nm=700, step_nm=10
        )

        self.assertTrue(spectrum_generation_support(supported)[0])
        is_supported, reason = spectrum_generation_support(unsupported)
        self.assertFalse(is_supported)
        self.assertIn("5", reason)
        self.assertIn("Photometric", reason)

    def test_generate_uses_descending_labsolutions_fields_and_attests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings, template = self._fixture(root)
            backend = FakeMethodBackend()
            runtime_calls: list[bool] = []
            manager = SpectrumMethodManager(
                settings,
                backend=backend,
                runtime_manager_factory=lambda: FakeRuntimeManager(runtime_calls),  # type: ignore[arg-type]
            )
            request = build_measurement_request(
                mode="spectrum", start_nm=400, stop_nm=700, step_nm=5
            )

            result = manager.generate(request)

            self.assertEqual(runtime_calls, [True, False])
            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(backend.calls[0]["upper_nm"], 700.0)
            self.assertEqual(backend.calls[0]["lower_nm"], 400.0)
            self.assertEqual(backend.calls[0]["step_nm"], 5.0)
            target = Path(result["method_file"])
            self.assertTrue(target.is_file())
            self.assertEqual(template.read_bytes(), b"immutable spectrum template")
            attestation = target.with_suffix(target.suffix + ".generation.json")
            payload = json.loads(attestation.read_text(encoding="utf-8"))
            self.assertEqual(payload["readback"]["labsolutions_start_nm"], 700.0)
            self.assertEqual(payload["readback"]["labsolutions_end_nm"], 400.0)
            self.assertEqual(payload["runtime_after"]["hello"]["return_code"], 0)

            reused = manager.generate(request)
            self.assertEqual(reused["status"], "reused")
            self.assertFalse(reused["created"])
            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(runtime_calls, [True, False])

    def test_unsupported_interval_never_touches_runtime_or_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings, _ = self._fixture(Path(temporary_directory))
            backend = FakeMethodBackend()
            runtime_calls: list[bool] = []
            manager = SpectrumMethodManager(
                settings,
                backend=backend,
                runtime_manager_factory=lambda: FakeRuntimeManager(runtime_calls),  # type: ignore[arg-type]
            )
            request = build_measurement_request(
                mode="spectrum", start_nm=400, stop_nm=700, step_nm=10
            )

            with self.assertRaisesRegex(MeasurementPlanError, "only offers"):
                manager.generate(request)

            self.assertEqual(runtime_calls, [])
            self.assertEqual(backend.calls, [])

    def test_active_batch_blocks_method_editing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings, _ = self._fixture(root)
            (root / "data" / ".active_spectrum_batch.json").write_text(
                '{"batch_id":"active"}', encoding="utf-8"
            )
            backend = FakeMethodBackend()
            manager = SpectrumMethodManager(
                settings,
                backend=backend,
                runtime_manager_factory=lambda: FakeRuntimeManager([]),  # type: ignore[arg-type]
            )

            with self.assertRaisesRegex(
                SpectrumMethodGenerationError, "batch is active"
            ):
                manager.generate(
                    build_measurement_request(
                        mode="spectrum", start_nm=400, stop_nm=700, step_nm=1
                    )
                )

            self.assertEqual(backend.calls, [])

    def test_runtime_is_restored_after_generation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings, _ = self._fixture(Path(temporary_directory))
            backend = FakeMethodBackend(error=RuntimeError("save failed"))
            runtime_calls: list[bool] = []
            manager = SpectrumMethodManager(
                settings,
                backend=backend,
                runtime_manager_factory=lambda: FakeRuntimeManager(runtime_calls),  # type: ignore[arg-type]
            )

            with self.assertRaisesRegex(SpectrumMethodGenerationError, "save failed"):
                manager.generate(
                    build_measurement_request(
                        mode="spectrum", start_nm=400, stop_nm=700, step_nm=1
                    )
                )

            self.assertEqual(runtime_calls, [True, False])


class PhotometricMethodManagerTests(unittest.TestCase):
    def _fixture(self, root: Path):
        for name in ("control", "data", "export", "generated"):
            (root / name).mkdir()
        template = root / "photometric_absorbance.vphm"
        template.write_bytes(b"immutable photometric template")
        digest = hashlib.sha256(template.read_bytes()).hexdigest().upper()
        config = root / "control.toml"
        config.write_text(
            f"""
[labsolutions]
command_dir = "{(root / "control").as_posix()}"
mode = "spectrum"
poll_interval_seconds = 0.01
lock_timeout_seconds = 1.0

[runtime]
enabled = true
executable = "D:/UVNavi.exe"

[export]
directory = "{(root / "export").as_posix()}"

[spectrum]
data_dir = "{(root / "data").as_posix()}"

[method_generation]
output_directory = "{(root / "generated").as_posix()}"

[method_templates.photometric_absorbance]
mode = "photometric"
signal_type = "absorbance"
method_file = "{template.as_posix()}"
sha256 = "{digest}"
""".strip(),
            encoding="utf-8",
        )
        return load_settings(config)

    def test_31_points_are_generated_as_four_verified_methods(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = self._fixture(Path(temporary_directory))
            backend = FakePhotometricBackend()
            runtime_calls: list[bool] = []
            manager = PhotometricMethodManager(
                settings,
                backend=backend,
                runtime_manager_factory=lambda: FakeRuntimeManager(runtime_calls),  # type: ignore[arg-type]
            )
            request = build_measurement_request(
                mode="photometric", wavelengths_nm=list(range(400, 701, 10))
            )

            result = manager.generate(request)

            self.assertEqual(result["segment_count"], 4)
            self.assertEqual(
                [len(call["wavelengths_nm"]) for call in backend.calls], [10, 10, 10, 1]
            )
            self.assertEqual(runtime_calls, [True, False])
            self.assertTrue(
                all(Path(path).is_file() for path in result["method_files"])
            )
            self.assertEqual(
                [
                    segment["readback"]["wavelength_count"]
                    for segment in result["segments"]
                ],
                [10, 10, 10, 1],
            )

            reused = manager.generate(request)
            self.assertEqual(reused["status"], "reused")
            self.assertFalse(reused["created"])
            self.assertEqual(len(backend.calls), 4)
            self.assertEqual(runtime_calls, [True, False])

    def test_generation_failure_still_restores_photometric_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = self._fixture(Path(temporary_directory))
            runtime_calls: list[bool] = []
            manager = PhotometricMethodManager(
                settings,
                backend=FakePhotometricBackend(error=RuntimeError("save failed")),
                runtime_manager_factory=lambda: FakeRuntimeManager(runtime_calls),  # type: ignore[arg-type]
            )

            with self.assertRaisesRegex(SpectrumMethodGenerationError, "save failed"):
                manager.generate(
                    build_measurement_request(
                        mode="photometric", wavelengths_nm=[400, 410]
                    )
                )

            self.assertEqual(runtime_calls, [True, False])


if __name__ == "__main__":
    unittest.main()
