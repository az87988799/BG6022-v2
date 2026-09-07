from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.ids import (
    ArtifactId,
    AssessmentId,
    EvidenceId,
    ExecutionId,
    RunId,
    WorkflowRecordId,
    new_id,
)
from orca_agent.domain.p5 import GeometryRecord
from orca_agent.domain.p6 import (
    MethodContext,
    P6ClaimStatus,
    P6ComparabilityStatus,
    P6EvidenceRecord,
    P6EvidenceType,
    P6Locator,
    P6MinimumStatus,
    P6ModeKind,
    P6SourceOrigin,
    ScientificAssessment,
    ScientificCheck,
)
from orca_agent.science.claims import (
    build_difference_claim,
    build_energy_claim,
    validate_claim,
)
from orca_agent.science.comparability import compare_electronic_energy
from orca_agent.science.evaluator import evaluate_minimum
from orca_agent.science.modes import ModeAnalysis, classify_modes
from orca_agent.science.policy import get_policy

_HASH = "0" * 64
_TIME = datetime(2026, 9, 7, tzinfo=UTC)


def _geometry(*, linear: bool = False) -> GeometryRecord:
    coordinates = (
        (0.0, 0.0, 0.0),
        (0.9572, 0.0, 0.0),
        (0.0, 0.0 if linear else 0.9273, 0.0),
    )
    return GeometryRecord(
        record_id=new_id(WorkflowRecordId),
        run_id=new_id(RunId),
        confirmed_molecule_id=new_id(WorkflowRecordId),
        identity_hash=_HASH,
        canonical_isomeric_smiles="O",
        molecular_formula="H2O",
        formal_charge=0,
        multiplicity=1,
        atom_symbols=("O", "H", "H"),
        atom_map=(0, 1, 2),
        coordinates=coordinates,
        rdkit_version="test",
        seed=6022,
        geometry_hash=_HASH,
        xyz_bytes_sha256=_HASH,
        record_hash=_HASH,
    )


def _modes(*, values: tuple[float, ...] | None = None, zero_column: int | None = None):
    frequencies = values or (0.0,) * 6 + (1653.25, 3813.58, 3932.73)
    matrix = [[0.0 for _ in range(9)] for _ in range(9)]
    for index in range(6, 9):
        if index != zero_column:
            matrix[index][index] = 1.0
    return tuple(frequencies), tuple(tuple(row) for row in matrix)


def test_normal_modes_use_fixed_projection_and_preserve_signed_values() -> None:
    policy = get_policy("p6.nonlinear.r2scan3c.v1")
    frequencies, matrix = _modes(values=(0.0,) * 6 + (-10.25, 3813.58, 3932.73))
    analysis = classify_modes(
        _geometry(),
        frequencies,
        tuple(str(value) for value in frequencies),
        matrix,
        policy,
    )
    assert analysis.layout_supported is True
    assert analysis.real_frequencies == (-10.25, 3813.58, 3932.73)
    assert [item.kind for item in analysis.classifications[:6]] == [P6ModeKind.PROJECTED_RIGID] * 6
    assert analysis.classifications[6].frequency == -10.25


@pytest.mark.parametrize(
    ("frequencies", "zero_column", "expected_reason"),
    [
        ((0.0,) * 5 + (0.1,) + (1653.25, 3813.58, 3932.73), None, "unsupported_mode_layout"),
        (None, 6, "unsupported_mode_layout"),
    ],
)
def test_normal_mode_layout_rejects_projection_or_extra_zero_column(
    frequencies, zero_column, expected_reason
) -> None:
    policy = get_policy("p6.nonlinear.r2scan3c.v1")
    actual_frequencies, matrix = _modes(values=frequencies, zero_column=zero_column)
    analysis = classify_modes(
        _geometry(),
        actual_frequencies,
        tuple(str(value) for value in actual_frequencies),
        matrix,
        policy,
    )
    assert analysis.layout_supported is False
    assert analysis.reason == expected_reason


def test_linear_geometry_and_incomplete_matrix_are_not_reclassified() -> None:
    policy = get_policy("p6.nonlinear.r2scan3c.v1")
    frequencies, matrix = _modes()
    linear = classify_modes(
        _geometry(linear=True),
        frequencies,
        tuple(str(value) for value in frequencies),
        matrix,
        policy,
    )
    incomplete = classify_modes(_geometry(), frequencies[:-1], (), matrix, policy)
    assert linear.reason == "molecule_is_linear_or_near_linear"
    assert linear.layout_supported is False
    assert incomplete.reason == "frequency_layout_incomplete"


