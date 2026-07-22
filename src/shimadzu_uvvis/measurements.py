"""Validate four-mode UV-Vis requests and bind them to method templates."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence

from .configuration import (
    METHOD_FILE_EXTENSIONS,
    MeasurementMode,
    MethodTemplate,
)
from .profiles import ScanDirection, SpectrumScanRequest


DATA_FILE_EXTENSIONS: Mapping[MeasurementMode, str] = {
    "spectrum": ".vspd",
    "photometric": ".vphd",
    "quantitation": ".vqud",
    "time_course": ".vtmd",
}
SPECTRUM_DATA_INTERVALS_NM = (0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0)
# LabSolutions UV-Vis 1.13 displays "The number of registered wavelengths is
# 10" when an eleventh Photometric wavelength is added.
PHOTOMETRIC_METHOD_WAVELENGTH_LIMIT = 10
PlanningMode = Literal["auto", "spectrum", "photometric", "quantitation", "time_course"]
MeasurementPurpose = Literal["measurement", "quantitation"]
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")


class MeasurementPlanError(ValueError):
    """Raised when a measurement request or template selection is invalid."""


def _positive_float(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise MeasurementPlanError(f"{name} must be a finite number greater than zero")
    return float(value)


def _wavelengths(values: Sequence[float] | None, name: str) -> tuple[float, ...]:
    if values is None or not values:
        raise MeasurementPlanError(f"{name} is required and cannot be empty")
    normalized = tuple(_positive_float(value, name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise MeasurementPlanError(f"{name} cannot contain duplicate wavelengths")
    return normalized


def _number_token(value: float) -> str:
    return f"{value:g}".replace(".", "p")


@dataclass(frozen=True, slots=True)
class MeasurementRequest:
    mode: MeasurementMode
    signal_type: str
    parameters: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RoutedMeasurementRequest:
    requested_mode: PlanningMode
    request: MeasurementRequest
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_mode": self.requested_mode,
            "selected_mode": self.request.mode,
            "reason": self.reason,
            "selected_before_labsolutions_start": True,
        }


@dataclass(frozen=True, slots=True)
class ResolvedMethodTemplate:
    request: MeasurementRequest
    template: MethodTemplate
    generated_method_file: Path


class MethodTemplateRegistry:
    """Resolve one template by mode and signal type without editing it."""

    def __init__(self, templates: Mapping[str, MethodTemplate]) -> None:
        self._templates = dict(templates)

    def resolve(
        self,
        *,
        mode: MeasurementMode,
        signal_type: str,
        template_name: str | None,
    ) -> MethodTemplate:
        if template_name is not None:
            try:
                template = self._templates[template_name]
            except KeyError as exc:
                available = ", ".join(sorted(self._templates)) or "none configured"
                raise MeasurementPlanError(
                    f"unknown method template {template_name!r}; available: {available}"
                ) from exc
            if template.mode != mode or template.signal_type != signal_type:
                raise MeasurementPlanError(
                    f"method template {template_name!r} is {template.mode}/"
                    f"{template.signal_type}, not {mode}/{signal_type}"
                )
            return template

        matches = [
            template
            for template in self._templates.values()
            if template.mode == mode and template.signal_type == signal_type
        ]
        if not matches:
            raise MeasurementPlanError(
                f"no method template is registered for {mode}/{signal_type}"
            )
        if len(matches) > 1:
            names = ", ".join(sorted(template.name for template in matches))
            raise MeasurementPlanError(
                f"multiple method templates match {mode}/{signal_type}: {names}; "
                "specify template_name"
            )
        return matches[0]


def build_measurement_request(
    *,
    mode: MeasurementMode,
    signal_type: str = "absorbance",
    start_nm: float | None = None,
    stop_nm: float | None = None,
    step_nm: float | None = None,
    direction: ScanDirection | None = None,
    wavelength_nm: float | None = None,
    wavelengths_nm: Sequence[float] | None = None,
    interval_seconds: float | None = None,
    duration_seconds: float | None = None,
) -> MeasurementRequest:
    """Build one strict mode-specific request from MCP-friendly optional fields."""

    if mode not in METHOD_FILE_EXTENSIONS:
        raise MeasurementPlanError(f"unsupported measurement mode: {mode!r}")
    normalized_signal = signal_type.strip() if isinstance(signal_type, str) else ""
    if not _IDENTIFIER_PATTERN.fullmatch(normalized_signal):
        raise MeasurementPlanError(
            "signal_type must contain only ASCII letters, digits, underscores, "
            "or hyphens"
        )

    supplied = {
        "start_nm": start_nm,
        "stop_nm": stop_nm,
        "step_nm": step_nm,
        "direction": direction,
        "wavelength_nm": wavelength_nm,
        "wavelengths_nm": wavelengths_nm,
        "interval_seconds": interval_seconds,
        "duration_seconds": duration_seconds,
    }
    allowed = {
        "spectrum": {"start_nm", "stop_nm", "step_nm", "direction"},
        "photometric": {"wavelengths_nm"},
        "quantitation": {"wavelength_nm"},
        "time_course": {"wavelength_nm", "interval_seconds", "duration_seconds"},
    }[mode]
    unexpected = sorted(
        name
        for name, value in supplied.items()
        if value is not None and name not in allowed
    )
    if unexpected:
        raise MeasurementPlanError(f"{mode} does not accept: {', '.join(unexpected)}")

    if mode == "spectrum":
        if start_nm is None or stop_nm is None or step_nm is None:
            raise MeasurementPlanError(
                "spectrum requires start_nm, stop_nm, and step_nm"
            )
        scan = SpectrumScanRequest.from_boundaries(
            start_nm, stop_nm, step_nm, direction=direction
        )
        parameters = scan.as_dict()
    elif mode == "photometric":
        parameters = {
            "wavelengths_nm": list(_wavelengths(wavelengths_nm, "wavelengths_nm"))
        }
    elif mode == "quantitation":
        if wavelength_nm is None:
            raise MeasurementPlanError("quantitation requires wavelength_nm")
        parameters = {"wavelength_nm": _positive_float(wavelength_nm, "wavelength_nm")}
    else:
        if wavelength_nm is None:
            raise MeasurementPlanError("time_course requires wavelength_nm")
        if interval_seconds is None or duration_seconds is None:
            raise MeasurementPlanError(
                "time_course requires interval_seconds and duration_seconds"
            )
        interval = _positive_float(interval_seconds, "interval_seconds")
        duration = _positive_float(duration_seconds, "duration_seconds")
        if duration < interval:
            raise MeasurementPlanError(
                "duration_seconds must be greater than or equal to interval_seconds"
            )
        quotient = duration / interval
        if not math.isclose(quotient, round(quotient), abs_tol=1e-9):
            raise MeasurementPlanError(
                "duration_seconds must be evenly divisible by interval_seconds"
            )
        parameters = {
            "wavelength_nm": _positive_float(wavelength_nm, "wavelength_nm"),
            "interval_seconds": interval,
            "duration_seconds": duration,
            "point_count": round(quotient) + 1,
        }
    return MeasurementRequest(mode, normalized_signal, parameters)


def _is_spectrum_interval_supported(step_nm: float) -> bool:
    return any(
        math.isclose(step_nm, supported, abs_tol=1e-9)
        for supported in SPECTRUM_DATA_INTERVALS_NM
    )


def _range_wavelengths(
    scan: SpectrumScanRequest,
    *,
    start_nm: float,
    stop_nm: float,
    direction: ScanDirection | None,
) -> list[float]:
    descending = direction == "descending" or (direction is None and start_nm > stop_nm)
    first = scan.upper_nm if descending else scan.lower_nm
    delta = -scan.step_nm if descending else scan.step_nm
    return [round(first + index * delta, 12) for index in range(scan.point_count)]


def route_measurement_request(
    *,
    mode: PlanningMode = "auto",
    measurement_purpose: MeasurementPurpose = "measurement",
    signal_type: str = "absorbance",
    start_nm: float | None = None,
    stop_nm: float | None = None,
    step_nm: float | None = None,
    direction: ScanDirection | None = None,
    wavelength_nm: float | None = None,
    wavelengths_nm: Sequence[float] | None = None,
    interval_seconds: float | None = None,
    duration_seconds: float | None = None,
) -> RoutedMeasurementRequest:
    """Select a LabSolutions mode before opening an editor or touching hardware."""

    if measurement_purpose not in ("measurement", "quantitation"):
        raise MeasurementPlanError(
            "measurement_purpose must be 'measurement' or 'quantitation'"
        )
    if mode != "auto":
        request = build_measurement_request(
            mode=mode,
            signal_type=signal_type,
            start_nm=start_nm,
            stop_nm=stop_nm,
            step_nm=step_nm,
            direction=direction,
            wavelength_nm=wavelength_nm,
            wavelengths_nm=wavelengths_nm,
            interval_seconds=interval_seconds,
            duration_seconds=duration_seconds,
        )
        if measurement_purpose == "quantitation" and mode != "quantitation":
            raise MeasurementPlanError(
                "measurement_purpose='quantitation' requires quantitation mode"
            )
        return RoutedMeasurementRequest(
            requested_mode=mode,
            request=request,
            reason=f"The caller explicitly requested {mode} mode.",
        )

    range_values = (start_nm, stop_nm, step_nm)
    has_range = any(value is not None for value in range_values)
    has_times = interval_seconds is not None or duration_seconds is not None

    if measurement_purpose == "quantitation":
        if (
            has_range
            or wavelengths_nm is not None
            or has_times
            or direction is not None
        ):
            raise MeasurementPlanError(
                "automatic quantitation routing accepts wavelength_nm only"
            )
        request = build_measurement_request(
            mode="quantitation",
            signal_type=signal_type,
            wavelength_nm=wavelength_nm,
        )
        return RoutedMeasurementRequest(
            requested_mode="auto",
            request=request,
            reason=(
                "A concentration/standard-curve purpose at one wavelength requires "
                "Quantitation mode."
            ),
        )

    if has_range:
        if any(value is None for value in range_values):
            raise MeasurementPlanError(
                "automatic range routing requires start_nm, stop_nm, and step_nm"
            )
        if wavelength_nm is not None or wavelengths_nm is not None or has_times:
            raise MeasurementPlanError(
                "a wavelength range cannot be combined with fixed, discrete, or time fields"
            )
        spectrum = build_measurement_request(
            mode="spectrum",
            signal_type=signal_type,
            start_nm=start_nm,
            stop_nm=stop_nm,
            step_nm=step_nm,
            direction=direction,
        )
        normalized_step = float(spectrum.parameters["step_nm"])
        if _is_spectrum_interval_supported(normalized_step):
            return RoutedMeasurementRequest(
                requested_mode="auto",
                request=spectrum,
                reason=(
                    f"{normalized_step:g} nm is supported by the installed Spectrum "
                    "editor, so the range remains a continuous Spectrum scan."
                ),
            )
        assert start_nm is not None and stop_nm is not None
        wavelengths = _range_wavelengths(
            SpectrumScanRequest.from_boundaries(
                start_nm, stop_nm, normalized_step, direction=direction
            ),
            start_nm=float(start_nm),
            stop_nm=float(stop_nm),
            direction=direction,
        )
        photometric = build_measurement_request(
            mode="photometric",
            signal_type=signal_type,
            wavelengths_nm=wavelengths,
        )
        supported = ", ".join(f"{value:g}" for value in SPECTRUM_DATA_INTERVALS_NM)
        return RoutedMeasurementRequest(
            requested_mode="auto",
            request=photometric,
            reason=(
                f"{normalized_step:g} nm is not a Spectrum data interval on this "
                f"installation ({supported} nm are supported). Photometric mode "
                f"represents the exact {len(wavelengths)}-point wavelength list."
            ),
        )

    if wavelengths_nm is not None:
        if wavelength_nm is not None or has_times or direction is not None:
            raise MeasurementPlanError(
                "discrete wavelengths cannot be combined with fixed or time fields"
            )
        request = build_measurement_request(
            mode="photometric",
            signal_type=signal_type,
            wavelengths_nm=wavelengths_nm,
        )
        return RoutedMeasurementRequest(
            requested_mode="auto",
            request=request,
            reason="A discrete wavelength list requires Photometric mode.",
        )

    if has_times:
        if direction is not None:
            raise MeasurementPlanError(
                "direction is not valid for a fixed-wavelength time course"
            )
        request = build_measurement_request(
            mode="time_course",
            signal_type=signal_type,
            wavelength_nm=wavelength_nm,
            interval_seconds=interval_seconds,
            duration_seconds=duration_seconds,
        )
        return RoutedMeasurementRequest(
            requested_mode="auto",
            request=request,
            reason=(
                "A fixed wavelength with an interval and duration requires Time "
                "Course mode."
            ),
        )

    if wavelength_nm is not None:
        if direction is not None:
            raise MeasurementPlanError(
                "direction is not valid for a fixed-wavelength measurement"
            )
        request = build_measurement_request(
            mode="photometric",
            signal_type=signal_type,
            wavelengths_nm=[wavelength_nm],
        )
        return RoutedMeasurementRequest(
            requested_mode="auto",
            request=request,
            reason=(
                "A single absorbance reading without a standard-curve purpose uses "
                "Photometric mode."
            ),
        )

    raise MeasurementPlanError(
        "automatic mode routing requires a wavelength range, a wavelength list, "
        "one wavelength, or time-course fields"
    )


def generated_method_name(request: MeasurementRequest, extension: str) -> str:
    parameters = request.parameters
    if request.mode == "spectrum":
        stem = (
            f"spectrum_{_number_token(float(parameters['lower_nm']))}_"
            f"{_number_token(float(parameters['upper_nm']))}_"
            f"{_number_token(float(parameters['step_nm']))}nm"
        )
    elif request.mode == "photometric":
        wavelengths = "_".join(
            _number_token(float(value)) for value in parameters["wavelengths_nm"]
        )
        stem = f"photometric_{wavelengths}nm"
    elif request.mode == "quantitation":
        stem = f"quantitation_{_number_token(float(parameters['wavelength_nm']))}nm"
    else:
        stem = (
            f"time_course_{_number_token(float(parameters['wavelength_nm']))}nm_"
            f"{_number_token(float(parameters['interval_seconds']))}s_"
            f"{_number_token(float(parameters['duration_seconds']))}s"
        )
    return f"{stem}_{request.signal_type}{extension}"


def method_generation_requests(
    request: MeasurementRequest,
) -> tuple[MeasurementRequest, ...]:
    """Split one logical request into method-sized LabSolutions requests."""

    if request.mode != "photometric":
        return (request,)
    wavelengths = list(request.parameters["wavelengths_nm"])
    return tuple(
        build_measurement_request(
            mode="photometric",
            signal_type=request.signal_type,
            wavelengths_nm=wavelengths[
                index : index + PHOTOMETRIC_METHOD_WAVELENGTH_LIMIT
            ],
        )
        for index in range(0, len(wavelengths), PHOTOMETRIC_METHOD_WAVELENGTH_LIMIT)
    )


def resolve_method_template(
    templates: Mapping[str, MethodTemplate],
    generated_method_dir: Path,
    request: MeasurementRequest,
    *,
    template_name: str | None = None,
) -> ResolvedMethodTemplate:
    template = MethodTemplateRegistry(templates).resolve(
        mode=request.mode,
        signal_type=request.signal_type,
        template_name=template_name,
    )
    extension = template.method_file.suffix.lower()
    target = generated_method_dir / generated_method_name(request, extension)
    return ResolvedMethodTemplate(request, template, target)
