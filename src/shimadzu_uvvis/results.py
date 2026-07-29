"""Normalize LabSolutions exports and render dependency-free PNG spectra."""

from __future__ import annotations

import csv
import io
import math
import os
import re
import shutil
import struct
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import olefile

from .audit import write_json_atomic


class PhotometricResultError(RuntimeError):
    """Raised when exported Photometric values cannot be verified."""


class SpectrumResultError(RuntimeError):
    """Raised when an exported Spectrum cannot be verified against its method."""


class TimeCourseResultError(RuntimeError):
    """Raised when an exported Time Course cannot be validated."""


@dataclass(frozen=True, slots=True)
class AbsorbancePoint:
    wavelength_nm: float
    absorbance: float

    def as_dict(self) -> dict[str, float]:
        return {
            "wavelength_nm": self.wavelength_nm,
            "absorbance": self.absorbance,
        }


@dataclass(frozen=True, slots=True)
class TimeCoursePoint:
    time_seconds: float
    absorbance: float

    def as_dict(self) -> dict[str, float]:
        return {
            "time_seconds": self.time_seconds,
            "time_min": self.time_seconds / 60.0,
            "absorbance": self.absorbance,
        }


def _decode_export(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise PhotometricResultError(f"cannot decode Photometric export: {path}")


def _rows(path: Path) -> list[list[str]]:
    text = _decode_export(path)
    candidates = []
    for delimiter in (",", "\t", ";"):
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        score = sum(max(0, len(row) - 1) for row in rows)
        candidates.append((score, rows))
    rows = max(candidates, key=lambda item: item[0])[1]
    return [
        [cell.strip() for cell in row]
        for row in rows
        if any(cell.strip() for cell in row)
    ]


def _row_candidates(path: Path) -> list[list[list[str]]]:
    try:
        text = _decode_export(path)
    except PhotometricResultError as exc:
        raise SpectrumResultError(f"cannot decode Spectrum export: {path}") from exc
    candidates: list[list[list[str]]] = []
    for delimiter in (",", "\t", ";"):
        rows = [
            [cell.strip() for cell in row]
            for row in csv.reader(io.StringIO(text), delimiter=delimiter)
            if any(cell.strip() for cell in row)
        ]
        if rows not in candidates:
            candidates.append(rows)
    return candidates


def _number(value: str) -> float | None:
    normalized = value.strip().replace("\u2212", "-")
    if not normalized:
        return None
    if normalized.count(",") == 1 and "." not in normalized:
        normalized = normalized.replace(",", ".")
    try:
        result = float(normalized)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _same_wavelength(left: float, right: float) -> bool:
    return math.isclose(left, right, abs_tol=1e-6)


def _is_wavelength_header(value: str) -> bool:
    folded = value.casefold().strip()
    compact = re.sub(r"[^a-z0-9]+", "", folded)
    return (
        "wave" in folded
        or "波长" in value
        or compact in {"nm", "wavelengthnm"}
    )


def _is_absorbance_header(value: str) -> bool:
    folded = value.casefold().strip()
    compact = re.sub(r"[^a-z0-9]+", "", folded)
    return "abs" in folded or "吸光" in value or compact == "a"


def _time_header_unit(value: str) -> str | None:
    folded = value.casefold().strip()
    compact = re.sub(r"[^a-z0-9]+", "", folded)
    if "time" not in folded and "时间" not in value:
        return None
    if any(token in folded for token in ("min", "minute", "分钟")):
        return "minutes"
    if any(token in folded for token in ("sec", "second", "秒")):
        return "seconds"
    if compact.endswith("min"):
        return "minutes"
    return "seconds"


def _time_course_grid(interval_seconds: float, duration_seconds: float) -> list[float]:
    values = (interval_seconds, duration_seconds)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        for value in values
    ):
        raise TimeCourseResultError(
            "Time Course interval and duration must be finite positive numbers"
        )
    interval = float(interval_seconds)
    duration = float(duration_seconds)
    quotient = duration / interval
    if not math.isclose(quotient, round(quotient), abs_tol=1e-9):
        raise TimeCourseResultError(
            "Time Course duration must be evenly divisible by interval_seconds"
        )
    return [round(index * interval, 12) for index in range(round(quotient) + 1)]


