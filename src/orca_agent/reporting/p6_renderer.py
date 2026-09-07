"""Deterministic P6 scientific report rendering and verification."""

from __future__ import annotations

from pathlib import Path

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.ids import ReportManifestId, RunId, WorkflowRecordId, new_id
from orca_agent.domain.p6 import (
    ComparabilityAssessment,
    MethodContext,
    P6ArtifactRef,
    P6ClaimRecord,
    P6EvidenceRecord,
    P6ReportManifest,
    P6SourceSnapshot,
    P6WorkflowState,
    ScientificAssessment,
    ScientificPolicy,
    ThermochemistryContext,
)
from orca_agent.evidence.p6_ingestion import P6Ingestion, load_source_bundle
from orca_agent.infrastructure.artifacts import ArtifactRef, ArtifactStore
from orca_agent.infrastructure.clock import Clock, SystemClock
from orca_agent.infrastructure.p3_records import ArtifactRecordRepository
from orca_agent.infrastructure.p6_records import P6RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult
from orca_agent.orchestration.effect_receipts import EffectSuccessReceiptV1
from orca_agent.orchestration.p6_versions import P6_RENDERER_VERSION
from orca_agent.science.claims import validate_claim


class P6ReportRenderer:
    """Render only validated P6 records; no scientific values are recomputed here."""

    def __init__(
        self, database_path: str | Path, state_root: str | Path, *, clock: Clock | None = None
    ) -> None:
        self.database_path = Path(database_path).resolve()
        self.state_root = Path(state_root).resolve()
        self.clock = clock or SystemClock()

    def render(self, permit) -> HandlerResult:
        if permit.effect.effect_type != "internal.p6.render_report":
            return HandlerResult(success=False)
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                if uow.runs is None or uow.events is None:
                    raise StateIntegrityError("P6 report repositories are unavailable")
                uow.begin()
                snapshot = uow.runs.get_verified(
                    permit.effect.run_id, uow.events, outbox=uow.outbox
                )
                if not isinstance(snapshot.state, P6WorkflowState):
                    raise StateIntegrityError("P6 report effect belongs to a non-P6 run")
                records = P6RecordRepository(uow.connection)
                existing = records.latest_p6(
                    run_id=permit.effect.run_id,
                    record_type="p6.report_manifest",
                    model_type=P6ReportManifest,
                )
                if existing is not None:
                    manifest = existing[1]
                    manifest_artifact = _find_manifest_artifact(
                        connection=uow.connection,
                        state_root=self.state_root,
                        run_id=permit.effect.run_id,
                        manifest=manifest,
                    )
                    uow.commit()
                    return HandlerResult(
                        success=True,
                        result_summary=EffectSuccessReceiptV1(
                            artifact_ids=tuple(
                                item
                                for item in (
                                    manifest.markdown_artifact_id,
                                    manifest.json_artifact_id,
                                    manifest_artifact.artifact_id,
                                )
                            )
                        ),
                    )
                ingestion, policy, assessment, evidence, claims, comparisons = (
                    self._validated_inputs(uow.connection, snapshot.state)
                )
                report_value = _report_value(
                    run_id=permit.effect.run_id,
                    source=ingestion.snapshot,
                    policy=policy,
                    ingestion=ingestion,
                    assessment=assessment,
                    evidence=evidence,
                    claims=claims,
                    comparisons=comparisons,
                )
                json_bytes = canonical_json_bytes(report_value)
                markdown_bytes = _markdown_bytes(report_value)
                store = ArtifactStore(self.state_root, clock=self.clock)
                markdown = store.put_owned(
                    connection=uow.connection,
                    run_id=permit.effect.run_id,
                    scope_id=str(permit.effect.run_id),
                    role="p6_report_md",
                    content=markdown_bytes,
                    media_type="text/markdown; charset=utf-8",
                )
                json_artifact = store.put_owned(
                    connection=uow.connection,
                    run_id=permit.effect.run_id,
                    scope_id=str(permit.effect.run_id),
                    role="p6_report_json",
                    content=json_bytes,
                    media_type="application/vnd.orca-agent.p6-report+json",
                )
                dependencies = _manifest_dependencies(
                    connection=uow.connection,
                    run_id=permit.effect.run_id,
                    source=ingestion.snapshot,
                    markdown=markdown,
                    json_artifact=json_artifact,
                )
                manifest = P6ReportManifest.create(
                    report_manifest_id=new_id(ReportManifestId),
                    record_id=new_id(WorkflowRecordId),
                    run_id=permit.effect.run_id,
                    source_p5_run_id=ingestion.snapshot.source_p5_run_id,
                    source_snapshot_id=ingestion.snapshot.snapshot_id,
                    source_snapshot_hash=ingestion.snapshot.snapshot_hash,
                    policy_id=policy.policy_id,
                    policy_hash=policy.policy_hash,
                    assessment_id=assessment.assessment_id,
                    assessment_hash=assessment.assessment_hash,
                    evidence_ids=tuple(item.evidence_id for item in evidence),
                    claim_ids=tuple(item.claim_id for item in claims),
                    markdown_artifact_id=markdown.artifact_id,
                    json_artifact_id=json_artifact.artifact_id,
                    markdown_hash=markdown.content_hash,
                    json_hash=json_artifact.content_hash,
                    markdown_size_bytes=len(markdown_bytes),
                    json_size_bytes=len(json_bytes),
                    dependencies=dependencies,
                    coverage=report_value["coverage"],
                    created_at_utc=snapshot.created_at_utc,
                )
                manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
                manifest_artifact = store.put_owned(
                    connection=uow.connection,
                    run_id=permit.effect.run_id,
                    scope_id=str(permit.effect.run_id),
                    role="p6_manifest",
                    content=manifest_bytes,
                    media_type="application/vnd.orca-agent.p6-manifest+json",
                )
                records.append_p6(
                    run_id=permit.effect.run_id,
                    record_type="p6.report_manifest",
                    record=manifest,
                    created_at_utc=snapshot.created_at_utc,
                    source_event_id=permit.effect.source_event_id,
                    record_id=manifest.record_id,
                )
                uow.commit()
                return HandlerResult(
                    success=True,
                    result_summary=EffectSuccessReceiptV1(
                        artifact_ids=(
                            markdown.artifact_id,
                            json_artifact.artifact_id,
                            manifest_artifact.artifact_id,
                        )
                    ),
                )
        except Exception:
            return HandlerResult(success=False)

    def verify(self, run_id: RunId) -> dict[str, object]:
        """Verify the complete local-ledger report and deterministic bytes."""

        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                if uow.runs is None or uow.events is None:
                    raise StateIntegrityError("P6 report repositories are unavailable")
                uow.begin()
                snapshot = uow.runs.get_verified(run_id, uow.events, outbox=uow.outbox)
                if not isinstance(snapshot.state, P6WorkflowState):
                    raise StateIntegrityError("run is not a P6 workflow")
                records = P6RecordRepository(uow.connection)
                manifest_entry = records.latest_p6(
                    run_id=run_id,
                    record_type="p6.report_manifest",
                    model_type=P6ReportManifest,
                )
                if manifest_entry is None:
                    raise StateIntegrityError("P6 report manifest is missing")
                ingestion, policy, assessment, evidence, claims, comparisons = (
                    self._validated_inputs(uow.connection, snapshot.state)
                )
                manifest = manifest_entry[1]
                if snapshot.state.report_manifest_id != manifest.report_manifest_id:
                    raise StateIntegrityError("P6 state and report manifest are not bound")
                if snapshot.state.assessment_hash != manifest.assessment_hash:
                    raise StateIntegrityError("P6 state and report assessment hashes are not bound")
                if tuple(snapshot.state.evidence_ids) != tuple(manifest.evidence_ids):
                    raise StateIntegrityError("P6 state and report evidence closure are not bound")
                if tuple(snapshot.state.claim_ids) != tuple(manifest.claim_ids):
                    raise StateIntegrityError("P6 state and report claim closure are not bound")
                artifacts = ArtifactRecordRepository(uow.connection)
                manifest_artifact = _find_manifest_artifact(
                    connection=uow.connection,
                    state_root=self.state_root,
                    run_id=run_id,
                    manifest=manifest,
                )
                if manifest_artifact.artifact_id not in snapshot.state.report_artifact_ids:
                    raise StateIntegrityError("P6 state does not reference its manifest artifact")
                md_record = artifacts.get(manifest.markdown_artifact_id)
                json_record = artifacts.get(manifest.json_artifact_id)
                if md_record is None or json_record is None:
                    raise StateIntegrityError("P6 report artifact metadata is missing")
                if md_record.run_id != run_id or json_record.run_id != run_id:
                    raise StateIntegrityError("P6 report artifact owner is invalid")
                md_bytes = ArtifactStore(self.state_root).read(md_record)
                json_bytes = ArtifactStore(self.state_root).read(json_record)
                if (
                    md_record.content_hash != manifest.markdown_hash
                    or json_record.content_hash != manifest.json_hash
                    or len(md_bytes) != manifest.markdown_size_bytes
                    or len(json_bytes) != manifest.json_size_bytes
                ):
                    raise StateIntegrityError("P6 report artifact hash or size is invalid")
                _verify_manifest_dependencies(
                    connection=uow.connection,
                    state_root=self.state_root,
                    run_id=run_id,
                    source=ingestion.snapshot,
                    manifest=manifest,
                )
                report_value = _report_value(
                    run_id=run_id,
                    source=ingestion.snapshot,
                    policy=policy,
                    ingestion=ingestion,
                    assessment=assessment,
                    evidence=evidence,
                    claims=claims,
                    comparisons=comparisons,
                )
                if json_bytes != canonical_json_bytes(report_value) or md_bytes != _markdown_bytes(
                    report_value
                ):
                    raise StateIntegrityError("P6 report bytes are not deterministic")
                if (
                    manifest.source_p5_run_id != ingestion.snapshot.source_p5_run_id
                    or manifest.source_snapshot_id != ingestion.snapshot.snapshot_id
                    or manifest.source_snapshot_hash != ingestion.snapshot.snapshot_hash
                    or manifest.policy_id != policy.policy_id
                    or manifest.policy_hash != policy.policy_hash
                    or manifest.assessment_id != assessment.assessment_id
                    or manifest.assessment_hash != assessment.assessment_hash
                    or tuple(manifest.evidence_ids) != tuple(item.evidence_id for item in evidence)
                    or tuple(manifest.claim_ids) != tuple(item.claim_id for item in claims)
                    or manifest.coverage != report_value["coverage"]
                    or manifest.renderer_version != P6_RENDERER_VERSION
                ):
                    raise StateIntegrityError("P6 report manifest bindings are invalid")
                uow.commit()
            return {
                "valid": True,
                "workflow": "p6",
                "verification_scope": manifest.verification_scope,
                "run_id": str(run_id),
                "report_manifest_id": str(manifest.report_manifest_id),
                "manifest_artifact_id": str(manifest_artifact.artifact_id),
                "manifest_hash": manifest.manifest_hash,
                "report_bytes_verified": True,
                "evidence_verified": True,
                "claims_verified": True,
                "source_reparse_verified": True,
            }
        except (StateIntegrityError, ValueError, OSError):
            return {
                "valid": False,
                "workflow": "p6",
                "run_id": str(run_id),
                "report_bytes_verified": False,
                "evidence_verified": False,
                "claims_verified": False,
                "source_reparse_verified": False,
            }

    def _validated_inputs(self, connection, state: P6WorkflowState):
        records = P6RecordRepository(connection)
        source = next(
            (
                item
                for _record_id, record_type, item in records.list_p6_for_run(state.run_id)
                if record_type == "p6.source_snapshot"
                and isinstance(item, P6SourceSnapshot)
                and item.snapshot_id == state.source_snapshot_id
            ),
            None,
        )
        if source is None or source.snapshot_hash != state.source_snapshot_hash:
            raise StateIntegrityError("P6 source snapshot is missing or changed")
        policy_entry = records.latest_p6(
            run_id=state.run_id, record_type="p6.policy", model_type=ScientificPolicy
        )
        if (
            policy_entry is None
            or policy_entry[1].policy_id != state.policy_id
            or policy_entry[1].policy_hash != state.policy_hash
        ):
            raise StateIntegrityError("P6 scientific policy is missing or changed")
        assessment_entry = records.latest_p6(
            run_id=state.run_id, record_type="p6.assessment", model_type=ScientificAssessment
        )
        if assessment_entry is None or state.assessment_id != assessment_entry[1].assessment_id:
            raise StateIntegrityError("P6 assessment is missing or not acknowledged")
        if (
            state.assessment_hash is not None
            and state.assessment_hash != assessment_entry[1].assessment_hash
        ):
            raise StateIntegrityError("P6 assessment hash is missing or changed")
        policy = policy_entry[1]
        assessment = assessment_entry[1]
        evidence = tuple(
            item
            for _id, kind, item in records.list_p6_for_run(state.run_id)
            if kind == "p6.evidence" and isinstance(item, P6EvidenceRecord)
        )
        evidence_by_id = {item.evidence_id: item for item in evidence}
        if tuple(item.evidence_id for item in evidence) != assessment.evidence_ids:
            raise StateIntegrityError("P6 assessment evidence closure is incomplete")
        claims = tuple(
            item
            for _id, kind, item in records.list_p6_for_run(state.run_id)
            if kind == "p6.claim" and isinstance(item, P6ClaimRecord)
        )
        comparisons = tuple(
            item
            for _id, kind, item in records.list_p6_for_run(state.run_id)
            if kind == "p6.comparability" and isinstance(item, ComparabilityAssessment)
        )
        external_evidence: dict[object, P6EvidenceRecord] = {}
        for comparison in comparisons:
            if comparison.candidate_assessment_id != assessment.assessment_id:
                raise StateIntegrityError("P6 comparison candidate assessment is not current")
            reference = records.find_assessment(comparison.reference_assessment_id)
            if reference is None:
                raise StateIntegrityError("P6 comparison reference assessment is missing")
            _reference_run_id, reference_assessment = reference
            if reference_assessment.assessment_hash != comparison.reference_assessment_hash:
                raise StateIntegrityError("P6 comparison reference assessment hash changed")
            for _id, kind, item in records.list_p6_for_run(_reference_run_id):
                if kind == "p6.evidence" and isinstance(item, P6EvidenceRecord):
                    external_evidence[item.evidence_id] = item
        for claim in claims:
            validate_claim(
                claim,
                evidence=evidence_by_id,
                external_evidence=external_evidence,
                assessment=assessment,
                policy=policy,
            )
        ingestion = load_source_bundle(
            connection=connection,
            state_root=self.state_root,
            source_run_id=source.source_p5_run_id,
            expected_snapshot=source,
        )
        if (
            assessment.source_snapshot_hash != source.snapshot_hash
            or assessment.policy_hash != policy.policy_hash
        ):
            raise StateIntegrityError("P6 assessment bindings are invalid")
        method = next(
            (item.context for item in evidence if isinstance(item.context, MethodContext)), None
        )
        thermo = next(
            (item.context for item in evidence if isinstance(item.context, ThermochemistryContext)),
            None,
        )
        if method != ingestion.method_context or thermo != ingestion.thermochemistry_context:
            raise StateIntegrityError("P6 context evidence does not match source observations")
        return ingestion, policy, assessment, evidence, claims, comparisons


