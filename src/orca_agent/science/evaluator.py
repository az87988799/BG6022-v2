"""Pure minimum-support policy evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from orca_agent.domain.p6 import (
    P6MinimumStatus,
    ScientificCheck,
    ScientificPolicy,
)

from .modes import ModeAnalysis


@dataclass(frozen=True)
class MinimumEvaluation:
    status: P6MinimumStatus
    reason: str
    checks: tuple[ScientificCheck, ...]
    missing_requirements: tuple[str, ...]
    warnings: tuple[str, ...]


def evaluate_minimum(
    *,
    policy: ScientificPolicy,
    opt_converged: bool,
    freq_complete: bool,
    geometry_bound: bool,
    method_supported: bool,
    context_supported: bool,
    hessian_complete: bool,
    mode_analysis: ModeAnalysis | None,
    requested_nodes: tuple[str, ...] | None = None,
) -> MinimumEvaluation:
    requested = (
        None
        if requested_nodes is None
        else frozenset(item.casefold().strip() for item in requested_nodes)
    )
    if requested is not None and not {"opt", "freq"}.issubset(requested):
        checks = (
            ScientificCheck(
                check_id="opt_converged",
                status=(
                    "not_applicable"
                    if "opt" not in requested
                    else "passed"
                    if opt_converged
                    else "failed"
                ),
                summary=(
                    "Opt was not requested"
                    if "opt" not in requested
                    else "Opt convergence is required"
                ),
            ),
            ScientificCheck(
                check_id="freq_complete",
                status=(
                    "not_applicable"
                    if "freq" not in requested
                    else "passed"
                    if freq_complete
                    else "failed"
                ),
                summary=(
                    "Freq was not requested"
                    if "freq" not in requested
                    else "Freq output and Hessian are complete"
                ),
            ),
            ScientificCheck(
                check_id="geometry_binding",
                status=(
                    "not_applicable"
                    if not {"opt", "freq"}.issubset(requested)
                    else "passed"
                    if geometry_bound
                    else "failed"
                ),
                summary=(
                    "Opt/Freq geometry binding is not applicable to this scope"
                    if not {"opt", "freq"}.issubset(requested)
                    else "Freq uses the bound optimized geometry"
                ),
            ),
            ScientificCheck(
                check_id="method_scope",
                status="passed" if method_supported else "failed",
                summary="Method and ORCA version are in policy",
            ),
            ScientificCheck(
                check_id="state_scope",
                status="passed" if context_supported else "failed",
                summary="Charge, multiplicity and gas environment are in policy",
            ),
            ScientificCheck(
                check_id="hessian",
                status=(
                    "not_applicable"
                    if "freq" not in requested
                    else "passed"
                    if hessian_complete
                    else "failed"
                ),
                summary=(
                    "Hessian was not requested"
                    if "freq" not in requested
                    else "Hessian layout is complete"
                ),
            ),
        )
        return MinimumEvaluation(
            P6MinimumStatus.INCONCLUSIVE,
            "minimum_assessment_not_requested",
            checks,
            (),
            ("local_minimum_support_not_requested",),
        )
    missing: list[str] = []
    for ok, name in (
        (opt_converged, "optimized_geometry_not_converged"),
        (freq_complete, "frequency_result_incomplete"),
        (geometry_bound, "frequency_geometry_binding_missing"),
        (method_supported, "method_or_orca_version_outside_policy"),
        (context_supported, "charge_multiplicity_or_environment_outside_policy"),
        (hessian_complete, "hessian_incomplete"),
    ):
        if not ok:
            missing.append(name)
    checks = [
        ScientificCheck(
            check_id="opt_converged",
            status="passed" if opt_converged else "failed",
            summary="Opt convergence is required",
        ),
        ScientificCheck(
            check_id="freq_complete",
            status="passed" if freq_complete else "failed",
            summary="Freq output and Hessian are complete",
        ),
        ScientificCheck(
            check_id="geometry_binding",
            status="passed" if geometry_bound else "failed",
            summary="Freq uses the bound optimized geometry",
        ),
        ScientificCheck(
            check_id="method_scope",
            status="passed" if method_supported else "failed",
            summary="Method and ORCA version are in policy",
        ),
        ScientificCheck(
            check_id="state_scope",
            status="passed" if context_supported else "failed",
            summary="Charge, multiplicity and gas environment are in policy",
        ),
        ScientificCheck(
            check_id="hessian",
            status="passed" if hessian_complete else "failed",
            summary="Hessian layout is complete",
        ),
    ]
    if missing:
        return MinimumEvaluation(
            P6MinimumStatus.INCONCLUSIVE,
            "minimum_prerequisites_incomplete",
            tuple(checks),
            tuple(missing),
            (),
        )
    if mode_analysis is None or not mode_analysis.layout_supported:
        reason = (
            "unsupported_mode_layout"
            if mode_analysis is None
            else (mode_analysis.reason or "unsupported_mode_layout")
        )
        checks.append(
            ScientificCheck(check_id="mode_layout", status="inconclusive", summary=reason)
        )
        return MinimumEvaluation(P6MinimumStatus.INCONCLUSIVE, reason, tuple(checks), (), ())
    checks.append(
        ScientificCheck(
            check_id="mode_layout",
            status="passed",
            summary="Projected rigid and vibrational mode layout is valid",
        )
    )
    real = mode_analysis.real_frequencies
    if any(value < policy.significant_imaginary_frequency_cm1 for value in real):
        return MinimumEvaluation(
            P6MinimumStatus.NOT_SUPPORTED,
            "significant_imaginary_frequency",
            tuple(checks),
            (),
            (),
        )
    if any(
        policy.significant_imaginary_frequency_cm1 <= value <= policy.near_zero_frequency_cm1
        for value in real
    ):
        return MinimumEvaluation(
            P6MinimumStatus.INCONCLUSIVE,
            "near_zero_or_small_imaginary_frequency",
            tuple(checks),
            (),
            (),
        )
    warnings = (
        ("low_frequency_mode_limit",)
        if any(1.0 < value <= policy.low_frequency_limit_cm1 for value in real)
        else ()
    )
    return MinimumEvaluation(
        P6MinimumStatus.SUPPORTED_WITHIN_POLICY,
        "all_vibrational_candidates_above_policy_floor",
        tuple(checks),
        (),
        warnings,
    )


__all__ = ["MinimumEvaluation", "evaluate_minimum"]
