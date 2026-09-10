"""Atomic one-shot launch authority shared by the fake gateway and LocalRunner."""

from datetime import UTC, datetime

from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import EffectId, ExecutionId, WorkerId
from orca_agent.domain.p5 import P5ApprovalGrant, P5ExecutionBinding, P5Phase
from orca_agent.infrastructure.outbox import DispatchPermit
from orca_agent.infrastructure.p3_records import ActionRepository
from orca_agent.infrastructure.p5_records import LocalJobRepository, P5RecordRepository
from orca_agent.infrastructure.p7_records import P7RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork


def permit_fields(permit):
    if not isinstance(permit, DispatchPermit):
        raise ValueError("a shared dispatch permit is required")
    return {
        "effect_id": str(permit.effect.effect_id),
        "worker_id": str(permit.worker_id),
        "generation": permit.generation,
        "run_revision": permit.run_revision,
        "policy_version": permit.policy_version,
    }


def consume_ticket(root, spec, *, now=None):
    """Commit the sole launch right after checking permit, approval and cancellation."""
    now = now or datetime.now(UTC)
    with SQLiteUnitOfWork(root) as uow:
        uow.begin()
        jobs = LocalJobRepository(uow.connection)
        job = jobs.get_by_execution(ExecutionId(spec["execution_id"]))
        if (
            job is None
            or job.launch_token != spec["launch_token"]
            or job.binding_hash != spec["binding_hash"]
        ):
            raise ValueError("launch ticket identity mismatch")
        fields = spec["permit"]
        effect = uow.outbox.get_required(EffectId(fields["effect_id"]))
        permit = DispatchPermit(
            effect,
            WorkerId(fields["worker_id"]),
            fields["generation"],
            fields["run_revision"],
            fields["policy_version"],
        )
        uow.outbox.validate_dispatch_permit(permit=permit, now=now)
        state = uow.runs.get_verified(
            job.run_id, uow.events, interrupts=uow.interrupts, outbox=uow.outbox
        ).state
        grant = P5RecordRepository(uow.connection).latest_p5(
            run_id=job.run_id, record_type="p5.approval_grant", model_type=P5ApprovalGrant
        )
        binding = P5RecordRepository(uow.connection).get_exact_p5(
            run_id=job.run_id,
            record_id=job.binding_id,
            record_type="p5.execution_binding",
            model_type=P5ExecutionBinding,
        )
        if (
            effect.run_id != job.run_id
            or effect.effect_type != "external.p5.launch_orca"
            or effect.payload.get("action_id") != str(job.action_id)
            or effect.payload.get("binding_hash") != job.binding_hash
            or job.launch_generation != permit.generation
            or state.phase is not P5Phase.DISPATCH_PENDING
            or state.current_action_id != job.action_id
            or state.current_execution_id != job.execution_id
            or state.cancel_requested
            or job.cancel_requested
            or job.deadline_utc <= now
            or grant is None
            or grant[1].grant_id != state.current_grant_id
            or grant[1].binding_hash != job.binding_hash
            or grant[1].expires_at_utc <= now
        ):
            raise ValueError("launch permit, approval, deadline or cancellation is invalid")
        if binding is None:
            raise ValueError("launch P5 binding is missing")
        if binding.parent_authorization_id is not None:
            authorization = P7RecordRepository(uow.connection).get_execution_authorization(
                binding.parent_authorization_id
            )
            if (
                authorization is None
                or authorization.status == "revoked"
                or authorization.expires_at_utc <= now
                or authorization.authorization_hash != binding.parent_authorization_hash
                or authorization.prepared_calculation_id != binding.preparation_snapshot_id
                or authorization.prepared_snapshot_hash != binding.preparation_snapshot_hash
                or authorization.credential_for(binding.node_id)
                != binding.parent_authorization_credential
                or grant[1].parent_authorization_id != binding.parent_authorization_id
                or grant[1].parent_authorization_hash != binding.parent_authorization_hash
                or grant[1].parent_authorization_credential
                != binding.parent_authorization_credential
            ):
                raise ValueError("parent execution authorization is missing or mismatched")
        elif grant[1].parent_authorization_id is not None:
            raise ValueError("approval grant carries an unexpected parent authorization")
        ledger = ActionRepository(uow.connection).get(job.action_id)
        if (
            ledger is None
            or ledger.run_id != job.run_id
            or ledger.conversation_id != state.conversation_id
            or ledger.action.action_hash != grant[1].action_hash
            or ledger.action.primitive.parameters.get("binding_hash") != job.binding_hash
            or ledger.approval_grant_id != grant[1].grant_id
            or sha256_hex(ledger.action.execution_envelope) != grant[1].envelope_hash
            or sha256_hex(ledger.action.budget) != grant[1].budget_hash
        ):
            raise ValueError("validated action ledger and approval differ")
        if not jobs.consume_launch_ticket(
            execution_id=job.execution_id, generation=permit.generation, now=now
        ):
            raise ValueError("launch ticket already consumed; reconcile without restarting")
        uow.commit()