def _p6_artifact_ref(
    run_id: RunId, artifact: ArtifactRef, *, role: str = "p6_report"
) -> P6ArtifactRef:
    return P6ArtifactRef(
        artifact_id=artifact.artifact_id,
        content_hash=artifact.content_hash,
        size_bytes=artifact.size_bytes,
        media_type=artifact.media_type,
        role=role,
        relative_path=artifact.relative_path,
        owner_run_id=run_id,
    )


_P6_ARTIFACT_ROLES = {
    "application/vnd.orca-agent.p6-source-snapshot+json": "p6_source_snapshot",
    "application/vnd.orca-agent.p6-policy+json": "p6_policy",
    "application/vnd.orca-agent.p6-evidence+json": "p6_evidence",
    "application/vnd.orca-agent.p6-assessment+json": "p6_assessment",
    "application/vnd.orca-agent.p6-claims+json": "p6_claim",
    "text/markdown; charset=utf-8": "p6_report_md",
    "application/vnd.orca-agent.p6-report+json": "p6_report_json",
}


def _manifest_dependencies(
    *,
    connection,
    run_id: RunId,
    source: P6SourceSnapshot,
    markdown: ArtifactRef,
    json_artifact: ArtifactRef,
) -> tuple[P6ArtifactRef, ...]:
    values: list[P6ArtifactRef] = list(source.artifacts)
    known = {item.artifact_id for item in values}
    artifacts = ArtifactRecordRepository(connection).list_for_run(run_id)
    for artifact in artifacts:
        role = _P6_ARTIFACT_ROLES.get(artifact.media_type)
        if role is None or artifact.artifact_id in known:
            continue
        values.append(_p6_artifact_ref(run_id, _artifact_ref(artifact), role=role))
        known.add(artifact.artifact_id)
    for artifact, role in ((markdown, "p6_report_md"), (json_artifact, "p6_report_json")):
        if artifact.artifact_id not in known:
            values.append(_p6_artifact_ref(run_id, artifact, role=role))
            known.add(artifact.artifact_id)
    return tuple(values)


