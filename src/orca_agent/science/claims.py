"""Claim constructors and validators bound to P6 evidence."""

from __future__ import annotations

from collections.abc import Mapping

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.p6 import (
    ComparabilityAssessment,
    P6ClaimRecord,
    P6ClaimStatus,
    P6ClaimType,
    P6EvidenceRecord,
    P6MinimumStatus,
    P6SourceOrigin,
    ScientificAssessment,
    ScientificPolicy,
)


def build_energy_claim(
    *,
    assessment: ScientificAssessment,
    policy: ScientificPolicy,
    evidence: P6EvidenceRecord,
    subject_result_id,
    claim_id=None,
) -> P6ClaimRecord:
    _require_evidence(evidence, quantity="electronic_energy")
    status, limitations = _origin_claim_status(assessment)
    return P6ClaimRecord.create(
        record_id=_record_id(),
        claim_id=claim_id or _claim_id(),
        claim_type=P6ClaimType.ELECTRONIC_ENERGY,
        subject_result_ids=(subject_result_id,),
        quantity="electronic_energy",
        value=evidence.value,
        raw_value_token=evidence.raw_value_token,
        unit="Eh",
        evidence_ids=(evidence.evidence_id,),
        evidence_hashes=(evidence.evidence_hash,),
        assessment_id=assessment.assessment_id,
        assessment_hash=assessment.assessment_hash,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        status=status,
        limitations=limitations,
    )


def build_frequency_claim(
    *,
    assessment: ScientificAssessment,
    policy: ScientificPolicy,
    evidence: P6EvidenceRecord,
    subject_result_id,
    claim_id=None,
) -> P6ClaimRecord:
    _require_evidence(evidence, quantity="vibrational_frequency")
    status, limitations = _origin_claim_status(assessment)
    return P6ClaimRecord.create(
        record_id=_record_id(),
        claim_id=claim_id or _claim_id(),
        claim_type=P6ClaimType.VIBRATIONAL_FREQUENCY,
        subject_result_ids=(subject_result_id,),
        quantity="vibrational_frequency",
        value=evidence.value,
        raw_value_token=evidence.raw_value_token,
        unit="cm^-1",
        evidence_ids=(evidence.evidence_id,),
        evidence_hashes=(evidence.evidence_hash,),
        assessment_id=assessment.assessment_id,
        assessment_hash=assessment.assessment_hash,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        status=status,
        limitations=limitations,
    )


def build_minimum_claim(
    *,
    assessment: ScientificAssessment,
    policy: ScientificPolicy,
    subject_result_id,
    evidence_hashes: tuple[str, ...] | None = None,
    claim_id=None,
) -> P6ClaimRecord:
    if evidence_hashes is None or len(evidence_hashes) != len(assessment.evidence_ids):
        raise StateIntegrityError("minimum claim requires the hashes of its evidence records")
    supported = (
        assessment.minimum_status is P6MinimumStatus.SUPPORTED_WITHIN_POLICY
        and assessment.source_origin is P6SourceOrigin.ORCA_LOCAL
    )
    status = P6ClaimStatus.SUPPORTED if supported else P6ClaimStatus.QUALIFIED
    limitations = (
        "local minimum support is limited to the registered method, convergence and mode policy",
        "global minimum and thermodynamic stability are not established",
    )
    if assessment.warnings:
        limitations += tuple(assessment.warnings)
    return P6ClaimRecord.create(
        record_id=_record_id(),
        claim_id=claim_id or _claim_id(),
        claim_type=P6ClaimType.LOCAL_MINIMUM_SUPPORT,
        subject_result_ids=(subject_result_id,),
        quantity="local_minimum_support",
        value=assessment.minimum_status.value,
        raw_value_token=assessment.minimum_status.value,
        unit=None,
        evidence_ids=assessment.evidence_ids,
        evidence_hashes=evidence_hashes,
        assessment_id=assessment.assessment_id,
        assessment_hash=assessment.assessment_hash,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        status=status,
        limitations=limitations,
    )


def build_difference_claim(
    *,
    assessment: ScientificAssessment,
    policy: ScientificPolicy,
    comparison: ComparabilityAssessment,
    subject_result_ids,
    evidence_hashes,
    claim_id=None,
) -> P6ClaimRecord:
    if comparison.status.value != "compatible":
        raise StateIntegrityError(
            "cannot build an energy difference claim from an incompatible comparison"
        )
    return P6ClaimRecord.create(
        record_id=_record_id(),
        claim_id=claim_id or _claim_id(),
        claim_type=P6ClaimType.ELECTRONIC_ENERGY_DIFFERENCE,
        subject_result_ids=tuple(subject_result_ids),
        quantity="electronic_energy_difference",
        value=comparison.delta_energy,
        raw_value_token=comparison.delta_energy_token,
        unit="Eh",
        evidence_ids=(
            comparison.candidate_energy_evidence_id,
            comparison.reference_energy_evidence_id,
        ),
        evidence_hashes=tuple(evidence_hashes),
        assessment_id=assessment.assessment_id,
        assessment_hash=assessment.assessment_hash,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        status=P6ClaimStatus.QUALIFIED,
        formula="E_candidate - E_reference",
        limitations=(
            "electronic energy difference is not a free-energy or global-stability ranking",
        ),
    )