def _validated_time_course_points(
    values: Sequence[tuple[float, float]],
    expected: Sequence[float],
) -> list[TimeCoursePoint]:
    if not values:
        raise TimeCourseResultError("Time Course export contains no numeric points")
    interval = expected[1] - expected[0] if len(expected) > 1 else 1.0
    tolerance = max(1e-6, abs(interval) * 1e-4)
    by_index: dict[int, float] = {}
    for time_seconds, absorbance in values:
        index = round(time_seconds / interval)
        if index < 0 or index >= len(expected) or not math.isclose(
            time_seconds, expected[index], abs_tol=tolerance
        ):
            raise TimeCourseResultError(
                "Time Course export contains a time outside the requested grid: "
                f"{time_seconds:g} s"
            )
        if index in by_index:
            raise TimeCourseResultError(
                f"Time Course export contains duplicate time {expected[index]:g} s"
            )
        by_index[index] = absorbance
    if len(by_index) != len(expected):
        missing = [value for index, value in enumerate(expected) if index not in by_index]
        preview = ", ".join(f"{value:g}" for value in missing[:10])
        suffix = "..." if len(missing) > 10 else ""
        raise TimeCourseResultError(
            f"Time Course export is missing {len(missing)} requested times: "
            f"{preview}{suffix}"
        )
    return [
        TimeCoursePoint(time_seconds, by_index[index])
        for index, time_seconds in enumerate(expected)
    ]


def parse_time_course_export(
    path: Path,
    *,
    interval_seconds: float,
    duration_seconds: float,
) -> list[TimeCoursePoint]:
    """Parse a LabSolutions Time Course CSV into the requested time grid."""

    expected = _time_course_grid(interval_seconds, duration_seconds)
    for rows in _row_candidates(path):
        for header_index, header in enumerate(rows):
            time_columns = [
                (index, unit)
                for index, value in enumerate(header)
                if (unit := _time_header_unit(value)) is not None
            ]
            absorbance_columns = [
                index for index, value in enumerate(header) if _is_absorbance_header(value)
            ]
            for time_index, unit in time_columns:
                for absorbance_index in absorbance_columns:
                    if time_index == absorbance_index:
                        continue
                    values: list[tuple[float, float]] = []
                    for row in rows[header_index + 1 :]:
                        if max(time_index, absorbance_index) >= len(row):
                            continue
                        time_value = _number(row[time_index])
                        absorbance = _number(row[absorbance_index])
                        if time_value is None or absorbance is None:
                            continue
                        time_seconds = time_value * 60.0 if unit == "minutes" else time_value
                        values.append((time_seconds, absorbance))
                    if values:
                        try:
                            return _validated_time_course_points(values, expected)
                        except TimeCourseResultError:
                            pass
    raise TimeCourseResultError(
        f"cannot find a complete Time Course time/absorbance table in {path}"
    )


def _spectrum_grid(
    lower_nm: float, upper_nm: float, step_nm: float
) -> list[float]:
    values = (lower_nm, upper_nm, step_nm)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in values
    ):
        raise SpectrumResultError("Spectrum range and interval must be finite numbers")
    lower = float(lower_nm)
    upper = float(upper_nm)
    step = float(step_nm)
    if lower <= 0 or upper <= lower or step <= 0:
        raise SpectrumResultError(
            "Spectrum requires 0 < lower_nm < upper_nm and step_nm > 0"
        )
    quotient = (upper - lower) / step
    if not math.isclose(quotient, round(quotient), abs_tol=1e-9):
        raise SpectrumResultError(
            "Spectrum range must be evenly divisible by step_nm"
        )
    return [round(lower + index * step, 12) for index in range(round(quotient) + 1)]


def _validated_spectrum_points(
    values: Sequence[tuple[float, float]],
    expected: Sequence[float],
) -> list[AbsorbancePoint]:
    if not values:
        raise SpectrumResultError("Spectrum export contains no numeric points")
    by_index: dict[int, float] = {}
    lower = expected[0]
    upper = expected[-1]
    step = expected[1] - expected[0] if len(expected) > 1 else 1.0
    tolerance = max(1e-6, abs(step) * 1e-6)
    for wavelength, absorbance in values:
        if wavelength < lower - tolerance or wavelength > upper + tolerance:
            raise SpectrumResultError(
                "Spectrum export range does not match the requested method: "
                f"unexpected {wavelength:g} nm"
            )
        index = round((wavelength - lower) / step)
        if index < 0 or index >= len(expected) or not math.isclose(
            wavelength, expected[index], abs_tol=tolerance
        ):
            raise SpectrumResultError(
                "Spectrum export contains a wavelength outside the requested grid: "
                f"{wavelength:g} nm"
            )
        if index in by_index:
            raise SpectrumResultError(
                f"Spectrum export contains duplicate wavelength {expected[index]:g} nm"
            )
        by_index[index] = absorbance
    if len(by_index) != len(expected):
        missing = [
            wavelength
            for index, wavelength in enumerate(expected)
            if index not in by_index
        ]
        preview = ", ".join(f"{value:g}" for value in missing[:10])
        suffix = "..." if len(missing) > 10 else ""
        raise SpectrumResultError(
            f"Spectrum export is missing {len(missing)} requested wavelengths: "
            f"{preview}{suffix}"
        )
    return [
        AbsorbancePoint(wavelength, by_index[index])
        for index, wavelength in enumerate(expected)
    ]


