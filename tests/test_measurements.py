from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shimadzu_uvvis.configuration import MethodTemplate
from shimadzu_uvvis.measurements import (
    MeasurementPlanError,
    build_measurement_request,
    resolve_method_template,
)


class MeasurementRequestTests(unittest.TestCase):
    def test_spectrum_normalizes_boundaries_and_counts_points(self) -> None:
        request = build_measurement_request(
            mode="spectrum", start_nm=700, stop_nm=400, step_nm=1
        )

        self.assertEqual(request.parameters["lower_nm"], 400.0)
        self.assertEqual(request.parameters["upper_nm"], 700.0)
        self.assertEqual(request.parameters["point_count"], 301)

    def test_photometric_rejects_duplicate_wavelengths(self) -> None:
        with self.assertRaisesRegex(MeasurementPlanError, "duplicate"):
            build_measurement_request(
                mode="photometric", wavelengths_nm=[450, 450]
            )

    def test_quantitation_requires_one_wavelength(self) -> None:
        with self.assertRaisesRegex(MeasurementPlanError, "wavelength_nm"):
            build_measurement_request(mode="quantitation")

    def test_time_course_validates_time_grid(self) -> None:
        with self.assertRaisesRegex(MeasurementPlanError, "evenly divisible"):
            build_measurement_request(
                mode="time_course",
                wavelength_nm=520,
                interval_seconds=3,
                duration_seconds=10,
            )

    def test_mode_rejects_parameters_that_belong_to_another_mode(self) -> None:
        with self.assertRaisesRegex(MeasurementPlanError, "does not accept: start_nm"):
            build_measurement_request(
                mode="photometric",
                wavelengths_nm=[520],
                start_nm=400,
            )

    def test_signal_type_cannot_escape_generated_directory(self) -> None:
        with self.assertRaisesRegex(MeasurementPlanError, "signal_type"):
            build_measurement_request(
                mode="quantitation",
                wavelength_nm=520,
                signal_type="../absorbance",
            )

    def test_template_target_uses_mode_specific_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            template = MethodTemplate(
                name="photometric_absorbance",
                mode="photometric",
                method_file=root / "template.vphm",
                signal_type="absorbance",
            )
            request = build_measurement_request(
                mode="photometric", wavelengths_nm=[450, 520, 650]
            )

            resolved = resolve_method_template(
                {template.name: template}, root / "generated", request
            )

            self.assertEqual(
                resolved.generated_method_file.name,
                "photometric_450_520_650nm_absorbance.vphm",
            )


if __name__ == "__main__":
    unittest.main()
