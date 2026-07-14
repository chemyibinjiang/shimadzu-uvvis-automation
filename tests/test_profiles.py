from __future__ import annotations

import unittest
from pathlib import Path

from shimadzu_uvvis.configuration import ScanProfile
from shimadzu_uvvis.profiles import (
    AmbiguousScanProfileError,
    ScanProfileNotFoundError,
    ScanProfileRegistry,
    ScanProfileResolutionError,
    SpectrumScanRequest,
    resolve_scan_profile,
)


def _profile(
    name: str, start_nm: float, stop_nm: float, step_nm: float
) -> ScanProfile:
    return ScanProfile(
        name=name,
        method_file=Path(f"C:/UVVis-Data/Parameter/{name}.vspm"),
        start_nm=start_nm,
        stop_nm=stop_nm,
        step_nm=step_nm,
    )


class ScanProfileRegistryTests(unittest.TestCase):
    def test_resolves_changing_scan_requests_without_hard_coded_ranges(self) -> None:
        profiles = {
            "visible_1nm": _profile("visible_1nm", 400, 700, 1),
            "wide_2nm": _profile("wide_2nm", 200, 800, 2),
        }

        visible = resolve_scan_profile(
            profiles, start_nm=400, stop_nm=700, step_nm=1
        )
        wide = resolve_scan_profile(
            profiles, start_nm=200, stop_nm=800, step_nm=2
        )

        self.assertEqual(visible.profile.name, "visible_1nm")
        self.assertEqual(visible.request.point_count, 301)
        self.assertEqual(wide.profile.name, "wide_2nm")
        self.assertEqual(wide.request.point_count, 301)

    def test_range_is_direction_independent_unless_direction_is_requested(self) -> None:
        descending = _profile("visible_desc", 700, 400, 1)
        registry = ScanProfileRegistry({descending.name: descending})

        semantic_range = SpectrumScanRequest.from_boundaries(400, 700, 1)
        result = registry.resolve(semantic_range)

        self.assertEqual(result.profile.name, "visible_desc")
        self.assertEqual(result.as_dict()["profile"]["direction"], "descending")

        ascending_request = SpectrumScanRequest.from_boundaries(
            400, 700, 1, direction="ascending"
        )
        with self.assertRaises(ScanProfileNotFoundError):
            registry.resolve(ascending_request)

    def test_direction_or_profile_name_resolves_ambiguous_methods(self) -> None:
        ascending = _profile("visible_asc", 400, 700, 1)
        descending = _profile("visible_desc", 700, 400, 1)
        registry = ScanProfileRegistry(
            {ascending.name: ascending, descending.name: descending}
        )

        with self.assertRaises(AmbiguousScanProfileError):
            registry.resolve(SpectrumScanRequest.from_boundaries(400, 700, 1))

        by_direction = registry.resolve(
            SpectrumScanRequest.from_boundaries(
                400, 700, 1, direction="descending"
            )
        )
        by_name = registry.resolve(
            SpectrumScanRequest.from_boundaries(
                400, 700, 1, profile_name="visible_asc"
            )
        )

        self.assertEqual(by_direction.profile.name, "visible_desc")
        self.assertEqual(by_name.profile.name, "visible_asc")

    def test_missing_or_mismatched_profile_is_rejected(self) -> None:
        visible = _profile("visible_1nm", 400, 700, 1)
        registry = ScanProfileRegistry({visible.name: visible})

        with self.assertRaisesRegex(
            ScanProfileNotFoundError, "no registered LabSolutions method"
        ):
            registry.resolve(SpectrumScanRequest.from_boundaries(350, 750, 1))

        with self.assertRaisesRegex(ScanProfileNotFoundError, "does not match"):
            registry.resolve(
                SpectrumScanRequest.from_boundaries(
                    400, 700, 2, profile_name="visible_1nm"
                )
            )

    def test_invalid_grid_is_rejected_before_profile_lookup(self) -> None:
        with self.assertRaisesRegex(
            ScanProfileResolutionError, "evenly divisible"
        ):
            SpectrumScanRequest.from_boundaries(400, 700, 7)

        with self.assertRaisesRegex(ScanProfileResolutionError, "greater than zero"):
            SpectrumScanRequest.from_boundaries(400, 700, -1)

        with self.assertRaisesRegex(ScanProfileResolutionError, "finite number"):
            SpectrumScanRequest.from_boundaries("400", 700, 1)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