def _artifact_ref(artifact) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact.artifact_id,
        content_hash=artifact.content_hash,
        size_bytes=artifact.size_bytes,
        media_type=artifact.media_type,
        relative_path=artifact.relative_path,
    )


def _verify_manifest_dependencies(
    *,
    connection,
    state_root: Path,
    run_id: RunId,
    source: P6SourceSnapshot,
    manifest: P6ReportManifest,
) -> None:
    artifacts = ArtifactRecordRepository(connection)
    dependency_ids = {item.artifact_id for item in manifest.dependencies}
    required_ids = {item.artifact_id for item in source.artifacts} | {
        manifest.markdown_artifact_id,
        manifest.json_artifact_id,
    }
    if not required_ids.issubset(dependency_ids):
        raise StateIntegrityError("P6 manifest dependency closure is incomplete")
    if (
        manifest.manifest_artifact_id is not None
        and manifest.manifest_artifact_id in dependency_ids
    ):
        raise StateIntegrityError("P6 manifest must not depend on itself")
    for dependency in manifest.dependencies:
        record = artifacts.get(dependency.artifact_id)
        if record is None:
            raise StateIntegrityError("P6 manifest dependency metadata is missing")
        if (
            record.content_hash != dependency.content_hash
            or record.size_bytes != dependency.size_bytes
            or record.media_type != dependency.media_type
            or record.relative_path != dependency.relative_path
            or record.run_id != dependency.owner_run_id
        ):
            raise StateIntegrityError("P6 manifest dependency metadata changed")
        ArtifactStore(state_root).read(record)


