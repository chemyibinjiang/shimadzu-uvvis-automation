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


@dataclass(frozen=True, slots=True)
class AbsorbancePoint:
    wavelength_nm: float
    absorbance: float

    def as_dict(self) -> dict[str, float]:
        return {
            "wavelength_nm": self.wavelength_nm,
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


def _png_chunk(name: bytes, data: bytes) -> bytes:
    body = name + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def write_spectrum_png(path: Path, points: Sequence[AbsorbancePoint]) -> None:
    """Render a stable 1000x600 line plot using only the standard library."""

    if not points:
        raise PhotometricResultError("cannot plot an empty Photometric result")
    width, height = 1000, 600
    left, right, top, bottom = 90, 40, 40, 70
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

    for tick in range(6):
        x = left + round(plot_width * tick / 5)
        y = top + round(plot_height * tick / 5)
        _line(pixels, width, height, (x, top), (x, top + plot_height), (230, 234, 238))
        _line(pixels, width, height, (left, y), (left + plot_width, y), (230, 234, 238))
    _line(pixels, width, height, (left, top), (left, top + plot_height), (60, 65, 70))
    _line(
        pixels,
        width,
        height,
        (left, top + plot_height),
        (left + plot_width, top + plot_height),
        (60, 65, 70),
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
    for offset in range(-4, 5):
        _line(
            pixels,
            width,
            height,
            (maximum[0] - 4, maximum[1] + offset),
            (maximum[0] + 4, maximum[1] + offset),
            (190, 45, 45),
        )

    scanlines = b"".join(
        b"\0" + bytes(pixels[row * width * 3 : (row + 1) * width * 3])
        for row in range(height)
    )
    maximum_point = points[maximum_index]
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
