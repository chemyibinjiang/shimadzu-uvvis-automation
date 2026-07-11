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
    SpectrumRunResult,
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
    "SpectrumRunResult",
    "load_settings",
]

__version__ = "0.3.0"