def _find_manifest_artifact(
    *,
    connection,
    state_root: Path,
    run_id: RunId,
    manifest: P6ReportManifest,
):
    expected = canonical_json_bytes(manifest.model_dump(mode="json"))
    store = ArtifactStore(state_root)
    matches = []
    for artifact in ArtifactRecordRepository(connection).list_for_run(run_id):
        if artifact.media_type != "application/vnd.orca-agent.p6-manifest+json":
            continue
        if store.read(artifact) == expected:
            matches.append(artifact)
    if len(matches) != 1:
        raise StateIntegrityError("P6 manifest artifact is missing or ambiguous")
    return matches[0]


def _report_value(
    *,
    run_id: RunId,
    source: P6SourceSnapshot,
    policy: ScientificPolicy,
    ingestion: P6Ingestion,
    assessment: ScientificAssessment,
    evidence: tuple[P6EvidenceRecord, ...],
    claims: tuple[P6ClaimRecord, ...],
    comparisons: tuple[ComparabilityAssessment, ...],
) -> dict[str, object]:
    method = next(
        (item.context for item in evidence if isinstance(item.context, MethodContext)), None
    )
    thermo = next(
        (item.context for item in evidence if isinstance(item.context, ThermochemistryContext)),
        None,
    )
    coverage = {
        "source_origin": source.source_origin.value,
        "source_results": len(source.results),
        "complete_results": sum(item.parse_status == "complete" for item in source.results),
        "electronic_energy_claims": sum(
            item.claim_type.value == "electronic_energy" for item in claims
        ),
        "vibrational_frequency_claims": sum(
            item.claim_type.value == "vibrational_frequency" for item in claims
        ),
        "minimum_status": assessment.minimum_status.value,
        "thermochemistry_context_reported": thermo is not None,
        "free_energy_claims": 0,
        "global_minimum_claims": 0,
    }
    return {
        "report_schema": "p6-report/v1",
        "renderer_version": P6_RENDERER_VERSION,
        "run_id": str(run_id),
        "source_snapshot": source.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
        "method_context": None if method is None else method.model_dump(mode="json"),
        "thermochemistry_context": None if thermo is None else thermo.model_dump(mode="json"),
        "results": [item.model_dump(mode="json") for item in source.results],
        "assessment": assessment.model_dump(mode="json"),
        "comparisons": [item.model_dump(mode="json") for item in comparisons],
        "claims": [item.model_dump(mode="json") for item in claims],
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "coverage": coverage,
        "limitations": list(assessment.limitations),
    }