@pytest.mark.parametrize(
    ("real", "status", "warning"),
    [
        ((-20.0001,), P6MinimumStatus.NOT_SUPPORTED, False),
        ((-20.0,), P6MinimumStatus.INCONCLUSIVE, False),
        ((1.0,), P6MinimumStatus.INCONCLUSIVE, False),
        ((1.0001,), P6MinimumStatus.SUPPORTED_WITHIN_POLICY, True),
        ((50.0,), P6MinimumStatus.SUPPORTED_WITHIN_POLICY, True),
        ((50.0001,), P6MinimumStatus.SUPPORTED_WITHIN_POLICY, False),
    ],
)
def test_minimum_policy_boundary_matrix(real, status, warning) -> None:
    evaluation = evaluate_minimum(
        policy=get_policy("p6.nonlinear.r2scan3c.v1"),
        opt_converged=True,
        freq_complete=True,
        geometry_bound=True,
        method_supported=True,
        context_supported=True,
        hessian_complete=True,
        mode_analysis=ModeAnalysis((), tuple(real), True, None),
    )
    assert evaluation.status is status
    assert bool(evaluation.warnings) is warning


def _context(*, orca_version: str | None = "6.1.1", settings_hash: str | None = _HASH):
    return MethodContext.create(
        confirmed_molecule_id=WorkflowRecordId("workflow_" + "a" * 32),
        identity_hash=_HASH,
        canonical_isomeric_smiles="O",
        molecular_formula="H2O",
        atom_symbols=("O", "H", "H"),
        formal_charge=0,
        multiplicity=1,
        method_profile_id="baseline.r2scan3c.v1",
        method_profile_hash=_HASH,
        orca_version=orca_version,
        protocol_id="p5.opt_freq_sp.r2scan3c.v1",
        protocol_hash=_HASH,
        settings_hash=settings_hash,
    )


def _evidence(
    *,
    result_id: WorkflowRecordId,
    context: MethodContext,
    value: float,
    evidence_type: P6EvidenceType = P6EvidenceType.ELECTRONIC_ENERGY,
    quantity: str = "electronic_energy",
    unit: str = "Eh",
) -> P6EvidenceRecord:
    return P6EvidenceRecord.create(
        record_id=new_id(WorkflowRecordId),
        evidence_id=new_id(EvidenceId),
        source_p5_run_id=new_id(RunId),
        source_result_id=result_id,
        source_execution_id=new_id(ExecutionId),
        quantity=quantity,
        evidence_type=evidence_type,
        value=value,
        raw_value_token=f"{value:.12f}",
        unit=unit,
        locator=P6Locator(
            artifact_id=ArtifactId("artifact_" + "b" * 32),
            artifact_hash=_HASH,
            line=1,
        ),
        source_artifact_id=ArtifactId("artifact_" + "b" * 32),
        source_artifact_hash=_HASH,
        source_origin=P6SourceOrigin.ORCA_LOCAL,
        context_hash=context.context_hash,
        context=context,
    )


def _assessment(
    *,
    assessment_id: AssessmentId | None = None,
    source_origin: P6SourceOrigin = P6SourceOrigin.ORCA_LOCAL,
    result_id: WorkflowRecordId,
    evidence: P6EvidenceRecord,
    context: MethodContext,
) -> ScientificAssessment:
    del context
    return ScientificAssessment.create(
        record_id=new_id(WorkflowRecordId),
        assessment_id=assessment_id or new_id(AssessmentId),
        run_id=new_id(RunId),
        source_p5_run_id=evidence.source_p5_run_id,
        source_snapshot_id=new_id(WorkflowRecordId),
        source_snapshot_hash=_HASH,
        policy_id="p6.nonlinear.r2scan3c.v1",
        policy_hash=get_policy("p6.nonlinear.r2scan3c.v1").policy_hash,
        source_origin=source_origin,
        result_ids=(result_id,),
        evidence_ids=(evidence.evidence_id,),
        energy_available=True,
        minimum_status=P6MinimumStatus.SUPPORTED_WITHIN_POLICY,
        minimum_reason="all_vibrational_candidates_above_policy_floor",
        checks=(ScientificCheck(check_id="test", status="passed", summary="test"),),
        mode_classifications=(),
        integrity_verified=True,
    )


