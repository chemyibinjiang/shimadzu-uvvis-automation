"""Validate four-mode UV-Vis requests and bind them to method templates."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

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
        name for name, value in supplied.items() if value is not None and name not in allowed
    )
    if unexpected:
        raise MeasurementPlanError(
            f"{mode} does not accept: {', '.join(unexpected)}"
        )

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
        parameters = {"wavelengths_nm": list(_wavelengths(wavelengths_nm, "wavelengths_nm"))}
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