def _parse_spectrum_rows(
    rows: Sequence[Sequence[str]], expected: Sequence[float]
) -> list[AbsorbancePoint]:
    errors: list[SpectrumResultError] = []
    for header_index, row in enumerate(rows):
        wavelength_columns = [
            index for index, cell in enumerate(row) if _is_wavelength_header(cell)
        ]
        absorbance_columns = [
            index for index, cell in enumerate(row) if _is_absorbance_header(cell)
        ]
        for wavelength_column in wavelength_columns:
            for absorbance_column in absorbance_columns:
                if wavelength_column == absorbance_column:
                    continue
                values: list[tuple[float, float]] = []
                for data_row in rows[header_index + 1 :]:
                    if max(wavelength_column, absorbance_column) >= len(data_row):
                        continue
                    wavelength = _number(data_row[wavelength_column])
                    absorbance = _number(data_row[absorbance_column])
                    if wavelength is None or absorbance is None:
                        continue
                    values.append((wavelength, absorbance))
                try:
                    return _validated_spectrum_points(values, expected)
                except SpectrumResultError as exc:
                    errors.append(exc)

    values = []
    for row in rows:
        numbers = [value for cell in row if (value := _number(cell)) is not None]
        if len(numbers) >= 2:
            values.append((numbers[0], numbers[1]))
    try:
        return _validated_spectrum_points(values, expected)
    except SpectrumResultError as exc:
        errors.append(exc)
    raise errors[-1]


def parse_spectrum_export(
    path: Path,
    *,
    lower_nm: float,
    upper_nm: float,
    step_nm: float,
) -> list[AbsorbancePoint]:
    """Parse and normalize one exact Spectrum grid into ascending wavelength order."""

    expected = _spectrum_grid(lower_nm, upper_nm, step_nm)
    errors: list[SpectrumResultError] = []
    results: list[list[AbsorbancePoint]] = []
    for rows in _row_candidates(path):
        try:
            parsed = _parse_spectrum_rows(rows, expected)
        except SpectrumResultError as exc:
            errors.append(exc)
            continue
        if parsed not in results:
            results.append(parsed)
    if not results:
        detail = str(errors[-1]) if errors else "no supported delimited rows"
        raise SpectrumResultError(f"cannot verify Spectrum export {path}: {detail}")
    if len(results) > 1:
        raise SpectrumResultError(
            f"Spectrum export {path} has multiple ambiguous table interpretations"
        )
    return results[0]


def parse_spectrum_data_file(
    path: Path,
    *,
    lower_nm: float,
    upper_nm: float,
    step_nm: float,
) -> list[AbsorbancePoint]:
    """Read the verified X/Y double streams from one LabSolutions .vspd file."""

    expected = _spectrum_grid(lower_nm, upper_nm, step_nm)
    root = [
        "DataStorage1",
        "DataSetGroup",
        "DataSet1",
        "DataSpectrumStorage",
        "Data",
    ]
    header_name = root + ["Data Header.1"]
    x_name = root + ["X Data.1"]
    y_name = root + ["Y Data.1"]
    try:
        if not olefile.isOleFile(str(path)):
            raise SpectrumResultError(
                f"Spectrum data file is not an OLE compound file: {path}"
            )
        with olefile.OleFileIO(str(path)) as compound:
            missing = [
                "/".join(name)
                for name in (header_name, x_name, y_name)
                if not compound.exists(name)
            ]
            if missing:
                raise SpectrumResultError(
                    "Spectrum data file is missing required streams: "
                    + ", ".join(missing)
                )
            header = compound.openstream(header_name).read()
            x_data = compound.openstream(x_name).read()
            y_data = compound.openstream(y_name).read()
    except SpectrumResultError:
        raise
    except (OSError, olefile.OleFileError) as exc:
        raise SpectrumResultError(
            f"cannot read Spectrum data file {path}: {exc}"
        ) from exc
    if len(header) != 8:
        raise SpectrumResultError(
            f"unsupported Spectrum data header size: {len(header)}"
        )
    point_count, _data_column_count = struct.unpack("<II", header)
    if point_count <= 0 or point_count > 10_000_000:
        raise SpectrumResultError(
            f"invalid Spectrum data point count: {point_count}"
        )
    expected_size = point_count * 8
    if len(x_data) != expected_size or len(y_data) != expected_size:
        raise SpectrumResultError(
            "Spectrum X/Y stream sizes do not match the data header: "
            f"count={point_count}, x={len(x_data)}, y={len(y_data)}"
        )
    wavelengths = struct.unpack(f"<{point_count}d", x_data)
    absorbances = struct.unpack(f"<{point_count}d", y_data)
    if any(not math.isfinite(value) for value in wavelengths) or any(
        not math.isfinite(value) for value in absorbances
    ):
        raise SpectrumResultError("Spectrum X/Y streams contain non-finite values")
    return _validated_spectrum_points(
        list(zip(wavelengths, absorbances, strict=True)),
        expected,
    )


