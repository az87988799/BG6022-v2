"""Claim constructors and validators bound to P6 evidence."""

from __future__ import annotations

from collections.abc import Mapping

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.p6 import (
    ComparabilityAssessment,
    MethodContext,
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
    _require_subject(assessment, evidence, subject_result_id)
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
    _require_subject(assessment, evidence, subject_result_id)
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
    subject_result_ids=None,
    evidence_hashes=None,
    claim_id=None,
    candidate_evidence: P6EvidenceRecord | None = None,
    reference_evidence: P6EvidenceRecord | None = None,
) -> P6ClaimRecord:
    if comparison.status.value != "compatible":
        raise StateIntegrityError(
            "cannot build an energy difference claim from an incompatible comparison"
        )
    if candidate_evidence is not None or reference_evidence is not None:
        if candidate_evidence is None or reference_evidence is None:
            raise StateIntegrityError("energy difference claim evidence must be supplied in pairs")
        _require_evidence(candidate_evidence, quantity="electronic_energy")
        _require_evidence(reference_evidence, quantity="electronic_energy")
        if (
            candidate_evidence.evidence_id != comparison.candidate_energy_evidence_id
            or reference_evidence.evidence_id != comparison.reference_energy_evidence_id
        ):
            raise StateIntegrityError("energy difference claim evidence order is invalid")
        expected_subjects = (
            candidate_evidence.source_result_id,
            reference_evidence.source_result_id,
        )
        if any(item is None for item in expected_subjects):
            raise StateIntegrityError("energy difference claim evidence has no source result")
        expected_hashes = (
            candidate_evidence.evidence_hash,
            reference_evidence.evidence_hash,
        )
        if subject_result_ids is None:
            subject_result_ids = expected_subjects
        elif tuple(subject_result_ids) != expected_subjects:
            raise StateIntegrityError("energy difference claim subjects are not candidate-first")
        if evidence_hashes is None:
            evidence_hashes = expected_hashes
        elif tuple(evidence_hashes) != expected_hashes:
            raise StateIntegrityError("energy difference claim hashes are not candidate-first")
    if subject_result_ids is None or evidence_hashes is None:
        raise StateIntegrityError(
            "energy difference claim requires two subjects and evidence hashes"
        )
    subject_result_ids = tuple(subject_result_ids)
    evidence_hashes = tuple(evidence_hashes)
    if len(subject_result_ids) != 2 or len(evidence_hashes) != 2:
        raise StateIntegrityError("energy difference claim requires exactly two ordered sides")
    return P6ClaimRecord.create(
        record_id=_record_id(),
        claim_id=claim_id or _claim_id(),
        claim_type=P6ClaimType.ELECTRONIC_ENERGY_DIFFERENCE,
        subject_result_ids=subject_result_ids,
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
    comparison: ComparabilityAssessment | None = None,
    reference_assessment: ScientificAssessment | None = None,
    reference_assessment_id: object | None = None,
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
    if not assessment.integrity_verified:
        raise StateIntegrityError("claim assessment integrity is not verified")
    if claim.quantity != claim.claim_type.value:
        raise StateIntegrityError("claim quantity is not supported for its type")
    required_count = {
        P6ClaimType.ELECTRONIC_ENERGY: 1,
        P6ClaimType.VIBRATIONAL_FREQUENCY: 1,
        P6ClaimType.ELECTRONIC_ENERGY_DIFFERENCE: 2,
        P6ClaimType.LOCAL_MINIMUM_SUPPORT: len(assessment.evidence_ids),
    }[claim.claim_type]
    if len(claim.evidence_ids) != required_count or required_count == 0:
        raise StateIntegrityError("claim evidence cardinality is invalid")
    expected_status = _origin_claim_status(assessment)[0]
    if claim.claim_type is P6ClaimType.LOCAL_MINIMUM_SUPPORT:
        if assessment.minimum_status is not P6MinimumStatus.SUPPORTED_WITHIN_POLICY:
            expected_status = P6ClaimStatus.QUALIFIED
        if (
            claim.evidence_ids != assessment.evidence_ids
            or len(claim.subject_result_ids) != 1
            or claim.subject_result_ids[0] not in assessment.result_ids
            or claim.unit is not None
            or claim.raw_value_token != assessment.minimum_status.value
        ):
            raise StateIntegrityError("minimum claim scope is invalid")
    elif claim.claim_type is P6ClaimType.ELECTRONIC_ENERGY_DIFFERENCE:
        expected_status = P6ClaimStatus.QUALIFIED
    if claim.status is not expected_status:
        raise StateIntegrityError("claim support status is invalid")
    if claim.claim_type is P6ClaimType.ELECTRONIC_ENERGY_DIFFERENCE:
        _validate_difference_claim_context(
            claim=claim,
            assessment=assessment,
            evidence=evidence,
            external_evidence=external_evidence,
            comparison=comparison,
            reference_assessment=reference_assessment,
            reference_assessment_id=reference_assessment_id,
        )
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
        if not is_external and (
            item.evidence_id not in assessment.evidence_ids
            or item.source_origin != assessment.source_origin
            or (
                item.source_result_id is not None
                and item.source_result_id not in assessment.result_ids
            )
        ):
            raise StateIntegrityError("claim evidence is outside the assessment source closure")
        values.append(item)
    if claim.claim_type is not P6ClaimType.LOCAL_MINIMUM_SUPPORT:
        if claim.subject_result_ids != tuple(item.source_result_id for item in values):
            raise StateIntegrityError("claim subjects do not match evidence in order")
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
        if (
            claim.value != expected
            or claim.unit != "Eh"
            or claim.raw_value_token != f"{expected:.17g}"
        ):
            raise StateIntegrityError("energy difference value is not derived from evidence")


def _validate_difference_claim_context(
    *,
    claim: P6ClaimRecord,
    assessment: ScientificAssessment,
    evidence: Mapping[object, P6EvidenceRecord],
    external_evidence: Mapping[object, P6EvidenceRecord] | None,
    comparison: ComparabilityAssessment | None,
    reference_assessment: ScientificAssessment | None,
    reference_assessment_id: object | None,
) -> None:
    if comparison is None or reference_assessment is None:
        raise StateIntegrityError(
            "energy difference claim requires a verified comparison and reference assessment"
        )
    if comparison.status.value != "compatible":
        raise StateIntegrityError("energy difference claim requires a compatible comparison")
    if (
        comparison.candidate_assessment_id != assessment.assessment_id
        or comparison.candidate_assessment_hash != assessment.assessment_hash
        or comparison.reference_assessment_id != reference_assessment.assessment_id
        or comparison.reference_assessment_hash != reference_assessment.assessment_hash
        or (
            reference_assessment_id is not None
            and comparison.reference_assessment_id != reference_assessment_id
        )
    ):
        raise StateIntegrityError("energy difference comparison assessment binding is invalid")
    if not reference_assessment.integrity_verified:
        raise StateIntegrityError("energy difference reference assessment is not verified")

    reference = {} if external_evidence is None else dict(external_evidence)
    candidate_energy = evidence.get(comparison.candidate_energy_evidence_id)
    reference_energy = reference.get(comparison.reference_energy_evidence_id)
    if candidate_energy is None or reference_energy is None:
        raise StateIntegrityError("energy difference comparison evidence is missing")
    _require_comparison_evidence(
        candidate_energy,
        assessment,
        label="candidate",
        expected_source_origin=assessment.source_origin,
    )
    _require_comparison_evidence(
        reference_energy,
        reference_assessment,
        label="reference",
        expected_source_origin=reference_assessment.source_origin,
    )
    _require_evidence(candidate_energy, quantity="electronic_energy")
    _require_evidence(reference_energy, quantity="electronic_energy")
    candidate_context = _method_context_for_evidence(
        evidence, assessment, candidate_energy.context_hash
    )
    reference_context = _method_context_for_evidence(
        reference, reference_assessment, reference_energy.context_hash
    )
    if candidate_context is None or reference_context is None:
        raise StateIntegrityError("energy difference comparison method context is missing")

    from orca_agent.science.comparability import compare_electronic_energy

    regenerated = compare_electronic_energy(
        candidate_assessment=assessment,
        candidate_context=candidate_context,
        candidate_evidence=candidate_energy,
        reference_assessment=reference_assessment,
        reference_context=reference_context,
        reference_evidence=reference_energy,
    )
    excluded_comparison = {"record_id", "comparability_hash"}
    if comparison.model_dump(mode="json", exclude=excluded_comparison) != regenerated.model_dump(
        mode="json", exclude=excluded_comparison
    ):
        raise StateIntegrityError("energy difference comparison does not follow verified evidence")

    expected_subjects = (candidate_energy.source_result_id, reference_energy.source_result_id)
    if any(item is None for item in expected_subjects):
        raise StateIntegrityError("energy difference comparison evidence has no source result")
    expected_hashes = (candidate_energy.evidence_hash, reference_energy.evidence_hash)
    expected_delta = float(candidate_energy.value) - float(reference_energy.value)
    expected_token = f"{expected_delta:.17g}"
    if (
        claim.evidence_ids
        != (
            comparison.candidate_energy_evidence_id,
            comparison.reference_energy_evidence_id,
        )
        or claim.evidence_hashes != expected_hashes
        or claim.subject_result_ids != expected_subjects
        or claim.quantity != "electronic_energy_difference"
        or claim.unit != "Eh"
        or claim.formula != "E_candidate - E_reference"
        or claim.value != expected_delta
        or claim.raw_value_token != expected_token
        or comparison.delta_energy != expected_delta
        or comparison.delta_energy_token != expected_token
    ):
        raise StateIntegrityError(
            "energy difference claim is not bound to candidate/reference order"
        )


def _require_comparison_evidence(
    item: P6EvidenceRecord,
    assessment: ScientificAssessment,
    *,
    label: str,
    expected_source_origin: P6SourceOrigin,
) -> None:
    if (
        item.evidence_id not in assessment.evidence_ids
        or item.source_result_id is None
        or item.source_result_id not in assessment.result_ids
        or item.source_p5_run_id != assessment.source_p5_run_id
        or item.source_origin != expected_source_origin
    ):
        raise StateIntegrityError(
            f"energy difference {label} evidence is outside its source closure"
        )


def _method_context_for_evidence(
    values: Mapping[object, P6EvidenceRecord],
    assessment: ScientificAssessment,
    context_hash: str | None,
) -> MethodContext | None:
    if context_hash is None:
        return None
    for item in values.values():
        if (
            item.evidence_id in assessment.evidence_ids
            and item.context_hash == context_hash
            and isinstance(item.context, MethodContext)
        ):
            return item.context
    return None


def _require_subject(assessment, evidence, subject_result_id) -> None:
    if (
        not assessment.integrity_verified
        or subject_result_id != evidence.source_result_id
        or subject_result_id not in assessment.result_ids
        or evidence.evidence_id not in assessment.evidence_ids
        or evidence.source_origin != assessment.source_origin
    ):
        raise StateIntegrityError("claim subject or evidence is outside its assessment")


def _require_evidence(evidence: P6EvidenceRecord, *, quantity: str) -> None:
    if evidence.quantity != quantity or evidence.value is None or evidence.raw_value_token is None:
        raise StateIntegrityError("required scientific evidence is missing")
    unit = {"electronic_energy": "Eh", "vibrational_frequency": "cm^-1"}[quantity]
    if evidence.unit != unit or evidence.evidence_type.value != quantity:
        raise StateIntegrityError("scientific evidence quantity or unit is invalid")


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
