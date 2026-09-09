"""Application service for the offline, source-derived P6 workflow."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from orca_agent.application.effect_completion import EffectCompletionReport
from orca_agent.application.errors import (
    ApplicationError,
    DuplicateCommandConflictError,
    InvalidTransitionError,
    RevisionConflictError,
    StateIntegrityError,
)
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import GENESIS_EVENT_HASH, sha256_hex
from orca_agent.domain.ids import (
    AssessmentId,
    CommandId,
    ConversationId,
    EventId,
    RunId,
    WorkflowRecordId,
    completion_command_id,
    new_id,
)
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p5 import P5NodeKind, P5ParseStatus
from orca_agent.domain.p6 import (
    ComparabilityAssessment,
    MethodContext,
    P6ClaimRecord,
    P6EvidenceRecord,
    P6ModeKind,
    P6Phase,
    P6ReportManifest,
    P6SourceSnapshot,
    P6WorkflowState,
    ScientificAssessment,
    ScientificPolicy,
)
from orca_agent.evidence.p6_ingestion import P6Ingestion, P6ResultBundle, load_source_bundle
from orca_agent.identity.geometry import parse_xyz_bytes
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.clock import Clock, SystemClock
from orca_agent.infrastructure.outbox import DispatchPermit, OutboxStatus
from orca_agent.infrastructure.p6_records import P6RecordRepository
from orca_agent.infrastructure.repositories import RunSnapshot
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.infrastructure.worker import HandlerResult, OutboxWorker
from orca_agent.orchestration.codes import HandlerErrorCode, handler_error_message
from orca_agent.orchestration.commands import CommandType
from orca_agent.orchestration.dispatch_policy import (
    P6_EFFECT_REGISTRY,
    DispatchDecision,
    evaluate_dispatch,
)
from orca_agent.orchestration.effect_receipts import (
    EffectSuccessReceiptV1,
    parse_effect_success_receipt,
    receipt_json,
)
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.events import EventType
from orca_agent.orchestration.p6_commands import AssessP6Run, CancelP6Run
from orca_agent.orchestration.p6_kernel import (
    P6KernelEvent,
    P6Transition,
    expected_p6_application_result,
    reduce_p6_event,
)
from orca_agent.orchestration.p6_versions import P6_ENGINE_VERSION, P6_SCHEMA_VERSION
from orca_agent.orchestration.replay import state_hash
from orca_agent.orchestration.state import RunStatus
from orca_agent.reporting.p6_renderer import P6ReportRenderer
from orca_agent.science.claims import (
    build_difference_claim,
    build_energy_claim,
    build_frequency_claim,
    build_minimum_claim,
    validate_claim,
)
from orca_agent.science.comparability import compare_electronic_energy
from orca_agent.science.evaluator import evaluate_minimum
from orca_agent.science.modes import ModeAnalysis, classify_modes
from orca_agent.science.policy import get_policy


@dataclass(frozen=True)
class P6RunView:
    """Read-only inspection projection for the P6 CLI."""

    run_id: RunId
    conversation_id: ConversationId
    revision: int
    state: P6WorkflowState
    source_snapshot: P6SourceSnapshot
    policy: ScientificPolicy
    assessment: ScientificAssessment | None
    evidence: tuple[P6EvidenceRecord, ...]
    claims: tuple[P6ClaimRecord, ...]
    comparisons: tuple[ComparabilityAssessment, ...]
    report_manifest: P6ReportManifest | None
    coverage: dict[str, object]
    diagnostics: tuple[str, ...] = ()

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        def dump(value: object) -> object:
            if value is None:
                return None
            model_dump = getattr(value, "model_dump", None)
            return model_dump(mode=mode) if callable(model_dump) else value

        return {
            "run_id": str(self.run_id),
            "conversation_id": str(self.conversation_id),
            "revision": self.revision,
            "state": dump(self.state),
            "source_snapshot": dump(self.source_snapshot),
            "policy": dump(self.policy),
            "assessment": dump(self.assessment),
            "evidence": [dump(item) for item in self.evidence],
            "claims": [dump(item) for item in self.claims],
            "comparisons": [dump(item) for item in self.comparisons],
            "report_manifest": dump(self.report_manifest),
            "coverage": self.coverage,
            "diagnostics": list(self.diagnostics),
        }


class P6ApplicationService:
    """Freeze a verified P5 source, then run only offline P6 effects."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        clock: Clock | None = None,
        max_attempts: int = 5,
    ) -> None:
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.state_root = Path(state_root).resolve()
        self.database_path = resolve_database_path(self.state_root)
        self.clock = clock or SystemClock()
        self.max_attempts = max_attempts

    def assess(self, command: AssessP6Run) -> ApplicationResult:
        """Create a derived run and freeze its P5 source closure."""

        if not isinstance(command, AssessP6Run):
            raise TypeError("P6 assess requires AssessP6Run")
        command_hash = command.command_hash()
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                self._require_kernel(uow)
                uow.begin()
                replayed = self._replayed_command(uow, command.command_id, command_hash)
                if replayed is not None:
                    uow.commit()
                    return replayed
                if uow.runs.get(command.run_id) is not None:
                    raise DuplicateCommandConflictError("P6 run already exists")

                ingestion = load_source_bundle(
                    connection=uow.connection,
                    state_root=self.state_root,
                    source_run_id=command.source_p5_run_id,
                    expected_revision=command.expected_source_revision,
                )
                policy = get_policy(command.profile_id)
                event_id = new_id(EventId)
                effect = EffectSpec(
                    effect_index=0,
                    effect_type="internal.p6.assess",
                    effect_class=EffectClass.INTERNAL,
                    payload={
                        "run_id": str(command.run_id),
                        "source_p5_run_id": str(command.source_p5_run_id),
                        "source_snapshot_id": str(ingestion.snapshot.snapshot_id),
                        "source_snapshot_hash": ingestion.snapshot.snapshot_hash,
                        "policy_id": policy.policy_id,
                        "policy_hash": policy.policy_hash,
                    },
                )
                state = P6WorkflowState(
                    run_id=command.run_id,
                    status=RunStatus.READY,
                    phase=P6Phase.ASSESSMENT_PENDING,
                    conversation_id=self._source_conversation(uow, command.source_p5_run_id),
                    source_p5_run_id=command.source_p5_run_id,
                    source_snapshot_id=ingestion.snapshot.snapshot_id,
                    source_snapshot_hash=ingestion.snapshot.snapshot_hash,
                    source_revision=ingestion.snapshot.source_revision,
                    source_event_head_id=ingestion.snapshot.source_event_head_id,
                    policy_id=policy.policy_id,
                    policy_hash=policy.policy_hash,
                    profile_id=command.profile_id,
                    assessment_effect_id=effect.effect_id(event_id),
                    reference_assessment_id=command.reference_assessment_id,
                    last_outcome_code="run_created",
                )
                event, transition, result = _build_p6_event(
                    events=uow.events,
                    snapshot=None,
                    state=state,
                    event_id=event_id,
                    command_id=command.command_id,
                    command_hash=command_hash,
                    command_type=CommandType.P6_ASSESS,
                    event_type=EventType.RUN_CREATED,
                    outcome_code="run_created",
                    effects=(effect,),
                    occurred_at_utc=command.requested_at_utc,
                )
                if transition.next_state != state:
                    raise StateIntegrityError("P6 creation reducer changed the requested state")
                uow.runs.insert(
                    RunSnapshot(
                        run_id=command.run_id,
                        schema_version=P6_SCHEMA_VERSION,
                        engine_version=P6_ENGINE_VERSION,
                        revision=1,
                        state=state,
                        state_hash=state_hash(state),
                        last_event_id=event.event_id,
                        created_at_utc=command.requested_at_utc,
                        updated_at_utc=command.requested_at_utc,
                    )
                )
                uow.events.append(event, command_hash=command_hash)
                uow.outbox.register_effects(
                    event=event,
                    run_id=command.run_id,
                    effects=transition.effects,
                    available_at_utc=command.requested_at_utc,
                    created_at_utc=command.requested_at_utc,
                )
                artifact_store = ArtifactStore(self.state_root, clock=self.clock)
                artifact_store.put_owned(
                    connection=uow.connection,
                    run_id=command.run_id,
                    scope_id=str(ingestion.snapshot.snapshot_id),
                    role="p6_source_snapshot",
                    content=canonical_json_bytes(ingestion.snapshot.model_dump(mode="json")),
                    media_type="application/vnd.orca-agent.p6-source-snapshot+json",
                )
                artifact_store.put_owned(
                    connection=uow.connection,
                    run_id=command.run_id,
                    scope_id=str(policy.policy_id),
                    role="p6_policy",
                    content=canonical_json_bytes(policy.model_dump(mode="json")),
                    media_type="application/vnd.orca-agent.p6-policy+json",
                )
                records = P6RecordRepository(uow.connection)
                records.append_p6(
                    run_id=command.run_id,
                    record_type="p6.source_snapshot",
                    record=ingestion.snapshot,
                    created_at_utc=command.requested_at_utc,
                    source_event_id=event.event_id,
                    record_id=ingestion.snapshot.record_id,
                )
                records.append_p6(
                    run_id=command.run_id,
                    record_type="p6.policy",
                    record=policy,
                    created_at_utc=command.requested_at_utc,
                    source_event_id=event.event_id,
                    record_id=policy.record_id,
                )
                uow.command_receipts.append_event(
                    event=event, recorded_at_utc=command.requested_at_utc
                )
                uow.commit()
                return result
        except Exception as error:
            return self._rejected(command.run_id, error)

    def cancel(self, command: CancelP6Run) -> ApplicationResult:
        if not isinstance(command, CancelP6Run):
            raise TypeError("P6 cancel requires CancelP6Run")
        command_hash = command.command_hash()
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                self._require_kernel(uow)
                uow.begin()
                replayed = self._replayed_command(uow, command.command_id, command_hash)
                if replayed is not None:
                    uow.commit()
                    return replayed
                snapshot = uow.runs.get_verified(command.run_id, uow.events, outbox=uow.outbox)
                if not isinstance(snapshot.state, P6WorkflowState):
                    raise StateIntegrityError("run is not a P6 workflow")
                if snapshot.revision != command.expected_revision:
                    raise RevisionConflictError("P6 cancellation revision is stale")
                if snapshot.state.conversation_id != command.conversation_id:
                    raise InvalidTransitionError(
                        "P6 cancellation conversation does not own the run"
                    )
                if snapshot.state.status.is_terminal or snapshot.state.phase is P6Phase.COMPLETED:
                    raise InvalidTransitionError("P6 run is already terminal")
                next_state = snapshot.state.model_copy(
                    update={
                        "status": RunStatus.CANCELLED,
                        "phase": P6Phase.CANCELLED,
                        "last_outcome_code": command.reason_code,
                    }
                )
                event, transition, result = _build_p6_event(
                    events=uow.events,
                    snapshot=snapshot,
                    state=next_state,
                    event_id=new_id(EventId),
                    command_id=command.command_id,
                    command_hash=command_hash,
                    command_type=CommandType.P6_CANCEL,
                    event_type=EventType.RUN_CANCELLED,
                    outcome_code=command.reason_code,
                    effects=(),
                    occurred_at_utc=command.requested_at_utc,
                )
                if transition.next_state != next_state:
                    raise StateIntegrityError("P6 cancellation reducer changed the requested state")
                uow.events.append(event, command_hash=command_hash)
                uow.outbox.cancel_p6_internal_for_run(
                    run_id=command.run_id, now=command.requested_at_utc
                )
                if not uow.runs.compare_and_swap(
                    run_id=command.run_id,
                    expected_revision=snapshot.revision,
                    state=next_state,
                    event_id=event.event_id,
                    updated_at_utc=command.requested_at_utc,
                ):
                    raise RevisionConflictError("P6 cancellation revision changed")
                uow.command_receipts.append_event(
                    event=event, recorded_at_utc=command.requested_at_utc
                )
                uow.commit()
                return result
        except Exception as error:
            return self._rejected(command.run_id, error)

    def inspect(self, run_id: RunId) -> P6RunView:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            self._require_kernel(uow)
            uow.begin()
            snapshot = uow.runs.get_verified(run_id, uow.events, outbox=uow.outbox)
            if not isinstance(snapshot.state, P6WorkflowState):
                raise StateIntegrityError("run is not a P6 workflow")
            records = P6RecordRepository(uow.connection)
            source = _source_snapshot_record(records, run_id, snapshot.state.source_snapshot_id)
            policy_entry = records.latest_p6(
                run_id=run_id,
                record_type="p6.policy",
                model_type=ScientificPolicy,
            )
            if source is None or policy_entry is None:
                raise StateIntegrityError("P6 source snapshot or policy is missing")
            entries = records.list_p6_for_run(run_id)
            assessment = next(
                (
                    item
                    for _id, kind, item in reversed(entries)
                    if kind == "p6.assessment" and isinstance(item, ScientificAssessment)
                ),
                None,
            )
            evidence = tuple(
                item
                for _id, kind, item in entries
                if kind == "p6.evidence" and isinstance(item, P6EvidenceRecord)
            )
            claims = tuple(
                item
                for _id, kind, item in entries
                if kind == "p6.claim" and isinstance(item, P6ClaimRecord)
            )
            comparisons = tuple(
                item
                for _id, kind, item in entries
                if kind == "p6.comparability" and isinstance(item, ComparabilityAssessment)
            )
            manifest = next(
                (
                    item
                    for _id, kind, item in reversed(entries)
                    if kind == "p6.report_manifest" and isinstance(item, P6ReportManifest)
                ),
                None,
            )
            coverage = {
                "source_results": len(source.results),
                "complete_results": sum(
                    item.parse_status == P5ParseStatus.COMPLETE.value for item in source.results
                ),
                "requested_primitives": list(
                    dict.fromkeys(item.primitive for item in source.results)
                ),
                "unrequested_primitives": [
                    item
                    for item in ("opt", "freq", "sp")
                    if item not in {result.primitive for result in source.results}
                ],
                "evidence": len(evidence),
                "claims": len(claims),
                "minimum_status": None if assessment is None else assessment.minimum_status.value,
                "minimum_reason": None if assessment is None else assessment.minimum_reason,
                "report_manifest_id": None
                if manifest is None
                else str(manifest.report_manifest_id),
            }
            diagnostics = ()
            if assessment is None:
                diagnostics = ("scientific_assessment=not_evaluated", "claim_status=not_generated")
            elif not claims:
                diagnostics = ("claim_status=not_generated",)
            uow.commit()
            return P6RunView(
                run_id=run_id,
                conversation_id=snapshot.state.conversation_id,
                revision=snapshot.revision,
                state=snapshot.state,
                source_snapshot=source,
                policy=policy_entry[1],
                assessment=assessment,
                evidence=evidence,
                claims=claims,
                comparisons=comparisons,
                report_manifest=manifest,
                coverage=coverage,
                diagnostics=diagnostics,
            )

    def create_worker(
        self,
        *,
        worker_id=None,
        lease_duration: timedelta = timedelta(seconds=30),
    ) -> OutboxWorker:
        prepared = {}
        renderer = P6ReportRenderer(self.database_path, self.state_root, clock=self.clock)

        def handler(permit: DispatchPermit) -> HandlerResult:
            if permit.effect.effect_type == "internal.p6.assess":
                prepared[(permit.effect.effect_id, permit.generation)] = self._prepare_assessment(
                    permit
                )
                return HandlerResult(success=True, result_summary=EffectSuccessReceiptV1())
            if permit.effect.effect_type == "internal.p6.render_report":
                prepared[(permit.effect.effect_id, permit.generation)] = renderer.prepare(permit)
                return HandlerResult(success=True, result_summary=EffectSuccessReceiptV1())
            return HandlerResult(success=False, error_code=HandlerErrorCode.HANDLER_FAILED)

        return OutboxWorker(
            self.database_path,
            handler,
            clock=self.clock,
            worker_id=worker_id,
            lease_duration=lease_duration,
            max_attempts=self.max_attempts,
            registry=P6_EFFECT_REGISTRY,
            completion_service_factory=lambda: P6EffectCompletion(
                self, max_attempts=self.max_attempts, prepared=prepared
            ),
        )

    def _prepare_assessment(self, permit: DispatchPermit):
        """Prepare immutable scientific records without publishing any business rows."""
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            uow.connection.execute("BEGIN")
            uow.outbox.validate_handler_permit(permit=permit, now=self.clock.now_utc())
            self._require_kernel(uow)
            snapshot = uow.runs.get_verified(permit.effect.run_id, uow.events, outbox=uow.outbox)
            if not isinstance(snapshot.state, P6WorkflowState):
                raise StateIntegrityError("P6 assessment effect belongs to a non-P6 run")
            if snapshot.state.phase is not P6Phase.ASSESSMENT_PENDING:
                raise InvalidTransitionError("P6 assessment effect is not in the active phase")
            records = P6RecordRepository(uow.connection)
            existing = records.latest_p6(
                run_id=permit.effect.run_id,
                record_type="p6.assessment",
                model_type=ScientificAssessment,
            )
            if existing is not None:
                raise StateIntegrityError("unacknowledged P6 assessment already exists")
            source = _source_snapshot_record(
                records, permit.effect.run_id, snapshot.state.source_snapshot_id
            )
            policy_entry = records.latest_p6(
                run_id=permit.effect.run_id,
                record_type="p6.policy",
                model_type=ScientificPolicy,
            )
            if source is None or policy_entry is None:
                raise StateIntegrityError("P6 assessment source or policy is missing")
            if source.snapshot_hash != snapshot.state.source_snapshot_hash:
                raise StateIntegrityError("P6 assessment source snapshot binding is invalid")
            ingestion = load_source_bundle(
                connection=uow.connection,
                state_root=self.state_root,
                source_run_id=source.source_p5_run_id,
                expected_snapshot=source,
            )
            if ingestion.snapshot != source:
                raise StateIntegrityError("P6 source snapshot changed before assessment")
            policy = policy_entry[1]
            if (
                policy.policy_id != snapshot.state.policy_id
                or policy.policy_hash != snapshot.state.policy_hash
            ):
                raise StateIntegrityError("P6 assessment policy binding is invalid")
            assessment, mode_analysis = _make_assessment(ingestion, policy, snapshot.state)
            evidence = ingestion.evidence
            evidence_by_id = {item.evidence_id: item for item in evidence}
            claims = _make_claims(assessment, policy, ingestion, evidence, mode_analysis)
            comparison, external_evidence, reference_assessment = self._make_comparison(
                uow.connection,
                state=snapshot.state,
                assessment=assessment,
                ingestion=ingestion,
                evidence=evidence,
            )
            if snapshot.state.reference_assessment_id is None:
                if comparison is not None or reference_assessment is not None:
                    raise StateIntegrityError("comparison exists without a selected reference")
            elif comparison is None or reference_assessment is None:
                raise StateIntegrityError("selected reference has no comparison")
            if comparison is not None and comparison.status.value == "compatible":
                candidate_energy = _selected_energy_evidence(evidence, ingestion.snapshot)
                reference_energy = external_evidence.get(comparison.reference_energy_evidence_id)
                if candidate_energy is not None and reference_energy is not None:
                    difference = build_difference_claim(
                        assessment=assessment,
                        policy=policy,
                        comparison=comparison,
                        subject_result_ids=(
                            candidate_energy.source_result_id,
                            reference_energy.source_result_id,
                        ),
                        evidence_hashes=(
                            candidate_energy.evidence_hash,
                            reference_energy.evidence_hash,
                        ),
                        candidate_evidence=candidate_energy,
                        reference_evidence=reference_energy,
                    )
                    validate_claim(
                        difference,
                        evidence=evidence_by_id,
                        external_evidence=external_evidence,
                        assessment=assessment,
                        policy=policy,
                        comparison=comparison,
                        reference_assessment=reference_assessment,
                        reference_assessment_id=snapshot.state.reference_assessment_id,
                    )
                    claims += (difference,)
            for claim in claims:
                validate_claim(
                    claim,
                    evidence=evidence_by_id,
                    external_evidence=external_evidence,
                    assessment=assessment,
                    policy=policy,
                    comparison=comparison,
                    reference_assessment=reference_assessment,
                    reference_assessment_id=snapshot.state.reference_assessment_id,
                )

            return (
                ingestion,
                policy,
                assessment,
                evidence,
                claims,
                comparison,
                reference_assessment,
                external_evidence,
            )

    def _assess_effect(self, permit: DispatchPermit, *, transaction, prepared) -> HandlerResult:
        (
            ingestion,
            policy,
            assessment,
            evidence,
            claims,
            comparison,
            reference_assessment,
            external_evidence,
        ) = prepared
        with nullcontext(transaction) as uow:
            snapshot = uow.runs.get_verified(permit.effect.run_id, uow.events, outbox=uow.outbox)
            if not isinstance(snapshot.state, P6WorkflowState):
                raise StateIntegrityError("P6 assessment publication has a non-P6 state")
            records = P6RecordRepository(uow.connection)
            current_source = load_source_bundle(
                connection=uow.connection,
                state_root=self.state_root,
                source_run_id=ingestion.snapshot.source_p5_run_id,
                expected_snapshot=ingestion.snapshot,
            )
            if current_source.snapshot != ingestion.snapshot:
                raise StateIntegrityError("prepared assessment source changed")
            excluded = {"record_id", "evidence_id", "evidence_hash"}
            if [item.model_dump(mode="json", exclude=excluded) for item in evidence] != [
                item.model_dump(mode="json", exclude=excluded) for item in current_source.evidence
            ]:
                raise StateIntegrityError("prepared evidence differs from original observations")
            for claim in claims:
                validate_claim(
                    claim,
                    evidence={item.evidence_id: item for item in evidence},
                    external_evidence=external_evidence,
                    assessment=assessment,
                    policy=policy,
                    comparison=comparison,
                    reference_assessment=reference_assessment,
                    reference_assessment_id=snapshot.state.reference_assessment_id,
                )
            store = ArtifactStore(self.state_root, clock=self.clock)
            evidence_bytes = canonical_json_bytes(
                [item.model_dump(mode="json") for item in evidence]
            )
            store.put_owned(
                connection=uow.connection,
                run_id=permit.effect.run_id,
                scope_id=str(permit.effect.run_id),
                role="p6_evidence",
                content=evidence_bytes,
                media_type="application/vnd.orca-agent.p6-evidence+json",
            )
            assessment_bytes = canonical_json_bytes(assessment.model_dump(mode="json"))
            store.put_owned(
                connection=uow.connection,
                run_id=permit.effect.run_id,
                scope_id=str(assessment.assessment_id),
                role="p6_assessment",
                content=assessment_bytes,
                media_type="application/vnd.orca-agent.p6-assessment+json",
            )
            claim_bytes = canonical_json_bytes([item.model_dump(mode="json") for item in claims])
            store.put_owned(
                connection=uow.connection,
                run_id=permit.effect.run_id,
                scope_id=str(assessment.assessment_id),
                role="p6_claim",
                content=claim_bytes,
                media_type="application/vnd.orca-agent.p6-claims+json",
            )
            created_at = self.clock.now_utc()
            for item in evidence:
                records.append_p6(
                    run_id=permit.effect.run_id,
                    record_type="p6.evidence",
                    record=item,
                    created_at_utc=created_at,
                    source_event_id=permit.effect.source_event_id,
                    record_id=item.record_id,
                )
            records.append_p6(
                run_id=permit.effect.run_id,
                record_type="p6.assessment",
                record=assessment,
                created_at_utc=created_at,
                source_event_id=permit.effect.source_event_id,
                record_id=assessment.record_id,
            )
            if comparison is not None:
                records.append_p6(
                    run_id=permit.effect.run_id,
                    record_type="p6.comparability",
                    record=comparison,
                    created_at_utc=created_at,
                    source_event_id=permit.effect.source_event_id,
                    record_id=comparison.record_id,
                )
            for item in claims:
                records.append_p6(
                    run_id=permit.effect.run_id,
                    record_type="p6.claim",
                    record=item,
                    created_at_utc=created_at,
                    source_event_id=permit.effect.source_event_id,
                    record_id=item.record_id,
                )
            return HandlerResult(success=True, result_summary=EffectSuccessReceiptV1())

    def _make_comparison(
        self,
        connection,
        *,
        state: P6WorkflowState,
        assessment: ScientificAssessment,
        ingestion: P6Ingestion,
        evidence: tuple[P6EvidenceRecord, ...],
    ) -> tuple[
        ComparabilityAssessment | None,
        dict[object, P6EvidenceRecord],
        ScientificAssessment | None,
    ]:
        if state.reference_assessment_id is None:
            return None, {}, None
        reference = self._reference_inputs(connection, state.reference_assessment_id)
        if reference is None:
            raise StateIntegrityError("explicit reference assessment was not found")
        candidate_energy = _selected_energy_evidence(evidence, ingestion.snapshot)
        if candidate_energy is None:
            raise StateIntegrityError("candidate has no electronic energy for comparison")
        _run_id, reference_assessment, reference_energy, reference_method, reference_evidence = (
            reference
        )
        comparison = compare_electronic_energy(
            candidate_assessment=assessment,
            candidate_context=ingestion.method_context,
            candidate_evidence=candidate_energy,
            reference_assessment=reference_assessment,
            reference_context=reference_method,
            reference_evidence=reference_energy,
        )
        return (
            comparison,
            {item.evidence_id: item for item in reference_evidence},
            reference_assessment,
        )

    @staticmethod
    def _reference_inputs(connection, assessment_id: AssessmentId):
        records = P6RecordRepository(connection)
        found = records.find_assessment(assessment_id)
        if found is None:
            return None
        run_id, assessment = found
        entries = records.list_p6_for_run(run_id)
        source = next(
            (
                item
                for _id, kind, item in entries
                if kind == "p6.source_snapshot"
                and isinstance(item, P6SourceSnapshot)
                and item.snapshot_hash == assessment.source_snapshot_hash
            ),
            None,
        )
        if source is None:
            raise StateIntegrityError("reference assessment source snapshot is missing")
        evidence = tuple(
            item
            for _id, kind, item in entries
            if kind == "p6.evidence" and isinstance(item, P6EvidenceRecord)
        )
        energy = _selected_energy_evidence(evidence, source)
        method = next(
            (item.context for item in evidence if isinstance(item.context, MethodContext)),
            None,
        )
        if energy is None or method is None:
            raise StateIntegrityError("reference assessment lacks comparable energy context")
        return run_id, assessment, energy, method, evidence

    @staticmethod
    def _source_conversation(uow, source_run_id: RunId) -> ConversationId:
        source = uow.runs.get_verified(source_run_id, uow.events, outbox=uow.outbox)
        conversation = getattr(source.state, "conversation_id", None)
        if not isinstance(conversation, ConversationId):
            raise StateIntegrityError("P5 source conversation is missing")
        return conversation

    @staticmethod
    def _require_kernel(uow) -> None:
        if (
            uow.runs is None
            or uow.events is None
            or uow.outbox is None
            or uow.command_receipts is None
        ):
            raise StateIntegrityError("P6 kernel repositories are unavailable")

    @staticmethod
    def _replayed_command(
        uow, command_id: CommandId, command_hash: str
    ) -> ApplicationResult | None:
        stored = uow.events.get_by_command_id(command_id)
        if stored is None:
            return None
        if not isinstance(stored.event, P6KernelEvent):
            raise DuplicateCommandConflictError("command ID belongs to another workflow")
        if stored.command_hash != command_hash:
            raise DuplicateCommandConflictError("same command ID has a different payload")
        return ApplicationResult.model_validate_json(
            json.dumps(thaw_json(stored.event.result), ensure_ascii=False), strict=True
        )

    def _verified_snapshot(self, run_id: RunId) -> RunSnapshot:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            self._require_kernel(uow)
            uow.begin()
            snapshot = uow.runs.get_verified(run_id, uow.events, outbox=uow.outbox)
            uow.commit()
            return snapshot

    def _rejected(self, run_id: RunId, error: Exception) -> ApplicationResult:
        try:
            snapshot = self._verified_snapshot(run_id)
            status = RunStatus(snapshot.state.status.value)
            revision = snapshot.revision
        except Exception:
            status = RunStatus.CREATED
            revision = 0
        if isinstance(error, ApplicationError):
            code = error.code
            details = dict(error.details)
        else:
            code = getattr(error, "code", "p6_error")
            details = {"message": str(error)[:256]}
        return ApplicationResult.rejected_result(
            code=code,
            run_id=run_id,
            revision=revision,
            status=status,
            details=details,
        )


