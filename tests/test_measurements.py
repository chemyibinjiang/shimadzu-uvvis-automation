from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shimadzu_uvvis.configuration import MethodTemplate
from shimadzu_uvvis.measurements import (
    MeasurementPlanError,
    build_measurement_request,
    resolve_method_template,
    route_measurement_request,
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
            build_measurement_request(mode="photometric", wavelengths_nm=[450, 450])

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

    def test_auto_routes_supported_range_to_spectrum(self) -> None:
        routed = route_measurement_request(
            start_nm=400,
            stop_nm=700,
            step_nm=5,
        )

        self.assertEqual(routed.request.mode, "spectrum")
        self.assertEqual(routed.request.parameters["point_count"], 61)
        self.assertTrue(routed.as_dict()["selected_before_labsolutions_start"])

    def test_auto_routes_unsupported_spectrum_interval_to_exact_photometric_list(
        self,
    ) -> None:
        routed = route_measurement_request(
            start_nm=400,
            stop_nm=700,
            step_nm=10,
        )

        self.assertEqual(routed.request.mode, "photometric")
        self.assertEqual(
            routed.request.parameters["wavelengths_nm"],
            [float(value) for value in range(400, 701, 10)],
        )
        self.assertIn("not a Spectrum data interval", routed.reason)
        self.assertIn("31-point", routed.reason)

    def test_auto_range_routing_preserves_descending_order(self) -> None:
        routed = route_measurement_request(
            start_nm=700,
            stop_nm=400,
            step_nm=10,
        )

        self.assertEqual(
            routed.request.parameters["wavelengths_nm"],
            [float(value) for value in range(700, 399, -10)],
        )

    def test_auto_routes_fixed_wavelength_time_series_to_time_course(self) -> None:
        routed = route_measurement_request(
            wavelength_nm=520,
            interval_seconds=1,
            duration_seconds=60,
        )

        self.assertEqual(routed.request.mode, "time_course")
        self.assertEqual(routed.request.parameters["point_count"], 61)

    def test_auto_routes_standard_curve_purpose_to_quantitation(self) -> None:
        routed = route_measurement_request(
            measurement_purpose="quantitation",
            wavelength_nm=520,
        )

        self.assertEqual(routed.request.mode, "quantitation")

    def test_auto_rejects_incomplete_range_before_mode_selection(self) -> None:
        with self.assertRaisesRegex(MeasurementPlanError, "requires start_nm"):
            route_measurement_request(start_nm=400, stop_nm=700)

    def test_auto_rejects_unknown_measurement_purpose(self) -> None:
        with self.assertRaisesRegex(MeasurementPlanError, "measurement_purpose"):
            route_measurement_request(
                measurement_purpose="unknown",  # type: ignore[arg-type]
                wavelength_nm=520,
            )


if __name__ == "__main__":
    unittest.main()
