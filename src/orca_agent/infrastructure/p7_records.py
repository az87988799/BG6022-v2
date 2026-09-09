"""Durable P7 projections backed by the shared SQLite state root."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from pydantic import ValidationError

from orca_agent.application.errors import StateIntegrityError
from orca_agent.domain.canonical import canonical_json_bytes
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p7_conversation import (
    ContextSnapshot,
    ConversationState,
    ResponseRecord,
    ResponseSource,
    TurnInterpretation,
    TurnRecord,
    TurnStatus,
)
from orca_agent.domain.p7_planning import PlanningRecord
from orca_agent.domain.p7_task import (
    CalculationPlan,
    CalculationRequest,
    DeliveryRecord,
    HandoffRecord,
    OutputSpec,
    PendingActionRecord,
    PlanValidation,
    StopReason,
    TaskPhase,
    TaskRecord,
)
from orca_agent.infrastructure.clock import format_utc, parse_utc


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _load_json(value: object, *, what: str) -> object:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError) as error:
        raise StateIntegrityError(f"stored {what} JSON is invalid") from error


class P7RecordRepository:
    """Read/write façade; all state-changing callers supply their own UoW."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # Conversation -----------------------------------------------------
    def insert_conversation(self, state: ConversationState) -> None:
        self.connection.execute(
            "INSERT INTO p7_conversations("
            "conversation_id, schema_version, engine_version, revision, status, model_calls, "
            "model_call_budget, current_turn_id, active_task_id, state_json, state_hash, "
            "created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(state.conversation_id),
                state.schema_version,
                state.engine_version,
                state.revision,
                state.status.value,
                state.model_calls,
                state.model_call_budget,
                state.current_turn_id,
                state.active_task_id,
                _json(state.model_dump(mode="json")),
                state.state_hash,
                format_utc(state.created_at_utc),
                format_utc(state.updated_at_utc),
            ),
        )

    def get_conversation(self, conversation_id: str) -> ConversationState | None:
        row = self.connection.execute(
            "SELECT state_json, state_hash FROM p7_conversations WHERE conversation_id = ?",
            (str(conversation_id),),
        ).fetchone()
        if row is None:
            return None
        data = _load_json(row[0], what="conversation")
        if not isinstance(data, dict) or data.get("state_hash") != row[1]:
            raise StateIntegrityError("conversation state hash is inconsistent")
        try:
            return ConversationState.model_validate_json(str(row[0]), strict=True)
        except (ValidationError, TypeError, ValueError) as error:
            raise StateIntegrityError("stored conversation state is invalid") from error

    def update_conversation(self, state: ConversationState, *, expected_revision: int) -> bool:
        cursor = self.connection.execute(
            "UPDATE p7_conversations SET revision=?, status=?, model_calls=?, model_call_budget=?, "
            "state_json=?, state_hash=?, current_turn_id=?, active_task_id=?, "
            "updated_at_utc=? WHERE conversation_id=? AND revision=?",
            (
                state.revision,
                state.status.value,
                state.model_calls,
                state.model_call_budget,
                _json(state.model_dump(mode="json")),
                state.state_hash,
                state.current_turn_id,
                state.active_task_id,
                format_utc(state.updated_at_utc),
                str(state.conversation_id),
                expected_revision,
            ),
        )
        return cursor.rowcount == 1

    def list_conversation_ids(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT conversation_id FROM p7_conversations ORDER BY created_at_utc"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    # Turns ------------------------------------------------------------
    def insert_turn(self, turn: TurnRecord) -> None:
        self.connection.execute(
            "INSERT INTO p7_turns(turn_id, conversation_id, sequence_no, status, user_text, "
            "context_json, context_hash, interpretation_json, interpretation_hash, response_json, "
            "response_hash, task_ids_json, created_at_utc, updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._turn_values(turn),
        )

    @staticmethod
    def _turn_values(turn: TurnRecord) -> tuple[object, ...]:
        interpretation = (
            None if turn.interpretation is None else turn.interpretation.model_dump(mode="json")
        )
        response = {
            "text": turn.response_text,
            "payload": thaw_json(turn.response_payload),
            "source": turn.response_source.value,
            "error_code": turn.error_code,
            "record_hash": turn.record_hash,
        }
        return (
            turn.turn_id,
            str(turn.conversation_id),
            turn.sequence_no,
            turn.status.value,
            turn.user_text,
            _json(turn.context_snapshot.model_dump(mode="json")),
            turn.context_snapshot.snapshot_hash,
            None if interpretation is None else _json(interpretation),
            None if interpretation is None else sha256_hex(interpretation),
            _json(response),
            sha256_hex(response),
            _json(list(turn.task_ids)),
            format_utc(turn.created_at_utc),
            format_utc(turn.updated_at_utc),
        )

    def get_turn(self, conversation_id: str, turn_id: str) -> TurnRecord | None:
        row = self.connection.execute(
            "SELECT turn_id, conversation_id, sequence_no, status, user_text, context_json, "
            "context_hash, interpretation_json, response_json, task_ids_json, created_at_utc, "
            "updated_at_utc FROM p7_turns WHERE conversation_id=? AND turn_id=?",
            (str(conversation_id), str(turn_id)),
        ).fetchone()
        return None if row is None else self._turn_from_row(row)

    def latest_turn(self, conversation_id: str) -> TurnRecord | None:
        row = self.connection.execute(
            "SELECT turn_id, conversation_id, sequence_no, status, user_text, context_json, "
            "context_hash, interpretation_json, response_json, task_ids_json, created_at_utc, "
            "updated_at_utc FROM p7_turns WHERE conversation_id=? "
            "ORDER BY sequence_no DESC LIMIT 1",
            (str(conversation_id),),
        ).fetchone()
        return None if row is None else self._turn_from_row(row)

    def list_turns(self, conversation_id: str, *, limit: int = 12) -> tuple[TurnRecord, ...]:
        rows = self.connection.execute(
            "SELECT turn_id, conversation_id, sequence_no, status, user_text, context_json, "
            "context_hash, interpretation_json, response_json, task_ids_json, created_at_utc, "
            "updated_at_utc FROM p7_turns WHERE conversation_id=? "
            "ORDER BY sequence_no DESC LIMIT ?",
            (str(conversation_id), limit),
        ).fetchall()
        return tuple(reversed(tuple(self._turn_from_row(row) for row in rows)))

    def update_turn(self, turn: TurnRecord) -> None:
        cursor = self.connection.execute(
            "UPDATE p7_turns SET status=?, context_json=?, context_hash=?, interpretation_json=?, "
            "interpretation_hash=?, response_json=?, response_hash=?, task_ids_json=?, "
            "updated_at_utc=? "
            "WHERE turn_id=? AND conversation_id=?",
            (
                turn.status.value,
                _json(turn.context_snapshot.model_dump(mode="json")),
                turn.context_snapshot.snapshot_hash,
                None
                if turn.interpretation is None
                else _json(turn.interpretation.model_dump(mode="json")),
                None
                if turn.interpretation is None
                else sha256_hex(turn.interpretation.model_dump(mode="json")),
                _json(
                    {
                        "text": turn.response_text,
                        "payload": thaw_json(turn.response_payload),
                        "source": turn.response_source.value,
                        "error_code": turn.error_code,
                        "record_hash": turn.record_hash,
                    }
                ),
                sha256_hex(
                    {
                        "text": turn.response_text,
                        "payload": thaw_json(turn.response_payload),
                        "source": turn.response_source.value,
                        "error_code": turn.error_code,
                        "record_hash": turn.record_hash,
                    }
                ),
                _json(list(turn.task_ids)),
                format_utc(turn.updated_at_utc),
                turn.turn_id,
                str(turn.conversation_id),
            ),
        )
        if cursor.rowcount != 1:
            raise StateIntegrityError("P7 turn disappeared during update")

    def _turn_from_row(self, row: sqlite3.Row) -> TurnRecord:
        context = ContextSnapshot.model_validate_json(str(row[5]), strict=True)
        interpretation = (
            None
            if row[7] is None
            else TurnInterpretation.model_validate_json(str(row[7]), strict=True)
        )
        response = _load_json(row[8], what="turn response")
        if not isinstance(response, dict):
            raise StateIntegrityError("turn response is not an object")
        task_ids_value = _load_json(row[9], what="turn task IDs")
        if not isinstance(task_ids_value, list):
            raise StateIntegrityError("turn task IDs are not an array")
        created = parse_utc(str(row[10]))
        updated = parse_utc(str(row[11]))
        values = {
            "turn_id": str(row[0]),
            "conversation_id": str(row[1]),
            "sequence_no": int(row[2]),
            "status": TurnStatus(str(row[3])),
            "user_text": str(row[4]),
            "context_snapshot": context,
            "interpretation": interpretation,
            "response_text": response.get("text"),
            "response_payload": response.get("payload", {}),
            "response_source": ResponseSource(str(response.get("source", "program"))),
            "task_ids": tuple(str(item) for item in task_ids_value),
            "error_code": response.get("error_code"),
            "created_at_utc": created,
            "updated_at_utc": updated,
        }
        return TurnRecord.create(**values)

    # Tasks ------------------------------------------------------------
    def insert_task(self, task: TaskRecord) -> None:
        request = None if task.request is None else task.request.model_dump(mode="json")
        plan = None if task.plan is None else task.plan.model_dump(mode="json")
        validation = None if task.validation is None else task.validation.model_dump(mode="json")
        delivery_spec = (
            None
            if task.delivery_output_spec is None
            else task.delivery_output_spec.model_dump(mode="json")
        )
        self.connection.execute(
            "INSERT INTO p7_tasks(task_id, conversation_id, alias, revision, state, stop_reason, "
            "request_json, request_hash, plan_json, plan_hash, validation_json, validation_hash, "
            "accepted_plan_hash, accepted_output_spec_hash, delivery_output_spec_json, "
            "delivery_output_spec_hash, p4_run_id, p5_run_id, p6_run_id, clarification_count, "
            "no_progress_count, current_delivery_id, created_at_utc, updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.task_id,
                task.conversation_id,
                task.alias,
                task.revision,
                task.state.value,
                None if task.stop_reason is None else task.stop_reason.value,
                None if request is None else _json(request),
                None if task.request is None else task.request.request_hash,
                None if plan is None else _json(plan),
                None if task.plan is None else task.plan.plan_hash,
                None if validation is None else _json(validation),
                None if task.validation is None else task.validation.validation_hash,
                task.accepted_plan_hash,
                task.accepted_output_spec_hash,
                None if delivery_spec is None else _json(delivery_spec),
                None
                if task.delivery_output_spec is None
                else task.delivery_output_spec.output_spec_hash,
                task.p4_run_id,
                task.p5_run_id,
                task.p6_run_id,
                task.clarification_count,
                task.no_progress_count,
                task.current_delivery_id,
                format_utc(task.created_at_utc),
                format_utc(task.updated_at_utc),
            ),
        )

    def get_task(self, conversation_id: str, task_id: str) -> TaskRecord | None:
        row = self.connection.execute(
            "SELECT * FROM p7_tasks WHERE conversation_id=? AND task_id=?",
            (str(conversation_id), str(task_id)),
        ).fetchone()
        return None if row is None else self._task_from_row(row)

    def get_task_by_alias(self, conversation_id: str, alias: str) -> TaskRecord | None:
        row = self.connection.execute(
            "SELECT * FROM p7_tasks WHERE conversation_id=? AND alias=?",
            (str(conversation_id), alias),
        ).fetchone()
        return None if row is None else self._task_from_row(row)

    def list_tasks(self, conversation_id: str) -> tuple[TaskRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM p7_tasks WHERE conversation_id=? ORDER BY created_at_utc, task_id",
            (str(conversation_id),),
        ).fetchall()
        return tuple(self._task_from_row(row) for row in rows)

    def update_task(self, task: TaskRecord, *, expected_revision: int) -> bool:
        request = None if task.request is None else task.request.model_dump(mode="json")
        plan = None if task.plan is None else task.plan.model_dump(mode="json")
        validation = None if task.validation is None else task.validation.model_dump(mode="json")
        delivery_spec = (
            None
            if task.delivery_output_spec is None
            else task.delivery_output_spec.model_dump(mode="json")
        )
        cursor = self.connection.execute(
            "UPDATE p7_tasks SET revision=?, state=?, stop_reason=?, "
            "request_json=?, request_hash=?, "
            "plan_json=?, plan_hash=?, validation_json=?, validation_hash=?, accepted_plan_hash=?, "
            "accepted_output_spec_hash=?, delivery_output_spec_json=?, "
            "delivery_output_spec_hash=?, "
            "p4_run_id=?, p5_run_id=?, p6_run_id=?, clarification_count=?, no_progress_count=?, "
            "current_delivery_id=?, updated_at_utc=? "
            "WHERE task_id=? AND conversation_id=? AND revision=?",
            (
                task.revision,
                task.state.value,
                None if task.stop_reason is None else task.stop_reason.value,
                None if request is None else _json(request),
                None if task.request is None else task.request.request_hash,
                None if plan is None else _json(plan),
                None if task.plan is None else task.plan.plan_hash,
                None if validation is None else _json(validation),
                None if task.validation is None else task.validation.validation_hash,
                task.accepted_plan_hash,
                task.accepted_output_spec_hash,
                None if delivery_spec is None else _json(delivery_spec),
                None
                if task.delivery_output_spec is None
                else task.delivery_output_spec.output_spec_hash,
                task.p4_run_id,
                task.p5_run_id,
                task.p6_run_id,
                task.clarification_count,
                task.no_progress_count,
                task.current_delivery_id,
                format_utc(task.updated_at_utc),
                task.task_id,
                task.conversation_id,
                expected_revision,
            ),
        )
        return cursor.rowcount == 1

    def _task_from_row(self, row: sqlite3.Row) -> TaskRecord:
        def optional_model(index: int, model_type):
            return (
                None
                if row[index] is None
                else model_type.model_validate_json(str(row[index]), strict=True)
            )

        # SELECT * order is fixed by the migration statement above.
        return TaskRecord(
            task_id=str(row[0]),
            conversation_id=str(row[1]),
            alias=str(row[2]),
            revision=int(row[3]),
            state=TaskPhase(str(row[4])),
            stop_reason=None if row[5] is None else StopReason(str(row[5])),
            request=optional_model(6, CalculationRequest),
            plan=optional_model(8, CalculationPlan),
            validation=optional_model(10, PlanValidation),
            accepted_plan_hash=row[12],
            accepted_output_spec_hash=row[13],
            delivery_output_spec=optional_model(14, OutputSpec),
            p4_run_id=row[16],
            p5_run_id=row[17],
            p6_run_id=row[18],
            clarification_count=int(row[19]),
            no_progress_count=int(row[20]),
            current_delivery_id=row[21],
            created_at_utc=parse_utc(str(row[22])),
            updated_at_utc=parse_utc(str(row[23])),
        )

    # Planning evidence ---------------------------------------------
    def insert_planning_record(self, record: PlanningRecord) -> None:
        payload = record.model_dump(mode="json")
        self.connection.execute(
            "INSERT INTO p7_plan_proposals("
            "proposal_id, conversation_id, task_id, source_turn_id, task_revision, action, "
            "request_hash, compiled_plan_hash, compiled_protocol_id, source_task_id, "
            "external_opt_result_id, validation_status, record_json, record_hash, created_at_utc"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.proposal_id,
                record.conversation_id,
                record.task_id,
                record.source_turn_id,
                record.task_revision,
                record.action,
                record.request_hash,
                record.compiled_plan_hash,
                record.compiled_protocol_id,
                record.source_task_id,
                record.external_opt_result_id,
                record.validation_status.value,
                _json(payload),
                record.record_hash,
                format_utc(record.created_at_utc),
            ),
        )

    def get_planning_record(self, proposal_id: str) -> PlanningRecord | None:
        row = self.connection.execute(
            "SELECT record_json, record_hash FROM p7_plan_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        return None if row is None else self._planning_from_row(row)

    def get_planning_record_for_task(
        self, task_id: str, task_revision: int
    ) -> PlanningRecord | None:
        """Resolve the exact task revision; never fall back to the latest row."""

        row = self.connection.execute(
            "SELECT record_json, record_hash FROM p7_plan_proposals "
            "WHERE task_id=? AND task_revision=?",
            (task_id, task_revision),
        ).fetchone()
        return None if row is None else self._planning_from_row(row)

    def get_planning_record_for_plan(
        self, task_id: str, request_hash: str, compiled_plan_hash: str
    ) -> PlanningRecord | None:
        """Load the immutable proposal that produced one exact compiled plan."""

        row = self.connection.execute(
            "SELECT record_json, record_hash FROM p7_plan_proposals "
            "WHERE task_id=? AND request_hash=? AND compiled_plan_hash=?",
            (task_id, request_hash, compiled_plan_hash),
        ).fetchone()
        return None if row is None else self._planning_from_row(row)

    def list_planning_records(
        self,
        *,
        task_id: str | None = None,
        conversation_id: str | None = None,
    ) -> tuple[PlanningRecord, ...]:
        query = "SELECT record_json, record_hash FROM p7_plan_proposals WHERE 1=1"
        values: list[object] = []
        if task_id is not None:
            query += " AND task_id=?"
            values.append(task_id)
        if conversation_id is not None:
            query += " AND conversation_id=?"
            values.append(conversation_id)
        query += " ORDER BY created_at_utc, proposal_id"
        rows = self.connection.execute(query, tuple(values)).fetchall()
        return tuple(self._planning_from_row(row) for row in rows)

    @staticmethod
    def _planning_from_row(row: sqlite3.Row) -> PlanningRecord:
        data = _load_json(row[0], what="planning record")
        if not isinstance(data, dict):
            raise StateIntegrityError("stored planning record is not an object")
        if data.get("record_hash") != row[1]:
            raise StateIntegrityError("planning record hash is inconsistent")
        try:
            return PlanningRecord.model_validate_json(_json(data), strict=True)
        except (ValidationError, TypeError, ValueError) as error:
            raise StateIntegrityError("stored planning record is invalid") from error

    # Links, pending actions, handoffs, responses --------------------
    def ensure_task_link(
        self, *, conversation_id: str, task_id: str, link_kind: str, now: datetime
    ) -> None:
        link_shape = {
            "conversation_id": conversation_id,
            "task_id": task_id,
            "link_kind": link_kind,
        }
        link_id = f"link_{sha256_hex(link_shape)[:32]}"
        self.connection.execute(
            "INSERT OR IGNORE INTO p7_task_links("
            "link_id, conversation_id, task_id, link_kind, created_at_utc"
            ") VALUES (?, ?, ?, ?, ?)",
            (link_id, conversation_id, task_id, link_kind, format_utc(now)),
        )

    def insert_pending(self, pending: PendingActionRecord) -> None:
        self.connection.execute(
            "INSERT INTO p7_pending_actions("
            "token, conversation_id, task_id, action_type, target_id, "
            "expected_revision, content_hash, payload_json, status, decision, "
            "created_at_utc, consumed_at_utc"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pending.token,
                pending.conversation_id,
                pending.task_id,
                pending.action_type,
                pending.target_id,
                pending.expected_revision,
                pending.content_hash,
                _json(thaw_json(pending.payload)),
                pending.status,
                pending.decision,
                format_utc(pending.created_at_utc),
                None if pending.consumed_at_utc is None else format_utc(pending.consumed_at_utc),
            ),
        )

    def get_pending(self, conversation_id: str, token: str) -> PendingActionRecord | None:
        row = self.connection.execute(
            "SELECT token, conversation_id, task_id, action_type, target_id, "
            "expected_revision, content_hash, payload_json, status, decision, "
            "created_at_utc, consumed_at_utc "
            "FROM p7_pending_actions WHERE conversation_id=? AND token=?",
            (str(conversation_id), token),
        ).fetchone()
        if row is None:
            return None
        values = {
            "token": str(row[0]),
            "conversation_id": str(row[1]),
            "task_id": None if row[2] is None else str(row[2]),
            "action_type": str(row[3]),
            "target_id": str(row[4]),
            "expected_revision": int(row[5]),
            "content_hash": str(row[6]),
            "payload": _load_json(row[7], what="pending action payload"),
            "status": str(row[8]),
            "decision": None if row[9] is None else str(row[9]),
            "created_at_utc": str(row[10]),
            "consumed_at_utc": None if row[11] is None else str(row[11]),
        }
        return PendingActionRecord.model_validate_json(_json(values), strict=True)

    def list_pending(
        self, conversation_id: str, *, status: str | None = "pending"
    ) -> tuple[PendingActionRecord, ...]:
        query = (
            "SELECT token, conversation_id, task_id, action_type, target_id, "
            "expected_revision, content_hash, payload_json, status, decision, "
            "created_at_utc, consumed_at_utc "
            "FROM p7_pending_actions WHERE conversation_id=?"
        )
        parameters: tuple[object, ...] = (str(conversation_id),)
        if status is not None:
            query += " AND status=?"
            parameters += (status,)
        query += " ORDER BY created_at_utc, token"
        rows = self.connection.execute(query, parameters).fetchall()
        return tuple(self.get_pending(conversation_id, str(row[0])) for row in rows)  # type: ignore[return-value]

    def consume_pending(
        self, *, conversation_id: str, token: str, decision: str, now: datetime
    ) -> PendingActionRecord:
        pending = self.get_pending(conversation_id, token)
        if pending is None:
            raise StateIntegrityError("pending action token was not found")
        if pending.status != "pending":
            raise StateIntegrityError("pending action token is no longer pending")
        self.connection.execute(
            "UPDATE p7_pending_actions SET status=?, decision=?, consumed_at_utc=? "
            "WHERE conversation_id=? AND token=? AND status='pending'",
            (
                "consumed" if decision == "accept" else "rejected",
                decision,
                format_utc(now),
                conversation_id,
                token,
            ),
        )
        return pending.model_copy(
            update={
                "status": "consumed" if decision == "accept" else "rejected",
                "decision": decision,
                "consumed_at_utc": now,
            }
        )

    def stale_task_pending(self, task_id: str, *, new_revision: int) -> None:
        self.connection.execute(
            "UPDATE p7_pending_actions SET status='stale' "
            "WHERE task_id=? AND status='pending' AND expected_revision < ?",
            (task_id, new_revision),
        )

    def insert_response(self, response: ResponseRecord) -> None:
        self.connection.execute(
            "INSERT INTO p7_responses("
            "response_id, conversation_id, turn_id, subrequest_index, intent, "
            "response_json, response_hash, delivery_id, created_at_utc"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                response.response_id,
                str(response.conversation_id),
                response.turn_id,
                response.subrequest_index,
                response.intent.value,
                _json(response.model_dump(mode="json")),
                response.response_hash,
                response.delivery_id,
                format_utc(response.created_at_utc),
            ),
        )

    def responses_for_turn(self, conversation_id: str, turn_id: str) -> tuple[ResponseRecord, ...]:
        rows = self.connection.execute(
            "SELECT response_json FROM p7_responses "
            "WHERE conversation_id=? AND turn_id=? ORDER BY subrequest_index",
            (str(conversation_id), str(turn_id)),
        ).fetchall()
        values = []
        for row in rows:
            data = _load_json(row[0], what="response")
            if not isinstance(data, dict):
                raise StateIntegrityError("stored response is not an object")
            values.append(ResponseRecord.model_validate_json(_json(data), strict=True))
        return tuple(values)

    # Model attempt/receipt facts ------------------------------------
    def insert_model_attempt(self, values: dict[str, object]) -> None:
        self.connection.execute(
            "INSERT INTO p7_model_attempts(attempt_id, turn_id, slot, generation, status, "
            "request_json, request_hash, context_hash, lease_expires_at_utc, started_at_utc, "
            "receipt_id, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                values["attempt_id"],
                values["turn_id"],
                values["slot"],
                values["generation"],
                values["status"],
                _json(values["request"]),
                values["request_hash"],
                values["context_hash"],
                values["lease_expires_at_utc"],
                values.get("started_at_utc"),
                values.get("receipt_id"),
                values["created_at_utc"],
            ),
        )

    def get_model_attempt(self, turn_id: str, slot: str) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT attempt_id, turn_id, slot, generation, status, request_json, request_hash, "
            "context_hash, lease_expires_at_utc, started_at_utc, receipt_id, created_at_utc "
            "FROM p7_model_attempts WHERE turn_id=? AND slot=?",
            (turn_id, slot),
        ).fetchone()
        if row is None:
            return None
        return {
            "attempt_id": str(row[0]),
            "turn_id": str(row[1]),
            "slot": str(row[2]),
            "generation": int(row[3]),
            "status": str(row[4]),
            "request": _load_json(row[5], what="model attempt request"),
            "request_hash": str(row[6]),
            "context_hash": str(row[7]),
            "lease_expires_at_utc": str(row[8]),
            "started_at_utc": None if row[9] is None else str(row[9]),
            "receipt_id": None if row[10] is None else str(row[10]),
            "created_at_utc": str(row[11]),
        }

    def update_model_attempt(self, attempt_id: str, **values: object) -> None:
        allowed = {
            "status",
            "started_at_utc",
            "receipt_id",
            "lease_expires_at_utc",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return
        assignments = ", ".join(f"{key}=?" for key in values)
        cursor = self.connection.execute(
            f"UPDATE p7_model_attempts SET {assignments} WHERE attempt_id=?",
            (*values.values(), attempt_id),
        )
        if cursor.rowcount != 1:
            raise StateIntegrityError("model attempt disappeared during update")

    def insert_model_receipt(self, values: dict[str, object]) -> None:
        self.connection.execute(
            "INSERT INTO p7_model_receipts(receipt_id, attempt_id, provider, model, outcome, "
            "response_bytes, response_hash, provider_request_id, usage_json, error_code, "
            "error_message, received_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                values["receipt_id"],
                values["attempt_id"],
                values["provider"],
                values.get("model"),
                values["outcome"],
                values.get("response_bytes"),
                values.get("response_hash"),
                values.get("provider_request_id"),
                None if values.get("usage") is None else _json(values["usage"]),
                values.get("error_code"),
                values.get("error_message"),
                values["received_at_utc"],
            ),
        )

    def get_model_receipt(self, attempt_id: str) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT receipt_id, attempt_id, provider, model, outcome, response_bytes, "
            "response_hash, provider_request_id, usage_json, error_code, error_message, "
            "received_at_utc "
            "FROM p7_model_receipts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "receipt_id": str(row[0]),
            "attempt_id": str(row[1]),
            "provider": str(row[2]),
            "model": None if row[3] is None else str(row[3]),
            "outcome": str(row[4]),
            "response_bytes": row[5],
            "response_hash": None if row[6] is None else str(row[6]),
            "provider_request_id": None if row[7] is None else str(row[7]),
            "usage": {} if row[8] is None else _load_json(row[8], what="model receipt usage"),
            "error_code": None if row[9] is None else str(row[9]),
            "error_message": None if row[10] is None else str(row[10]),
            "received_at_utc": str(row[11]),
        }

    def get_handoff(self, task_id: str, target: str) -> HandoffRecord | None:
        row = self.connection.execute(
            "SELECT handoff_id, task_id, target, command_id, child_id, expected_revision, "
            "payload_json, payload_hash, status, target_run_id, created_at_utc, "
            "updated_at_utc FROM p7_handoffs WHERE task_id=? AND target=?",
            (task_id, target),
        ).fetchone()
        if row is None:
            return None
        values = {
            "handoff_id": str(row[0]),
            "task_id": str(row[1]),
            "target": str(row[2]),
            "command_id": str(row[3]),
            "child_id": str(row[4]),
            "expected_revision": int(row[5]),
            "payload": _load_json(row[6], what="handoff payload"),
            "payload_hash": str(row[7]),
            "status": str(row[8]),
            "target_run_id": None if row[9] is None else str(row[9]),
            "created_at_utc": str(row[10]),
            "updated_at_utc": str(row[11]),
        }
        return HandoffRecord.model_validate_json(_json(values), strict=True)

    def insert_handoff(self, handoff: HandoffRecord) -> None:
        self.connection.execute(
            "INSERT INTO p7_handoffs("
            "handoff_id, task_id, target, command_id, child_id, expected_revision, "
            "payload_json, payload_hash, status, target_run_id, created_at_utc, "
            "updated_at_utc"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                handoff.handoff_id,
                handoff.task_id,
                handoff.target,
                handoff.command_id,
                handoff.child_id,
                handoff.expected_revision,
                _json(thaw_json(handoff.payload)),
                handoff.payload_hash,
                handoff.status,
                handoff.target_run_id,
                format_utc(handoff.created_at_utc),
                format_utc(handoff.updated_at_utc),
            ),
        )

    def update_handoff(self, handoff: HandoffRecord) -> None:
        self.connection.execute(
            "UPDATE p7_handoffs SET status=?, target_run_id=?, updated_at_utc=? WHERE handoff_id=?",
            (
                handoff.status,
                handoff.target_run_id,
                format_utc(handoff.updated_at_utc),
                handoff.handoff_id,
            ),
        )

    def insert_delivery(self, delivery: DeliveryRecord) -> None:
        self.connection.execute(
            "INSERT INTO p7_deliveries("
            "delivery_id, task_id, delivery_version, output_spec_json, "
            "output_spec_hash, source_json, source_hash, fulfillment_json, "
            "overall_status, created_at_utc"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                delivery.delivery_id,
                delivery.task_id,
                delivery.delivery_version,
                _json(delivery.output_spec.model_dump(mode="json")),
                delivery.output_spec.output_spec_hash,
                _json(thaw_json(delivery.source)),
                delivery.source_hash,
                _json([item.model_dump(mode="json") for item in delivery.fulfillment]),
                delivery.overall_status.value,
                format_utc(delivery.created_at_utc),
            ),
        )

    def get_delivery(self, task_id: str, delivery_id: str | None = None) -> DeliveryRecord | None:
        if delivery_id is None:
            row = self.connection.execute(
                "SELECT * FROM p7_deliveries WHERE task_id=? "
                "ORDER BY delivery_version DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM p7_deliveries WHERE task_id=? AND delivery_id=?",
                (task_id, delivery_id),
            ).fetchone()
        if row is None:
            return None
        return self._delivery_from_row(row)

    def list_deliveries(self, task_id: str) -> tuple[DeliveryRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM p7_deliveries WHERE task_id=? ORDER BY delivery_version", (task_id,)
        ).fetchall()
        return tuple(self._delivery_from_row(row) for row in rows)

    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
        from orca_agent.domain.p7_task import Fulfillment, OverallDeliveryStatus

        source = _load_json(row[5], what="delivery source")
        fulfillment_value = _load_json(row[7], what="fulfillment")
        if not isinstance(fulfillment_value, list):
            raise StateIntegrityError("stored fulfillment is not an array")
        delivery = DeliveryRecord.create(
            delivery_id=str(row[0]),
            task_id=str(row[1]),
            delivery_version=int(row[2]),
            output_spec=OutputSpec.model_validate_json(str(row[3]), strict=True),
            source=source,
            fulfillment=tuple(
                Fulfillment.model_validate_json(_json(item), strict=True)
                for item in fulfillment_value
            ),
            overall_status=OverallDeliveryStatus(str(row[8])),
            created_at_utc=parse_utc(str(row[9])),
        )
        if delivery.source_hash != str(row[6]) or delivery.output_spec.output_spec_hash != str(
            row[4]
        ):
            raise StateIntegrityError("stored delivery hashes are inconsistent")
        return delivery


__all__ = ["P7RecordRepository"]
