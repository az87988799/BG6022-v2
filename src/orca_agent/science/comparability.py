"""Explicit, finite comparability for electronic energies only."""

from __future__ import annotations

from orca_agent.domain.p6 import (
    ComparabilityAssessment,
    ComparabilityDimension,
    MethodContext,
    P6ComparabilityStatus,
    P6EvidenceRecord,
    ScientificAssessment,
)


def compare_electronic_energy(
    *,
    candidate_assessment: ScientificAssessment,
    candidate_context: MethodContext,
    candidate_evidence: P6EvidenceRecord,
    reference_assessment: ScientificAssessment,
    reference_context: MethodContext,
    reference_evidence: P6EvidenceRecord,
) -> ComparabilityAssessment:
    dimensions = (
        _dimension(
            "confirmed_identity", candidate_context.identity_hash, reference_context.identity_hash
        ),
        _dimension(
            "canonical_smiles",
            candidate_context.canonical_isomeric_smiles,
            reference_context.canonical_isomeric_smiles,
        ),
        _dimension(
            "molecular_formula",
            candidate_context.molecular_formula,
            reference_context.molecular_formula,
        ),
        _dimension(
            "formal_charge", candidate_context.formal_charge, reference_context.formal_charge
        ),
        _dimension("multiplicity", candidate_context.multiplicity, reference_context.multiplicity),
        _dimension(
            "method_profile",
            (candidate_context.method_profile_id, candidate_context.method_profile_hash),
            (reference_context.method_profile_id, reference_context.method_profile_hash),
        ),
        _dimension("orca_version", candidate_context.orca_version, reference_context.orca_version),
        _dimension("environment", candidate_context.environment, reference_context.environment),
        _dimension(
            "settings_hash", candidate_context.settings_hash, reference_context.settings_hash
        ),
        _dimension(
            "source_origin",
            candidate_assessment.source_origin.value,
            reference_assessment.source_origin.value,
        ),
        _dimension(
            "integrity",
            candidate_assessment.integrity_verified,
            reference_assessment.integrity_verified,
        ),
        _dimension(
            "quantity_unit",
            (candidate_evidence.quantity, candidate_evidence.unit),
            (reference_evidence.quantity, reference_evidence.unit),
        ),
    )
    if any(item.status is P6ComparabilityStatus.INCOMPATIBLE for item in dimensions):
        status = P6ComparabilityStatus.INCOMPATIBLE
    elif any(item.status is P6ComparabilityStatus.UNKNOWN for item in dimensions):
        status = P6ComparabilityStatus.UNKNOWN
    else:
        status = P6ComparabilityStatus.COMPATIBLE
    delta = None
    delta_token = None
    limitations: tuple[str, ...] = ()
    if status is P6ComparabilityStatus.COMPATIBLE:
        if (
            not isinstance(candidate_evidence.value, (int, float))
            or isinstance(candidate_evidence.value, bool)
            or not isinstance(reference_evidence.value, (int, float))
            or isinstance(reference_evidence.value, bool)
        ):
            status = P6ComparabilityStatus.UNKNOWN
            limitations = ("electronic_energy_values_are_missing_or_non_numeric",)
        else:
            delta = float(candidate_evidence.value) - float(reference_evidence.value)
            delta_token = f"{delta:.17g}"
    if status is not P6ComparabilityStatus.COMPATIBLE and not limitations:
        limitations = ("electronic_energy_comparison_is_not_supported",)
    return ComparabilityAssessment.create(
        record_id=_workflow_record_id(),
        candidate_assessment_id=candidate_assessment.assessment_id,
        candidate_assessment_hash=candidate_assessment.assessment_hash,
        reference_assessment_id=reference_assessment.assessment_id,
        reference_assessment_hash=reference_assessment.assessment_hash,
        candidate_energy_evidence_id=candidate_evidence.evidence_id,
        reference_energy_evidence_id=reference_evidence.evidence_id,
        status=status,
        dimensions=dimensions,
        delta_energy=delta,
        delta_energy_token=delta_token,
        limitations=limitations,
    )


def _dimension(name: str, candidate: object, reference: object) -> ComparabilityDimension:
    if candidate is None or reference is None:
        status = P6ComparabilityStatus.UNKNOWN
        reason = "one_or_both_values_are_unreported"
    elif candidate == reference:
        status = P6ComparabilityStatus.COMPATIBLE
        reason = "values_match"
    else:
        status = P6ComparabilityStatus.INCOMPATIBLE
        reason = "values_differ"
    return ComparabilityDimension(
        name=name,
        candidate_value=_json_value(candidate),
        reference_value=_json_value(reference),
        status=status,
        reason=reason,
    )


def _json_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _workflow_record_id():
    from orca_agent.domain.ids import WorkflowRecordId, new_id

    return new_id(WorkflowRecordId)


__all__ = ["compare_electronic_energy"]