def normalize_spectrum_data_file(
    *,
    data_file: Path,
    lower_nm: float,
    upper_nm: float,
    step_nm: float,
    csv_file: Path,
) -> list[AbsorbancePoint]:
    """Convert one verified LabSolutions .vspd into the standard Spectrum CSV."""

    points = parse_spectrum_data_file(
        data_file,
        lower_nm=lower_nm,
        upper_nm=upper_nm,
        step_nm=step_nm,
    )
    _write_csv(csv_file, points)
    return points


def _header_wavelength(value: str, expected: Sequence[float]) -> float | None:
    compact = value.strip()
    match = re.search(r"(?<!\d)(\d{2,4}(?:[.,]\d+)?)(?!\d)", compact)
    if match is None:
        return None
    candidate = _number(match.group(1))
    if candidate is None:
        return None
    if not (
        compact.casefold().startswith("a")
        or "nm" in compact.casefold()
        or _number(compact) is not None
    ):
        return None
    return next(
        (value for value in expected if _same_wavelength(value, candidate)), None
    )


def parse_photometric_export(
    path: Path, expected_wavelengths_nm: Sequence[float]
) -> list[AbsorbancePoint]:
    """Extract exactly the expected wavelengths from one LabSolutions export."""

    expected = [float(value) for value in expected_wavelengths_nm]
    rows = _rows(path)

    # Long form: locate named wavelength and absorbance columns first.
    for header_index, row in enumerate(rows):
        folded = [cell.casefold() for cell in row]
        wavelength_columns = [
            index
            for index, cell in enumerate(folded)
            if "wave" in cell or "波长" in cell or "波長" in cell
        ]
        absorbance_columns = [
            index
            for index, cell in enumerate(folded)
            if "abs" in cell or "吸光" in cell
        ]
        if not wavelength_columns or not absorbance_columns:
            continue
        values: dict[float, float] = {}
        wave_column = wavelength_columns[0]
        absorbance_column = absorbance_columns[0]
        for data_row in rows[header_index + 1 :]:
            if max(wave_column, absorbance_column) >= len(data_row):
                continue
            wave = _number(data_row[wave_column])
            absorbance = _number(data_row[absorbance_column])
            if wave is None or absorbance is None:
                continue
            matched = next(
                (item for item in expected if _same_wavelength(item, wave)), None
            )
            if matched is not None:
                values[matched] = absorbance
        if len(values) == len(expected):
            return [AbsorbancePoint(wave, values[wave]) for wave in expected]

    # Wide form: registered wavelength labels such as A400.0 are columns.
    for header_index, row in enumerate(rows):
        columns: dict[float, int] = {}
        for index, cell in enumerate(row):
            wave = _header_wavelength(cell, expected)
            if wave is not None:
                columns[wave] = index
        if len(columns) != len(expected):
            continue
        for data_row in rows[header_index + 1 :]:
            values = {
                wave: _number(data_row[index]) if index < len(data_row) else None
                for wave, index in columns.items()
            }
            if all(value is not None for value in values.values()):
                return [AbsorbancePoint(wave, float(values[wave])) for wave in expected]

    # Headerless long form is accepted only when every expected point is present.
    values: dict[float, float] = {}
    for row in rows:
        numbers = [value for cell in row if (value := _number(cell)) is not None]
        if len(numbers) < 2:
            continue
        matched = next(
            (item for item in expected if _same_wavelength(item, numbers[0])), None
        )
        if matched is not None:
            values[matched] = numbers[1]
    if len(values) == len(expected):
        return [AbsorbancePoint(wave, values[wave]) for wave in expected]
    raise PhotometricResultError(
        f"export {path} does not contain exactly these wavelengths: {expected}"
    )


def _decode_vphd_absorbance(data: bytes, *, stream_name: str) -> float:
    if len(data) < 7:
        raise PhotometricResultError(
            f"Photometric data stream is too short: {stream_name}"
        )
    value_count = int.from_bytes(data[4:6], "little")
    text_length = data[6]
    if value_count != 1 or len(data) != 7 + text_length:
        raise PhotometricResultError(
            f"unsupported Photometric data stream layout: {stream_name}"
        )
    try:
        text = data[7:].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PhotometricResultError(
            f"Photometric value is not ASCII text: {stream_name}"
        ) from exc
    value = _number(text)
    if value is None:
        raise PhotometricResultError(
            f"Photometric value is not numeric in {stream_name}: {text!r}"
        )
    return value


