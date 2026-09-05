"""Render the P3 fake assessment as traceable Markdown and JSON artifacts."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    ArtifactId,
    ConversationId,
    ExecutionId,
    ReportManifestId,
    RunId,
)
from orca_agent.domain.models import ValidatedClaim
from orca_agent.domain.p3 import (
    FixtureScientificAssessment,
    P3WorkflowState,
    ReportManifestV1,
    WorkflowPhase,
    hash_model_fields,
)
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.clock import Clock, SystemClock
from orca_agent.infrastructure.p3_records import (
    ActionRepository,
    ArtifactRecordRepository,
    EvidenceRepository,
    JobRepository,
    P3RecordRepository,
    StoredArtifact,
)
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.effect_receipts import EffectSuccessReceiptV1
from orca_agent.orchestration.effects import EffectClass
from orca_agent.planning.water import load_water_fixture

_P3_NAMESPACE = uuid.UUID("d18ac8ab-649d-49e3-b46b-8d9bb1c5f95d")


def _deterministic_report_id(execution_id: ExecutionId) -> ReportManifestId:
    return ReportManifestId(f"report_{uuid.uuid5(_P3_NAMESPACE, f'report:{execution_id}').hex}")


class P3ReportRenderer:
    def __init__(
        self,
        database_path: str | Path,
        state_root: str | Path,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.database_path = resolve_database_path(database_path)
        self.state_root = Path(state_root)
        self.clock = clock or SystemClock()
        self.artifacts = ArtifactStore(self.state_root, clock=self.clock)

    def render(self, permit: object):
        from orca_agent.infrastructure.worker import HandlerResult

        effect = getattr(permit, "effect", None)
        if effect is None or effect.effect_type != "internal.p3.render_report":
            return HandlerResult(success=False)
        if effect.effect_class is not EffectClass.INTERNAL:
            return HandlerResult(success=False)
        payload = dict(effect.payload)
        run_id = RunId(str(effect.run_id))
        effect_id = str(effect.effect_id)
        conversation_id = ConversationId(str(payload["conversation_id"]))
        action_id = ActionId(str(payload["action_id"]))
        action_hash = payload.get("action_hash")
        envelope_hash = payload.get("envelope_hash")
        budget_hash = payload.get("budget_hash")
        execution_id = ExecutionId(str(payload["execution_id"]))
        assessment_artifact_id = ArtifactId(str(payload["assessment_artifact_id"]))
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            if any(item is None for item in (uow.runs, uow.events, uow.interrupts, uow.outbox)):
                raise StateIntegrityError("report kernel repositories are unavailable")
            snapshot = uow.runs.get_verified(
                run_id,
                uow.events,
                interrupts=uow.interrupts,
                outbox=uow.outbox,
            )
            if not isinstance(snapshot.state, P3WorkflowState):
                raise StateIntegrityError("report requires a P3 workflow")
            if (
                snapshot.state.phase is not WorkflowPhase.REPORT_PENDING
                or str(snapshot.state.report_effect_id) != effect_id
                or snapshot.state.conversation_id != conversation_id
                or snapshot.state.action_id != action_id
                or snapshot.state.action_hash != action_hash
                or snapshot.state.envelope_hash != envelope_hash
                or snapshot.state.budget_hash != budget_hash
                or snapshot.state.execution_id != execution_id
            ):
                raise StateIntegrityError("report effect binding is invalid")
            actions = ActionRepository(uow.connection)
            action = actions.get(action_id)
            if (
                action is None
                or action.run_id != run_id
                or action.conversation_id != conversation_id
                or action.action.action_hash != action_hash
            ):
                raise StateIntegrityError("report action binding is invalid")
            if (
                sha256_hex(action.action.execution_envelope) != envelope_hash
                or sha256_hex(action.action.budget) != budget_hash
            ):
                raise StateIntegrityError("report action hashes are invalid")
            records = P3RecordRepository(uow.connection)
            assessment_entry = records.latest(
                run_id=run_id,
                record_type="assessment",
                model_type=FixtureScientificAssessment,
            )
            claim_entry = records.latest_any(
                run_id=run_id,
                record_type="claim",
                model_type=ValidatedClaim,
                schema_version=1,
                engine_version="p1-domain-v1",
            )
            if assessment_entry is None or claim_entry is None:
                raise StateIntegrityError("report inputs are incomplete")
            assessment = assessment_entry[1]
            claim = claim_entry[1]
            fixture = load_water_fixture()
            if (
                assessment.action_id != action_id
                or assessment.execution_id != execution_id
                or assessment.claim_id != claim.claim_id
                or snapshot.state.assessment_id != assessment.assessment_id
                or snapshot.state.claim_id != claim.claim_id
                or not assessment.accepted
                or not assessment.fixture_verified_only
                or claim.claim_type.value != "energy"
                or claim.value != fixture.energy
                or claim.unit != fixture.unit
                or claim.limitations
                != ("Synthetic fixture only; this is not a real quantum-chemistry calculation.",)
                or assessment.limitations
                != ("Synthetic fixture only; no real ORCA execution was performed.",)
            ):
                raise StateIntegrityError("report input binding is invalid")
            assessment_artifact = ArtifactRecordRepository(uow.connection).get(
                assessment_artifact_id
            )
            if (
                assessment_artifact is None
                or assessment_artifact.run_id != run_id
                or assessment_artifact.action_id != action_id
                or assessment_artifact.execution_id != execution_id
                or self.artifacts.read(assessment_artifact) != canonical_json_bytes(assessment)
            ):
                raise StateIntegrityError("report assessment artifact is invalid")
            evidence_repository = EvidenceRepository(uow.connection)
            evidence_artifacts: list[tuple[object, object]] = []
            for evidence_id in assessment.evidence_ids:
                stored_evidence = evidence_repository.get_with_binding(evidence_id)
                if (
                    stored_evidence is None
                    or stored_evidence.run_id != run_id
                    or stored_evidence.action_id != action_id
                    or stored_evidence.execution_id != execution_id
                    or stored_evidence.artifact_id not in stored_evidence.record.artifact_refs
                ):
                    raise StateIntegrityError("report evidence binding is invalid")
                evidence_artifact = ArtifactRecordRepository(uow.connection).get(
                    stored_evidence.artifact_id
                )
                if (
                    evidence_artifact is None
                    or evidence_artifact.run_id != run_id
                    or evidence_artifact.action_id != action_id
                    or evidence_artifact.execution_id != execution_id
                ):
                    raise StateIntegrityError("report evidence artifact binding is invalid")
                evidence_artifacts.append((stored_evidence, evidence_artifact))
            if claim.status.value != "qualified" or claim.evidence_ids != assessment.evidence_ids:
                raise StateIntegrityError("report claim is not a qualified assessment claim")
            if len(evidence_artifacts) != 1:
                raise StateIntegrityError("P3 report requires one fixed evidence record")
            evidence = evidence_artifacts[0][0].record
            if (
                evidence.payload.get("energy") != claim.value
                or evidence.payload.get("unit") != claim.unit
                or evidence.payload.get("source") != "fake_fixture"
            ):
                raise StateIntegrityError("report claim is not bound to evidence")
            evidence_artifact = evidence_artifacts[0][1]
            report_json_value = {
                "report_schema": "p3-report/v1",
                "data_origin": "fake_fixture",
                "real_scientific_result": False,
                "backend": "fake",
                "fake_marker": "fake_fixture_only",
                "run_id": str(run_id),
                "action_id": str(action_id),
                "execution_id": str(execution_id),
                "action_hash": action_hash,
                "envelope_hash": envelope_hash,
                "budget_hash": budget_hash,
                "fixture": {
                    "id": "water_sp_v1",
                    "version": "1",
                    "source": "fake_fixture",
                },
                "planner_version": "p3.water.fixture.planner.v1",
                "validator_version": "p3.water.fixture.validator.v1",
                "assessment_version": "p3-evidence-v1",
                "renderer_version": "p3-renderer-v1",
                "execution_status": "succeeded",
                "assessment_status": "fixture_verified_only",
                "claim_status": claim.status.value,
                "scientific_result": {
                    "value": claim.value,
                    "unit": claim.unit,
                    "claim_id": str(claim.claim_id),
                    "evidence_id": str(evidence.evidence_id),
                    "artifact_id": str(evidence_artifact.artifact_id),
                    "artifact_hash": evidence_artifact.content_hash,
                },
                "claim": claim.model_dump(mode="json"),
                "assessment": assessment.model_dump(mode="json"),
                "evidence": [evidence.model_dump(mode="json")],
                "evidence_artifact": {
                    "artifact_id": str(evidence_artifact.artifact_id),
                    "content_hash": evidence_artifact.content_hash,
                },
                "assessment_artifact_id": str(assessment_artifact_id),
                "limitations": list(assessment.limitations),
                "integrity": {
                    "traceability_verified": True,
                    "artifact_hashes_verified": True,
                },
            }
            json_bytes = canonical_json_bytes(report_json_value)
            json_artifact = self.artifacts.put(
                connection=uow.connection,
                run_id=run_id,
                action_id=action_id,
                execution_id=execution_id,
                content=json_bytes,
                media_type="application/vnd.orca-agent.p3-report+json",
            )
            markdown = self._markdown(
                run_id,
                action_id,
                execution_id,
                claim,
                assessment,
                evidence_artifact=evidence_artifact,
                json_artifact=json_artifact,
            )
            markdown_bytes = markdown.encode("utf-8")
            markdown_artifact = self.artifacts.put(
                connection=uow.connection,
                run_id=run_id,
                action_id=action_id,
                execution_id=execution_id,
                content=markdown_bytes,
                media_type="text/markdown; charset=utf-8",
            )
            manifest_id = _deterministic_report_id(execution_id)
            manifest_values = {
                "report_manifest_id": manifest_id,
                "schema_version": 2,
                "engine_version": "p3-water-v1",
                "run_id": run_id,
                "action_id": action_id,
                "execution_id": execution_id,
                "claim_id": claim.claim_id,
                "evidence_ids": assessment.evidence_ids,
                "markdown_artifact_id": markdown_artifact.artifact_id,
                "json_artifact_id": json_artifact.artifact_id,
                "markdown_hash": hashlib.sha256(markdown_bytes).hexdigest(),
                "json_hash": hashlib.sha256(json_bytes).hexdigest(),
                "renderer_version": "p3-renderer-v1",
                "fake_marker": "fake_fixture_only",
            }
            manifest = ReportManifestV1(
                **manifest_values,
                manifest_hash=hash_model_fields(
                    ReportManifestV1, manifest_values, exclude="manifest_hash"
                ),
            )
            existing_manifest = records.latest(
                run_id=run_id,
                record_type="report_manifest",
                model_type=ReportManifestV1,
            )
            if existing_manifest is None:
                records.append(
                    run_id=run_id,
                    record_type="report_manifest",
                    record=manifest,
                    created_at_utc=self.clock.now_utc(),
                    source_event_id=effect.source_event_id,
                )
            elif existing_manifest[1] != manifest:
                raise StateIntegrityError("existing report manifest binding is invalid")
            uow.commit()
        return HandlerResult(
            success=True,
            result_summary=EffectSuccessReceiptV1(
                artifact_ids=(markdown_artifact.artifact_id, json_artifact.artifact_id)
            ),
        )

    def verify(self, run_id: RunId) -> dict[str, object]:
        """Verify the durable report and its complete traceability chain."""

        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            if any(item is None for item in (uow.runs, uow.events, uow.interrupts, uow.outbox)):
                raise StateIntegrityError("report kernel repositories are unavailable")
            snapshot = uow.runs.get_verified(
                run_id,
                uow.events,
                interrupts=uow.interrupts,
                outbox=uow.outbox,
            )
            if (
                not isinstance(snapshot.state, P3WorkflowState)
                or snapshot.state.phase is not WorkflowPhase.COMPLETED
            ):
                raise StateIntegrityError("report is not an acknowledged completed result")
            records = P3RecordRepository(uow.connection)
            manifest_entry = records.latest(
                run_id=run_id,
                record_type="report_manifest",
                model_type=ReportManifestV1,
            )
            claim_entry = records.latest_any(
                run_id=run_id,
                record_type="claim",
                model_type=ValidatedClaim,
                schema_version=1,
                engine_version="p1-domain-v1",
            )
            assessment_entry = records.latest(
                run_id=run_id,
                record_type="assessment",
                model_type=FixtureScientificAssessment,
            )
            action = ActionRepository(uow.connection).get_by_run(run_id)
            if (
                manifest_entry is None
                or claim_entry is None
                or assessment_entry is None
                or action is None
            ):
                raise StateIntegrityError("report traceability records are incomplete")
            manifest = manifest_entry[1]
            claim = claim_entry[1]
            assessment = assessment_entry[1]
            fixture = load_water_fixture()
            if (
                snapshot.state.report_manifest_id != manifest.report_manifest_id
                or manifest.run_id != run_id
                or manifest.action_id != action.action.action_id
                or manifest.execution_id != snapshot.state.execution_id
                or manifest.claim_id != claim.claim_id
                or manifest.evidence_ids != assessment.evidence_ids
                or assessment.claim_id != claim.claim_id
                or assessment.action_id != action.action.action_id
                or assessment.execution_id != manifest.execution_id
                or not assessment.accepted
                or not assessment.fixture_verified_only
                or claim.claim_type.value != "energy"
                or claim.status.value != "qualified"
                or claim.value != fixture.energy
                or claim.unit != fixture.unit
                or claim.limitations
                != ("Synthetic fixture only; this is not a real quantum-chemistry calculation.",)
                or assessment.limitations
                != ("Synthetic fixture only; no real ORCA execution was performed.",)
            ):
                raise StateIntegrityError("report manifest binding is invalid")
            if (
                action.run_id != run_id
                or action.conversation_id != snapshot.state.conversation_id
                or action.action.action_hash != snapshot.state.action_hash
                or sha256_hex(action.action.execution_envelope) != snapshot.state.envelope_hash
                or sha256_hex(action.action.budget) != snapshot.state.budget_hash
            ):
                raise StateIntegrityError("report action identity is invalid")
            artifact_records = ArtifactRecordRepository(uow.connection)
            markdown_record = artifact_records.get(manifest.markdown_artifact_id)
            json_record = artifact_records.get(manifest.json_artifact_id)
            if markdown_record is None or json_record is None:
                raise StateIntegrityError("report artifacts are missing")
            _assert_report_artifact_owner(
                markdown_record, run_id, action.action.action_id, manifest.execution_id
            )
            _assert_report_artifact_owner(
                json_record, run_id, action.action.action_id, manifest.execution_id
            )
            markdown_bytes = self.artifacts.read(markdown_record)
            json_bytes = self.artifacts.read(json_record)
            if (
                hashlib.sha256(markdown_bytes).hexdigest() != manifest.markdown_hash
                or hashlib.sha256(json_bytes).hexdigest() != manifest.json_hash
            ):
                raise StateIntegrityError("report artifact hash does not match manifest")
            stored_report_evidence = EvidenceRepository(uow.connection).get_with_binding(
                assessment.evidence_ids[0]
            )
            if stored_report_evidence is None:
                raise StateIntegrityError("report evidence is missing")
            report_evidence_artifact = artifact_records.get(stored_report_evidence.artifact_id)
            if report_evidence_artifact is None:
                raise StateIntegrityError("report evidence artifact is missing")
            accepted_artifacts = set(snapshot.state.accepted_artifact_ids)
            report_input_artifacts = accepted_artifacts - {
                manifest.markdown_artifact_id,
                manifest.json_artifact_id,
                stored_report_evidence.artifact_id,
            }
            if len(report_input_artifacts) != 1:
                raise StateIntegrityError("report accepted artifact set is invalid")
            assessment_artifact_id = next(iter(report_input_artifacts))
            assessment_artifact = artifact_records.get(assessment_artifact_id)
            if assessment_artifact is None:
                raise StateIntegrityError("report assessment artifact is missing")
            _assert_report_artifact_owner(
                assessment_artifact, run_id, action.action.action_id, manifest.execution_id
            )
            if self.artifacts.read(assessment_artifact) != canonical_json_bytes(assessment):
                raise StateIntegrityError("report assessment artifact is invalid")
            _assert_report_artifact_owner(
                report_evidence_artifact,
                run_id,
                action.action.action_id,
                manifest.execution_id,
            )
            raw_result = self.artifacts.read(report_evidence_artifact)
            _verify_report_raw_result(
                raw_result,
                action_id=action.action.action_id,
                action_hash=action.action.action_hash,
                execution_id=manifest.execution_id,
            )
            job = JobRepository(uow.connection).get_by_execution(manifest.execution_id)
            if (
                job is None
                or job.run_id != run_id
                or job.action_id != action.action.action_id
                or job.status != "succeeded"
                or job.raw_result_artifact_id != report_evidence_artifact.artifact_id
                or job.fixture_id != "water_sp_v1"
                or job.fixture_version != "1"
                or job.fixture_hash != fixture.fixture_hash
                or job.input_hash
                != sha256_hex(
                    {
                        "action_hash": action.action.action_hash,
                        "execution_id": str(manifest.execution_id),
                        "fixture_hash": fixture.fixture_hash,
                    }
                )
            ):
                raise StateIntegrityError("report job binding is invalid")
            expected_evidence_payload = {
                "execution_id": str(manifest.execution_id),
                "fixture_id": "water_sp_v1",
                "fixture_version": "1",
                "fixture_hash": fixture.fixture_hash,
                "energy": fixture.energy,
                "unit": fixture.unit,
                "source": fixture.source,
            }
            if (
                stored_report_evidence.record.action_id != action.action.action_id
                or stored_report_evidence.record.artifact_refs
                != (report_evidence_artifact.artifact_id,)
                or dict(stored_report_evidence.record.payload) != expected_evidence_payload
            ):
                raise StateIntegrityError("report evidence content is not fixture-bound")
            try:
                report_json = json.loads(json_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise StateIntegrityError("report JSON is invalid") from error
            expected_report_keys = {
                "report_schema",
                "data_origin",
                "real_scientific_result",
                "backend",
                "fake_marker",
                "run_id",
                "action_id",
                "execution_id",
                "action_hash",
                "envelope_hash",
                "budget_hash",
                "fixture",
                "planner_version",
                "validator_version",
                "assessment_version",
                "renderer_version",
                "execution_status",
                "assessment_status",
                "claim_status",
                "scientific_result",
                "claim",
                "assessment",
                "evidence",
                "evidence_artifact",
                "assessment_artifact_id",
                "limitations",
                "integrity",
            }
            expected_scientific_result = {
                "value": claim.value,
                "unit": claim.unit,
                "claim_id": str(claim.claim_id),
                "evidence_id": str(assessment.evidence_ids[0]),
                "artifact_id": str(report_evidence_artifact.artifact_id),
                "artifact_hash": report_evidence_artifact.content_hash,
            }
            if (
                not isinstance(report_json, dict)
                or set(report_json) != expected_report_keys
                or canonical_json_bytes(report_json) != json_bytes
                or report_json.get("report_schema") != "p3-report/v1"
                or report_json.get("fake_marker") != "fake_fixture_only"
                or report_json.get("data_origin") != "fake_fixture"
                or report_json.get("real_scientific_result") is not False
                or report_json.get("backend") != "fake"
                or report_json.get("run_id") != str(run_id)
                or report_json.get("action_id") != str(action.action.action_id)
                or report_json.get("execution_id") != str(manifest.execution_id)
                or report_json.get("action_hash") != action.action.action_hash
                or report_json.get("envelope_hash") != snapshot.state.envelope_hash
                or report_json.get("budget_hash") != snapshot.state.budget_hash
                or report_json.get("fixture")
                != {"id": "water_sp_v1", "version": "1", "source": "fake_fixture"}
                or report_json.get("planner_version") != "p3.water.fixture.planner.v1"
                or report_json.get("validator_version") != "p3.water.fixture.validator.v1"
                or report_json.get("assessment_version") != "p3-evidence-v1"
                or report_json.get("renderer_version") != "p3-renderer-v1"
                or report_json.get("execution_status") != "succeeded"
                or report_json.get("assessment_status") != "fixture_verified_only"
                or report_json.get("claim_status") != claim.status.value
                or report_json.get("scientific_result") != expected_scientific_result
                or report_json.get("claim") != claim.model_dump(mode="json")
                or report_json.get("assessment") != assessment.model_dump(mode="json")
                or report_json.get("evidence")
                != [stored_report_evidence.record.model_dump(mode="json")]
                or report_json.get("evidence_artifact")
                != {
                    "artifact_id": str(report_evidence_artifact.artifact_id),
                    "content_hash": report_evidence_artifact.content_hash,
                }
                or report_json.get("assessment_artifact_id") != str(assessment_artifact_id)
                or report_json.get("limitations") != list(assessment.limitations)
                or report_json.get("integrity")
                != {"traceability_verified": True, "artifact_hashes_verified": True}
            ):
                raise StateIntegrityError("report JSON content is not the acknowledged model")
            evidence_repository = EvidenceRepository(uow.connection)
            for evidence_id in assessment.evidence_ids:
                stored_evidence = evidence_repository.get_with_binding(evidence_id)
                if (
                    stored_evidence is None
                    or stored_evidence.run_id != run_id
                    or stored_evidence.action_id != action.action.action_id
                    or stored_evidence.execution_id != manifest.execution_id
                ):
                    raise StateIntegrityError("report evidence binding is invalid")
                evidence_artifact = artifact_records.get(stored_evidence.artifact_id)
                if evidence_artifact is None:
                    raise StateIntegrityError("report evidence artifact is missing")
                _assert_report_artifact_owner(
                    evidence_artifact, run_id, action.action.action_id, manifest.execution_id
                )
                self.artifacts.read(evidence_artifact)
                if evidence_artifact.artifact_id != report_evidence_artifact.artifact_id:
                    raise StateIntegrityError("report manifest is missing evidence artifact")
            if b"FAKE FIXTURE ONLY" not in markdown_bytes:
                raise StateIntegrityError("report Markdown fake marker is missing")
            uow.commit()
        return {
            "valid": True,
            "run_id": str(run_id),
            "report_manifest_id": str(manifest.report_manifest_id),
            "markdown_artifact_id": str(manifest.markdown_artifact_id),
            "json_artifact_id": str(manifest.json_artifact_id),
        }

    @staticmethod
    def _markdown(
        run_id,
        action_id,
        execution_id,
        claim,
        assessment,
        *,
        evidence_artifact,
        json_artifact,
    ) -> str:
        value = claim.value
        unit = claim.unit or ""
        limitations = "\n".join(f"- {item}" for item in assessment.limitations)
        return (
            "# BG6022 P3 Water report\n\n"
            "> **FAKE FIXTURE ONLY** — no ORCA, LLM, PubChem, RDKit, or "
            "network execution was performed.\n\n"
            f"- Run: `{run_id}`\n"
            f"- Action: `{action_id}`\n"
            f"- Execution: `{execution_id}`\n"
            "- Data origin: `fake_fixture`\n"
            "- Backend: `fake`\n"
            "- Real scientific result: `false`\n"
            "- Execution status: `succeeded`\n"
            "- Assessment status: `fixture_verified_only`\n"
            f"- Claim status: `{claim.status.value}`\n"
            f"- Qualified fixture energy: `{value} {unit}`\n\n"
            f"- Scientific result: `{value} {unit}`\n"
            f"- Claim: `{claim.claim_id}`\n"
            f"- Evidence: `{claim.evidence_ids[0]}`\n"
            f"- Evidence artifact: `{evidence_artifact.artifact_id}` "
            f"(sha256 `{evidence_artifact.content_hash}`)\n"
            f"- JSON report artifact: `{json_artifact.artifact_id}` "
            f"(sha256 `{json_artifact.content_hash}`)\n\n"
            "- Integrity: `traceability_verified`, `artifact_hashes_verified`\n\n"
            "## Limitations\n\n"
            f"{limitations}\n"
        )


__all__ = ["P3ReportRenderer"]


def _verify_report_raw_result(
    content: bytes,
    *,
    action_id: ActionId,
    action_hash: str,
    execution_id: ExecutionId,
) -> None:
    fixture = load_water_fixture()
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateIntegrityError("report raw result is invalid") from error
    expected_input_hash = sha256_hex(
        {
            "action_hash": action_hash,
            "execution_id": str(execution_id),
            "fixture_hash": fixture.fixture_hash,
        }
    )
    expected = {
        "format": "p3-fake-water-result-v1",
        "schema_version": 2,
        "engine_version": "p3-water-v1",
        "execution_id": str(execution_id),
        "action_id": str(action_id),
        "input_hash": expected_input_hash,
        "fixture_id": fixture.fixture_id,
        "fixture_version": fixture.fixture_version,
        "fixture_hash": fixture.fixture_hash,
        "energy": fixture.energy,
        "unit": fixture.unit,
        "source": fixture.source,
    }
    if not isinstance(value, dict) or canonical_json_bytes(value) != content or value != expected:
        raise StateIntegrityError("report raw result binding is invalid")


def _assert_report_artifact_owner(
    artifact: StoredArtifact,
    run_id: RunId,
    action_id: ActionId,
    execution_id: ExecutionId,
) -> None:
    if (
        artifact.run_id != run_id
        or artifact.action_id != action_id
        or artifact.execution_id != execution_id
    ):
        raise StateIntegrityError("report artifact owner binding is invalid")
