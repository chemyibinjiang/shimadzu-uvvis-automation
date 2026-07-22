from __future__ import annotations

import struct
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from shimadzu_uvvis.results import (
    PhotometricResultError,
    SpectrumResultError,
    build_photometric_result,
    build_spectrum_result,
    normalize_photometric_data_file,
    normalize_spectrum_data_file,
    parse_photometric_data_file,
    parse_photometric_export,
    parse_spectrum_data_file,
    parse_spectrum_export,
)


class PhotometricResultTests(unittest.TestCase):
    def test_parses_named_absorbance_streams_from_vphd(self) -> None:
        streams = {
            ("Sample Table", "Column Data", "A400.0"): (
                b"\x0a\x00\xff\xff\x01\x00\x05" b"0.125"
            ),
            ("Sample Table", "Column Data", "A410.0"): (
                b"\x0a\x00\xff\xff\x01\x00\x06" b"-0.002"
            ),
        }

        class FakeCompound:
            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def listdir(self, *, streams: bool, storages: bool):
                self.assert_flags = (streams, storages)
                return [list(name) for name in streams_data]

            def openstream(self, name: list[str]):
                return BytesIO(streams_data[tuple(name)])

        streams_data = streams
        with (
            patch("shimadzu_uvvis.results.olefile.isOleFile", return_value=True),
            patch(
                "shimadzu_uvvis.results.olefile.OleFileIO",
                return_value=FakeCompound(),
            ),
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            raw = Path(temporary_directory) / "segment.vphd"
            points = parse_photometric_data_file(raw, [400, 410])
            normalized = Path(temporary_directory) / "segment.csv"
            normalize_photometric_data_file(
                data_file=raw,
                expected_wavelengths_nm=[400, 410],
                csv_file=normalized,
            )
            normalized_lines = normalized.read_text(encoding="utf-8").splitlines()

        self.assertEqual([point.absorbance for point in points], [0.125, -0.002])
        self.assertEqual(
            normalized_lines,
            ["wavelength_nm,absorbance", "400,0.125", "410,-0.002"],
        )

    def test_parses_long_and_wide_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            long_form = root / "long.csv"
            long_form.write_text(
                "wavelength_nm,absorbance\n400,0.1\n410,0.2\n",
                encoding="utf-8",
            )
            wide_form = root / "wide.csv"
            wide_form.write_text(
                "Sample ID,A420.0,A430.0\nsample,0.3,0.4\n",
                encoding="utf-8",
            )

            self.assertEqual(
                [
                    point.absorbance
                    for point in parse_photometric_export(long_form, [400, 410])
                ],
                [0.1, 0.2],
            )
            self.assertEqual(
                [
                    point.absorbance
                    for point in parse_photometric_export(wide_form, [420, 430])
                ],
                [0.3, 0.4],
            )

    def test_builds_merged_outputs_and_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.csv"
            second = root / "second.csv"
            first.write_text(
                "wavelength,absorbance\n400,0.1\n410,0.8\n", encoding="utf-8"
            )
            second.write_text("A420.0,A430.0\n0.4,0.2\n", encoding="utf-8")

            result = build_photometric_result(
                export_files=[first, second],
                expected_segments=[[400, 410], [420, 430]],
                csv_file=root / "result.csv",
                json_file=root / "result.json",
                png_file=root / "result.png",
                batch_id="batch",
                sample_id="sample",
                publish_root=root / "published",
            )

            self.assertEqual(result["point_count"], 4)
            self.assertEqual(
                result["maximum_absorbance"],
                {"wavelength_nm": 410.0, "absorbance": 0.8},
            )
            self.assertEqual(
                (root / "result.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n"
            )
            self.assertTrue(
                (root / "published" / "batch" / "sample" / "result.png").is_file()
            )

    def test_missing_wavelength_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "bad.csv"
            path.write_text("wavelength,absorbance\n400,0.1\n", encoding="utf-8")
            with self.assertRaises(PhotometricResultError):
                parse_photometric_export(path, [400, 410])


class SpectrumResultTests(unittest.TestCase):
    def test_parses_x_y_streams_from_vspd(self) -> None:
        streams = {
            (
                "DataStorage1",
                "DataSetGroup",
                "DataSet1",
                "DataSpectrumStorage",
                "Data",
                "Data Header.1",
            ): struct.pack("<II", 3, 2),
            (
                "DataStorage1",
                "DataSetGroup",
                "DataSet1",
                "DataSpectrumStorage",
                "Data",
                "X Data.1",
            ): struct.pack("<3d", 402.0, 401.0, 400.0),
            (
                "DataStorage1",
                "DataSetGroup",
                "DataSet1",
                "DataSpectrumStorage",
                "Data",
                "Y Data.1",
            ): struct.pack("<3d", 0.2, 0.7, 0.1),
        }

        class FakeCompound:
            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def exists(self, name: list[str]) -> bool:
                return tuple(name) in streams

            def openstream(self, name: list[str]):
                return BytesIO(streams[tuple(name)])

        with (
            patch("shimadzu_uvvis.results.olefile.isOleFile", return_value=True),
            patch(
                "shimadzu_uvvis.results.olefile.OleFileIO",
                return_value=FakeCompound(),
            ),
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            raw = Path(temporary_directory) / "sample.vspd"
            points = parse_spectrum_data_file(
                raw,
                lower_nm=400,
                upper_nm=402,
                step_nm=1,
            )
            normalized = Path(temporary_directory) / "sample.csv"
            normalize_spectrum_data_file(
                data_file=raw,
                lower_nm=400,
                upper_nm=402,
                step_nm=1,
                csv_file=normalized,
            )
            normalized_lines = normalized.read_text(encoding="utf-8").splitlines()

        self.assertEqual(
            [point.wavelength_nm for point in points], [400.0, 401.0, 402.0]
        )
        self.assertEqual([point.absorbance for point in points], [0.1, 0.7, 0.2])
        self.assertEqual(
            normalized_lines,
            [
                "wavelength_nm,absorbance",
                "400,0.1",
                "401,0.7",
                "402,0.2",
            ],
        )

    def test_parses_descending_export_into_exact_ascending_grid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "spectrum.csv"
            path.write_text(
                "Sample ID,sample\n"
                "Wavelength (nm),Abs.\n"
                "403,0.1\n402,0.8\n401,0.4\n400,0.2\n",
                encoding="utf-8",
            )

            points = parse_spectrum_export(
                path,
                lower_nm=400,
                upper_nm=403,
                step_nm=1,
            )

            self.assertEqual(
                [point.wavelength_nm for point in points],
                [400.0, 401.0, 402.0, 403.0],
            )
            self.assertEqual([point.absorbance for point in points], [0.2, 0.4, 0.8, 0.1])

    def test_builds_spectrum_outputs_and_publishes_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export = root / "source.csv"
            export.write_text(
                "wavelength_nm,absorbance\n"
                "400,0.1\n401,0.7\n402,0.2\n",
                encoding="utf-8",
            )

            result = build_spectrum_result(
                export_file=export,
                lower_nm=400,
                upper_nm=402,
                step_nm=1,
                csv_file=root / "result.csv",
                json_file=root / "result.json",
                png_file=root / "result.png",
                batch_id="batch",
                sample_id="sample",
                publish_root=root / "published",
            )

            self.assertEqual(result["mode"], "spectrum")
            self.assertEqual(result["point_count"], 3)
            self.assertEqual(
                result["maximum_absorbance"],
                {"wavelength_nm": 401.0, "absorbance": 0.7},
            )
            self.assertEqual(
                (root / "result.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n"
            )
            published = root / "published" / "batch" / "sample"
            self.assertTrue((published / "result.csv").is_file())
            self.assertTrue((published / "result.json").is_file())
            self.assertTrue((published / "result.png").is_file())

    def test_rejects_missing_duplicate_and_off_grid_wavelengths(self) -> None:
        cases = {
            "missing": "400,0.1\n402,0.2\n",
            "duplicate": "400,0.1\n401,0.2\n401,0.3\n402,0.4\n",
            "off_grid": "400,0.1\n400.5,0.2\n401,0.3\n402,0.4\n",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name, rows in cases.items():
                with self.subTest(name=name):
                    path = root / f"{name}.csv"
                    path.write_text(
                        "wavelength,absorbance\n" + rows,
                        encoding="utf-8",
                    )
                    with self.assertRaises(SpectrumResultError):
                        parse_spectrum_export(
                            path,
                            lower_nm=400,
                            upper_nm=402,
                            step_nm=1,
                        )


if __name__ == "__main__":
    unittest.main()