class P6EffectCompletion:
    """Complete P6 effects through the shared fenced outbox protocol."""

    def __init__(self, service: P6ApplicationService, *, max_attempts: int, prepared=None) -> None:
        self.service = service
        self.max_attempts = max_attempts
        self.prepared = {} if prepared is None else prepared

    def complete(self, permit: DispatchPermit, result: HandlerResult) -> EffectCompletionReport:
        prepared_value = self.prepared.pop((permit.effect.effect_id, permit.generation), None)
        normalized = _normalize_handler_result(result)
        success, receipt, error_code = normalized
        terminal = success or permit.generation >= self.max_attempts
        outcome = "succeeded" if success else ("dead_letter" if terminal else "retry")
        now = self.service.clock.now_utc()
        with SQLiteUnitOfWork(self.service.database_path, clock=self.service.clock) as uow:
            self.service._require_kernel(uow)
            uow.begin()
            snapshot = uow.runs.get_verified(permit.effect.run_id, uow.events, outbox=uow.outbox)
            if not isinstance(snapshot.state, P6WorkflowState):
                raise StateIntegrityError("P6 effect has a non-P6 snapshot")
            current = uow.outbox.get(permit.effect.effect_id)
            if current is None:
                raise StateIntegrityError("P6 effect disappeared")

            # The handler never publishes records. Revalidate and publish all derived
            # data in the same writer transaction as the completion event and CAS.
            if current.status is OutboxStatus.DISPATCHING and success:
                uow.outbox.validate_dispatch_permit(permit=permit, now=self.service.clock.now_utc())
                if snapshot.revision != permit.run_revision:
                    raise RevisionConflictError("P6 dispatch revision changed")
                uow.connection.execute("SAVEPOINT p6_publication")
                try:
                    if permit.effect.effect_type == "internal.p6.assess":
                        published = self.service._assess_effect(
                            permit,
                            transaction=uow,
                            prepared=prepared_value,
                        )
                    else:
                        published = P6ReportRenderer(
                            self.service.database_path,
                            self.service.state_root,
                            clock=self.service.clock,
                        ).render(
                            permit,
                            transaction=uow,
                            prepared=prepared_value,
                        )
                    success, receipt, error_code = _normalize_handler_result(published)
                    if not success:
                        raise StateIntegrityError("P6 publication failed")
                    now = self.service.clock.now_utc()
                    uow.outbox.validate_dispatch_permit(permit=permit, now=now)

                except (StateIntegrityError, OSError):
                    uow.connection.execute("ROLLBACK TO p6_publication")
                    success, receipt, error_code = False, None, HandlerErrorCode.HANDLER_FAILED
                finally:
                    uow.connection.execute("RELEASE p6_publication")

            terminal = success or permit.generation >= self.max_attempts
            outcome = "succeeded" if success else ("dead_letter" if terminal else "retry")
            command_id = completion_command_id(permit.effect.effect_id, permit.generation, outcome)
            if success and current.status is OutboxStatus.SUCCEEDED:
                previous_receipt = uow.command_receipts.get(command_id)
                if previous_receipt is not None:
                    previous_event = uow.events.get(previous_receipt.result_event_id)
                    receipt = EffectSuccessReceiptV1.model_validate_json(
                        json.dumps(thaw_json(previous_event.payload["result_summary"]))
                    )
            command_hash = _completion_command_hash(
                command_id=command_id,
                permit=permit,
                outcome=outcome,
                receipt=receipt,
                error_code=error_code,
            )
            if terminal:
                existing = uow.command_receipts.get(command_id)
                if existing is not None:
                    event = uow.events.get(existing.result_event_id)
                    if (
                        existing.command_hash != command_hash
                        or existing.run_id != permit.effect.run_id
                        or event is None
                        or not isinstance(event, P6KernelEvent)
                        or event.payload.get("effect_id") != str(permit.effect.effect_id)
                    ):
                        raise StateIntegrityError("P6 completion receipt conflicts with its effect")
                    uow.commit()
                    return EffectCompletionReport(
                        permit.effect.effect_id,
                        outcome,
                        permit.generation,
                        ApplicationResult.model_validate_json(
                            json.dumps(thaw_json(event.result), ensure_ascii=False), strict=True
                        ),
                    )

            uow.outbox.validate_dispatch_permit(permit=permit, now=now)
            if (
                evaluate_dispatch(snapshot.state, current, P6_EFFECT_REGISTRY)
                is not DispatchDecision.ALLOW
            ):
                raise StateIntegrityError("P6 effect is blocked by dispatch policy")
            if not terminal:
                uow.outbox.retry_dispatch_in_transaction(
                    permit=permit,
                    now=now,
                    error_code=error_code or HandlerErrorCode.HANDLER_FAILED,
                )
                uow.commit()
                return EffectCompletionReport(permit.effect.effect_id, "retry", permit.generation)

            event_id = new_id(EventId)
            if success:
                if receipt is None:
                    raise StateIntegrityError("successful P6 completion has no receipt")
                next_state, effects, outcome_code = self._success_state(
                    uow, snapshot, permit, event_id, receipt
                )
                event_type = EventType.EFFECT_SUCCEEDED
                command_type = CommandType.RECORD_EFFECT_SUCCEEDED
                extra = {
                    "effect_id": str(permit.effect.effect_id),
                    "result_summary": receipt_json(receipt),
                }
            else:
                failure = error_code or HandlerErrorCode.HANDLER_FAILED
                next_state = snapshot.state.model_copy(
                    update={
                        "status": RunStatus.FAILED,
                        "phase": P6Phase.FAILED,
                        "last_error_code": failure.value,
                        "last_error_message": handler_error_message(failure),
                        "last_outcome_code": "effect_dead_lettered",
                    }
                )
                effects = ()
                outcome_code = "effect_dead_lettered"
                event_type = EventType.EFFECT_DEAD_LETTERED
                command_type = CommandType.RECORD_EFFECT_FAILED
                extra = {
                    "effect_id": str(permit.effect.effect_id),
                    "error_code": failure.value,
                    "error_message": handler_error_message(failure),
                }

            event, transition, application_result = _build_p6_event(
                events=uow.events,
                snapshot=snapshot,
                state=next_state,
                event_id=event_id,
                command_id=command_id,
                command_hash=command_hash,
                command_type=command_type,
                event_type=event_type,
                outcome_code=outcome_code,
                effects=effects,
                occurred_at_utc=now,
                extra_payload=extra,
            )
            uow.events.append(event, command_hash=command_hash)
            uow.outbox.complete_terminal_in_transaction(
                permit=permit,
                status=OutboxStatus.SUCCEEDED if success else OutboxStatus.DEAD_LETTER,
                now=now,
                audit_event_id=event.event_id,
                result_summary=receipt if success else None,
                error_code=None if success else error_code or HandlerErrorCode.HANDLER_FAILED,
            )
            if effects:
                uow.outbox.register_effects(
                    event=event,
                    run_id=permit.effect.run_id,
                    effects=transition.effects,
                    available_at_utc=now,
                    created_at_utc=now,
                )
            if next_state.status.is_terminal:
                uow.outbox.cancel_pending_for_run(run_id=permit.effect.run_id, now=now)
            if not uow.runs.compare_and_swap(
                run_id=permit.effect.run_id,
                expected_revision=snapshot.revision,
                state=transition.next_state,
                event_id=event.event_id,
                updated_at_utc=now,
            ):
                raise RevisionConflictError("P6 completion revision changed")
            uow.command_receipts.append_event(event=event, recorded_at_utc=now)
            uow.commit()
            return EffectCompletionReport(
                permit.effect.effect_id,
                outcome,
                permit.generation,
                application_result,
            )

    def _success_state(
        self,
        uow,
        snapshot: RunSnapshot,
        permit: DispatchPermit,
        event_id: EventId,
        receipt: EffectSuccessReceiptV1,
    ) -> tuple[P6WorkflowState, tuple[EffectSpec, ...], str]:
        records = P6RecordRepository(uow.connection)
        if permit.effect.effect_type == "internal.p6.assess":
            entry = records.latest_p6(
                run_id=permit.effect.run_id,
                record_type="p6.assessment",
                model_type=ScientificAssessment,
            )
            if entry is None:
                raise StateIntegrityError("P6 assessment completion has no assessment record")
            assessment = entry[1]
            entries = records.list_p6_for_run(permit.effect.run_id)
            claims = tuple(
                item.claim_id
                for _id, kind, item in entries
                if kind == "p6.claim" and isinstance(item, P6ClaimRecord)
            )
            comparison = next(
                (
                    item.record_id
                    for _id, kind, item in entries
                    if kind == "p6.comparability" and isinstance(item, ComparabilityAssessment)
                ),
                None,
            )
            effect = EffectSpec(
                effect_index=0,
                effect_type="internal.p6.render_report",
                effect_class=EffectClass.INTERNAL,
                payload={
                    "run_id": str(permit.effect.run_id),
                    "source_snapshot_id": str(snapshot.state.source_snapshot_id),
                    "source_snapshot_hash": snapshot.state.source_snapshot_hash,
                    "assessment_id": str(assessment.assessment_id),
                    "assessment_hash": assessment.assessment_hash,
                    "policy_id": snapshot.state.policy_id,
                    "policy_hash": snapshot.state.policy_hash,
                },
            )
            next_state = snapshot.state.model_copy(
                update={
                    "phase": P6Phase.REPORT_PENDING,
                    "assessment_id": assessment.assessment_id,
                    "assessment_hash": assessment.assessment_hash,
                    "evidence_ids": assessment.evidence_ids,
                    "claim_ids": claims,
                    "comparability_id": comparison,
                    "report_effect_id": effect.effect_id(event_id),
                    "last_outcome_code": "assessment_completed",
                }
            )
            return next_state, (effect,), "assessment_completed"

        if permit.effect.effect_type != "internal.p6.render_report":
            raise StateIntegrityError("unknown P6 effect type")
        manifest_entry = records.latest_p6(
            run_id=permit.effect.run_id,
            record_type="p6.report_manifest",
            model_type=P6ReportManifest,
        )
        if manifest_entry is None:
            raise StateIntegrityError("P6 report completion has no manifest")
        manifest = manifest_entry[1]
        if not receipt.artifact_ids:
            raise StateIntegrityError("P6 report receipt has no report artifacts")
        return (
            snapshot.state.model_copy(
                update={
                    "phase": P6Phase.COMPLETED,
                    "assessment_id": manifest.assessment_id,
                    "assessment_hash": manifest.assessment_hash,
                    "evidence_ids": manifest.evidence_ids,
                    "claim_ids": manifest.claim_ids,
                    "report_manifest_id": manifest.report_manifest_id,
                    "report_artifact_ids": receipt.artifact_ids,
                    "last_outcome_code": "report_completed",
                }
            ),
            (),
            "report_completed",
        )


