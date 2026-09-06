"""Application boundary for P4 molecule identity and planning-only runs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, field_validator

from orca_agent.application.errors import (
    ApplicationError,
    DuplicateCommandConflictError,
    EffectInFlightError,
    InvalidTransitionError,
    RevisionConflictError,
    StateIntegrityError,
    StorageError,
)
from orca_agent.application.results import ApplicationResult
from orca_agent.domain.errors import DomainError
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import (
    CommandId,
    ConversationId,
    EventId,
    InterruptId,
    PlanProposalId,
    PrimitiveId,
    ProblemSpecId,
    RunId,
    WorkflowRecordId,
    is_new_external_command_id,
    new_id,
)
from orca_agent.domain.json_types import FrozenJsonObject, freeze_json_object, thaw_json
from orca_agent.domain.models import Environment
from orca_agent.domain.p4 import (
    CandidateBundle,
    ConfirmedMolecule,
    IdentityDecision,
    IdentityProvider,
    LookupAttempt,
    LookupStatus,
    MoleculeInputKind,
    MoleculeQuery,
    P4Model,
    P4Phase,
    P4Result,
    P4WorkflowState,
    PreparedPlan,
    ResponseEnvelope,
)
from orca_agent.domain.registry import RegistrySnapshot
from orca_agent.infrastructure.artifacts import ArtifactStore
from orca_agent.infrastructure.clock import Clock, SystemClock, format_utc
from orca_agent.infrastructure.command_receipts import CommandReceipt
from orca_agent.infrastructure.outbox import DispatchPermit, OutboxRecord, OutboxStatus
from orca_agent.infrastructure.p3_records import (
    ArtifactRecordRepository,
    P4RecordRepository,
)
from orca_agent.infrastructure.repositories import RunSnapshot
from orca_agent.infrastructure.sqlite import resolve_database_path
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.orchestration.commands import CommandType
from orca_agent.orchestration.dispatch_policy import (
    P4_EFFECT_REGISTRY,
)
from orca_agent.orchestration.effects import EffectClass, EffectSpec
from orca_agent.orchestration.events import EventType
from orca_agent.orchestration.p4_commands import (
    CancelPlanningRun,
    ConfirmMoleculeIdentity,
    StartPlanningRun,
)
from orca_agent.orchestration.p4_kernel import (
    P4KernelEvent,
    P4Transition,
    expected_p4_application_result,
    reduce_p4_event,
)
from orca_agent.orchestration.p4_versions import (
    P4_ENGINE_VERSION,
    P4_SCHEMA_VERSION,
)
from orca_agent.orchestration.replay import state_hash
from orca_agent.orchestration.state import RunStatus
from orca_agent.planning.protocols import expand_ground_state_plan
from orca_agent.planning.registry import fixed_registry_snapshot
from orca_agent.planning.validator import validate_ground_state_plan

from .effect_completion import EffectCompletionService
from .p4_handlers import P4IdentityHandler

if TYPE_CHECKING:
    from orca_agent.identity.ports import PubChemPort


_P4_OBJECT_NAMESPACE = uuid.UUID("6ca6b92b-5cb0-4d2d-a267-becf12a30ab4")
_CONFIRMATION_HOURS = 24


class P4RunView(P4Model):
    """Fully verified read view for a P4 run."""

    run_id: RunId
    conversation_id: ConversationId
    revision: int = Field(ge=1)
    state: P4WorkflowState
    query: MoleculeQuery
    registry: RegistrySnapshot
    candidate_bundle: CandidateBundle | None
    confirmed_molecule: ConfirmedMolecule | None
    prepared_plan: PreparedPlan | None
    interrupt: FrozenJsonObject | None
    attempts: tuple[LookupAttempt, ...]
    outbox: tuple[FrozenJsonObject, ...]
    diagnostics: tuple[str, ...] = ()

    @field_validator("interrupt", mode="before")
    @classmethod
    def _interrupt(cls, value: object) -> FrozenJsonObject | None:
        return None if value is None else freeze_json_object(value)

    @field_validator("outbox", mode="before")
    @classmethod
    def _outbox(cls, value: object) -> tuple[FrozenJsonObject, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("outbox view must be an array")
        return tuple(freeze_json_object(item) for item in value)


class _P4Readiness:
    """Worker claim hook that reads trusted Retry-After history on its claim connection."""

    def __init__(self) -> None:
        self.connection: sqlite3.Connection | None = None

    def bind_connection(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __call__(self, record: OutboxRecord, snapshot: object, now: datetime) -> bool:
        if record.schema_version != P4_SCHEMA_VERSION:
            return True
        if not isinstance(snapshot.state, P4WorkflowState):
            return True
        if self.connection is None:
            raise StateIntegrityError("P4 readiness hook is not bound to a claim connection")
        latest = P4RecordRepository(self.connection).latest_attempt(
            run_id=record.run_id,
            effect_id=record.effect_id,
        )
        if latest is None or latest.retry_not_before_utc is None:
            return True
        return latest.retry_not_before_utc <= now


class P4ApplicationService:
    """Public P4 commands; identity is confirmed before a fixed plan exists."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        clock: Clock | None = None,
        fake_adapter: PubChemPort | None = None,
        pubchem_adapter: PubChemPort | None = None,
        allow_network: bool = False,
        max_attempts: int = 3,
    ) -> None:
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.state_root = Path(state_root)
        self.database_path = resolve_database_path(self.state_root)
        self.clock = clock or SystemClock()
        self.max_attempts = max_attempts
        self.fake_adapter = fake_adapter
        if pubchem_adapter is None:
            from orca_agent.identity.http_pubchem import HttpPubChemAdapter

            pubchem_adapter = HttpPubChemAdapter(allow_network=allow_network)
        self.pubchem_adapter = pubchem_adapter
        self.allow_network = allow_network

    def start(self, command: StartPlanningRun) -> P4Result:
        try:
            if not is_new_external_command_id(command.command_id):
                raise InvalidTransitionError("new P4 command ID must be UUID4")
            if not command.new_conversation:
                raise InvalidTransitionError("P4 prepare requires a new conversation")
            normalized = self._validate_start(command)
            query = MoleculeQuery.create(
                query_id=_deterministic_id(WorkflowRecordId, command.run_id, "query"),
                run_id=command.run_id,
                conversation_id=command.conversation_id,
                input_kind=command.input_kind,
                raw_input=command.raw_input,
                normalized_input=normalized,
                charge=command.charge,
                multiplicity=command.multiplicity,
                provider=command.provider,
                protocol_id=command.protocol_id,
            )
            registry = fixed_registry_snapshot(
                record_id=_deterministic_id(WorkflowRecordId, command.run_id, "registry")
            )
            effect_payload = {
                "run_id": str(command.run_id),
                "conversation_id": str(command.conversation_id),
                "query_id": str(query.query_id),
                "query_hash": query.query_hash,
                "registry_snapshot_id": str(registry.record_id),
                "registry_snapshot_hash": registry.snapshot_hash,
                "input_kind": command.input_kind.value,
                "provider": command.provider.value,
                "protocol_id": command.protocol_id,
                "workflow_schema_version": P4_SCHEMA_VERSION,
                "workflow_engine_version": P4_ENGINE_VERSION,
            }
            effect_type = (
                "internal.p4.resolve_smiles"
                if command.input_kind is MoleculeInputKind.SMILES
                else "external.p4.resolve_pubchem"
            )
            effect_class = (
                EffectClass.INTERNAL
                if effect_type.startswith("internal.")
                else EffectClass.EXTERNAL
            )
            event_id = new_id(EventId)
            effect = EffectSpec(
                effect_index=0,
                effect_type=effect_type,
                effect_class=effect_class,
                payload=effect_payload,
            )
            effect = EffectSpec(
                effect_index=effect.effect_index,
                effect_type=effect.effect_type,
                effect_class=effect.effect_class,
                payload={**effect_payload, "effect_id": str(effect.effect_id(event_id))},
            )
            payload = {
                "run_id": str(command.run_id),
                "conversation_id": str(command.conversation_id),
                "query_id": str(query.query_id),
                "query_hash": query.query_hash,
                "registry_snapshot_id": str(registry.record_id),
                "registry_snapshot_hash": registry.snapshot_hash,
                "effects": [effect.model_dump(mode="json")],
            }
            now = self.clock.now_utc()
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                self._require_kernel(uow)
                uow.begin()
                existing_run = uow.runs.get(command.run_id)
                if existing_run is not None:
                    receipt = uow.command_receipts.get(command.command_id)
                    if receipt is None or receipt.command_hash != command.command_hash():
                        raise DuplicateCommandConflictError("P4 prepare run ID is already bound")
                    result = self._result_for_receipt(uow, command, receipt)
                    uow.commit()
                    return result
                if uow.command_receipts.get(command.command_id) is not None:
                    raise DuplicateCommandConflictError("P4 prepare command ID is already bound")
                event, transition, result = self._build_p4_event(
                    current=None,
                    events=uow.events,
                    event_id=event_id,
                    command_id=command.command_id,
                    command_hash=command.command_hash(),
                    command_type=CommandType.CREATE_RUN,
                    event_type=EventType.RUN_CREATED,
                    payload=payload,
                    occurred_at_utc=now,
                )
                uow.runs.insert(
                    RunSnapshot(
                        run_id=command.run_id,
                        schema_version=P4_SCHEMA_VERSION,
                        engine_version=P4_ENGINE_VERSION,
                        revision=1,
                        state=transition.next_state,
                        state_hash=state_hash(transition.next_state),
                        last_event_id=event.event_id,
                        created_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                uow.events.append(event, command_hash=command.command_hash())
                uow.outbox.register_effects(
                    event=event,
                    run_id=command.run_id,
                    effects=transition.effects,
                    available_at_utc=now,
                    created_at_utc=now,
                )
                records = P4RecordRepository(uow.connection)
                records.append_p4(
                    run_id=command.run_id,
                    record_type="p4.molecule_query",
                    record=query,
                    created_at_utc=now,
                    source_event_id=event.event_id,
                    record_id=query.query_id,
                )
                records.append_p4(
                    run_id=command.run_id,
                    record_type="p4.registry_snapshot",
                    record=registry,
                    created_at_utc=now,
                    source_event_id=event.event_id,
                    record_id=registry.record_id,
                )
                records.append_p4(
                    run_id=command.run_id,
                    record_type="p4.workflow_state",
                    record=transition.next_state,
                    created_at_utc=now,
                    source_event_id=event.event_id,
                )
                uow.command_receipts.append_event(event=event, recorded_at_utc=now)
                uow.commit()
                return _p4_result_from_application(result, transition.next_state)
        except Exception as error:
            return self._safe_reject(command.run_id, command.conversation_id, error)

    def confirm(self, command: ConfirmMoleculeIdentity) -> P4Result:
        try:
            if not is_new_external_command_id(command.command_id):
                raise InvalidTransitionError("new P4 command ID must be UUID4")
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                self._require_kernel(uow)
                uow.begin()
                existing = uow.command_receipts.get(command.command_id)
                if existing is not None:
                    if existing.command_hash != command.command_hash():
                        raise DuplicateCommandConflictError(
                            "P4 confirmation command hash conflicts"
                        )
                    result = self._result_for_receipt(uow, command, existing)
                    uow.commit()
                    return result
                snapshot = uow.runs.get_verified(
                    command.run_id,
                    uow.events,
                    interrupts=uow.interrupts,
                    outbox=uow.outbox,
                )
                state = self._p4_state(snapshot)
                if state.phase is not P4Phase.AWAITING_IDENTITY:
                    raise InvalidTransitionError("P4 run is not awaiting identity confirmation")
                self._validate_confirmation_bindings(uow, snapshot, command)
                interrupt = uow.interrupts.get(command.interrupt_id)
                if interrupt is None or interrupt.status.value != "pending":
                    raise InvalidTransitionError("identity confirmation interrupt is not pending")
                if self.clock.now_utc() >= interrupt.expires_at_utc:
                    now = self.clock.now_utc()
                    result, _transition, event = self._persist_p4_event(
                        uow=uow,
                        current=snapshot,
                        command_id=command.command_id,
                        command_hash=command.command_hash(),
                        command_type=CommandType.RESOLVE_INTERRUPT,
                        event_type=EventType.INTERRUPT_EXPIRED,
                        payload={
                            "interrupt_id": str(command.interrupt_id),
                            "expires_at_utc": format_utc(interrupt.expires_at_utc),
                        },
                        occurred_at_utc=now,
                    )
                    self._append_workflow_state(
                        uow, command.run_id, _transition.next_state, event.event_id, now
                    )
                    uow.commit()
                    return _p4_result_from_application(result, _transition.next_state)

                records = P4RecordRepository(uow.connection)
                query = records.get_exact(
                    run_id=command.run_id,
                    record_id=state.query_id,
                    record_type="p4.molecule_query",
                    model_type=MoleculeQuery,
                )
                registry = records.get_exact(
                    run_id=command.run_id,
                    record_id=state.registry_snapshot_id,
                    record_type="p4.registry_snapshot",
                    model_type=RegistrySnapshot,
                )
                bundle = records.get_exact(
                    run_id=command.run_id,
                    record_id=state.candidate_bundle_id,
                    record_type="p4.candidate_bundle",
                    model_type=CandidateBundle,
                )
                if query is None or registry is None or bundle is None:
                    raise StateIntegrityError("P4 identity records are incomplete")
                if (
                    bundle.bundle_hash != state.candidate_bundle_hash
                    or bundle.candidate_set_hash != state.candidate_set_hash
                ):
                    raise StateIntegrityError("P4 candidate bundle is not bound to state")
                candidate = self._selected_candidate(bundle, command)
                if command.decision is IdentityDecision.REJECT:
                    response = self._confirmation_response(command)
                    payload = {
                        "interrupt_id": str(command.interrupt_id),
                        "response": response,
                    }
                    result, transition, event = self._persist_p4_event(
                        uow=uow,
                        current=snapshot,
                        command_id=command.command_id,
                        command_hash=command.command_hash(),
                        command_type=CommandType.RESOLVE_INTERRUPT,
                        event_type=EventType.INTERRUPT_RESOLVED,
                        payload=payload,
                        occurred_at_utc=self.clock.now_utc(),
                    )
                    self._append_workflow_state(
                        uow,
                        command.run_id,
                        transition.next_state,
                        event.event_id,
                        self.clock.now_utc(),
                    )
                    uow.commit()
                    return _p4_result_from_application(result, transition.next_state)
                if not bundle.confirmable:
                    raise InvalidTransitionError(
                        "candidate bundle is outside the P4 protocol range"
                    )
                self._verify_candidate_source(
                    uow,
                    command.run_id,
                    command,
                    candidate,
                    bundle=bundle,
                    expected_effect_id=state.identity_effect_id,
                )
                event_id = new_id(EventId)
                confirmed = ConfirmedMolecule.create(
                    record_id=_deterministic_id(
                        WorkflowRecordId, command.run_id, f"confirmed:{command.command_id}"
                    ),
                    run_id=command.run_id,
                    query_id=query.query_id,
                    query_hash=query.query_hash,
                    candidate_set_hash=bundle.candidate_set_hash,
                    candidate_id=candidate.candidate_id,
                    candidate_hash=candidate.candidate_hash,
                    canonical_isomeric_smiles=candidate.canonical_isomeric_smiles,
                    molecular_formula=candidate.molecular_formula,
                    formal_charge=query.charge,
                    multiplicity=query.multiplicity,
                    environment=Environment.GAS,
                    structure_hash=str(candidate.checks.get("structure_hash", ""))
                    if candidate.checks.get("structure_hash")
                    else sha256_hex(
                        {
                            "canonical_isomeric_smiles": candidate.canonical_isomeric_smiles,
                            "formal_charge": candidate.formal_charge,
                            "isotope_labels": list(candidate.isotope_labels),
                            "normalization_strategy": candidate.normalization_strategy,
                            "rdkit_version": candidate.rdkit_version,
                        }
                    ),
                    provider=candidate.provider,
                    confirmation_command_id=command.command_id,
                    confirmation_event_id=event_id,
                )
                plan = expand_ground_state_plan(
                    confirmed=confirmed,
                    snapshot=registry,
                    problem_spec_id=_deterministic_id(
                        ProblemSpecId, command.run_id, f"problem:{command.command_id}"
                    ),
                    proposal_id=_deterministic_id(
                        PlanProposalId, command.run_id, f"proposal:{command.command_id}"
                    ),
                    optimization_id=_deterministic_id(
                        PrimitiveId, command.run_id, f"optimization:{command.command_id}"
                    ),
                    frequency_id=_deterministic_id(
                        PrimitiveId, command.run_id, f"frequency:{command.command_id}"
                    ),
                    prepared_plan_id=_deterministic_id(
                        WorkflowRecordId, command.run_id, f"prepared_plan:{command.command_id}"
                    ),
                )
                validate_ground_state_plan(plan, confirmed=confirmed, snapshot=registry)
                response = self._confirmation_response(command)
                response.update(
                    {
                        "confirmed_molecule_id": str(confirmed.record_id),
                        "confirmed_molecule_hash": confirmed.identity_record_hash,
                        "prepared_plan_id": str(plan.record_id),
                        "prepared_plan_hash": plan.plan_hash,
                    }
                )
                payload = {
                    "interrupt_id": str(command.interrupt_id),
                    "response": response,
                    "confirmed_molecule_id": str(confirmed.record_id),
                    "confirmed_molecule_hash": confirmed.identity_record_hash,
                    "prepared_plan_id": str(plan.record_id),
                    "prepared_plan_hash": plan.plan_hash,
                }
                result, transition, event = self._persist_p4_event(
                    uow=uow,
                    current=snapshot,
                    command_id=command.command_id,
                    command_hash=command.command_hash(),
                    command_type=CommandType.RESOLVE_INTERRUPT,
                    event_type=EventType.INTERRUPT_RESOLVED,
                    payload=payload,
                    occurred_at_utc=self.clock.now_utc(),
                    event_id=event_id,
                )
                records.append_any(
                    run_id=command.run_id,
                    record_type="problem_spec",
                    record=plan.problem_spec,
                    schema_version=1,
                    engine_version="p1-domain-v1",
                    created_at_utc=self.clock.now_utc(),
                    source_event_id=event.event_id,
                    record_id=plan.problem_spec.record_id,
                )
                records.append_any(
                    run_id=command.run_id,
                    record_type="plan_proposal",
                    record=plan.proposal,
                    schema_version=1,
                    engine_version="p1-domain-v1",
                    created_at_utc=self.clock.now_utc(),
                    source_event_id=event.event_id,
                    record_id=plan.proposal.proposal_id,
                )
                records.append_p4(
                    run_id=command.run_id,
                    record_type="p4.confirmed_molecule",
                    record=confirmed,
                    created_at_utc=self.clock.now_utc(),
                    source_event_id=event.event_id,
                    record_id=confirmed.record_id,
                )
                records.append_p4(
                    run_id=command.run_id,
                    record_type="p4.prepared_plan",
                    record=plan,
                    created_at_utc=self.clock.now_utc(),
                    source_event_id=event.event_id,
                    record_id=plan.record_id,
                )
                self._append_workflow_state(
                    uow, command.run_id, transition.next_state, event.event_id, self.clock.now_utc()
                )
                uow.commit()
                return _p4_result_from_application(result, transition.next_state)
        except Exception as error:
            return self._safe_reject(command.run_id, command.conversation_id, error)

    def cancel(self, command: CancelPlanningRun) -> P4Result:
        try:
            if not is_new_external_command_id(command.command_id):
                raise InvalidTransitionError("new P4 command ID must be UUID4")
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                self._require_kernel(uow)
                uow.begin()
                existing = uow.command_receipts.get(command.command_id)
                if existing is not None:
                    if existing.command_hash != command.command_hash():
                        raise DuplicateCommandConflictError("P4 cancel command hash conflicts")
                    result = self._result_for_receipt(uow, command, existing)
                    uow.commit()
                    return result
                snapshot = uow.runs.get_verified(
                    command.run_id,
                    uow.events,
                    interrupts=uow.interrupts,
                    outbox=uow.outbox,
                )
                state = self._p4_state(snapshot)
                if state.phase is P4Phase.PLAN_READY:
                    raise InvalidTransitionError("P4 plan-ready run cannot be cancelled")
                if snapshot.revision != command.expected_revision:
                    raise RevisionConflictError("P4 cancel expected revision is stale")
                if state.conversation_id != command.conversation_id:
                    raise InvalidTransitionError("conversation does not own the P4 workflow")
                if uow.outbox.has_dispatching_effect(command.run_id):
                    raise EffectInFlightError("P4 cancellation is blocked during dispatch")
                now = self.clock.now_utc()
                result, transition, event = self._persist_p4_event(
                    uow=uow,
                    current=snapshot,
                    command_id=command.command_id,
                    command_hash=command.command_hash(),
                    command_type=CommandType.CANCEL_RUN,
                    event_type=EventType.RUN_CANCELLED,
                    payload={"reason_code": command.reason_code},
                    occurred_at_utc=now,
                )
                self._append_workflow_state(
                    uow, command.run_id, transition.next_state, event.event_id, now
                )
                uow.commit()
                return _p4_result_from_application(result, transition.next_state)
        except Exception as error:
            return self._safe_reject(command.run_id, command.conversation_id, error)

    def inspect(self, run_id: RunId) -> P4RunView:
        with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
            self._require_kernel(uow)
            uow.begin()
            snapshot = uow.runs.get_verified(
                run_id,
                uow.events,
                interrupts=uow.interrupts,
                outbox=uow.outbox,
            )
            state = self._p4_state(snapshot)
            records = P4RecordRepository(uow.connection)
            query = records.get_exact(
                run_id=run_id,
                record_id=state.query_id,
                record_type="p4.molecule_query",
                model_type=MoleculeQuery,
            )
            registry = records.get_exact(
                run_id=run_id,
                record_id=state.registry_snapshot_id,
                record_type="p4.registry_snapshot",
                model_type=RegistrySnapshot,
            )
            if query is None or registry is None:
                raise StateIntegrityError("P4 query or registry snapshot is missing")
            bundle = (
                records.get_exact(
                    run_id=run_id,
                    record_id=state.candidate_bundle_id,
                    record_type="p4.candidate_bundle",
                    model_type=CandidateBundle,
                )
                if state.candidate_bundle_id is not None
                else None
            )
            confirmed = (
                records.get_exact(
                    run_id=run_id,
                    record_id=state.confirmed_molecule_id,
                    record_type="p4.confirmed_molecule",
                    model_type=ConfirmedMolecule,
                )
                if state.confirmed_molecule_id is not None
                else None
            )
            plan = (
                records.get_exact(
                    run_id=run_id,
                    record_id=state.prepared_plan_id,
                    record_type="p4.prepared_plan",
                    model_type=PreparedPlan,
                )
                if state.prepared_plan_id is not None
                else None
            )
            interrupt = None
            if state.pending_interrupt_id is not None:
                value = uow.interrupts.get(state.pending_interrupt_id)
                if value is None:
                    raise StateIntegrityError("P4 pending interrupt is missing")
                interrupt = {
                    "interrupt_id": str(value.interrupt_id),
                    "run_id": str(value.run_id),
                    "kind": value.kind,
                    "status": value.status.value,
                    "request_event_id": str(value.request_event_id),
                    "payload": thaw_json(value.payload),
                    "expires_at_utc": format_utc(value.expires_at_utc),
                }
            attempts = (
                records.attempts_for_effect(
                    run_id=run_id,
                    effect_id=state.identity_effect_id,
                )
                if state.identity_effect_id is not None
                else ()
            )
            if state.identity_effect_id is not None:
                for attempt in attempts:
                    self._verify_attempt_record_artifact(
                        uow,
                        attempt,
                        expected_effect_id=state.identity_effect_id,
                    )
                if bundle is not None:
                    self._verify_bundle_attempt_binding(bundle, attempts)
            view = P4RunView(
                run_id=run_id,
                conversation_id=state.conversation_id,
                revision=snapshot.revision,
                state=state,
                query=query,
                registry=registry,
                candidate_bundle=bundle,
                confirmed_molecule=confirmed,
                prepared_plan=plan,
                interrupt=interrupt,
                attempts=attempts,
                outbox=tuple(
                    {
                        "effect_id": str(item.effect_id),
                        "effect_type": item.effect_type,
                        "status": item.status.value,
                        "attempt_count": item.attempt_count,
                        "available_at_utc": format_utc(item.available_at_utc),
                        "next_retry_at": format_utc(item.available_at_utc)
                        if item.status is OutboxStatus.PENDING
                        else None,
                        "last_error_code": item.last_error_code,
                    }
                    for item in uow.outbox.list_for_run(run_id)
                ),
                diagnostics=(
                    (
                        "execution_not_implemented",
                        "execution_approval_required",
                    )
                    if state.phase is P4Phase.PLAN_READY
                    else ()
                ),
            )
            uow.commit()
            return view

    def create_worker(
        self,
        *,
        worker_id=None,
        lease_duration=timedelta(seconds=60),
    ):
        from orca_agent.infrastructure.worker import OutboxWorker

        handler = P4IdentityHandler(
            self.database_path,
            self.state_root,
            clock=self.clock,
            fake_adapter=self.fake_adapter,
            pubchem_adapter=self.pubchem_adapter,
        )
        readiness = _P4Readiness()

        def metadata_factory(*, uow, permit, completion, snapshot):
            return self._completion_metadata(
                uow=uow,
                permit=permit,
                completion=completion,
                snapshot=snapshot,
            )

        def retry_not_before_factory(*, uow, permit, completion, snapshot):
            attempt = P4RecordRepository(uow.connection).attempt_for_generation(
                run_id=permit.effect.run_id,
                effect_id=permit.effect.effect_id,
                generation=permit.generation,
                request_sequence=1,
            )
            return None if attempt is None else attempt.retry_not_before_utc

        def completion_factory():
            return EffectCompletionService(
                self.database_path,
                clock=self.clock,
                registry=P4_EFFECT_REGISTRY,
                max_attempts=self.max_attempts,
                completion_metadata_factory=metadata_factory,
                retry_not_before_factory=retry_not_before_factory,
            )

        return OutboxWorker(
            self.database_path,
            handler,
            clock=self.clock,
            worker_id=worker_id,
            lease_duration=lease_duration,
            max_attempts=self.max_attempts,
            registry=P4_EFFECT_REGISTRY,
            completion_service_factory=completion_factory,
            readiness_check=readiness,
        )

    def export_plan(self, run_id: RunId, *, format: str = "json") -> dict[str, object] | str:
        view = self.inspect(run_id)
        if view.prepared_plan is None:
            raise InvalidTransitionError("P4 run has no prepared plan")
        plan = view.prepared_plan
        if format == "json":
            return plan.model_dump(mode="json")
        if format != "md":
            raise ValueError("plan format must be json or md")
        steps = "\n".join(
            f"{index}. `{step.kind.value}` `{step.method_profile_id}` "
            f"(primitive `{step.primitive_id}`)"
            for index, step in enumerate(plan.proposal.steps, start=1)
        )
        return (
            "# P4 Prepared Plan\n\n"
            f"- Run: `{plan.run_id}`\n"
            f"- Plan hash: `{plan.plan_hash}`\n"
            f"- Protocol: `{plan.protocol_id}`\n"
            f"- Identity: `{plan.confirmed_molecule_id}`\n"
            "- Planning valid: `true`\n"
            "- Execution ready: `false`\n"
            "- Execution approved: `false`\n"
            "- Real scientific result: `false`\n"
            "- Initial 3D geometry: not generated in P4\n\n"
            "## Fixed steps\n\n"
            f"{steps}\n"
        )

    def _completion_metadata(self, *, uow, permit, completion, snapshot):
        if not isinstance(snapshot.state, P4WorkflowState):
            raise StateIntegrityError("P4 completion received a non-P4 run")
        records = P4RecordRepository(uow.connection)
        attempt = records.attempt_for_generation(
            run_id=permit.effect.run_id,
            effect_id=permit.effect.effect_id,
            generation=permit.generation,
            request_sequence=1,
        )
        if attempt is None:
            raise StateIntegrityError("P4 completion attempt is missing")
        self._verify_attempt_artifact(uow, attempt, permit)
        if attempt.status is not LookupStatus.SUCCEEDED:
            return {
                "resolution": "failed",
                "identity_error_code": attempt.error_code or "identity_lookup_failed",
            }
        entries = records.list_for_run(permit.effect.run_id)
        bundles = [
            value
            for record_type, value in ((item[1], item[2]) for item in entries)
            if record_type == "p4.candidate_bundle" and isinstance(value, CandidateBundle)
        ]
        matches = [
            bundle
            for bundle in bundles
            if bundle.winning_request.get("effect_id") == str(permit.effect.effect_id)
            and bundle.winning_request.get("generation") == permit.generation
            and bundle.winning_request.get("request_sequence") == 1
        ]
        if len(matches) != 1:
            raise StateIntegrityError("P4 completion candidate bundle is missing or ambiguous")
        bundle = matches[0]
        if not bundle.confirmable:
            return {
                "resolution": "failed",
                "identity_error_code": "protocol_unsupported",
            }
        interrupt_id = _deterministic_id(
            InterruptId, permit.effect.run_id, f"identity_confirmation:{permit.effect.effect_id}"
        )
        expires = self.clock.now_utc() + timedelta(hours=_CONFIRMATION_HOURS)
        request = {
            "interrupt_id": str(interrupt_id),
            "kind": "confirm_molecule_identity",
            "run_id": str(permit.effect.run_id),
            "query_id": str(snapshot.state.query_id),
            "query_hash": snapshot.state.query_hash,
            "effect_id": str(permit.effect.effect_id),
            "candidate_bundle_id": str(bundle.record_id),
            "candidate_bundle_hash": bundle.bundle_hash,
            "candidate_set_hash": bundle.candidate_set_hash,
            "expires_at_utc": format_utc(expires),
        }
        return {"resolution": "success", "confirmation_request": request}

    def _verify_attempt_artifact(self, uow, attempt: LookupAttempt, permit: DispatchPermit) -> None:
        self._verify_attempt_record_artifact(
            uow,
            attempt,
            expected_effect_id=permit.effect.effect_id,
        )

    def _verify_attempt_record_artifact(
        self,
        uow,
        attempt: LookupAttempt,
        *,
        expected_effect_id,
    ) -> None:
        if attempt.response_artifact_id is None:
            raise StateIntegrityError("P4 attempt has no response artifact")
        record = ArtifactRecordRepository(uow.connection).get(attempt.response_artifact_id)
        if record is None or record.run_id != attempt.run_id:
            raise StateIntegrityError("P4 response artifact is not owner-bound")
        envelope_bytes = ArtifactStore(self.state_root, clock=self.clock).read(record)
        try:
            envelope = ResponseEnvelope.model_validate_json(
                envelope_bytes.decode("utf-8"), strict=True
            )
        except (UnicodeDecodeError, ValueError, TypeError) as error:
            raise StateIntegrityError("P4 response envelope is invalid") from error
        if (
            envelope.run_id != attempt.run_id
            or envelope.effect_id != expected_effect_id
            or envelope.generation != attempt.generation
            or envelope.request_sequence != attempt.request_sequence
            or envelope.query_id != attempt.query_id
            or envelope.query_hash != attempt.query_hash
            or envelope.body_sha256 != attempt.response_body_sha256
            or envelope.envelope_hash != attempt.response_envelope_hash
        ):
            raise StateIntegrityError("P4 attempt is not bound to its response envelope")

    @staticmethod
    def _verify_bundle_attempt_binding(
        bundle: CandidateBundle,
        attempts: tuple[LookupAttempt, ...],
    ) -> None:
        winning = bundle.winning_request
        try:
            generation = int(winning["generation"])
            request_sequence = int(winning["request_sequence"])
            artifact_id = str(winning["response_artifact_id"])
            body_hash = str(winning["response_body_sha256"])
        except (KeyError, TypeError, ValueError) as error:
            raise StateIntegrityError("P4 candidate winning request is invalid") from error
        matches = tuple(
            attempt
            for attempt in attempts
            if attempt.generation == generation and attempt.request_sequence == request_sequence
        )
        if len(matches) != 1:
            raise StateIntegrityError("P4 candidate bundle attempt is missing or ambiguous")
        attempt = matches[0]
        if (
            winning.get("effect_id") != str(attempt.effect_id)
            or winning.get("query_hash") != attempt.query_hash
            or str(attempt.response_artifact_id) != artifact_id
            or attempt.response_body_sha256 != body_hash
            or any(
                candidate.source_artifact_id != attempt.response_artifact_id
                for candidate in bundle.candidates
            )
        ):
            raise StateIntegrityError("P4 candidate source is not bound to its attempt")

    def _validate_start(self, command: StartPlanningRun) -> str:
        from orca_agent.identity.ports import normalize_input

        if command.protocol_id != "ground_state_baseline_r2scan3c_v1":
            raise InvalidTransitionError("P4 protocol is unsupported")
        normalized = normalize_input(command.input_kind, command.raw_input)
        if command.input_kind is MoleculeInputKind.SMILES:
            if command.provider is not IdentityProvider.LOCAL:
                raise InvalidTransitionError("SMILES input must use the local provider")
        elif command.provider is IdentityProvider.LOCAL:
            raise InvalidTransitionError("name, CAS, and CID input require a provider")
        return normalized

    @staticmethod
    def _require_kernel(uow: SQLiteUnitOfWork) -> None:
        if any(
            item is None
            for item in (uow.runs, uow.events, uow.interrupts, uow.outbox, uow.command_receipts)
        ):
            raise StorageError("kernel repositories are unavailable")

    @staticmethod
    def _p4_state(snapshot: RunSnapshot) -> P4WorkflowState:
        if not isinstance(snapshot.state, P4WorkflowState):
            raise InvalidTransitionError("operation requires a schema-3 P4 run")
        return snapshot.state

    def _validate_confirmation_bindings(
        self, uow: SQLiteUnitOfWork, snapshot: RunSnapshot, command: ConfirmMoleculeIdentity
    ) -> None:
        state = self._p4_state(snapshot)
        if snapshot.revision != command.expected_revision:
            raise RevisionConflictError("P4 confirmation expected revision is stale")
        if state.conversation_id != command.conversation_id:
            raise InvalidTransitionError("conversation does not own the P4 workflow")
        if state.pending_interrupt_id != command.interrupt_id:
            raise InvalidTransitionError("P4 confirmation interrupt does not match state")
        if (
            command.query_id != state.query_id
            or command.query_hash != state.query_hash
            or command.candidate_bundle_id != state.candidate_bundle_id
            or command.candidate_bundle_hash != state.candidate_bundle_hash
            or command.candidate_set_hash != state.candidate_set_hash
        ):
            raise InvalidTransitionError("P4 confirmation record binding is invalid")
        interrupt = uow.interrupts.get(command.interrupt_id)
        if interrupt is None or interrupt.kind != "confirm_molecule_identity":
            raise InvalidTransitionError("P4 confirmation interrupt kind is invalid")
        expected = interrupt.payload
        for key, actual in (
            ("run_id", str(command.run_id)),
            ("query_id", str(command.query_id)),
            ("query_hash", command.query_hash),
            ("effect_id", str(state.identity_effect_id)),
            ("candidate_bundle_id", str(command.candidate_bundle_id)),
            ("candidate_bundle_hash", command.candidate_bundle_hash),
            ("candidate_set_hash", command.candidate_set_hash),
        ):
            if expected.get(key) != actual:
                raise StateIntegrityError("P4 confirmation interrupt projection is inconsistent")

    @staticmethod
    def _selected_candidate(bundle: CandidateBundle, command: ConfirmMoleculeIdentity):
        matches = tuple(
            candidate
            for candidate in bundle.candidates
            if candidate.candidate_id == command.candidate_id
        )
        if len(matches) != 1 or matches[0].candidate_hash != command.candidate_hash:
            raise InvalidTransitionError("P4 selected candidate binding is invalid")
        return matches[0]

    def _verify_candidate_source(
        self,
        uow,
        run_id: RunId,
        command: ConfirmMoleculeIdentity,
        candidate,
        *,
        bundle: CandidateBundle,
        expected_effect_id,
    ) -> None:
        if candidate.source_artifact_id is None:
            raise StateIntegrityError("P4 candidate has no source artifact")
        artifact = ArtifactRecordRepository(uow.connection).get(candidate.source_artifact_id)
        if artifact is None or artifact.run_id != run_id:
            raise StateIntegrityError("P4 candidate source artifact is not owner-bound")
        envelope_bytes = ArtifactStore(self.state_root, clock=self.clock).read(artifact)
        try:
            envelope = ResponseEnvelope.model_validate_json(
                envelope_bytes.decode("utf-8"), strict=True
            )
        except (UnicodeDecodeError, ValueError, TypeError) as error:
            raise StateIntegrityError("P4 candidate source envelope is invalid") from error
        winning_request = bundle.winning_request
        try:
            expected_generation = int(winning_request["generation"])
            expected_sequence = int(winning_request["request_sequence"])
        except (KeyError, TypeError, ValueError) as error:
            raise StateIntegrityError("P4 candidate winning request is invalid") from error
        if (
            envelope.run_id != run_id
            or envelope.query_id != command.query_id
            or envelope.query_hash != command.query_hash
            or envelope.effect_id != expected_effect_id
            or envelope.generation != expected_generation
            or envelope.request_sequence != expected_sequence
            or winning_request.get("response_artifact_id") != str(artifact.artifact_id)
            or winning_request.get("response_envelope_id") != str(envelope.record_id)
            or winning_request.get("response_body_sha256") != envelope.body_sha256
            or winning_request.get("effect_id") != str(expected_effect_id)
            or winning_request.get("query_hash") != command.query_hash
            or winning_request.get("provider") != candidate.provider.value
            or envelope.body_sha256 != candidate.source_body_sha256
            or envelope.provider != candidate.provider
        ):
            raise StateIntegrityError("P4 candidate source binding is invalid")

    @staticmethod
    def _confirmation_response(command: ConfirmMoleculeIdentity) -> dict[str, object]:
        return {
            "decision": command.decision.value,
            "query_id": str(command.query_id),
            "query_hash": command.query_hash,
            "candidate_bundle_id": str(command.candidate_bundle_id),
            "candidate_bundle_hash": command.candidate_bundle_hash,
            "candidate_set_hash": command.candidate_set_hash,
            "candidate_id": command.candidate_id,
            "candidate_hash": command.candidate_hash,
        }

    def _persist_p4_event(
        self,
        *,
        uow: SQLiteUnitOfWork,
        current: RunSnapshot,
        command_id: CommandId,
        command_hash: str,
        command_type: CommandType,
        event_type: EventType,
        payload: dict[str, object],
        occurred_at_utc: datetime,
        event_id: EventId | None = None,
    ) -> tuple[ApplicationResult, P4Transition, P4KernelEvent]:
        event, transition, result = self._build_p4_event(
            current=current,
            events=uow.events,
            event_id=event_id,
            command_id=command_id,
            command_hash=command_hash,
            command_type=command_type,
            event_type=event_type,
            payload=payload,
            occurred_at_utc=occurred_at_utc,
        )
        uow.events.append(event, command_hash=command_hash)
        uow.outbox.register_effects(
            event=event,
            run_id=current.run_id,
            effects=transition.effects,
            available_at_utc=occurred_at_utc,
            created_at_utc=occurred_at_utc,
        )
        uow.interrupts.apply_operations(event=event, operations=transition.interrupt_operations)
        if transition.next_state.status.is_terminal:
            uow.outbox.cancel_pending_for_run(run_id=current.run_id, now=occurred_at_utc)
        if not uow.runs.compare_and_swap(
            run_id=current.run_id,
            expected_revision=current.revision,
            state=transition.next_state,
            event_id=event.event_id,
            updated_at_utc=occurred_at_utc,
        ):
            raise RevisionConflictError("P4 expected revision was not current")
        uow.command_receipts.append_event(event=event, recorded_at_utc=occurred_at_utc)
        return result, transition, event

    @staticmethod
    def _append_workflow_state(uow, run_id, state, event_id, now) -> None:
        P4RecordRepository(uow.connection).append_p4(
            run_id=run_id,
            record_type="p4.workflow_state",
            record=state,
            created_at_utc=now,
            source_event_id=event_id,
        )

    @staticmethod
    def _build_p4_event(
        *,
        current: RunSnapshot | None,
        events,
        event_id: EventId | None,
        command_id: CommandId,
        command_hash: str,
        command_type: CommandType,
        event_type: EventType,
        payload: dict[str, object],
        occurred_at_utc: datetime,
    ) -> tuple[P4KernelEvent, P4Transition, ApplicationResult]:
        previous_hash = "0" * 64
        prior_state = None
        sequence = 1
        expected_revision = 0
        run_id = RunId(str(payload.get("run_id"))) if current is None else current.run_id
        if current is not None:
            prior_state = current.state
            if not isinstance(prior_state, P4WorkflowState):
                raise StateIntegrityError("P4 event requires a schema-3 state")
            previous = events.get(current.last_event_id)
            if not isinstance(previous, P4KernelEvent):
                raise StateIntegrityError("P4 previous event is missing")
            previous_hash = previous.event_hash
            sequence = current.revision + 1
            expected_revision = current.revision
        actual_event_id = event_id or new_id(EventId)
        placeholder = ApplicationResult.accepted_result(
            code=event_type.value,
            run_id=run_id,
            revision=sequence,
            status=RunStatus.CREATED
            if prior_state is None
            else RunStatus(prior_state.status.value),
            event_id=actual_event_id,
        )
        candidate = P4KernelEvent.create(
            event_id=actual_event_id,
            command_id=command_id,
            command_type=command_type,
            run_id=run_id,
            sequence_no=sequence,
            expected_revision=expected_revision,
            event_type=event_type,
            payload=payload,
            result=placeholder,
            occurred_at_utc=occurred_at_utc,
            command_hash=command_hash,
            previous_event_hash=previous_hash,
        )
        transition = reduce_p4_event(prior_state, candidate)
        result = expected_p4_application_result(
            prior_state=prior_state,
            event=candidate,
            transition=transition,
        )
        event = P4KernelEvent.create(
            event_id=candidate.event_id,
            command_id=candidate.command_id,
            command_type=candidate.command_type,
            run_id=candidate.run_id,
            sequence_no=candidate.sequence_no,
            expected_revision=candidate.expected_revision,
            event_type=candidate.event_type,
            payload=payload,
            result=result,
            occurred_at_utc=candidate.occurred_at_utc,
            recorded_at_utc=candidate.recorded_at_utc,
            command_hash=candidate.command_hash,
            previous_event_hash=candidate.previous_event_hash,
        )
        return event, transition, result

    def _result_for_receipt(self, uow, command, receipt: CommandReceipt) -> P4Result:
        expected_type = (
            CommandType.CREATE_RUN
            if isinstance(command, StartPlanningRun)
            else CommandType.RESOLVE_INTERRUPT
            if isinstance(command, ConfirmMoleculeIdentity)
            else CommandType.CANCEL_RUN
        )
        if (
            receipt.command_id != command.command_id
            or receipt.command_hash != command.command_hash()
            or receipt.command_type is not expected_type
            or receipt.run_id != command.run_id
            or receipt.binding_kind.value != "event"
        ):
            raise DuplicateCommandConflictError("P4 command receipt binding differs")
        event = uow.events.get(receipt.result_event_id)
        if (
            not isinstance(event, P4KernelEvent)
            or event.command_id != command.command_id
            or event.command_hash != command.command_hash()
            or event.command_type is not expected_type
        ):
            raise StateIntegrityError("P4 command result event is invalid")
        try:
            result = ApplicationResult.model_validate_json(
                json.dumps(thaw_json(event.result), ensure_ascii=False), strict=True
            )
        except (TypeError, ValueError) as error:
            raise StateIntegrityError("P4 command result is invalid") from error
        snapshot = uow.runs.get_verified(
            command.run_id,
            uow.events,
            interrupts=uow.interrupts,
            outbox=uow.outbox,
        )
        return _p4_result_from_application(
            result,
            snapshot.state if isinstance(snapshot.state, P4WorkflowState) else None,
        )

    def _safe_reject(
        self, run_id: RunId, conversation_id: ConversationId, error: Exception
    ) -> P4Result:
        revision = 0
        phase = P4Phase.FAILED
        status = RunStatus.FAILED
        try:
            with SQLiteUnitOfWork(self.database_path, clock=self.clock) as uow:
                snapshot = uow.runs.get(run_id) if uow.runs is not None else None
                if snapshot is not None:
                    revision = snapshot.revision
                    status = RunStatus(snapshot.state.status.value)
                    if isinstance(snapshot.state, P4WorkflowState):
                        phase = snapshot.state.phase
                if uow.connection is not None and uow.connection.in_transaction:
                    uow.rollback()
        except Exception:
            pass
        if isinstance(error, ApplicationError):
            code = error.code
            details = dict(error.details)
        elif isinstance(error, DomainError):
            code = "state_integrity_error"
            details = {}
        elif isinstance(error, sqlite3.OperationalError):
            code = "storage_busy" if "locked" in str(error).casefold() else "storage_error"
            details = {}
        else:
            code = "p4_request_rejected"
            details = {"error": type(error).__name__}
        return P4Result(
            accepted=False,
            code=code,
            run_id=run_id,
            revision=revision,
            status=status,
            phase=phase,
            conversation_id=conversation_id,
            details=details,
        )


def _deterministic_id(identifier_type, run_id: RunId, role: str):
    return identifier_type(
        f"{identifier_type.prefix}_{uuid.uuid5(_P4_OBJECT_NAMESPACE, f'{run_id}:{role}').hex}"
    )


def _p4_result_from_application(
    result: ApplicationResult, state: P4WorkflowState | None
) -> P4Result:
    if state is None:
        raise StateIntegrityError("P4 result needs its verified workflow state")
    details = thaw_json(result.details)
    if not isinstance(details, dict):
        details = {}
    raw_phase = details.get("phase")
    phase = state.phase
    if isinstance(raw_phase, str) and raw_phase != phase.value:
        raise StateIntegrityError("P4 result phase differs from verified state")
    return P4Result(
        accepted=result.accepted,
        code=result.code,
        run_id=result.run_id,
        revision=result.revision,
        status=result.status,
        phase=phase,
        conversation_id=state.conversation_id,
        details=details,
    )


__all__ = ["P4ApplicationService", "P4RunView"]