def validate_claim(
    claim: P6ClaimRecord,
    *,
    evidence: Mapping[object, P6EvidenceRecord],
    assessment: ScientificAssessment,
    policy: ScientificPolicy,
    external_evidence: Mapping[object, P6EvidenceRecord] | None = None,
) -> None:
    if (
        claim.assessment_id != assessment.assessment_id
        or claim.assessment_hash != assessment.assessment_hash
    ):
        raise StateIntegrityError("claim assessment binding is invalid")
    if claim.policy_id != policy.policy_id or claim.policy_hash != policy.policy_hash:
        raise StateIntegrityError("claim policy binding is invalid")
    if (
        assessment.source_origin is P6SourceOrigin.FAKE_FIXTURE
        and claim.status is P6ClaimStatus.SUPPORTED
    ):
        raise StateIntegrityError("fixture-origin evidence cannot produce a supported claim")
    if len(claim.evidence_ids) != len(claim.evidence_hashes):
        raise StateIntegrityError("claim evidence binding is incomplete")
    values = []
    external = {} if external_evidence is None else dict(external_evidence)
    for evidence_id, expected_hash in zip(claim.evidence_ids, claim.evidence_hashes, strict=True):
        item = evidence.get(evidence_id)
        is_external = False
        if item is None:
            item = external.get(evidence_id)
            is_external = item is not None
        if item is None or item.evidence_hash != expected_hash:
            raise StateIntegrityError("claim references missing or changed evidence")
        if is_external and claim.claim_type is not P6ClaimType.ELECTRONIC_ENERGY_DIFFERENCE:
            raise StateIntegrityError("only energy difference claims may use external evidence")
        if (
            not is_external
            and item.source_result_id not in assessment.result_ids
            and item.source_result_id is not None
        ):
            raise StateIntegrityError("claim evidence is outside the assessment source closure")
        values.append(item)
    if claim.claim_type is P6ClaimType.ELECTRONIC_ENERGY:
        _require_evidence(values[0], quantity="electronic_energy")
        if (
            claim.value != values[0].value
            or claim.raw_value_token != values[0].raw_value_token
            or claim.unit != "Eh"
        ):
            raise StateIntegrityError("energy claim value is not the evidence value")
    elif claim.claim_type is P6ClaimType.VIBRATIONAL_FREQUENCY:
        _require_evidence(values[0], quantity="vibrational_frequency")
        if (
            claim.value != values[0].value
            or claim.raw_value_token != values[0].raw_value_token
            or claim.unit != "cm^-1"
        ):
            raise StateIntegrityError("frequency claim value is not the evidence value")
    elif claim.claim_type is P6ClaimType.LOCAL_MINIMUM_SUPPORT:
        if claim.value != assessment.minimum_status.value:
            raise StateIntegrityError("minimum claim status is not the assessment status")
    elif claim.claim_type is P6ClaimType.ELECTRONIC_ENERGY_DIFFERENCE:
        if claim.formula != "E_candidate - E_reference" or len(values) != 2:
            raise StateIntegrityError("energy difference claim formula or evidence is invalid")
        if any(item.quantity != "electronic_energy" or item.unit != "Eh" for item in values):
            raise StateIntegrityError("energy difference evidence is not electronic energy")
        expected = float(values[0].value) - float(values[1].value)
        if claim.value != expected:
            raise StateIntegrityError("energy difference value is not derived from evidence")


def _require_evidence(evidence: P6EvidenceRecord, *, quantity: str) -> None:
    if evidence.quantity != quantity or evidence.value is None or evidence.raw_value_token is None:
        raise StateIntegrityError("required scientific evidence is missing")


def _origin_claim_status(
    assessment: ScientificAssessment,
) -> tuple[P6ClaimStatus, tuple[str, ...]]:
    if assessment.source_origin is P6SourceOrigin.ORCA_LOCAL:
        return P6ClaimStatus.SUPPORTED, ()
    return P6ClaimStatus.QUALIFIED, ("fixture-origin data is not a real ORCA scientific result",)


def _record_id():
    from orca_agent.domain.ids import WorkflowRecordId, new_id

    return new_id(WorkflowRecordId)


def _claim_id():
    from orca_agent.domain.ids import ClaimId, new_id

    return new_id(ClaimId)


__all__ = [
    "build_difference_claim",
    "build_energy_claim",
    "build_frequency_claim",
    "build_minimum_claim",
    "validate_claim",
]