def _build_p6_event(
    *,
    events,
    snapshot: RunSnapshot | None,
    state: P6WorkflowState,
    event_id: EventId,
    command_id: CommandId,
    command_hash: str,
    command_type: CommandType,
    event_type: EventType,
    outcome_code: str,
    effects: tuple[EffectSpec, ...],
    occurred_at_utc,
    extra_payload: dict[str, object] | None = None,
) -> tuple[P6KernelEvent, P6Transition, ApplicationResult]:
    expected_revision = 0 if snapshot is None else snapshot.revision
    if snapshot is None:
        previous_event_hash = GENESIS_EVENT_HASH
        prior_state = None
    else:
        previous = events.get(snapshot.last_event_id)
        if not isinstance(previous, P6KernelEvent):
            raise StateIntegrityError("P6 previous event is missing or has the wrong type")
        previous_event_hash = previous.event_hash
        prior_state = snapshot.state
    payload: dict[str, object] = {
        "next_state": state.model_dump(mode="json"),
        "outcome_code": outcome_code,
        "effects": [item.model_dump(mode="json") for item in effects],
    }
    if extra_payload:
        payload.update(extra_payload)
    placeholder = ApplicationResult.accepted_result(
        code=outcome_code,
        run_id=state.run_id,
        revision=expected_revision + 1,
        status=state.status,
        event_id=event_id,
        details={
            "phase": state.phase.value,
            "scientific_assessment": "not_evaluated"
            if state.phase is P6Phase.ASSESSMENT_PENDING
            else "available",
            "claim_status": "generated" if state.claim_ids else "not_generated",
        },
    )
    candidate = P6KernelEvent.create(
        event_id=event_id,
        command_id=command_id,
        command_type=command_type,
        run_id=state.run_id,
        sequence_no=expected_revision + 1,
        expected_revision=expected_revision,
        event_type=event_type,
        payload=payload,
        result=placeholder,
        occurred_at_utc=occurred_at_utc,
        command_hash=command_hash,
        previous_event_hash=previous_event_hash,
    )
    transition = reduce_p6_event(prior_state, candidate)
    application_result = expected_p6_application_result(event=candidate, transition=transition)
    event = P6KernelEvent.create(
        event_id=event_id,
        command_id=command_id,
        command_type=command_type,
        run_id=state.run_id,
        sequence_no=expected_revision + 1,
        expected_revision=expected_revision,
        event_type=event_type,
        payload=payload,
        result=application_result,
        occurred_at_utc=occurred_at_utc,
        recorded_at_utc=candidate.recorded_at_utc,
        command_hash=command_hash,
        previous_event_hash=previous_event_hash,
    )
    return event, transition, application_result


