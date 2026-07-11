"""Shimadzu LabSolutions UV-Vis text-exchange automation."""

from .client import (
    Feedback,
    LabSolutionsBusyError,
    LabSolutionsClient,
    LabSolutionsCommandError,
    LabSolutionsError,
    LabSolutionsProtocolError,
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
    "LabSolutionsTimeoutError",
    "SpectrumRunResult",
]

__version__ = "0.1.0"
