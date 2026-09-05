"""Turn one fake Water result into traceable evidence and a qualified claim."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    ActionId,
    ArtifactId,
    AssessmentId,
    ClaimId,
    ConversationId,
    EffectId,
    EvidenceId,
    ExecutionId,
    RunId,
)
from orca_agent.domain.models import (
    ClaimStatus,
    ClaimType,
    EvidenceRecord,
    EvidenceType,
    Provenance,
    ValidatedClaim,
)
from orca_agent.domain.p3 import (
    FixtureScientificAssessment,
    P3WorkflowState,
    ParsedFakeObservation,
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
)
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.effect_receipts import EffectSuccessReceiptV1
from orca_agent.orchestration.effects import EffectClass
from orca_agent.planning.water import load_water_fixture

_P3_NAMESPACE = uuid.UUID("a6ce1dbb-f8cf-4f1e-8f32-c01a2f0ab2e1")


def _deterministic_id(identifier_type, key: str):
    return identifier_type(f"{identifier_type.prefix}_{uuid.uuid5(_P3_NAMESPACE, key).hex}")


class P3EvidencePipeline:
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

    def assess(self, permit: object):
        from orca_agent.infrastructure.worker import HandlerResult

        effect = getattr(permit, "effect", None)
        if effect is None or effect.effect_type != "internal.p3.assess":
            return HandlerResult(success=False)
        if effect.effect_class is not EffectClass.INTERNAL:
            return HandlerResult(success=False)
        payload = dict(effect.payload)
        run_id = RunId(str(effect.run_id))
        effect_id = EffectId(str(effect.effect_id))
        conversation_id = ConversationId(str(payload["conversation_id"]))
        action_id = ActionId(str(payload["action_id"]))
        action_hash = payload.get("action_hash")
        envelope_hash = payload.get("envelope_hash")
        budget_hash = payload.get("budget_hash")
        execution_id = ExecutionId(str(payload["execution_id"]))
        raw_artifact_id = ArtifactId(str(payload["raw_result_artifact_id"]))
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.begin()
            if any(item is None for item in (uow.runs, uow.events, uow.interrupts, uow.outbox)):
                raise StateIntegrityError("assessment kernel repositories are unavailable")
            snapshot = uow.runs.get_verified(
                run_id,
                uow.events,
                interrupts=uow.interrupts,
                outbox=uow.outbox,
            )
            if not isinstance(snapshot.state, P3WorkflowState):
                raise StateIntegrityError("assessment requires a P3 workflow")
            if (
                snapshot.state.phase is not WorkflowPhase.ASSESSMENT_PENDING
                or snapshot.state.assessment_effect_id != effect_id
                or snapshot.state.execution_id != execution_id
                or snapshot.state.conversation_id != conversation_id
                or snapshot.state.action_id != action_id
                or snapshot.state.action_hash != action_hash
                or snapshot.state.envelope_hash != envelope_hash
                or snapshot.state.budget_hash != budget_hash
            ):
                raise StateIntegrityError("assessment effect binding is invalid")
            actions = ActionRepository(uow.connection)
            action = actions.get(action_id)
            if (
                action is None
                or action.run_id != run_id
                or str(action.conversation_id) != str(conversation_id)
                or action.action.action_hash != action_hash
                or sha256_hex(action.action.execution_envelope) != envelope_hash
                or sha256_hex(action.action.budget) != budget_hash
            ):
                raise StateIntegrityError("assessment action binding is invalid")
            job = JobRepository(uow.connection).get_by_execution(execution_id)
            if (
                job is None
                or job.run_id != run_id
                or job.action_id != action_id
                or job.fixture_id != "water_sp_v1"
                or job.fixture_version != "1"
                or job.fixture_hash != load_water_fixture().fixture_hash
                or snapshot.state.job_id != job.job_id
                or job.raw_result_artifact_id != raw_artifact_id
            ):
                raise StateIntegrityError("assessment job binding is invalid")
            artifact = ArtifactRecordRepository(uow.connection).get(raw_artifact_id)
            if (
                artifact is None
                or artifact.run_id != run_id
                or artifact.action_id != action_id
                or artifact.execution_id != execution_id
            ):
                raise StateIntegrityError("assessment artifact binding is invalid")
            raw_bytes = self.artifacts.read(artifact)
            try:
                raw = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise StateIntegrityError("fake result artifact is invalid JSON") from error
            if canonical_json_bytes(raw) != raw_bytes:
                raise StateIntegrityError("fake result artifact is not canonical JSON")
            observation = self._parse_observation(
                raw,
                action_id=action_id,
                execution_id=execution_id,
                action_hash=action.action.action_hash,
            )
            evidence_id = _deterministic_id(EvidenceId, f"evidence:{execution_id}")
            claim_id = _deterministic_id(ClaimId, f"claim:{execution_id}")
            assessment_id = _deterministic_id(AssessmentId, f"assessment:{execution_id}")
            evidence = EvidenceRecord.create(
                evidence_id=evidence_id,
                action_id=action_id,
                evidence_type=EvidenceType.PARSED_ENERGY,
                payload={
                    "execution_id": str(execution_id),
                    "fixture_id": observation.fixture_id,
                    "fixture_version": observation.fixture_version,
                    "fixture_hash": observation.fixture_hash,
                    "energy": observation.energy,
                    "unit": observation.unit,
                    "source": observation.source,
                },
                artifact_refs=(raw_artifact_id,),
                provenance=Provenance(
                    producer="p3.fake.evidence",
                    producer_version="p3-evidence-v1",
                    created_at=job.created_at_utc,
                ),
            )
            evidence_repo = EvidenceRepository(uow.connection)
            existing_evidence = evidence_repo.get_with_binding(evidence_id)
            if existing_evidence is None:
                evidence_repo.insert(
                    run_id=run_id,
                    execution_id=execution_id,
                    artifact_id=raw_artifact_id,
                    record=evidence,
                    now=job.created_at_utc,
                )
            elif (
                existing_evidence.run_id != run_id
                or existing_evidence.action_id != action_id
                or existing_evidence.execution_id != execution_id
                or existing_evidence.artifact_id != raw_artifact_id
                or existing_evidence.record != evidence
            ):
                raise StateIntegrityError("existing evidence binding is invalid")
            claim = ValidatedClaim.create(
                claim_id=claim_id,
                claim_type=ClaimType.ENERGY,
                value=observation.energy,
                unit=observation.unit,
                evidence_ids=(evidence_id,),
                status=ClaimStatus.QUALIFIED,
                limitations=(
                    "Synthetic fixture only; this is not a real quantum-chemistry calculation.",
                ),
            )
            records = P3RecordRepository(uow.connection)
            claim_entry = records.latest_any(
                run_id=run_id,
                record_type="claim",
                model_type=ValidatedClaim,
                schema_version=1,
                engine_version="p1-domain-v1",
            )
            if claim_entry is None:
                records.append_any(
                    run_id=run_id,
                    record_type="claim",
                    record=claim,
                    schema_version=1,
                    engine_version="p1-domain-v1",
                    created_at_utc=job.created_at_utc,
                    source_event_id=effect.source_event_id,
                )
            elif claim_entry[1] != claim:
                raise StateIntegrityError("existing claim binding is invalid")
            assessment_values = {
                "assessment_id": assessment_id,
                "schema_version": 2,
                "engine_version": "p3-water-v1",
                "run_id": run_id,
                "action_id": action_id,
                "execution_id": execution_id,
                "evidence_ids": (evidence_id,),
                "claim_id": claim_id,
                "fixture_verified_only": True,
                "accepted": True,
                "limitations": ("Synthetic fixture only; no real ORCA execution was performed.",),
            }
            assessment = FixtureScientificAssessment(
                **assessment_values,
                assessment_hash=hash_model_fields(
                    FixtureScientificAssessment, assessment_values, exclude="assessment_hash"
                ),
            )
            existing_assessment = records.latest(
                run_id=run_id,
                record_type="assessment",
                model_type=FixtureScientificAssessment,
            )
            if existing_assessment is None:
                records.append(
                    run_id=run_id,
                    record_type="assessment",
                    record=assessment,
                    created_at_utc=job.created_at_utc,
                    source_event_id=effect.source_event_id,
                )
            elif existing_assessment[1] != assessment:
                raise StateIntegrityError("existing assessment binding is invalid")
            assessment_artifact = self.artifacts.put(
                connection=uow.connection,
                run_id=run_id,
                action_id=action_id,
                execution_id=execution_id,
                content=(canonical_json_bytes(assessment)),
                media_type="application/vnd.orca-agent.p3-assessment+json",
            )
            uow.commit()
        return HandlerResult(
            success=True,
            result_summary=EffectSuccessReceiptV1(artifact_ids=(assessment_artifact.artifact_id,)),
        )

    def _parse_observation(
        self,
        raw: object,
        *,
        action_id: ActionId,
        execution_id: ExecutionId,
        action_hash: str,
    ) -> ParsedFakeObservation:
        if not isinstance(raw, dict) or raw.get("format") != "p3-fake-water-result-v1":
            raise StateIntegrityError("fake result format is invalid")
        fixture = load_water_fixture()
        expected_input_hash = sha256_hex(
            {
                "action_hash": action_hash,
                "execution_id": str(execution_id),
                "fixture_hash": fixture.fixture_hash,
            }
        )
        expected_keys = {
            "format",
            "schema_version",
            "engine_version",
            "execution_id",
            "action_id",
            "input_hash",
            "fixture_id",
            "fixture_version",
            "fixture_hash",
            "energy",
            "unit",
            "source",
        }
        if set(raw) != expected_keys:
            raise StateIntegrityError("fake result fields are invalid")
        if raw.get("input_hash") != expected_input_hash:
            # The action hash is checked by the exact value below after the
            # caller has loaded the action; a malformed input hash still fails.
            raise StateIntegrityError("fake result input hash is invalid")
        selected = {
            "schema_version": raw.get("schema_version"),
            "engine_version": raw.get("engine_version"),
            "action_id": raw.get("action_id"),
            "execution_id": raw.get("execution_id"),
            "fixture_id": raw.get("fixture_id"),
            "fixture_version": raw.get("fixture_version"),
            "fixture_hash": raw.get("fixture_hash"),
            "energy": raw.get("energy"),
            "unit": raw.get("unit"),
            "source": raw.get("source"),
        }
        try:
            observation = ParsedFakeObservation.model_validate(selected, strict=True)
        except Exception as error:
            raise StateIntegrityError("fake result observation is invalid") from error
        if (
            observation.action_id != action_id
            or observation.execution_id != execution_id
            or observation.fixture_hash != fixture.fixture_hash
            or observation.energy != fixture.energy
            or observation.unit != fixture.unit
            or observation.source != fixture.source
        ):
            raise StateIntegrityError("fake result observation binding is invalid")
        return observation


__all__ = ["P3EvidencePipeline"]