def parse_photometric_data_file(
    path: Path, expected_wavelengths_nm: Sequence[float]
) -> list[AbsorbancePoint]:
    """Read one-sample absorbance values directly from a LabSolutions .vphd."""

    expected = [float(value) for value in expected_wavelengths_nm]
    try:
        if not olefile.isOleFile(str(path)):
            raise PhotometricResultError(
                f"Photometric data file is not an OLE compound file: {path}"
            )
        values: dict[float, float] = {}
        with olefile.OleFileIO(str(path)) as compound:
            for stream in compound.listdir(streams=True, storages=False):
                if (
                    len(stream) != 3
                    or stream[0] != "Sample Table"
                    or stream[1] != "Column Data"
                ):
                    continue
                wavelength = _header_wavelength(stream[2], expected)
                if wavelength is None:
                    continue
                if wavelength in values:
                    raise PhotometricResultError(
                        f"duplicate Photometric wavelength stream: {wavelength:g} nm"
                    )
                data = compound.openstream(stream).read()
                values[wavelength] = _decode_vphd_absorbance(
                    data, stream_name="/".join(stream)
                )
    except PhotometricResultError:
        raise
    except (OSError, olefile.OleFileError) as exc:
        raise PhotometricResultError(
            f"cannot read Photometric data file {path}: {exc}"
        ) from exc
    if len(values) != len(expected):
        missing = [value for value in expected if value not in values]
        raise PhotometricResultError(
            f"Photometric data file {path} is missing wavelengths: {missing}"
        )
    return [AbsorbancePoint(wavelength, values[wavelength]) for wavelength in expected]


def _write_csv(path: Path, points: Sequence[AbsorbancePoint]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["wavelength_nm", "absorbance"])
            for point in points:
                writer.writerow(
                    [f"{point.wavelength_nm:g}", f"{point.absorbance:.12g}"]
                )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_time_course_csv(path: Path, points: Sequence[TimeCoursePoint]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_seconds", "time_min", "absorbance"])
            for point in points:
                writer.writerow(
                    [
                        f"{point.time_seconds:g}",
                        f"{point.time_seconds / 60.0:g}",
                        f"{point.absorbance:.12g}",
                    ]
                )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_photometric_data_file(
    *,
    data_file: Path,
    expected_wavelengths_nm: Sequence[float],
    csv_file: Path,
) -> list[AbsorbancePoint]:
    """Convert one verified LabSolutions .vphd segment to normalized CSV."""

    points = parse_photometric_data_file(data_file, expected_wavelengths_nm)
    _write_csv(csv_file, points)
    return points


def _line(
    pixels: bytearray,
    width: int,
    height: int,
    start: tuple[int, int],
    end: tuple[int, int],
    color: tuple[int, int, int],
) -> None:
    x0, y0 = start
    x1, y1 = end
    dx = abs(x1 - x0)
    sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0)
    sy = 1 if y0 < y1 else -1
    error = dx + dy
    while True:
        if 0 <= x0 < width and 0 <= y0 < height:
            offset = (y0 * width + x0) * 3
            pixels[offset : offset + 3] = bytes(color)
        if x0 == x1 and y0 == y1:
            break
        twice = 2 * error
        if twice >= dy:
            error += dy
            x0 += sx
        if twice <= dx:
            error += dx
            y0 += sy


