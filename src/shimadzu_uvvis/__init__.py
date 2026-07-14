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
from .configuration import ControlSettings, ScanProfile, load_settings
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
    "ScanProfile",
    "ResolvedScanProfile",
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
    "load_settings",
    "resolve_scan_profile",
]

__version__ = "0.5.0"
