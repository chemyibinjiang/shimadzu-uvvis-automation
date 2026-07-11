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

__all__ = [
    "Feedback",
    "LabSolutionsBusyError",
    "LabSolutionsClient",
    "LabSolutionsCommandError",
    "LabSolutionsError",
    "LabSolutionsProtocolError",
    "LabSolutionsRecoveryRequiredError",
    "LabSolutionsTimeoutError",
    "SpectrumRunResult",
]

__version__ = "0.2.0"