def test_energy_claim_is_bound_and_fixture_claim_is_qualified() -> None:
    policy = get_policy("p6.nonlinear.r2scan3c.v1")
    result_id = new_id(WorkflowRecordId)
    context = _context()
    evidence = _evidence(result_id=result_id, context=context, value=-76.4)
    assessment = _assessment(result_id=result_id, evidence=evidence, context=context)
    claim = build_energy_claim(
        assessment=assessment,
        policy=policy,
        evidence=evidence,
        subject_result_id=result_id,
    )
    assert claim.status is P6ClaimStatus.SUPPORTED
    validate_claim(
        claim,
        evidence={evidence.evidence_id: evidence},
        assessment=assessment,
        policy=policy,
    )
    fixture_assessment = _assessment(
        source_origin=P6SourceOrigin.FAKE_FIXTURE,
        result_id=result_id,
        evidence=evidence.model_copy(update={"source_origin": P6SourceOrigin.FAKE_FIXTURE}),
        context=context,
    )
    fixture_claim = build_energy_claim(
        assessment=fixture_assessment,
        policy=policy,
        evidence=evidence.model_copy(update={"source_origin": P6SourceOrigin.FAKE_FIXTURE}),
        subject_result_id=result_id,
    )
    assert fixture_claim.status is P6ClaimStatus.QUALIFIED


def test_claim_value_and_evidence_scope_tampering_is_rejected() -> None:
    policy = get_policy("p6.nonlinear.r2scan3c.v1")
    result_id = new_id(WorkflowRecordId)
    context = _context()
    evidence = _evidence(result_id=result_id, context=context, value=-76.4)
    assessment = _assessment(result_id=result_id, evidence=evidence, context=context)
    claim = build_energy_claim(
        assessment=assessment,
        policy=policy,
        evidence=evidence,
        subject_result_id=result_id,
    )
    with pytest.raises(StateIntegrityError, match="value is not the evidence value"):
        validate_claim(
            claim.model_copy(update={"value": -76.3}),
            evidence={evidence.evidence_id: evidence},
            assessment=assessment,
            policy=policy,
        )
    with pytest.raises(StateIntegrityError, match="missing or changed evidence"):
        validate_claim(
            claim.model_copy(update={"evidence_ids": (new_id(EvidenceId),)}),
            evidence={evidence.evidence_id: evidence},
            assessment=assessment,
            policy=policy,
        )


def test_comparison_direction_and_unknown_context_are_explicit() -> None:
    policy = get_policy("p6.nonlinear.r2scan3c.v1")
    candidate_id = new_id(WorkflowRecordId)
    reference_id = new_id(WorkflowRecordId)
    candidate_context = _context()
    reference_context = _context()
    candidate_evidence = _evidence(result_id=candidate_id, context=candidate_context, value=-76.4)
    reference_evidence = _evidence(result_id=reference_id, context=reference_context, value=-76.2)
    candidate_assessment = _assessment(
        result_id=candidate_id, evidence=candidate_evidence, context=candidate_context
    )
    reference_assessment = _assessment(
        result_id=reference_id, evidence=reference_evidence, context=reference_context
    )
    comparison = compare_electronic_energy(
        candidate_assessment=candidate_assessment,
        candidate_context=candidate_context,
        candidate_evidence=candidate_evidence,
        reference_assessment=reference_assessment,
        reference_context=reference_context,
        reference_evidence=reference_evidence,
    )
    assert comparison.status is P6ComparabilityStatus.COMPATIBLE
    assert comparison.delta_energy == pytest.approx(-0.2)
    difference = build_difference_claim(
        assessment=candidate_assessment,
        policy=policy,
        comparison=comparison,
        subject_result_ids=(candidate_id, reference_id),
        evidence_hashes=(candidate_evidence.evidence_hash, reference_evidence.evidence_hash),
    )
    validate_claim(
        difference,
        evidence={candidate_evidence.evidence_id: candidate_evidence},
        external_evidence={reference_evidence.evidence_id: reference_evidence},
        assessment=candidate_assessment,
        policy=policy,
    )
    unknown = compare_electronic_energy(
        candidate_assessment=candidate_assessment,
        candidate_context=_context(orca_version=None),
        candidate_evidence=candidate_evidence,
        reference_assessment=reference_assessment,
        reference_context=_context(orca_version=None),
        reference_evidence=reference_evidence,
    )
    assert unknown.status is P6ComparabilityStatus.UNKNOWN


def test_strict_policy_contract_rejects_unknown_schema_and_profile() -> None:
    policy = get_policy("p6.nonlinear.r2scan3c.v1")
    restored = type(policy).model_validate_json(policy.model_dump_json(), strict=True)
    assert restored == policy
    with pytest.raises(ValueError):
        get_policy("p6.unknown.v1")
    with pytest.raises(ValidationError):
        type(policy).model_validate({**policy.model_dump(mode="python"), "schema_version": 4})