def _completion_command_hash(
    *,
    command_id: CommandId,
    permit: DispatchPermit,
    outcome: str,
    receipt: EffectSuccessReceiptV1 | None,
    error_code: HandlerErrorCode | None,
) -> str:
    return sha256_hex(
        {
            "command_id": str(command_id),
            "command_type": "record_effect_succeeded"
            if outcome == "succeeded"
            else "record_effect_failed",
            "effect_id": str(permit.effect.effect_id),
            "error_code": None if error_code is None else error_code.value,
            "generation": permit.generation,
            "receipt": None if receipt is None else receipt_json(receipt),
            "run_id": str(permit.effect.run_id),
            "run_revision": permit.run_revision,
        }
    )


def _normalize_handler_result(
    result: HandlerResult,
) -> tuple[bool, EffectSuccessReceiptV1 | None, HandlerErrorCode | None]:
    if not isinstance(result, HandlerResult) or type(result.success) is not bool:
        return False, None, HandlerErrorCode.INVALID_HANDLER_RESULT
    if result.success:
        try:
            receipt = parse_effect_success_receipt(
                {"receipt_schema": "effect-success/v1", "outcome_code": "completed"}
                if result.result_summary is None
                else result.result_summary
            )
        except ValueError:
            return False, None, HandlerErrorCode.INVALID_HANDLER_RESULT
        return True, receipt, None
    code = result.error_code
    if code is None:
        code = HandlerErrorCode.HANDLER_FAILED
    if not isinstance(code, HandlerErrorCode):
        code = HandlerErrorCode.INVALID_HANDLER_RESULT
    return False, None, code