def _markdown_bytes(report: dict[str, object]) -> bytes:
    source = report["source_snapshot"]
    policy = report["policy"]
    method = report["method_context"]
    thermo = report["thermochemistry_context"]
    assessment = report["assessment"]
    results = report["results"]
    claims = report["claims"]
    comparisons = report["comparisons"]
    assert isinstance(source, dict) and isinstance(policy, dict) and isinstance(assessment, dict)
    method_name = method["method_display_name"] if isinstance(method, dict) else "not reported"
    orca_version = method.get("orca_version") if isinstance(method, dict) else None
    environment = (
        method.get("environment") if isinstance(method, dict) else source.get("environment")
    )
    lines = [
        "# BG6022 P6 Scientific Report",
        "",
        f"- P6 run: `{report['run_id']}`",
        f"- Source P5 run: `{source['source_p5_run_id']}`",
        f"- Source origin: `{source['source_origin']}`; processing: `{source['processing_mode']}`",
        "",
        "## Conclusion",
        "",
        f"- Minimum status: **{assessment['minimum_status']}**",
        f"- {assessment['minimum_reason']}",
        "- This is method- and policy-limited local-minimum support; it does not "
        "establish a global minimum or thermodynamic stability.",
        "",
        "## Method and scope",
        "",
        f"- Method: `{method_name}`",
        f"- ORCA version: `{orca_version}`",
        f"- Identity: `{source['canonical_isomeric_smiles']}` ({source['molecular_formula']})",
        f"- Charge / multiplicity: `{source['formal_charge']} / {source['multiplicity']}`",
        f"- Environment: `{environment}`",
        "- No LLM, network request, ORCA launch, geometry optimization, or "
        "automatic retry was performed by P6.",
        "",
        "## P5 result observations",
        "",
        "| Primitive | Result | Electronic energy (Eh) | Frequency count | Parse status |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for item in results:
        assert isinstance(item, dict)
        energy = item.get("energy")
        energy_text = "" if energy is None else f"{float(energy):.12f}"
        frequency_count = len(item.get("frequencies", []))
        lines.append(
            f"| `{item['primitive']}` | `{item['result_id']}` | {energy_text} | "
            f"{frequency_count} | `{item['parse_status']}` |"
        )
    lines.extend(["", "## Minimum checks", ""])
    lines.extend(
        f"- `{check['check_id']}`: {check['status']} — {check['summary']}"
        for check in assessment["checks"]
    )
    lines.extend(
        [
            "",
            "## Vibrational modes",
            "",
            "| Index | Frequency (cm^-1) | Kind |",
            "| ---: | ---: | --- |",
        ]
    )
    for item in assessment["mode_classifications"]:
        lines.append(
            f"| {item['mode_index']} | {float(item['frequency']):.12f} | `{item['kind']}` |"
        )
    lines.extend(["", "## Thermochemistry context", ""])
    if isinstance(thermo, dict):
        for key in (
            "temperature_K",
            "pressure_atm",
            "quasi_rrho",
            "cutoff_frequency_cm1",
            "frequency_scale_factor",
            "qrrho_reference_frequency_cm1",
            "standard_state",
            "symmetry_number",
        ):
            lines.append(f"- `{key}`: `{thermo.get(key)}`")
        lines.append("- Free-energy, ZPE, enthalpy, and Gibbs claims are outside this P6 MVP.")
    else:
        lines.append("- Not reported by a matched Freq result.")
    if comparisons:
        lines.extend(["", "## Explicit electronic-energy comparisons", ""])
        for item in comparisons:
            lines.append(
                f"- `{item['status']}`: ΔE = E_candidate − E_reference = "
                f"`{item.get('delta_energy')}` Eh"
            )
    lines.extend(["", "## Claims", ""])
    for claim in claims:
        lines.append(f"- `{claim['claim_type']}` / `{claim['status']}`: `{claim.get('value')}`")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in assessment["limitations"])
    lines.extend(
        [
            "",
            "## Verification references",
            "",
            f"- Evidence records: {len(report['evidence'])}",
            f"- Claims: {len(claims)}",
            f"- Policy hash: `{policy['policy_hash']}`",
            f"- Source snapshot hash: `{source['snapshot_hash']}`",
            "",
        ]
    )
    return ("\n".join(lines)).encode("utf-8")


__all__ = ["P6ReportRenderer"]
