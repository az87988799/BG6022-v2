"""Deterministic, read-only P7 result and conversation queries."""

from __future__ import annotations

import json
import re
from pathlib import Path

from orca_agent.application.p7_task_service import P7TaskService
from orca_agent.domain.ids import ConversationId, RunId
from orca_agent.domain.p7_task import (
    DeliveryRecord,
    OutputKind,
    OutputQuantity,
    OutputSpec,
)
from orca_agent.infrastructure.clock import Clock, SystemClock
from orca_agent.infrastructure.p7_records import P7RecordRepository
from orca_agent.infrastructure.unit_of_work import SQLiteUnitOfWork
from orca_agent.llm.ports import strict_json_loads
from orca_agent.presentation.p7_results import P7ResultPresenter


class TaskSelectionAmbiguousError(ValueError):
    """Raised when a context query cannot safely choose one task."""

    def __init__(self, candidates) -> None:
        self.candidates = tuple(candidates)
        labels = ", ".join(f"{item.alias} ({item.task_id})" for item in self.candidates)
        super().__init__(f"multiple tasks match; specify one of: {labels}")


class P7QueryService:
    """Read P7 state without invoking LLMs or downstream execution."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        clock: Clock | None = None,
        task_service: P7TaskService | None = None,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.clock = clock or SystemClock()
        self.task_service = task_service or P7TaskService(self.state_root, clock=self.clock)
        self.presenter = P7ResultPresenter()

    def query(
        self,
        conversation_id: ConversationId | str,
        *,
        request: dict[str, object] | None = None,
        task_id: str | None = None,
    ) -> dict[str, object]:
        conversation = str(conversation_id)
        request = {} if request is None else self._strict_request(request)
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            records = P7RecordRepository(uow.connection)
            state = records.get_conversation(conversation)
            if state is None:
                raise ValueError("conversation was not found")
            tasks = records.list_tasks(conversation)
            turns = records.list_turns(conversation, limit=12)
            pending = records.list_pending(conversation)
            uow.commit()
        result: dict[str, object] = {
            "conversation_id": conversation,
            "conversation": state.model_dump(mode="json"),
            "tasks": [item.model_dump(mode="json") for item in tasks],
            "turns": [item.model_dump(mode="json") for item in turns],
            "pending_actions": [item.model_dump(mode="json") for item in pending],
            "selected_task_id": None,
        }
        try:
            selected = self._select_task(tasks, state.active_task_id, task_id, request)
        except TaskSelectionAmbiguousError as error:
            result["error"] = {
                "code": "task_ambiguous",
                "candidates": [
                    {"task_id": item.task_id, "alias": item.alias, "state": item.state.value}
                    for item in error.candidates
                ],
            }
            return result
        result["selected_task_id"] = None if selected is None else selected.task_id
        if selected is None:
            if task_id or request.get("task_id") or request.get("task_alias"):
                result["error"] = {"code": "task_not_found_or_ambiguous"}
            return result
        view = self.task_service.task_view(conversation, selected.task_id)
        result["task"] = view["task"]
        result["task_sources"] = {
            key: value for key, value in view.items() if key in {"p4", "p5", "p6"}
        }
        result["task_status"] = selected.state.value
        query_text = str(request.get("text", "")).strip()
        if self._asks_for_method(query_text):
            result["method"] = self._method_view(selected)
        delivery = view.get("delivery")
        if delivery is not None:
            delivery_model = DeliveryRecord.model_validate_json(
                json.dumps(delivery, ensure_ascii=False), strict=True
            )
            output_spec = self._output_spec_from_request(
                request,
                fallback=delivery_model.output_spec,
            )
            # The delivery is the immutable scientific record.  Any layout,
            # precision, language, or quantity change is returned separately
            # as a typed view with its own hash.
            result["delivery"] = self.presenter.render(delivery_model, format="json")
            p5_view = self._read_projection(self.task_service.p5, selected.p5_run_id)
            p6_view = self._read_projection(self.task_service.p6, selected.p6_run_id)
            rendered_view = self.presenter.build_rendered_view(
                selected,
                delivery_model,
                output_spec=output_spec,
                p5_view=p5_view,
                p6_view=p6_view,
                now=self.clock.now_utc(),
            )
            result["rendered_from_delivery_id"] = delivery_model.delivery_id
            result["view"] = self.presenter.render_view(rendered_view, format="json")
        else:
            result["delivery"] = None
            result["view"] = None
        return result

    def messages(self, conversation_id: ConversationId | str) -> tuple[dict[str, object], ...]:
        with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
            uow.begin()
            state = P7RecordRepository(uow.connection).get_conversation(str(conversation_id))
            if state is None:
                raise ValueError("conversation was not found")
            values = P7RecordRepository(uow.connection).list_turns(str(conversation_id), limit=12)
            uow.commit()
        return tuple(item.model_dump(mode="json") for item in values)

    def verify(
        self,
        conversation_id: ConversationId | str,
        *,
        task_id: str | None = None,
    ) -> dict[str, object]:
        conversation = str(conversation_id)
        checks: list[dict[str, object]] = []
        try:
            with SQLiteUnitOfWork(state_root=self.state_root, clock=self.clock) as uow:
                uow.begin()
                records = P7RecordRepository(uow.connection)
                state = records.get_conversation(conversation)
                if state is None:
                    return {"valid": False, "code": "conversation_not_found"}
                tasks = records.list_tasks(conversation)
                selected = self._select_task(tasks, state.active_task_id, task_id, {})
                for turn in records.list_turns(conversation, limit=256):
                    checks.append({"turn_id": turn.turn_id, "valid": True, "kind": "turn_hash"})
                for item in tasks:
                    checks.append({"task_id": item.task_id, "valid": True, "kind": "task_binding"})
                    delivery = records.get_delivery(item.task_id)
                    if delivery is not None:
                        checks.append(
                            {
                                "delivery_id": delivery.delivery_id,
                                "valid": True,
                                "kind": "delivery_hash",
                            }
                        )
                uow.commit()
            if selected is not None:
                if selected.p5_run_id:
                    self.task_service.p5.inspect(RunId(selected.p5_run_id))
                    checks.append(
                        {"run_id": selected.p5_run_id, "valid": True, "kind": "p5_source"}
                    )
                if selected.p6_run_id:
                    self.task_service.p6.inspect(RunId(selected.p6_run_id))
                    checks.append(
                        {"run_id": selected.p6_run_id, "valid": True, "kind": "p6_source"}
                    )
            return {
                "valid": all(bool(item.get("valid")) for item in checks),
                "conversation_id": conversation,
                "task_id": None if selected is None else selected.task_id,
                "checks": checks,
            }
        except Exception as error:
            return {
                "valid": False,
                "conversation_id": conversation,
                "code": "integrity_error",
                "error": type(error).__name__,
                "checks": checks,
            }

    def link_existing_result(
        self,
        conversation_id: ConversationId | str,
        *,
        workflow: str,
        run_id: RunId | str,
    ) -> dict[str, object]:
        return self.task_service.link_existing_result(
            conversation_id,
            workflow=workflow,
            run_id=run_id,
        )

    def export(
        self,
        conversation_id: ConversationId | str,
        *,
        task_id: str | None = None,
        format: str = "md",
    ) -> dict[str, object] | str:
        result = self.query(conversation_id, task_id=task_id, request={"kind": "result"})
        delivery = result.get("delivery")
        if delivery is None:
            raise ValueError("selected task has no delivery")
        model = DeliveryRecord.model_validate_json(
            json.dumps(delivery, ensure_ascii=False), strict=True
        )
        return self.presenter.render(model, format=format)

    @staticmethod
    def _read_projection(service: object, run_id: str | None) -> object | None:
        if not run_id:
            return None
        try:
            return service.inspect(RunId(run_id))
        except Exception:
            return None

    @staticmethod
    def _asks_for_method(text: str) -> bool:
        lowered = text.casefold()
        return any(
            marker in lowered
            for marker in ("采用什么方法", "使用了什么方法", "什么方法", "which method", "method")
        )

    @staticmethod
    def _method_view(task) -> dict[str, object]:
        request = task.request
        plan = task.plan
        requested = (
            None
            if request is None or request.method is None
            else request.method.model_dump(mode="json")
        )
        return {
            "requested": requested,
            "profile_id": None if plan is None else plan.method_profile_id,
            "protocol_id": None if plan is None else plan.protocol_id,
            "environment": None if plan is None else plan.environment,
            "source": "task.request_and_plan",
        }

    @staticmethod
    def _strict_request(request: dict[str, object]) -> dict[str, object]:
        # Re-encode/decode with the same duplicate-key rejecting decoder used
        # at the HTTP/model edge.  This also rejects non-JSON values.
        value = strict_json_loads(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
        if not isinstance(value, dict):
            raise ValueError("query request must be a JSON object")
        allowed = {"kind", "task_id", "task_alias", "output_spec", "layout", "limit", "text"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"query request has unknown fields: {sorted(unknown)}")
        return value

    @staticmethod
    def _select_task(
        tasks, active_task_id: str | None, task_id: str | None, request: dict[str, object]
    ):
        requested_id = task_id or (str(request["task_id"]) if request.get("task_id") else None)
        if requested_id is not None:
            matches = tuple(item for item in tasks if item.task_id == requested_id)
            if len(matches) != 1:
                return None
            return matches[0]
        alias = request.get("task_alias")
        if alias is None and request.get("text"):
            alias = P7QueryService._alias_from_text(str(request["text"]))
        if alias is not None:
            normalized = str(alias).strip().casefold()
            if normalized in {
                "当前任务",
                "刚才那个",
                "刚才的任务",
                "上次",
                "上一个任务",
                "this task",
                "that task",
                "the current task",
            }:
                matches = tuple(item for item in tasks if item.task_id == active_task_id)
                if not matches and len(tasks) == 1:
                    matches = tasks
            else:
                matches = tuple(item for item in tasks if item.alias.casefold() == normalized)
            return matches[0] if len(matches) == 1 else None
        if active_task_id:
            matches = tuple(item for item in tasks if item.task_id == active_task_id)
            if len(matches) == 1:
                return matches[0]
        if len(tasks) == 1:
            return tasks[0]
        if len(tasks) == 0:
            return None
        raise TaskSelectionAmbiguousError(tasks)

    @staticmethod
    def _alias_from_text(text: str) -> str | None:
        lowered = text.casefold()
        if any(
            marker in lowered
            for marker in ("刚才", "上次", "当前任务", "this task", "that task")
        ):
            return "当前任务"
        return None

    @staticmethod
    def _output_spec_from_request(
        request: dict[str, object], *, fallback: OutputSpec
    ) -> OutputSpec:
        value = request.get("output_spec")
        if value is None and request.get("layout") is not None:
            value = {"layout": request["layout"]}
        if value is None and request.get("text"):
            text = str(request["text"])
            lowered = text.casefold()
            status_markers = ("算完了吗", "完成了吗", "状态", "status")
            value_markers = ("能量", "energy", "频率", "frequency", "结构", "geometry")
            if any(marker in lowered for marker in status_markers) and not any(
                marker in lowered for marker in value_markers
            ):
                value = {
                    "quantities": [{"kind": OutputKind.EXECUTION_STATUS.value, "required": True}],
                    "layout": "prose",
                }
            else:
                quantities = []
                if any(
                    marker in lowered
                    for marker in ("独立单点", "单点能量", "independent sp", "single point energy")
                ):
                    quantities.append(
                        {
                            "kind": OutputKind.INDEPENDENT_SP_ELECTRONIC_ENERGY.value,
                            "required": True,
                        }
                    )
                if any(marker in lowered for marker in ("优化结构", "optimized structure", "xyz")):
                    quantities.append(
                        {"kind": OutputKind.OPTIMIZED_GEOMETRY.value, "required": True}
                    )
                if any(marker in lowered for marker in ("频率", "vibrational", "frequency")):
                    quantities.append(
                        {"kind": OutputKind.VIBRATIONAL_FREQUENCIES.value, "required": True}
                    )
                if quantities:
                    value = {"quantities": quantities}
                precision = re.search(
                    r"(?:小数|decimal|precision)\s*(?:位|places)?\s*[:：=]?\s*(\d+)",
                    text,
                    re.I,
                )
                if precision:
                    value = {**(value or {}), "precision": int(precision.group(1))}
                if "table" in lowered or "表格" in lowered:
                    value = {**(value or {}), "layout": "table"}
        if value is None:
            return fallback
        if not isinstance(value, dict):
            raise ValueError("query output_spec must be an object")
        # Query display changes are deliberately constrained to an already
        # registered output contract; unknown quantities cannot be invented.
        values = fallback.model_dump(mode="json")
        values.pop("output_spec_hash", None)
        values.update(value)
        unknown = set(values) - {
            "schema_version",
            "quantities",
            "language",
            "style",
            "layout",
            "units",
            "precision",
            "artifacts",
            "include_evidence",
        }
        if unknown:
            raise ValueError(f"query output_spec has unknown fields: {sorted(unknown)}")
        if values.get("schema_version") != fallback.schema_version:
            raise ValueError("query output_spec schema version is unsupported")
        raw_quantities = values.get("quantities")
        if not isinstance(raw_quantities, list):
            raise ValueError("query output_spec.quantities must be an array")
        quantities: list[OutputQuantity] = []
        for item in raw_quantities:
            if not isinstance(item, dict):
                raise ValueError("query output_spec quantity must be an object")
            kind = item.get("kind")
            if kind not in {item.value for item in OutputKind}:
                raise ValueError(f"query output_spec kind is unsupported: {kind}")
            quantities.append(
                OutputQuantity(
                    kind=OutputKind(kind),
                    required=item.get("required", True),
                    source_selector=item.get("source_selector"),
                    unit=item.get("unit"),
                    label=item.get("label"),
                )
            )
        return OutputSpec.create(
            quantities=quantities,
            language=values.get("language", "zh"),
            style=values.get("style", "concise"),
            layout=values.get("layout", "prose"),
            units=values.get("units", {}),
            precision=values.get("precision"),
            artifacts=values.get("artifacts", ()),
            include_evidence=values.get("include_evidence", False),
        )


__all__ = ["P7QueryService", "TaskSelectionAmbiguousError"]