def _make_assessment(
    ingestion: P6Ingestion,
    policy: ScientificPolicy,
    state: P6WorkflowState,
) -> tuple[ScientificAssessment, ModeAnalysis | None]:
    bundles = ingestion.results
    opt = next((item for item in bundles if item.result.primitive is P5NodeKind.OPT), None)
    freq = next((item for item in bundles if item.result.primitive is P5NodeKind.FREQ), None)
    opt_converged = (
        opt is not None
        and opt.result.parse_status is P5ParseStatus.COMPLETE
        and opt.parsed is not None
        and opt.parsed.optimization_converged is True
    )
    freq_complete = (
        freq is not None
        and freq.result.parse_status is P5ParseStatus.COMPLETE
        and freq.parsed is not None
        and freq.observations is not None
    )
    geometry_bound = _geometry_bound(opt, freq)
    method = ingestion.method_context
    method_hashes = {item.binding.method_profile_hash for item in bundles}
    versions = {item.result.orca_version for item in bundles}
    method_supported = (
        ingestion.snapshot.source_origin.value == "orca_local"
        and method.method_profile_id == policy.supported_method_profile_id
        and method.method_profile_hash in method_hashes
        and len(method_hashes) == 1
        and versions == set(policy.supported_orca_versions)
    )
    context_supported = (
        method.formal_charge == policy.supported_charge
        and method.multiplicity == policy.supported_multiplicity
        and method.environment == policy.supported_environment
    )
    hessian_complete = bool(
        freq_complete
        and freq is not None
        and freq.observations is not None
        and freq.observations.hessian_dimension == 3 * len(method.atom_symbols)
    )
    mode_analysis = None
    if freq is not None and freq.observations is not None:
        mode_analysis = classify_modes(
            freq.geometry,
            freq.observations.frequencies,
            freq.observations.frequency_tokens,
            freq.observations.normal_modes,
            policy,
        )
    requested_nodes = tuple(dict.fromkeys(item.result.primitive.value for item in bundles))
    evaluation = evaluate_minimum(
        policy=policy,
        opt_converged=opt_converged,
        freq_complete=freq_complete,
        geometry_bound=geometry_bound,
        method_supported=method_supported,
        context_supported=context_supported,
        hessian_complete=hessian_complete,
        mode_analysis=mode_analysis,
        requested_nodes=requested_nodes,
    )
    evidence_ids = tuple(item.evidence_id for item in ingestion.evidence)
    checks = tuple(
        item.model_copy(update={"evidence_ids": evidence_ids}) for item in evaluation.checks
    )
    classifications = () if mode_analysis is None else mode_analysis.classifications
    if freq is not None and freq.observations is not None:
        from orca_agent.domain.p6 import P6Locator

        classifications = tuple(
            item.model_copy(
                update={
                    "frequency_locator": next(
                        (
                            ev.locator
                            for ev in ingestion.evidence
                            if ev.source_result_id == freq.source_ref.result_id
                            and ev.quantity == "vibrational_frequency"
                            and ev.locator is not None
                            and ev.locator.index == item.mode_index
                        ),
                        None,
                    ),
                    "mode_locator": (
                        P6Locator(
                            artifact_id=freq.source_ref.hessian_artifact_id,
                            artifact_hash=freq.source_ref.hessian_hash,
                            utf8_character_span=freq.observations.normal_modes_span,
                            block="normal_modes",
                            index=item.mode_index,
                        )
                        if freq.source_ref.hessian_artifact_id is not None
                        and freq.source_ref.hessian_hash is not None
                        and freq.observations.normal_modes
                        else None
                    ),
                }
            )
            for item in classifications
        )
    limitations = (
        "single starting conformer; no conformer search or global minimum search",
        "electronic energy only; no ZPE, enthalpy, Gibbs free energy, or "
        "thermodynamic ranking claim",
        "minimum support is limited to the registered method, geometry binding, "
        "and scientific policy",
    )
    if ingestion.snapshot.source_origin.value != "orca_local":
        limitations += ("fixture-origin data is not a real ORCA scientific result",)
    assessment = ScientificAssessment.create(
        record_id=new_id(WorkflowRecordId),
        assessment_id=new_id(AssessmentId),
        run_id=state.run_id,
        source_p5_run_id=ingestion.snapshot.source_p5_run_id,
        source_snapshot_id=ingestion.snapshot.snapshot_id,
        source_snapshot_hash=ingestion.snapshot.snapshot_hash,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        source_origin=ingestion.snapshot.source_origin,
        result_ids=tuple(item.source_ref.result_id for item in bundles),
        evidence_ids=evidence_ids,
        claim_ids=(),
        energy_available=any(item.energy is not None for item in ingestion.snapshot.results),
        minimum_status=evaluation.status,
        minimum_reason=evaluation.reason,
        checks=checks,
        mode_classifications=classifications,
        missing_requirements=evaluation.missing_requirements,
        warnings=evaluation.warnings,
        limitations=limitations,
        integrity_verified=True,
    )
    return assessment, mode_analysis


