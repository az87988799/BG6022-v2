"""P7 conversation boundary: ordered turns, routing, and safe responses."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

from orca_agent.application.errors import InvalidTransitionError, RevisionConflictError
from orca_agent.application.p7_query_service import P7QueryService
from orca_agent.application.p7_task_service import P7TaskService
from orca_agent.domain.hashing import sha256_hex
from orca_agent.domain.ids import AttemptId, ConversationId, TurnId, new_id
from orca_agent.domain.json_types import thaw_json
from orca_agent.domain.p7_conversation import (
    CalculationAction,
    CalculationIntent,
    ChemistryQAIntent,
    ContextQueryIntent,
    ContextSnapshot,
    ContextTask,
    ContextTurn,
    ConversationState,
    ConversationStatus,
    GeneralQAIntent,
    IntentKind,
    ResponseRecord,
    ResponseSource,
    TurnInterpretation,
    TurnRecord,
    TurnStatus,
)
from orca_agent.domain.p7_task import (
    StopReason,
    TaskPhase,
    TaskRecord,
    ValidationStatus,
)
from orca_agent.infrastructure.clock import Clock, SystemClock, format_utc
from orca_agent.infrastructure.p7_records import P7RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.llm.baseline import BaselinePlanner
from orca_agent.llm.ports import (
    ModelCallResponse,
    PlannerPort,
    validate_interpretation,
)
from orca_agent.orchestration.p7_versions import P7_POLICY_VERSION, PROMPT_VERSION, TURN_SCHEMA
from orca_agent.planning.p7_validator import P7PlanValidator

_MODEL_LEASE_SECONDS = 90
_MAX_TURN_MODEL_CALLS = 3


def _json_value(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return thaw_json(value)


def _state_with(state: ConversationState, **updates: object) -> ConversationState:
    candidate = state.model_copy(update={**updates, "state_hash": "0" * 64})
    return candidate.model_copy(
        update={"state_hash": sha256_hex(candidate.model_dump(mode="json", exclude={"state_hash"}))}
    )


class P7ConversationService:
    """The only service that turns free text into P7 task proposals."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        clock: Clock | None = None,
        task_service: P7TaskService | None = None,
        query_service: P7QueryService | None = None,
        planner: PlannerPort | None = None,
        fallback: str = "none",
        allow_llm: bool = False,
        planner_name: str = "baseline",
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.clock = clock or SystemClock()
        self.task_service = task_service or P7TaskService(self.state_root, clock=self.clock)
        self.query_service = query_service or P7QueryService(
            self.state_root, clock=self.clock, task_service=self.task_service
        )
        self.planner = planner or BaselinePlanner()
        self.fallback = fallback
        self.allow_llm = allow_llm
        self.planner_name = planner_name
        self.validator = P7PlanValidator(self.task_service.catalog)

    # Conversation lifecycle ----------------------------------------
    def new_conversation(
        self, *, conversation_id: ConversationId | None = None, budget: int = 120
    ) -> dict[str, object]:
        now = self.clock.now_utc()
        state = ConversationState.create(
            conversation_id=conversation_id,
            now=now,
            model_call_budget=budget,
        )
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            existing = records.get_conversation(str(state.conversation_id))
            if existing is None:
                records.insert_conversation(state)
            else:
                state = existing
            uow.commit()
        return state.model_dump(mode="json")

    def get_state(self, conversation_id: ConversationId | str) -> ConversationState:
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            state = P7RecordRepository(uow.connection).get_conversation(str(conversation_id))
            uow.commit()
        if state is None:
            raise ValueError("conversation was not found")
        return state

    def close(self, conversation_id: ConversationId | str) -> dict[str, object]:
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            state = records.get_conversation(str(conversation_id))
            if state is None:
                raise ValueError("conversation was not found")
            if state.current_turn_id is not None:
                raise InvalidTransitionError("cannot close a conversation with an active turn")
            if state.status is ConversationStatus.CLOSED:
                uow.commit()
                return state.model_dump(mode="json")
            updated = _state_with(
                state,
                status=ConversationStatus.CLOSED,
                revision=state.revision + 1,
                updated_at_utc=self.clock.now_utc(),
            )
            if not records.update_conversation(updated, expected_revision=state.revision):
                raise RevisionConflictError("conversation changed while closing")
            uow.commit()
            return updated.model_dump(mode="json")

    # Turn processing -------------------------------------------------
    def message(self, conversation_id: ConversationId | str, text: str) -> dict[str, object]:
        conversation = str(conversation_id)
        state, turn, context = self._receive_turn(conversation, text)
        try:
            interpretation, source, model_error = self._interpret(context, turn_id=turn.turn_id)
            if interpretation is None:
                return self._finish_failed(
                    conversation,
                    turn,
                    code=model_error or "interpretation_failed",
                    source=source,
                )
            if self._is_recommendation_acceptance(text):
                interpretation = self._rewrite_acceptance(
                    conversation, interpretation, turn.turn_id
                )
            implicit = self._unique_pending_action(conversation, text)
            if implicit is not None:
                interpretation = TurnInterpretation(
                    subrequests=(
                        CalculationIntent(
                            action=CalculationAction.REQUEST_EXECUTION,
                            task_alias=implicit[1],
                        ),
                    ),
                    source=ResponseSource.PROGRAM,
                )
                output = self._accept_pending_action(conversation, implicit[0])
                source = ResponseSource.PROGRAM
            else:
                output = self._dispatch(conversation, turn, interpretation, source=source)
            return self._finish_completed(
                conversation,
                turn,
                interpretation,
                output,
                source=source,
            )
        except Exception as error:
            return self._finish_failed(
                conversation,
                turn,
                code=type(error).__name__,
                source=ResponseSource.PROGRAM,
                error_message=str(error),
            )

    def _receive_turn(
        self, conversation: str, text: str
    ) -> tuple[ConversationState, TurnRecord, ContextSnapshot]:
        now = self.clock.now_utc()
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            state = records.get_conversation(conversation)
            if state is None:
                raise ValueError("conversation was not found")
            if state.status is ConversationStatus.CLOSED:
                raise InvalidTransitionError("conversation is closed")
            if state.current_turn_id is not None:
                active = records.get_turn(conversation, state.current_turn_id)
                if active is not None and active.status not in {
                    TurnStatus.COMPLETED,
                    TurnStatus.FAILED,
                    TurnStatus.OUTCOME_UNKNOWN,
                    TurnStatus.CANCELLED,
                }:
                    raise InvalidTransitionError("turn_in_progress")
            context = self._context_snapshot(records, state, text)
            turn = TurnRecord.create(
                turn_id=str(new_id(TurnId)),
                conversation_id=ConversationId(conversation),
                sequence_no=state.next_turn_sequence,
                status=TurnStatus.RECEIVED,
                user_text=text,
                context_snapshot=context,
                created_at_utc=now,
                updated_at_utc=now,
            )
            records.insert_turn(turn)
            updated = _state_with(
                state,
                revision=state.revision + 1,
                current_turn_id=turn.turn_id,
                next_turn_sequence=state.next_turn_sequence + 1,
                updated_at_utc=now,
            )
            if not records.update_conversation(updated, expected_revision=state.revision):
                raise RevisionConflictError("conversation changed while receiving turn")
            uow.commit()
            return updated, turn, context

    def _context_snapshot(
        self,
        records: P7RecordRepository,
        state: ConversationState,
        text: str,
    ) -> ContextSnapshot:
        tasks = records.list_tasks(str(state.conversation_id))
        turns = records.list_turns(str(state.conversation_id), limit=12)
        context_tasks = tuple(
            ContextTask(
                task_alias=item.alias,
                task_id=item.task_id,
                state=item.state.value,
                revision=item.revision,
                updated_at_utc=item.updated_at_utc,
                request_summary=self._request_summary(item),
            )
            for item in tasks[:8]
        )
        context_turns = tuple(
            ContextTurn(
                sequence_no=item.sequence_no,
                user_text=item.user_text,
                response_text=item.response_text or "（处理中）",
                turn_id=item.turn_id,
            )
            for item in turns
            if item.status is TurnStatus.COMPLETED
        )
        facts = {
            "policy_version": P7_POLICY_VERSION,
            "prompt_version": PROMPT_VERSION,
            "capability_ids": [item.capability_id for item in self.task_service.catalog.list()],
            "active_task_id": state.active_task_id,
        }
        while True:
            plain = {
                "schema_version": TURN_SCHEMA,
                "conversation_id": str(state.conversation_id),
                "turn_sequence_no": state.next_turn_sequence,
                "current_message": text,
                "active_task_alias": self._active_alias(context_tasks, state.active_task_id),
                "tasks": [_json_value(item) for item in context_tasks],
                "recent_turns": [_json_value(item) for item in context_turns],
                "facts": facts,
                "encoded_size_bytes": 0,
            }
            size = len(json.dumps(plain, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if size <= 65_536 or not context_turns:
                if size > 65_536:
                    raise ValueError("context_snapshot_too_large")
                return ContextSnapshot.create(
                    conversation_id=ConversationId(str(state.conversation_id)),
                    turn_sequence_no=state.next_turn_sequence,
                    current_message=text,
                    active_task_alias=self._active_alias(context_tasks, state.active_task_id),
                    tasks=context_tasks,
                    recent_turns=context_turns,
                    facts=facts,
                    encoded_size_bytes=size,
                )
            context_turns = context_turns[1:]

    @staticmethod
    def _request_summary(task: TaskRecord) -> dict[str, object]:
        if task.request is None:
            return {}
        request = task.request
        return {
            "molecule_kind": None if request.molecule_kind is None else request.molecule_kind.value,
            "molecule_value": request.molecule_value,
            "charge": None if request.charge is None else _json_value(request.charge),
            "multiplicity": None
            if request.multiplicity is None
            else _json_value(request.multiplicity),
            "method": None if request.method is None else _json_value(request.method),
            "environment": None
            if request.environment is None
            else _json_value(request.environment),
            "operations": list(request.operations),
            "output_spec_hash": request.output_spec.output_spec_hash,
        }

    @staticmethod
    def _active_alias(tasks: tuple[ContextTask, ...], active_task_id: str | None) -> str | None:
        return next((item.task_alias for item in tasks if item.task_id == active_task_id), None)

    def _interpret(
        self, context: ContextSnapshot, *, turn_id: str
    ) -> tuple[TurnInterpretation | None, ResponseSource, str | None]:
        planner = self.planner
        if getattr(planner, "adapter_id", "") == "deepseek_chat" and not self.allow_llm:
            if self.fallback != "baseline":
                return None, ResponseSource.DEEPSEEK, "llm_disabled"
            return BaselinePlanner().interpret(context), ResponseSource.BASELINE, None
        if getattr(planner, "adapter_id", "") == "baseline":
            return planner.interpret(context), ResponseSource.BASELINE, None

        state = self.get_state(str(context.conversation_id))
        if state.model_calls >= state.model_call_budget:
            if self.fallback == "baseline":
                return (
                    BaselinePlanner().interpret(context),
                    ResponseSource.BASELINE,
                    "model_budget_exhausted",
                )
            return None, ResponseSource.PROGRAM, "model_budget_exhausted"
        request_payload = {
            "adapter_id": getattr(planner, "adapter_id", "unknown"),
            "model": getattr(planner, "model", None),
            "context_hash": context.snapshot_hash,
            "prompt_version": PROMPT_VERSION,
            "turn_id": turn_id,
        }
        attempt_id = str(new_id(AttemptId))
        now = self.clock.now_utc()
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            prior = records.get_model_attempt(turn_id, "interpret")
            if prior is not None:
                receipt = records.get_model_receipt(str(prior["attempt_id"]))
                if receipt is not None:
                    uow.commit()
                    response = self._response_from_receipt(receipt)
                    return self._validate_model_response(response, context, turn_id)
                if prior["status"] in {"started", "reserved"}:
                    records.update_model_attempt(str(prior["attempt_id"]), status="unknown")
                    uow.commit()
                    return None, ResponseSource.DEEPSEEK, "outcome_unknown"
            records.insert_model_attempt(
                {
                    "attempt_id": attempt_id,
                    "turn_id": turn_id,
                    "slot": "interpret",
                    "generation": 1,
                    "status": "started",
                    "request": request_payload,
                    "request_hash": sha256_hex(request_payload),
                    "context_hash": context.snapshot_hash,
                    "lease_expires_at_utc": format_utc(
                        now + timedelta(seconds=_MODEL_LEASE_SECONDS)
                    ),
                    "started_at_utc": format_utc(now),
                    "created_at_utc": format_utc(now),
                }
            )
            uow.commit()

        response = planner.interpret(context)
        if isinstance(response, TurnInterpretation):
            response = ModelCallResponse(
                provider=getattr(planner, "adapter_id", "planner"),
                model=getattr(planner, "model", None),
                content=json.dumps(response.model_dump(mode="json"), ensure_ascii=False),
                raw_bytes=json.dumps(response.model_dump(mode="json"), ensure_ascii=False).encode(
                    "utf-8"
                ),
            )
        if not isinstance(response, ModelCallResponse):
            response = ModelCallResponse(
                provider=getattr(planner, "adapter_id", "planner"),
                model=getattr(planner, "model", None),
                error_code="invalid_planner_response",
                error_message="planner returned an unsupported response object",
            )
        raw = response.raw_bytes or (response.content.encode("utf-8") if response.content else None)
        receipt_id = f"modelreceipt_{uuid.uuid4().hex}"
        receipt_values = {
            "receipt_id": receipt_id,
            "attempt_id": attempt_id,
            "provider": response.provider,
            "model": response.model,
            "outcome": "success" if response.error_code is None and response.content else "error",
            "response_bytes": raw,
            "response_hash": None
            if raw is None
            else sha256_hex(raw.decode("utf-8", errors="replace")),
            "provider_request_id": response.provider_request_id,
            "usage": thaw_json(response.usage),
            "error_code": response.error_code,
            "error_message": response.error_message,
            "received_at_utc": format_utc(self.clock.now_utc()),
        }
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            records.insert_model_receipt(receipt_values)
            records.update_model_attempt(attempt_id, status="receipted", receipt_id=receipt_id)
            current = records.get_conversation(str(context.conversation_id))
            if current is not None:
                updated = _state_with(
                    current,
                    model_calls=current.model_calls + 1,
                    updated_at_utc=self.clock.now_utc(),
                )
                records.update_conversation(updated, expected_revision=current.revision)
            uow.commit()
        return self._validate_model_response(response, context, turn_id)

    def _validate_model_response(
        self,
        response: ModelCallResponse,
        context: ContextSnapshot,
        turn_id: str,
    ) -> tuple[TurnInterpretation | None, ResponseSource, str | None]:
        if response.error_code is not None or not response.content:
            if self.fallback == "baseline":
                return (
                    BaselinePlanner().interpret(context),
                    ResponseSource.BASELINE,
                    response.error_code,
                )
            return (
                None,
                ResponseSource.DEEPSEEK if response.provider == "deepseek" else ResponseSource.FAKE,
                response.error_code or "empty_model_output",
            )
        try:
            interpretation = validate_interpretation(response.content)
        except Exception:
            if self.fallback == "baseline":
                return (
                    BaselinePlanner().interpret(context),
                    ResponseSource.BASELINE,
                    "invalid_model_output",
                )
            return (
                None,
                ResponseSource.DEEPSEEK if response.provider == "deepseek" else ResponseSource.FAKE,
                "invalid_model_output",
            )
        source = ResponseSource.DEEPSEEK if response.provider == "deepseek" else ResponseSource.FAKE
        return interpretation.model_copy(update={"source": source}), source, None

    @staticmethod
    def _response_from_receipt(receipt: dict[str, object]) -> ModelCallResponse:
        raw = receipt.get("response_bytes")
        raw_bytes = raw if isinstance(raw, bytes) else None
        content: str | None = None
        if raw_bytes:
            decoded = raw_bytes.decode("utf-8", errors="replace")
            if receipt.get("provider") == "deepseek":
                try:
                    body = json.loads(decoded)
                    content_value = body["choices"][0]["message"]["content"]
                    content = content_value if isinstance(content_value, str) else None
                except (KeyError, IndexError, TypeError, ValueError):
                    content = None
            else:
                content = decoded
        return ModelCallResponse(
            provider=str(receipt.get("provider", "unknown")),
            model=None if receipt.get("model") is None else str(receipt.get("model")),
            content=content,
            raw_bytes=raw_bytes,
            provider_request_id=(
                None
                if receipt.get("provider_request_id") is None
                else str(receipt.get("provider_request_id"))
            ),
            usage=receipt.get("usage", {}),
            error_code=None
            if receipt.get("error_code") is None
            else str(receipt.get("error_code")),
            error_message=None
            if receipt.get("error_message") is None
            else str(receipt.get("error_message")),
        )

    def _dispatch(
        self,
        conversation: str,
        turn: TurnRecord,
        interpretation: TurnInterpretation,
        *,
        source: ResponseSource,
    ) -> dict[str, object]:
        responses: list[dict[str, object]] = []
        task_ids: list[str] = []
        pending_tokens: list[dict[str, object]] = []
        last_task_id: str | None = None
        for index, item in enumerate(interpretation.subrequests):
            if isinstance(item, CalculationIntent):
                result = self._calculation(conversation, turn, item)
            elif isinstance(item, ChemistryQAIntent):
                result = {
                    "text": item.answer_draft or BaselinePlanner._chemistry_answer(item.question),
                    "payload": {"kind": "chemistry_qa", "answer_source": "planner_draft"},
                }
            elif isinstance(item, GeneralQAIntent):
                result = {
                    "text": item.answer_draft
                    or "我可以回答一般问题，也可以查询当前会话中的已验证任务。",
                    "payload": {"kind": "general_qa", "answer_source": "planner_draft"},
                }
            elif isinstance(item, ContextQueryIntent):
                try:
                    query = self.query_service.query(
                        conversation,
                        task_id=None,
                        request={
                            "kind": "result",
                            "task_alias": item.task_alias,
                            "output_spec": thaw_json(item.output_spec)
                            if item.output_spec
                            else None,
                        },
                    )
                    result = {
                        "text": self._query_text(query),
                        "payload": query,
                        "task_id": query.get("selected_task_id"),
                    }
                except ValueError as error:
                    result = {
                        "text": f"无法唯一定位查询对象：{error}",
                        "payload": {"code": "query_ambiguous", "error": str(error)},
                    }
            else:
                result = {"text": "未识别的请求。", "payload": {"code": "unsupported_intent"}}
            if result.get("task_id"):
                task_id = str(result["task_id"])
                task_ids.append(task_id)
                last_task_id = task_id
            pending_tokens.extend(result.get("pending_actions", []))
            responses.append(
                {
                    "index": index,
                    "intent": item.intent.value,
                    **result,
                }
            )
        text = "\n\n".join(str(item["text"]) for item in responses)
        return {
            "text": text,
            "responses": responses,
            "task_ids": list(dict.fromkeys(task_ids)),
            "pending_actions": pending_tokens,
            "active_task_id": last_task_id,
        }

    def _unique_pending_action(
        self, conversation: str, text: str
    ) -> tuple[object, str | None] | None:
        """Conservatively map a short acknowledgement to one current token."""

        collapsed = "".join(
            character
            for character in text.strip().casefold()
            if character not in {" ", "\t", "\r", "\n", ",", "，", "。", ".", "!", "！", "?", "？"}
        )
        if collapsed not in {
            "好",
            "好的",
            "可以",
            "行",
            "继续",
            "这个",
            "接受",
            "接受计划",
            "接受这个",
            "开始吧",
            "继续执行",
            "goahead",
            "approve",
            "accept",
        }:
            return None
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            pending = tuple(
                item for item in records.list_pending(conversation) if item.status == "pending"
            )
            uow.commit()
        if len(pending) != 1:
            return None
        action = pending[0]
        alias = None
        if action.task_id is not None:
            task = self.task_service.get_task(conversation, action.task_id)
            alias = None if task is None else task.alias
        return action, alias

    def _accept_pending_action(self, conversation: str, action: object) -> dict[str, object]:
        token = getattr(action, "token", None)
        action_type = getattr(action, "action_type", "pending")
        task_id = getattr(action, "task_id", None)
        if not isinstance(token, str):
            raise ValueError("pending action token is invalid")
        result = self.task_service.accept_action(conversation, token, decision="accept")
        task_view = result.get("task") if isinstance(result, dict) else None
        state = task_view.get("state") if isinstance(task_view, dict) else None
        text = f"已接受当前待办：{action_type}。"
        if state == TaskPhase.IDENTITY_PENDING.value:
            text += "请确认唯一的分子身份候选。"
        elif state == TaskPhase.EXECUTION_PENDING.value:
            text += "请对具体 P5 执行节点逐项批准。"
        elif state == TaskPhase.ASSESSING.value:
            text += "任务已进入结果评估。"
        return {
            "text": text,
            "responses": [
                {
                    "index": 0,
                    "intent": IntentKind.CHEMICAL_CALCULATION.value,
                    "text": text,
                    "payload": result,
                    "task_id": task_id,
                    "pending_actions": result.get("pending_actions", [])
                    if isinstance(result, dict)
                    else [],
                }
            ],
            "task_ids": [] if task_id is None else [task_id],
            "pending_actions": result.get("pending_actions", [])
            if isinstance(result, dict)
            else [],
            "active_task_id": task_id,
        }

    def _calculation(
        self, conversation: str, turn: TurnRecord, intent: CalculationIntent
    ) -> dict[str, object]:
        if intent.action is CalculationAction.CANCEL_TASK:
            task = self._resolve_task(conversation, intent.task_alias)
            if task is None:
                return {"text": "请指定要取消的会话内任务。", "payload": {"code": "task_ambiguous"}}
            result = self.task_service.cancel_task(
                conversation,
                task.task_id,
                expected_revision=task.revision,
            )
            return {
                "text": f"任务 {task.alias} 已请求取消。",
                "payload": result,
                "task_id": task.task_id,
            }
        if intent.action is CalculationAction.REQUEST_EXECUTION:
            task = self._resolve_task(conversation, intent.task_alias)
            if task is None:
                return {
                    "text": "请指定要继续操作的会话内任务。",
                    "payload": {"code": "task_ambiguous"},
                }
            view = self.task_service.task_view(conversation, task.task_id)
            pending = view.get("pending_actions", [])
            if not pending:
                return {
                    "text": f"任务 {task.alias} 当前没有可操作的待办；状态是 {task.state.value}。",
                    "payload": view,
                    "task_id": task.task_id,
                }
            return {
                "text": (
                    f"任务 {task.alias} 有一个待办操作，请明确接受对应 token；模型不会代替批准。"
                ),
                "payload": view,
                "task_id": task.task_id,
                "pending_actions": pending,
            }
        task = self._resolve_task(conversation, intent.task_alias)
        acceptance = self._is_recommendation_acceptance(turn.user_text)
        existing = (
            task.request
            if task is not None
            and task.state
            in {
                TaskPhase.NEEDS_CLARIFICATION,
                TaskPhase.DRAFT,
            }
            else None
        )
        if intent.action is CalculationAction.REVISE_DRAFT and existing is None:
            return {
                "text": "当前没有可修改的待澄清计算草稿。",
                "payload": {"code": "draft_not_found"},
            }
        normalized = self.validator.normalize(
            intent,
            turn_id=turn.turn_id,
            existing_request=existing,
            accept_recommendations=acceptance,
        )
        if task is not None and existing is not None:
            # A revised draft gets a new task revision but keeps the stable
            # alias.  Old pending plan tokens are made stale by the update.
            return self._revise_task(conversation, task, normalized)
        view = self.task_service.create_task(
            conversation,
            alias=intent.task_alias,
            normalized=normalized,
            turn_id=turn.turn_id,
        )
        task_payload = view["task"]
        text = self._plan_text(view, normalized.validation)
        return {
            "text": text,
            "payload": view,
            "task_id": task_payload["task_id"],
            "pending_actions": view.get("pending_actions", []),
        }

    def _revise_task(self, conversation: str, task: TaskRecord, normalized) -> dict[str, object]:
        # TaskService intentionally exposes creation as the normal path; for a
        # draft revision preserve the task identity and increment its revision
        # in one P7 transaction.
        now = self.clock.now_utc()
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            current = records.get_task(conversation, task.task_id)
            if current is None:
                raise ValueError("draft task disappeared")
            if current.state not in {TaskPhase.NEEDS_CLARIFICATION, TaskPhase.DRAFT}:
                raise InvalidTransitionError("task draft is no longer editable")
            state = (
                TaskPhase.PLAN_READY
                if normalized.validation.status is ValidationStatus.VALID
                else TaskPhase.NEEDS_CLARIFICATION
                if normalized.validation.status is ValidationStatus.NEEDS_CLARIFICATION
                else TaskPhase.ENDED_WITHOUT_RESULT
            )
            updated = current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "state": state,
                    "stop_reason": StopReason.UNSUPPORTED
                    if state is TaskPhase.ENDED_WITHOUT_RESULT
                    else None,
                    "request": normalized.request,
                    "plan": normalized.plan,
                    "validation": normalized.validation,
                    "accepted_plan_hash": None,
                    "accepted_output_spec_hash": None,
                    "delivery_output_spec": normalized.request.output_spec,
                    "p4_run_id": None,
                    "p5_run_id": None,
                    "p6_run_id": None,
                    "current_delivery_id": None,
                    "updated_at_utc": now,
                }
            )
            records.stale_task_pending(current.task_id, new_revision=updated.revision)
            if not records.update_task(updated, expected_revision=current.revision):
                raise RevisionConflictError("draft revision raced with another update")
            if state is TaskPhase.PLAN_READY:
                self.task_service._insert_pending(
                    records,
                    conversation_id=conversation,
                    task_id=updated.task_id,
                    action_type="accept_plan",
                    target_id=updated.task_id,
                    expected_revision=updated.revision,
                    payload={
                        "task_id": updated.task_id,
                        "task_revision": updated.revision,
                        "plan_hash": normalized.plan.plan_hash,
                        "validation_hash": normalized.validation.validation_hash,
                        "output_spec_hash": normalized.request.output_spec.output_spec_hash,
                        "capability_id": normalized.plan.capability_id,
                        "capability_version": normalized.plan.capability_version,
                    },
                    now=now,
                )
            uow.commit()
        view = self.task_service.task_view(conversation, updated.task_id)
        return {
            "text": self._plan_text(view, normalized.validation),
            "payload": view,
            "task_id": updated.task_id,
            "pending_actions": view.get("pending_actions", []),
        }

    def _resolve_task(self, conversation: str, alias: str | None) -> TaskRecord | None:
        tasks = self.task_service.list_tasks(conversation)
        if alias:
            matches = tuple(item for item in tasks if item.alias == alias)
            return matches[0] if len(matches) == 1 else None
        if len(tasks) == 1:
            return tasks[0]
        state = self.get_state(conversation)
        if state.active_task_id:
            return next((item for item in tasks if item.task_id == state.active_task_id), None)
        return None

    @staticmethod
    def _plan_text(view: dict[str, object], validation) -> str:
        task = view["task"]
        if validation.status is ValidationStatus.VALID:
            pending = view.get("pending_actions", [])
            return (
                f"已为任务 {task['alias']} 生成受支持的 Opt → Freq → 独立 SP 计划。"
                "请明确接受计划后才会进入身份确认；不会自动启动执行。"
                + (f"待办 token：{pending[0]['token']}" if pending else "")
            )
        if validation.status is ValidationStatus.NEEDS_CLARIFICATION:
            return validation.clarification_question or "请补充计算所需信息。"
        if validation.status is ValidationStatus.UNSUPPORTED:
            return "当前请求不在 P7 首版能力范围内：" + "；".join(validation.unsupported_requests)
        return "当前请求未通过确定性校验：" + "；".join(validation.issues)

    @staticmethod
    def _query_text(query: dict[str, object]) -> str:
        delivery = query.get("delivery")
        if delivery is None:
            return "当前任务还没有可交付结果；已保留真实执行状态。"
        overall = delivery.get("overall_status") if isinstance(delivery, dict) else None
        return f"已读取当前任务的结果交付包，整体状态：{overall}。"

    @staticmethod
    def _is_recommendation_acceptance(text: str) -> bool:
        lowered = text.casefold()
        return any(
            item in lowered
            for item in ("接受推荐", "按推荐", "接受这个推荐", "accept recommendation")
        )

    def _rewrite_acceptance(
        self,
        conversation: str,
        interpretation: TurnInterpretation,
        turn_id: str,
    ) -> TurnInterpretation:
        tasks = self.task_service.list_tasks(conversation)
        drafts = tuple(
            item
            for item in tasks
            if item.state is TaskPhase.NEEDS_CLARIFICATION and item.request is not None
        )
        if len(drafts) != 1:
            return interpretation
        if len(interpretation.subrequests) == 1 and interpretation.subrequests[0].intent in {
            IntentKind.GENERAL_QA,
            IntentKind.CONTEXT_QUERY,
        }:
            return TurnInterpretation(
                subrequests=(
                    CalculationIntent(
                        action=CalculationAction.REVISE_DRAFT,
                        task_alias=drafts[0].alias,
                    ),
                ),
                source=interpretation.source,
            )
        return interpretation

    def _finish_completed(
        self,
        conversation: str,
        turn: TurnRecord,
        interpretation: TurnInterpretation,
        output: dict[str, object],
        *,
        source: ResponseSource,
    ) -> dict[str, object]:
        now = self.clock.now_utc()
        task_ids = tuple(str(item) for item in output.get("task_ids", []))
        response_records = []
        responses = output.get("responses", [])
        if not isinstance(responses, list):
            responses = []
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            state = records.get_conversation(conversation)
            current_turn = records.get_turn(conversation, turn.turn_id)
            if state is None or current_turn is None:
                raise ValueError("turn disappeared before completion")
            for index, item in enumerate(responses):
                if not isinstance(item, dict):
                    continue
                response_records.append(
                    ResponseRecord.create(
                        response_id=f"response_{uuid.uuid4().hex}",
                        turn_id=turn.turn_id,
                        conversation_id=ConversationId(conversation),
                        subrequest_index=index,
                        intent=IntentKind(str(item.get("intent", "general_qa"))),
                        source=source,
                        text=str(item.get("text", "")),
                        payload=item,
                        created_at_utc=now,
                    )
                )
                records.insert_response(response_records[-1])
            completed = TurnRecord.create(
                turn_id=current_turn.turn_id,
                conversation_id=ConversationId(conversation),
                sequence_no=current_turn.sequence_no,
                status=TurnStatus.COMPLETED,
                user_text=current_turn.user_text,
                context_snapshot=current_turn.context_snapshot,
                interpretation=interpretation,
                response_text=str(output.get("text", "")),
                response_payload=output,
                response_source=source,
                task_ids=task_ids,
                created_at_utc=current_turn.created_at_utc,
                updated_at_utc=now,
            )
            records.update_turn(completed)
            updated_state = _state_with(
                state,
                revision=state.revision + 1,
                current_turn_id=None,
                active_task_id=output.get("active_task_id")
                or (task_ids[-1] if task_ids else state.active_task_id),
                updated_at_utc=now,
            )
            if not records.update_conversation(updated_state, expected_revision=state.revision):
                raise RevisionConflictError("conversation changed while completing turn")
            uow.commit()
        return {
            "accepted": True,
            "conversation_id": conversation,
            "turn_id": turn.turn_id,
            "source": source.value,
            "text": output.get("text", ""),
            "task_ids": list(task_ids),
            "pending_actions": output.get("pending_actions", []),
            "responses": output.get("responses", []),
        }

    def _finish_failed(
        self,
        conversation: str,
        turn: TurnRecord,
        *,
        code: str,
        source: ResponseSource,
        error_message: str | None = None,
    ) -> dict[str, object]:
        now = self.clock.now_utc()
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            state = records.get_conversation(conversation)
            current = records.get_turn(conversation, turn.turn_id)
            if state is None or current is None:
                raise ValueError("turn disappeared before failure publication")
            failed = TurnRecord.create(
                turn_id=current.turn_id,
                conversation_id=ConversationId(conversation),
                sequence_no=current.sequence_no,
                status=TurnStatus.OUTCOME_UNKNOWN
                if code == "outcome_unknown"
                else TurnStatus.FAILED,
                user_text=current.user_text,
                context_snapshot=current.context_snapshot,
                response_text=f"本轮处理失败：{code}",
                response_payload={"error": error_message or code},
                response_source=source,
                task_ids=(),
                error_code=code,
                created_at_utc=current.created_at_utc,
                updated_at_utc=now,
            )
            records.update_turn(failed)
            updated = _state_with(
                state,
                revision=state.revision + 1,
                current_turn_id=None,
                updated_at_utc=now,
            )
            if not records.update_conversation(updated, expected_revision=state.revision):
                raise RevisionConflictError("conversation changed while publishing failure")
            uow.commit()
        return {
            "accepted": False,
            "conversation_id": conversation,
            "turn_id": turn.turn_id,
            "source": source.value,
            "code": code,
            "text": f"本轮处理失败：{code}",
        }

    def interrupt_turn(
        self, conversation_id: ConversationId | str, turn_id: str
    ) -> dict[str, object]:
        conversation = str(conversation_id)
        now = self.clock.now_utc()
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            state = records.get_conversation(conversation)
            turn = records.get_turn(conversation, turn_id)
            if state is None or turn is None:
                raise ValueError("conversation or turn was not found")
            if turn.status in {
                TurnStatus.COMPLETED,
                TurnStatus.FAILED,
                TurnStatus.OUTCOME_UNKNOWN,
                TurnStatus.CANCELLED,
            }:
                uow.commit()
                return {"accepted": True, "replayed": True, "turn_id": turn_id}
            cancelled = TurnRecord.create(
                turn_id=turn.turn_id,
                conversation_id=ConversationId(conversation),
                sequence_no=turn.sequence_no,
                status=TurnStatus.CANCELLED,
                user_text=turn.user_text,
                context_snapshot=turn.context_snapshot,
                response_text="本轮已中断；不会取消后台计算任务。",
                response_payload={"code": "turn_cancelled"},
                response_source=ResponseSource.PROGRAM,
                error_code="turn_cancelled",
                created_at_utc=turn.created_at_utc,
                updated_at_utc=now,
            )
            records.update_turn(cancelled)
            records.connection.execute(
                "UPDATE p7_model_attempts SET status='cancelled' "
                "WHERE turn_id=? AND status IN ('reserved','started')",
                (turn_id,),
            )
            updated = _state_with(
                state, revision=state.revision + 1, current_turn_id=None, updated_at_utc=now
            )
            if not records.update_conversation(updated, expected_revision=state.revision):
                raise RevisionConflictError("conversation changed while interrupting turn")
            uow.commit()
            return {"accepted": True, "turn_id": turn_id, "code": "turn_cancelled"}


__all__ = ["P7ConversationService"]
