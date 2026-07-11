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

__all__ = [
    "Feedback",
    "ControlSettings",
    "LabSolutionsBusyError",
    "LabSolutionsClient",
    "LabSolutionsCommandError",
    "LabSolutionsError",
    "LabSolutionsProtocolError",
    "LabSolutionsRecoveryRequiredError",
    "LabSolutionsTimeoutError",
    "ScanProfile",
    "SpectrumScheduleOverrunError",
    "SpectrumRunResult",
    "SpectrumSeriesPointResult",
    "SpectrumSeriesResult",
    "SpectrumSeriesSample",
    "load_settings",
]

__version__ = "0.4.0"