def _make_claims(
    assessment: ScientificAssessment,
    policy: ScientificPolicy,
    ingestion: P6Ingestion,
    evidence: tuple[P6EvidenceRecord, ...],
    mode_analysis: ModeAnalysis | None,
) -> tuple[P6ClaimRecord, ...]:
    values: list[P6ClaimRecord] = []
    if any(bundle.parsed is None for bundle in ingestion.results):
        return ()
    for bundle in ingestion.results:
        energy = next(
            (
                item
                for item in evidence
                if item.source_result_id == bundle.source_ref.result_id
                and item.quantity == "electronic_energy"
            ),
            None,
        )
        if energy is not None:
            values.append(
                build_energy_claim(
                    assessment=assessment,
                    policy=policy,
                    evidence=energy,
                    subject_result_id=bundle.source_ref.result_id,
                )
            )
        if bundle.observations is not None and mode_analysis is not None:
            for mode in mode_analysis.classifications:
                if mode.mode_index < 6 or mode.kind is not P6ModeKind.VIBRATIONAL_CANDIDATE:
                    continue
                frequency = next(
                    (
                        item
                        for item in evidence
                        if item.source_result_id == bundle.source_ref.result_id
                        and item.quantity == "vibrational_frequency"
                        and item.locator is not None
                        and item.locator.index == mode.mode_index
                    ),
                    None,
                )
                if frequency is not None:
                    values.append(
                        build_frequency_claim(
                            assessment=assessment,
                            policy=policy,
                            evidence=frequency,
                            subject_result_id=bundle.source_ref.result_id,
                        )
                    )
    requested_nodes = {item.result.primitive.value for item in ingestion.results}
    if {"opt", "freq"}.issubset(requested_nodes):
        subject = next(
            (
                item.source_ref.result_id
                for item in ingestion.results
                if item.result.primitive is P5NodeKind.FREQ
            ),
            ingestion.results[-1].source_ref.result_id,
        )
        values.append(
            build_minimum_claim(
                assessment=assessment,
                policy=policy,
                subject_result_id=subject,
                evidence_hashes=tuple(item.evidence_hash for item in evidence),
            )
        )
    return tuple(values)


