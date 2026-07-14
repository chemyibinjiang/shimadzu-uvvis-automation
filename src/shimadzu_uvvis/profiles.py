"""Resolve semantic spectrum scan requests to registered LabSolutions methods."""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from .configuration import ScanProfile


ScanDirection = Literal["ascending", "descending"]
_ABS_TOLERANCE = 1e-6


def _finite_float(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ScanProfileResolutionError(f"{name} must be a finite number")
    return float(value)


class ScanProfileResolutionError(ValueError):
    """Base error for scan request validation and profile resolution."""


class UnknownScanProfileError(ScanProfileResolutionError):
    """Raised when a request names a profile that is not registered."""


class ScanProfileNotFoundError(ScanProfileResolutionError):
    """Raised when no registered method exactly satisfies a scan request."""


class AmbiguousScanProfileError(ScanProfileResolutionError):
    """Raised when more than one registered method satisfies a request."""


@dataclass(frozen=True, slots=True)
class SpectrumScanRequest:
    """Instrument-independent spectrum request produced by an AI tutor or API."""

    lower_nm: float
    upper_nm: float
    step_nm: float
    direction: ScanDirection | None = None
    profile_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lower_nm", _finite_float(self.lower_nm, "lower_nm"))
        object.__setattr__(self, "upper_nm", _finite_float(self.upper_nm, "upper_nm"))
        object.__setattr__(self, "step_nm", _finite_float(self.step_nm, "step_nm"))
        if self.lower_nm <= 0 or self.upper_nm <= 0:
            raise ScanProfileResolutionError("scan wavelengths must be positive")
        if self.lower_nm >= self.upper_nm:
            raise ScanProfileResolutionError(
                "lower_nm must be less than upper_nm"
            )
        if self.step_nm <= 0:
            raise ScanProfileResolutionError("step_nm must be greater than zero")
        if self.direction not in (None, "ascending", "descending"):
            raise ScanProfileResolutionError(
                "direction must be 'ascending', 'descending', or omitted"
            )
        if self.profile_name is not None:
            if not isinstance(self.profile_name, str) or not self.profile_name.strip():
                raise ScanProfileResolutionError(
                    "profile_name must be a non-empty string"
                )

        interval_count = (self.upper_nm - self.lower_nm) / self.step_nm
        if not math.isclose(
            interval_count, round(interval_count), abs_tol=_ABS_TOLERANCE
        ):
            raise ScanProfileResolutionError(
                "scan range must be evenly divisible by step_nm"
            )

    @classmethod
    def from_boundaries(
        cls,
        start_nm: float,
        stop_nm: float,
        step_nm: float,
        *,
        direction: ScanDirection | None = None,
        profile_name: str | None = None,
    ) -> SpectrumScanRequest:
        """Create a semantic range request; boundary order need not imply direction."""

        start = _finite_float(start_nm, "start_nm")
        stop = _finite_float(stop_nm, "stop_nm")
        step = _finite_float(step_nm, "step_nm")
        return cls(
            lower_nm=min(start, stop),
            upper_nm=max(start, stop),
            step_nm=step,
            direction=direction,
            profile_name=profile_name,
        )

    @property
    def point_count(self) -> int:
        return round((self.upper_nm - self.lower_nm) / self.step_nm) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "lower_nm": self.lower_nm,
            "upper_nm": self.upper_nm,
            "step_nm": self.step_nm,
            "direction": self.direction,
            "profile_name": self.profile_name,
            "point_count": self.point_count,
        }


@dataclass(frozen=True, slots=True)
class ResolvedScanProfile:
    """A validated semantic request bound to one saved LabSolutions method."""

    request: SpectrumScanRequest
    profile: ScanProfile

    def as_dict(self) -> dict[str, object]:
        return {
            "request": self.request.as_dict(),
            "profile": profile_as_dict(self.profile),
        }


def profile_direction(profile: ScanProfile) -> ScanDirection:
    return "ascending" if profile.stop_nm > profile.start_nm else "descending"


