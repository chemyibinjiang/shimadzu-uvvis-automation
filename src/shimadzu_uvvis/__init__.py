"""Shimadzu LabSolutions UV-Vis text-exchange automation."""

from .client import (
    Feedback,
    LabSolutionsBusyError,
    LabSolutionsClient,
    LabSolutionsCommandError,
    LabSolutionsError,
    LabSolutionsProtocolError,
    LabSolutionsRecoveryRequiredError,
    LabSolutionsTimeoutError,
    SpectrumScheduleOverrunError,
    SpectrumRunResult,
    SpectrumSeriesPointResult,
    SpectrumSeriesResult,
    SpectrumSeriesSample,
)
from .configuration import (
    ControlSettings,
    MeasurementMode,
    MethodTemplate,
    ScanProfile,
    load_settings,
)
from .measurements import (
    MeasurementPlanError,
    MeasurementRequest,
    MethodTemplateRegistry,
    ResolvedMethodTemplate,
    build_measurement_request,
    resolve_method_template,
)
from .profiles import (
    AmbiguousScanProfileError,
    ResolvedScanProfile,
    ScanProfileNotFoundError,
    ScanProfileRegistry,
    ScanProfileResolutionError,
    SpectrumScanRequest,
    UnknownScanProfileError,
    resolve_scan_profile,
)

__all__ = [
    "Feedback",
    "AmbiguousScanProfileError",
    "ControlSettings",
    "LabSolutionsBusyError",
    "LabSolutionsClient",
    "LabSolutionsCommandError",
    "LabSolutionsError",
    "LabSolutionsProtocolError",
    "LabSolutionsRecoveryRequiredError",
    "LabSolutionsTimeoutError",
    "MeasurementMode",
    "MeasurementPlanError",
    "MeasurementRequest",
    "MethodTemplate",
    "MethodTemplateRegistry",
    "ScanProfile",
    "ResolvedScanProfile",
    "ResolvedMethodTemplate",
    "ScanProfileNotFoundError",
    "ScanProfileRegistry",
    "ScanProfileResolutionError",
    "SpectrumScanRequest",
    "SpectrumScheduleOverrunError",
    "SpectrumRunResult",
    "SpectrumSeriesPointResult",
    "SpectrumSeriesResult",
    "SpectrumSeriesSample",
    "UnknownScanProfileError",
    "build_measurement_request",
    "load_settings",
    "resolve_scan_profile",
    "resolve_method_template",
]

__version__ = "0.5.0"
