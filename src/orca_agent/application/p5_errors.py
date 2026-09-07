"""Typed diagnostics for the P5 execution boundary."""

from __future__ import annotations

from .errors import ApplicationError


class P5Error(ApplicationError):
    code = "p5_error"


class SourceIntegrityError(P5Error):
    code = "source_integrity_error"


class UnsupportedExecutionProfile(P5Error):
    code = "unsupported_execution_profile"


class UnsupportedIsotope(P5Error):
    code = "unsupported_isotope"


class GeometryGenerationFailed(P5Error):
    code = "geometry_generation_failed"


class GeometryBindingMismatch(P5Error):
    code = "geometry_binding_mismatch"


class ApprovalMismatch(P5Error):
    code = "approval_mismatch"


class ApprovalExpired(P5Error):
    code = "approval_expired"


class RealExecutionDisabled(P5Error):
    code = "real_execution_disabled"


class ExecutableVersionMismatch(P5Error):
    code = "executable_version_mismatch"


class LaunchStateUnknown(P5Error):
    code = "launch_state_unknown"


class ProcessIdentityMismatch(P5Error):
    code = "process_identity_mismatch"


class ResourceLimitExceeded(P5Error):
    code = "resource_limit_exceeded"


class OrcaNonzeroExit(P5Error):
    code = "orca_nonzero_exit"


class ScfNotConverged(P5Error):
    code = "scf_not_converged"


class OptimizationNotConverged(P5Error):
    code = "optimization_not_converged"


class OutputTruncated(P5Error):
    code = "output_truncated"


class RequiredOutputMissing(P5Error):
    code = "required_output_missing"


class TimedOut(P5Error):
    code = "timed_out"


class Cancelled(P5Error):
    code = "cancelled"


class Interrupted(P5Error):
    code = "interrupted"


__all__ = [
    "ApprovalExpired",
    "ApprovalMismatch",
    "Cancelled",
    "ExecutableVersionMismatch",
    "GeometryBindingMismatch",
    "GeometryGenerationFailed",
    "Interrupted",
    "LaunchStateUnknown",
    "OptimizationNotConverged",
    "OrcaNonzeroExit",
    "OutputTruncated",
    "P5Error",
    "ProcessIdentityMismatch",
    "RealExecutionDisabled",
    "RequiredOutputMissing",
    "ResourceLimitExceeded",
    "ScfNotConverged",
    "SourceIntegrityError",
    "TimedOut",
    "UnsupportedExecutionProfile",
    "UnsupportedIsotope",
]