def profile_as_dict(profile: ScanProfile) -> dict[str, object]:
    return {
        "name": profile.name,
        "method_file": str(profile.method_file),
        "start_nm": profile.start_nm,
        "stop_nm": profile.stop_nm,
        "lower_nm": min(profile.start_nm, profile.stop_nm),
        "upper_nm": max(profile.start_nm, profile.stop_nm),
        "step_nm": profile.step_nm,
        "direction": profile_direction(profile),
        "scan_speed_nm_per_min": profile.scan_speed_nm_per_min,
    }


class ScanProfileRegistry:
    """Read-only registry that performs exact, safety-oriented method matching."""

    def __init__(self, profiles: Mapping[str, ScanProfile]) -> None:
        copied = dict(profiles)
        for name, profile in copied.items():
            if name != profile.name:
                raise ValueError(
                    f"scan profile registry key {name!r} does not match "
                    f"profile name {profile.name!r}"
                )
        self._profiles = MappingProxyType(copied)

    def list_profiles(self) -> tuple[ScanProfile, ...]:
        return tuple(self._profiles[name] for name in sorted(self._profiles))

    def get(self, name: str) -> ScanProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            raise UnknownScanProfileError(
                f"unknown scan profile {name!r}; available: {self.describe()}"
            ) from exc

    def resolve(self, request: SpectrumScanRequest) -> ResolvedScanProfile:
        if request.profile_name is not None:
            profile = self.get(request.profile_name)
            if not self._matches(profile, request):
                raise ScanProfileNotFoundError(
                    f"requested {self._describe_request(request)} does not match "
                    f"profile {profile.name!r} ({self._describe_profile(profile)})"
                )
            return ResolvedScanProfile(request=request, profile=profile)

        matches = [
            profile
            for profile in self._profiles.values()
            if self._matches(profile, request)
        ]
        if not matches:
            raise ScanProfileNotFoundError(
                "no registered LabSolutions method matches "
                f"{self._describe_request(request)}; available: {self.describe()}"
            )
        if len(matches) > 1:
            names = ", ".join(sorted(profile.name for profile in matches))
            raise AmbiguousScanProfileError(
                f"multiple scan profiles match this request ({names}); "
                "specify profile_name or scan direction"
            )
        return ResolvedScanProfile(request=request, profile=matches[0])

    def describe(self) -> str:
        if not self._profiles:
            return "none configured"
        return ", ".join(
            f"{profile.name}={self._describe_profile(profile)}"
            for profile in self.list_profiles()
        )

    @staticmethod
    def _matches(profile: ScanProfile, request: SpectrumScanRequest) -> bool:
        range_matches = (
            math.isclose(
                min(profile.start_nm, profile.stop_nm),
                request.lower_nm,
                abs_tol=_ABS_TOLERANCE,
            )
            and math.isclose(
                max(profile.start_nm, profile.stop_nm),
                request.upper_nm,
                abs_tol=_ABS_TOLERANCE,
            )
            and math.isclose(
                profile.step_nm, request.step_nm, abs_tol=_ABS_TOLERANCE
            )
        )
        direction_matches = (
            request.direction is None
            or profile_direction(profile) == request.direction
        )
        return range_matches and direction_matches

    @staticmethod
    def _describe_request(request: SpectrumScanRequest) -> str:
        direction = request.direction or "any-direction"
        return (
            f"{request.lower_nm:g}:{request.upper_nm:g}:{request.step_nm:g} "
            f"({direction})"
        )

    @staticmethod
    def _describe_profile(profile: ScanProfile) -> str:
        return (
            f"{profile.start_nm:g}:{profile.stop_nm:g}:{profile.step_nm:g} "
            f"({profile_direction(profile)})"
        )


def resolve_scan_profile(
    profiles: Mapping[str, ScanProfile],
    *,
    start_nm: float,
    stop_nm: float,
    step_nm: float,
    direction: ScanDirection | None = None,
    profile_name: str | None = None,
) -> ResolvedScanProfile:
    """Convenience API for MCP tools and other adapters."""

    request = SpectrumScanRequest.from_boundaries(
        start_nm,
        stop_nm,
        step_nm,
        direction=direction,
        profile_name=profile_name,
    )
    return ScanProfileRegistry(profiles).resolve(request)