def _geometry_bound(opt: P6ResultBundle | None, freq: P6ResultBundle | None) -> bool:
    if opt is None or freq is None or opt.source_ref.optimized_geometry_artifact_id is None:
        return False
    try:
        symbols, coordinates = parse_xyz_bytes(
            opt.bytes_for(opt.source_ref.optimized_geometry_artifact_id)
        )
    except Exception:
        return False
    return (
        symbols == freq.geometry.atom_symbols
        and len(coordinates) == len(freq.geometry.coordinates)
        and all(
            abs(left - right) <= 2.0e-6
            for point_left, point_right in zip(coordinates, freq.geometry.coordinates, strict=True)
            for left, right in zip(point_left, point_right, strict=True)
        )
    )


def _selected_energy_evidence(
    evidence: tuple[P6EvidenceRecord, ...],
    source: P6SourceSnapshot,
) -> P6EvidenceRecord | None:
    sp_ids = {item.result_id for item in source.results if item.primitive == "sp"}
    values = tuple(item for item in evidence if item.quantity == "electronic_energy")
    preferred = tuple(item for item in values if item.source_result_id in sp_ids)
    return (preferred or values)[-1] if (preferred or values) else None


def _source_snapshot_record(
    records: P6RecordRepository,
    run_id: RunId,
    snapshot_id,
) -> P6SourceSnapshot | None:
    for _record_id, record_type, item in records.list_p6_for_run(run_id):
        if (
            record_type == "p6.source_snapshot"
            and isinstance(item, P6SourceSnapshot)
            and item.snapshot_id == snapshot_id
        ):
            return item
    return None


__all__ = ["P6ApplicationService", "P6EffectCompletion", "P6RunView"]