_PLOT_FONT: dict[str, tuple[str, ...]] = {
    " ": ("00000",) * 7,
    "(": ("00110", "01000", "10000", "10000", "10000", "01000", "00110"),
    ")": ("01100", "00010", "00001", "00001", "00001", "00010", "01100"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00100", "01000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    ":": ("00000", "00110", "00110", "00000", "00110", "00110", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
}


def _fill_rectangle(
    pixels: bytearray,
    width: int,
    height: int,
    left: int,
    top: int,
    right: int,
    bottom: int,
    color: tuple[int, int, int],
) -> None:
    clipped_left = max(0, left)
    clipped_right = min(width - 1, right)
    clipped_top = max(0, top)
    clipped_bottom = min(height - 1, bottom)
    if clipped_left > clipped_right or clipped_top > clipped_bottom:
        return
    row = bytes(color) * (clipped_right - clipped_left + 1)
    for y in range(clipped_top, clipped_bottom + 1):
        start = (y * width + clipped_left) * 3
        pixels[start : start + len(row)] = row


def _text_size(text: str, *, scale: int = 1, vertical: bool = False) -> tuple[int, int]:
    horizontal_width = max(0, (len(text) * 6 - 1) * scale)
    horizontal_height = 7 * scale
    return (
        (horizontal_height, horizontal_width)
        if vertical
        else (horizontal_width, horizontal_height)
    )


def _draw_text(
    pixels: bytearray,
    width: int,
    height: int,
    origin: tuple[int, int],
    text: str,
    color: tuple[int, int, int],
    *,
    scale: int = 1,
    vertical: bool = False,
) -> None:
    normalized = text.upper()
    horizontal_width, _ = _text_size(normalized, scale=scale)
    origin_x, origin_y = origin
    for character_index, character in enumerate(normalized):
        glyph = _PLOT_FONT.get(character, _PLOT_FONT["?"])
        character_x = character_index * 6 * scale
        for glyph_y, row in enumerate(glyph):
            for glyph_x, value in enumerate(row):
                if value != "1":
                    continue
                for scale_y in range(scale):
                    for scale_x in range(scale):
                        pixel_x = character_x + glyph_x * scale + scale_x
                        pixel_y = glyph_y * scale + scale_y
                        if vertical:
                            target_x = origin_x + pixel_y
                            target_y = origin_y + horizontal_width - 1 - pixel_x
                        else:
                            target_x = origin_x + pixel_x
                            target_y = origin_y + pixel_y
                        if 0 <= target_x < width and 0 <= target_y < height:
                            offset = (target_y * width + target_x) * 3
                            pixels[offset : offset + 3] = bytes(color)


def _format_plot_number(value: float, span: float, *, maximum: bool = False) -> str:
    if maximum:
        text = f"{value:.6g}"
    else:
        absolute_span = abs(span)
        decimals = 0 if absolute_span >= 100 else 1 if absolute_span >= 10 else 2 if absolute_span >= 1 else 3
        text = f"{value:.0f}" if decimals == 0 else f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", "-0.0", ""} else text


def _dashed_guide(
    pixels: bytearray,
    width: int,
    height: int,
    start: tuple[int, int],
    end: tuple[int, int],
    color: tuple[int, int, int],
) -> None:
    x0, y0 = start
    x1, y1 = end
    if x0 == x1:
        low, high = sorted((y0, y1))
        for y in range(low, high + 1, 10):
            _line(pixels, width, height, (x0, y), (x0, min(y + 5, high)), color)
    elif y0 == y1:
        low, high = sorted((x0, x1))
        for x in range(low, high + 1, 10):
            _line(pixels, width, height, (x, y0), (min(x + 5, high), y0), color)


def _png_chunk(name: bytes, data: bytes) -> bytes:
    body = name + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def write_spectrum_png(
    path: Path,
    points: Sequence[AbsorbancePoint],
    *,
    x_axis_label: str = "WAVELENGTH (NM)",
) -> None:
    """Render a stable 1000x600 line plot using only the standard library."""

    if not points:
        raise PhotometricResultError("cannot plot an empty Photometric result")
    width, height = 1000, 600
    left, right, top, bottom = 115, 45, 45, 95
    pixels = bytearray([255] * width * height * 3)
    plot_width = width - left - right
    plot_height = height - top - bottom
    wavelengths = [point.wavelength_nm for point in points]
    absorbances = [point.absorbance for point in points]
    x_min, x_max = min(wavelengths), max(wavelengths)
    y_min, y_max = min(absorbances), max(absorbances)
    if math.isclose(x_min, x_max):
        x_min -= 0.5
        x_max += 0.5
    if math.isclose(y_min, y_max):
        padding = max(0.05, abs(y_min) * 0.05)
        y_min -= padding
        y_max += padding
    else:
        padding = (y_max - y_min) * 0.08
        y_min -= padding
        y_max += padding

    axis_color = (45, 50, 55)
    label_color = (35, 40, 45)
    for tick in range(6):
        fraction = tick / 5
        x = left + round(plot_width * fraction)
        y = top + plot_height - round(plot_height * fraction)
        _line(pixels, width, height, (x, top), (x, top + plot_height), (230, 234, 238))
        _line(pixels, width, height, (left, y), (left + plot_width, y), (230, 234, 238))
        _line(
            pixels,
            width,
            height,
            (x, top + plot_height),
            (x, top + plot_height + 6),
            axis_color,
        )
        _line(pixels, width, height, (left - 6, y), (left, y), axis_color)

        x_value = x_min + (x_max - x_min) * fraction
        x_text = _format_plot_number(x_value, x_max - x_min)
        x_text_width, _ = _text_size(x_text, scale=2)
        _draw_text(
            pixels,
            width,
            height,
            (x - x_text_width // 2, top + plot_height + 12),
            x_text,
            label_color,
            scale=2,
        )

        y_value = y_min + (y_max - y_min) * fraction
        y_text = _format_plot_number(y_value, y_max - y_min)
        y_text_width, y_text_height = _text_size(y_text, scale=2)
        _draw_text(
            pixels,
            width,
            height,
            (left - y_text_width - 12, y - y_text_height // 2),
            y_text,
            label_color,
            scale=2,
        )
    _line(pixels, width, height, (left, top), (left, top + plot_height), axis_color)
    _line(
        pixels,
        width,
        height,
        (left, top + plot_height),
        (left + plot_width, top + plot_height),
        axis_color,
    )

    x_label_width, _ = _text_size(x_axis_label, scale=2)
    _draw_text(
        pixels,
        width,
        height,
        (left + (plot_width - x_label_width) // 2, height - 28),
        x_axis_label,
        label_color,
        scale=2,
    )
    y_axis_label = "ABSORBANCE"
    y_label_width, y_label_height = _text_size(y_axis_label, scale=2, vertical=True)
    _draw_text(
        pixels,
        width,
        height,
        (20, top + (plot_height - y_label_height) // 2),
        y_axis_label,
        label_color,
        scale=2,
        vertical=True,
    )

    coordinates = [
        (
            left + round((point.wavelength_nm - x_min) / (x_max - x_min) * plot_width),
            top + round((y_max - point.absorbance) / (y_max - y_min) * plot_height),
        )
        for point in points
    ]
    for start, end in zip(coordinates, coordinates[1:]):
        _line(pixels, width, height, start, end, (20, 105, 180))
    maximum_index = max(range(len(points)), key=lambda index: points[index].absorbance)
    maximum = coordinates[maximum_index]
    _dashed_guide(
        pixels,
        width,
        height,
        (left, maximum[1]),
        maximum,
        (215, 135, 135),
    )
    _dashed_guide(
        pixels,
        width,
        height,
        (maximum[0], top + plot_height),
        maximum,
        (215, 135, 135),
    )
    for offset in range(-4, 5):
        _line(
            pixels,
            width,
            height,
            (maximum[0] - 4, maximum[1] + offset),
            (maximum[0] + 4, maximum[1] + offset),
            (190, 45, 45),
        )

    maximum_point = points[maximum_index]
    maximum_text = (
        f"MAX X:{_format_plot_number(maximum_point.wavelength_nm, x_max - x_min, maximum=True)} NM "
        f"Y:{_format_plot_number(maximum_point.absorbance, y_max - y_min, maximum=True)}"
    )
    maximum_text_width, maximum_text_height = _text_size(maximum_text, scale=2)
    box_width = maximum_text_width + 16
    box_height = maximum_text_height + 14
    if maximum[0] + 18 + box_width <= left + plot_width:
        box_left = maximum[0] + 18
    else:
        box_left = maximum[0] - box_width - 18
    if maximum[1] + 18 + box_height <= top + plot_height:
        box_top = maximum[1] + 18
    else:
        box_top = maximum[1] - box_height - 18
    box_left = max(left + 8, min(box_left, left + plot_width - box_width - 8))
    box_top = max(top + 8, min(box_top, top + plot_height - box_height - 8))
    _line(
        pixels,
        width,
        height,
        maximum,
        (
            box_left if box_left > maximum[0] else box_left + box_width,
            box_top + box_height // 2,
        ),
        (190, 45, 45),
    )
    _fill_rectangle(
        pixels,
        width,
        height,
        box_left,
        box_top,
        box_left + box_width,
        box_top + box_height,
        (255, 255, 255),
    )
    _line(pixels, width, height, (box_left, box_top), (box_left + box_width, box_top), (190, 45, 45))
    _line(
        pixels,
        width,
        height,
        (box_left, box_top + box_height),
        (box_left + box_width, box_top + box_height),
        (190, 45, 45),
    )
    _line(pixels, width, height, (box_left, box_top), (box_left, box_top + box_height), (190, 45, 45))
    _line(
        pixels,
        width,
        height,
        (box_left + box_width, box_top),
        (box_left + box_width, box_top + box_height),
        (190, 45, 45),
    )
    _draw_text(
        pixels,
        width,
        height,
        (box_left + 8, box_top + 7),
        maximum_text,
        (150, 35, 35),
        scale=2,
    )

    scanlines = b"".join(
        b"\0" + bytes(pixels[row * width * 3 : (row + 1) * width * 3])
        for row in range(height)
    )
    description = (
        f"UV-Vis absorbance; maximum {maximum_point.absorbance:.12g} at "
        f"{maximum_point.wavelength_nm:g} nm"
    ).encode("latin-1", errors="replace")
    content = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"tEXt", b"Description\0" + description)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_time_course_png(path: Path, points: Sequence[TimeCoursePoint]) -> None:
    """Render a Time Course line plot with time in minutes on the x-axis."""

    spectrum_points = [
        AbsorbancePoint(point.time_seconds / 60.0, point.absorbance)
        for point in points
    ]
    write_spectrum_png(path, spectrum_points, x_axis_label="TIME (MIN)")


def _build_result_bundle(
    *,
    mode: str,
    points: Sequence[AbsorbancePoint],
    source_exports: Sequence[Path],
    csv_file: Path,
    json_file: Path,
    png_file: Path,
    batch_id: str,
    sample_id: str,
    publish_root: Path | None,
    request: dict[str, float] | None = None,
) -> dict[str, object]:
    if not points:
        raise RuntimeError("cannot build an empty UV-Vis result")
    maximum = max(points, key=lambda point: point.absorbance)
    _write_csv(csv_file, points)
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "batch_id": batch_id,
        "sample_id": sample_id,
        "point_count": len(points),
        "maximum_absorbance": maximum.as_dict(),
        "points": [point.as_dict() for point in points],
        "source_exports": [str(path) for path in source_exports],
        "csv_file": str(csv_file),
        "json_file": str(json_file),
        "png_file": str(png_file),
    }
    if request is not None:
        payload["request"] = request
    write_json_atomic(json_file, payload)
    write_spectrum_png(png_file, points)

    published: dict[str, str] | None = None
    if publish_root is not None:
        destination = publish_root / batch_id / sample_id
        destination.mkdir(parents=True, exist_ok=True)
        published = {}
        for name, source in (
            ("result.csv", csv_file),
            ("result.json", json_file),
            ("result.png", png_file),
        ):
            target = destination / name
            temporary = target.with_name(f".{name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            published[name] = str(target)
    payload["published"] = published
    write_json_atomic(json_file, payload)
    return payload


def build_spectrum_result(
    *,
    export_file: Path,
    lower_nm: float,
    upper_nm: float,
    step_nm: float,
    csv_file: Path,
    json_file: Path,
    png_file: Path,
    batch_id: str,
    sample_id: str,
    publish_root: Path | None,
) -> dict[str, object]:
    """Verify one Spectrum export and create the standard AI-facing result bundle."""

    points = parse_spectrum_export(
        export_file,
        lower_nm=lower_nm,
        upper_nm=upper_nm,
        step_nm=step_nm,
    )
    return _build_result_bundle(
        mode="spectrum",
        points=points,
        source_exports=[export_file],
        csv_file=csv_file,
        json_file=json_file,
        png_file=png_file,
        batch_id=batch_id,
        sample_id=sample_id,
        publish_root=publish_root,
        request={
            "lower_nm": float(lower_nm),
            "upper_nm": float(upper_nm),
            "step_nm": float(step_nm),
        },
    )


def build_photometric_result(
    *,
    export_files: Sequence[Path],
    expected_segments: Sequence[Sequence[float]],
    csv_file: Path,
    json_file: Path,
    png_file: Path,
    batch_id: str,
    sample_id: str,
    publish_root: Path | None,
) -> dict[str, object]:
    if len(export_files) != len(expected_segments):
        raise PhotometricResultError("export file and wavelength segment counts differ")
    points = [
        point
        for path, wavelengths in zip(export_files, expected_segments, strict=True)
        for point in parse_photometric_export(path, wavelengths)
    ]
    expected = [float(value) for segment in expected_segments for value in segment]
    if len(points) != len(expected) or any(
        not _same_wavelength(point.wavelength_nm, wavelength)
        for point, wavelength in zip(points, expected, strict=True)
    ):
        raise PhotometricResultError(
            "merged Photometric wavelengths do not match request"
        )
    return _build_result_bundle(
        mode="photometric",
        points=points,
        source_exports=export_files,
        csv_file=csv_file,
        json_file=json_file,
        png_file=png_file,
        batch_id=batch_id,
        sample_id=sample_id,
        publish_root=publish_root,
    )


def build_time_course_result(
    *,
    export_file: Path,
    wavelength_nm: float,
    interval_seconds: float,
    duration_seconds: float,
    csv_file: Path,
    json_file: Path,
    png_file: Path,
    batch_id: str,
    sample_id: str,
    publish_root: Path | None,
) -> dict[str, object]:
    """Verify a Time Course export and create an AI-facing kinetics bundle."""

    points = parse_time_course_export(
        export_file,
        interval_seconds=interval_seconds,
        duration_seconds=duration_seconds,
    )
    _write_time_course_csv(csv_file, points)
    write_time_course_png(png_file, points)
    record_rows = [point.as_dict() for point in points]
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": "time_course",
        "batch_id": batch_id,
        "sample_id": sample_id,
        "point_count": len(points),
        "request": {
            "wavelength_nm": float(wavelength_nm),
            "interval_seconds": float(interval_seconds),
            "duration_seconds": float(duration_seconds),
        },
        "points": record_rows,
        "record_rows": record_rows,
        "source_exports": [str(export_file)],
        "csv_file": str(csv_file),
        "json_file": str(json_file),
        "png_file": str(png_file),
    }
    write_json_atomic(json_file, payload)

    published: dict[str, str] | None = None
    if publish_root is not None:
        destination = publish_root / batch_id / sample_id
        destination.mkdir(parents=True, exist_ok=True)
        published = {}
        for name, source in (
            ("result.csv", csv_file),
            ("result.json", json_file),
            ("result.png", png_file),
        ):
            target = destination / name
            temporary = target.with_name(f".{name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            published[name] = str(target)
    payload["published"] = published
    write_json_atomic(json_file, payload)
    return payload
