"""Shared human/JSON terminal driver for the P7 conversation surface."""

from __future__ import annotations

import json
import queue
import sys
import threading
from collections import deque
from collections.abc import Callable
from typing import TextIO

from orca_agent.domain.ids import ConversationId

_STATUS_CACHE_SIZE = 256


class P7ChatDriver:
    """Run one bounded, approval-aware P7 conversation in a terminal.

    The driver is deliberately thin.  It owns terminal concerns only; task
    state, tokens, handoffs, and scientific values remain in application
    services.  ``auto_work`` means bounded fake/worker progress, never an
    automatic approval.
    """

    def __init__(
        self,
        runtime,
        conversation_id: str,
        *,
        auto_work: bool = True,
        max_effects: int = 8,
        max_seconds: float = 1.5,
        json_output: bool = False,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        on_conversation_changed: Callable[[str], None] | None = None,
    ) -> None:
        if max_effects < 1 or max_seconds <= 0:
            raise ValueError("chat progress bounds must be positive")
        self.runtime = runtime
        self.conversation_id = str(ConversationId(conversation_id))
        self.auto_work = auto_work
        self.max_effects = max_effects
        self.max_seconds = max_seconds
        self.json_output = json_output
        self.input_stream = input_stream or sys.stdin
        self.output_stream = output_stream or sys.stdout
        self.on_conversation_changed = on_conversation_changed
        self._seen_pending: deque[str] = deque(maxlen=_STATUS_CACHE_SIZE)
        self._seen_terminal: deque[str] = deque(maxlen=_STATUS_CACHE_SIZE)
        self._seen_activity: dict[str, deque[str]] = {}

    def run(self) -> int:
        """Serve stdin until EOF or an explicit exit command."""

        if not self.json_output:
            self._write_line(self._banner())
            self._write_line("P7 对话已启动。输入自然语言；/help 查看命令；输入 /exit 退出。")
        self._emit_state()
        lines: queue.Queue[str | None] = queue.Queue()

        def read_lines() -> None:
            while True:
                line = self.input_stream.readline()
                if not line:
                    lines.put(None)
                    return
                lines.put(line.rstrip("\r\n"))

        reader = threading.Thread(target=read_lines, name="p7-chat-input", daemon=True)
        reader.start()
        while True:
            if self.auto_work:
                self._progress_once()
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                return 0
            if not line.strip():
                continue
            result = self.handle_text(line)
            if result.get("code") == "exit":
                return 0

    def handle_text(self, text: str) -> dict[str, object]:
        """Handle one line and return the structured result for tests/callers."""

        stripped = text.strip()
        if not stripped:
            return {"accepted": True, "code": "empty_input"}
        if stripped.casefold() in {"退出", "quit", "exit"}:
            result = {"accepted": True, "code": "exit", "text": "已退出 P7 对话。"}
            self._emit(result)
            return result
        if stripped.startswith("/"):
            result = self._handle_command(stripped)
            self._emit(result)
            return result
        explicit = self._explicit_action(stripped)
        if explicit is not None:
            result = self._apply_action(*explicit)
        else:
            result = self._message(stripped)
        self._emit(result)
        if self.auto_work and result.get("accepted", True):
            self._progress_until_idle()
        return result

    def _message(self, text: str) -> dict[str, object]:
        # Generic acknowledgements are not enough to approve a scientific
        # action in the terminal.  Named decisions such as “接受计划” remain
        # explicit user input and are handled by the application boundary.
        if self._is_generic_ack(text) and self._has_pending_actions():
            return {
                "accepted": False,
                "conversation_id": self.conversation_id,
                "code": "explicit_token_required",
                "text": (
                    "请使用 /accept <token>，或明确输入"
                    "“确认草稿并准备/确认身份/确认并开始本次 Opt 计算”。"
                ),
            }
        try:
            return self.runtime.conversation.message(self.conversation_id, text)
        except Exception as error:
            return self._error("message_error", error)

    def _handle_command(self, command: str) -> dict[str, object]:
        parts = command.split(maxsplit=1)
        name = parts[0].casefold()
        argument = parts[1].strip() if len(parts) == 2 else ""
        if name in {"/exit", "/quit"}:
            return {"accepted": True, "code": "exit", "text": "已退出 P7 对话。"}
        if name == "/help":
            return {
                "accepted": True,
                "code": "help",
                "text": (
                    "/new 新会话；/resume <conversation_id> 恢复；/tasks 查看任务；"
                    "/status 查看当前任务；/use <任务> 切换当前任务；"
                    "/reconcile <任务> 核对既有 P5；/accept <token> 接受待办；"
                    "/reject <token> 拒绝待办；/exit 退出。"
                ),
            }
        if name == "/new":
            return self._new_conversation()
        if name == "/resume":
            if not argument:
                return {"accepted": False, "code": "conversation_required"}
            return self._resume(argument)
        if name == "/tasks":
            try:
                tasks = self.runtime.task.list_tasks(self.conversation_id)
                state = self.runtime.conversation.get_state(self.conversation_id)
                return {
                    "accepted": True,
                    "code": "tasks",
                    "conversation_id": self.conversation_id,
                    "selected_task_id": state.active_task_id,
                    "tasks": [
                        {
                            "task_id": item.task_id,
                            "alias": item.alias,
                            "summary": self._task_summary(item),
                            "state": item.state.value,
                            "revision": item.revision,
                            "selected": item.task_id == state.active_task_id,
                        }
                        for item in tasks
                    ],
                }
            except Exception as error:
                return self._error("tasks_error", error)
        if name == "/status":
            try:
                query = self.runtime.query.query(
                    self.conversation_id,
                    request={"kind": "conversation", "text": "当前任务状态"},
                )
                tasks = query.get("tasks", [])
                selected_id = query.get("selected_task_id")
                selected = next(
                    (
                        item
                        for item in tasks
                        if isinstance(item, dict) and item.get("task_id") == selected_id
                    ),
                    None,
                )
                if isinstance(selected, dict) and isinstance(query.get("execution_blocked"), dict):
                    selected = {**selected, "execution_blocked": query["execution_blocked"]}
                pending = [
                    item
                    for item in query.get("pending_actions", [])
                    if isinstance(item, dict)
                    and item.get("status", "pending") == "pending"
                    and (selected_id is None or item.get("task_id") == selected_id)
                ]
                return {
                    "accepted": True,
                    "code": "status",
                    "conversation_id": self.conversation_id,
                    "current_task": selected,
                    "selected_task_id": selected_id,
                    "pending_actions": pending,
                    "block_reason": self._block_reason(selected),
                    "query": query,
                }
            except Exception as error:
                return self._error("status_error", error)
        if name in {"/accept", "/approve", "/reject"}:
            if not argument:
                return {"accepted": False, "code": "token_required"}
            return self._apply_action(argument, "accept" if name != "/reject" else "reject")
        if name == "/use":
            if not argument:
                return {
                    "accepted": False,
                    "code": "task_required",
                    "text": "用法：/use <任务别名或任务ID>。",
                }
            try:
                result = self.runtime.conversation.select_task(self.conversation_id, argument)
                return {
                    "accepted": True,
                    "code": "task_selected",
                    "text": f"已切换当前任务：{result['task']['alias']}。",
                    **result,
                }
            except Exception as error:
                return self._error("task_select_error", error)
        if name == "/reconcile":
            if not argument:
                return {
                    "accepted": False,
                    "code": "task_required",
                    "text": "用法：/reconcile <任务别名或任务ID>。",
                }
            try:
                task = self._find_task(argument)
                if task is None:
                    raise ValueError("task ID or alias was not found")
                return {
                    "accepted": True,
                    "code": "reconciled",
                    "text": f"已请求核对任务 {task.alias} 的既有 P5 状态；不会新建计算。",
                    "response": self.runtime.task.reconcile_task(
                        self.conversation_id, task.task_id
                    ),
                }
            except Exception as error:
                return self._error("reconcile_error", error)
        return {"accepted": False, "code": "unknown_command", "text": "未知命令，请输入 /help。"}

    def _new_conversation(self) -> dict[str, object]:
        try:
            state = self.runtime.conversation.new_conversation()
            self.conversation_id = str(state["conversation_id"])
            self._seen_pending.clear()
            self._seen_terminal.clear()
            self._seen_activity.clear()
            self._notify_conversation_changed()
            return {"accepted": True, "code": "new_conversation", **state}
        except Exception as error:
            return self._error("new_conversation_error", error)

    def _resume(self, conversation_id: str) -> dict[str, object]:
        try:
            state = self.runtime.conversation.get_state(str(ConversationId(conversation_id)))
            self.conversation_id = str(state.conversation_id)
            self._seen_pending.clear()
            self._seen_terminal.clear()
            self._seen_activity.clear()
            self._notify_conversation_changed()
            return {
                "accepted": True,
                "code": "conversation_resumed",
                **state.model_dump(mode="json"),
            }
        except Exception as error:
            return self._error("resume_error", error)

    def _explicit_action(self, text: str) -> tuple[str, str] | None:
        parts = text.split()
        if len(parts) == 2 and parts[0].casefold() in {"accept", "approve", "接受", "批准"}:
            return parts[1], "accept"
        if len(parts) == 2 and parts[0].casefold() in {"reject", "拒绝"}:
            return parts[1], "reject"
        return None

    def _apply_action(self, token: str, decision: str) -> dict[str, object]:
        try:
            result = self.runtime.task.accept_action(
                self.conversation_id, token.strip(), decision=decision
            )
            task = result.get("task") if isinstance(result, dict) else None
            state = task.get("state") if isinstance(task, dict) else None
            return {
                "accepted": True,
                "code": "action_applied",
                "conversation_id": self.conversation_id,
                "decision": decision,
                "text": (
                    f"已{('接受' if decision == 'accept' else '拒绝')}待办；"
                    f"当前任务状态：{state or '已更新'}。"
                ),
                "response": result,
            }
        except Exception as error:
            return self._error("action_error", error)

    def _progress_until_idle(self) -> None:
        for _ in range(12):
            result = self._progress_once()
            if not result or int(result.get("effects", 0)) == 0:
                return

    def _progress_once(self) -> dict[str, object] | None:
        try:
            result = self.runtime.task.progress(
                self.conversation_id,
                max_effects=self.max_effects,
                max_seconds=self.max_seconds,
            )
        except Exception as error:
            result = self._error("progress_error", error)
        activity = result.get("activity", [])
        task_context: dict[str, dict[str, object]] = {}
        raw_tasks = result.get("tasks", [])
        if isinstance(raw_tasks, list):
            for item in raw_tasks:
                if not isinstance(item, dict):
                    continue
                task = item.get("task")
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("task_id", ""))
                pending = item.get("pending_actions", [])
                pending_types = tuple(
                    sorted(
                        str(action.get("action_type"))
                        for action in pending
                        if isinstance(action, dict) and action.get("action_type")
                    )
                )
                task_context[task_id] = {
                    "revision": task.get("revision"),
                    "state": task.get("state"),
                    "pending_types": pending_types,
                }
        visible_activity: list[object] = []
        if isinstance(activity, list):
            for item in activity:
                if not isinstance(item, dict):
                    continue
                task_key = str(item.get("task_id", "unknown"))
                context = task_context.get(task_key, {})
                signature = json.dumps(
                    {
                        "conversation_id": self.conversation_id,
                        "revision": item.get("revision", context.get("revision")),
                        "state": item.get("state", context.get("state")),
                        "current_node": item.get("node_id")
                        or item.get("primitive_id")
                        or item.get("step")
                        or item.get("phase"),
                        "pending_types": context.get("pending_types", ()),
                        "step": item.get("step"),
                        "phase": item.get("phase"),
                        "reason": item.get("reason"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                bucket = self._seen_activity.setdefault(
                    f"{self.conversation_id}:{task_key}", deque(maxlen=64)
                )
                if signature in bucket:
                    continue
                bucket.append(signature)
                visible_activity.append(item)
        if (
            result.get("effects")
            or visible_activity
            or result.get("code") not in {None, "progress"}
        ):
            progress = {"accepted": True, "code": "progress", **result}
            if result.get("code") not in {None, "progress"}:
                progress["accepted"] = bool(result.get("accepted", False))
            progress["activity"] = visible_activity
            self._emit(progress)
        self._emit_pending_and_terminal(result)
        return result

    def _emit_state(self) -> None:
        try:
            state = self.runtime.conversation.get_state(self.conversation_id)
            self._emit({"accepted": True, "code": "session", **state.model_dump(mode="json")})
        except Exception as error:
            self._emit(self._error("session_error", error))

    def _emit_pending_and_terminal(self, result: dict[str, object]) -> None:
        tasks = result.get("tasks", [])
        if not isinstance(tasks, list):
            return
        for item in tasks:
            if not isinstance(item, dict):
                continue
            task = item.get("task")
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id", ""))
            pending = item.get("pending_actions", [])
            if isinstance(pending, list):
                for action in pending:
                    if not isinstance(action, dict):
                        continue
                    token = str(action.get("token", ""))
                    if token and token not in self._seen_pending:
                        self._seen_pending.append(token)
                        self._emit(
                            {
                                "accepted": True,
                                "code": "pending_action",
                                "task_id": task_id,
                                "action_type": action.get("action_type"),
                                "token": token,
                                "text": self._action_text(action),
                            }
                        )
            state = str(task.get("state", ""))
            terminal_or_attention = {
                "result_ready",
                "ended_without_result",
                "reconciliation_required",
                "needs_clarification",
            }
            terminal_key = f"{self.conversation_id}:{task_id}:{task.get('revision')}:{state}"
            if state in terminal_or_attention and terminal_key not in self._seen_terminal:
                self._seen_terminal.append(terminal_key)
                try:
                    query = self.runtime.query.query(self.conversation_id, task_id=task_id)
                except Exception as error:
                    query = self._error("result_query_error", error)
                code = "result" if state == "result_ready" else "task_state"
                self._emit({"accepted": True, "code": code, **query})

    def _has_pending_actions(self) -> bool:
        try:
            pending = self.runtime.query.query(self.conversation_id).get("pending_actions", [])
            return any(
                isinstance(item, dict) and item.get("status") == "pending" for item in pending
            )
        except Exception:
            return False

    def _emit(self, payload: dict[str, object]) -> None:
        if self.json_output:
            self._write_line(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            return
        text = payload.get("text")
        token_notice = payload.get("code") == "pending_action" and payload.get("token")
        if isinstance(text, str) and text and not token_notice:
            self._write_line(text)
        responses = payload.get("responses")
        if isinstance(responses, list):
            for response in responses:
                if not isinstance(response, dict):
                    continue
                response_text = response.get("text")
                if isinstance(response_text, str) and response_text and response_text != text:
                    self._write_line(response_text)
                response_payload = response.get("payload")
                if isinstance(response_payload, dict):
                    view = response_payload.get("view")
                    if isinstance(view, dict) and view.get("rendered_text"):
                        self._write_line(str(view["rendered_text"]))
        code = payload.get("code")
        if code in {"pending_action", "action_applied"} and payload.get("token"):
            self._write_line(
                f"待办 {payload.get('action_type', '')} "
                f"token={payload['token']}；{payload.get('text', '')}"
            )
        elif code == "progress":
            activities = payload.get("activity", [])
            if isinstance(activities, list):
                for item in activities:
                    if isinstance(item, dict):
                        self._write_line(f"进度：{item.get('step', '继续处理')}。")
        elif code == "result":
            view = payload.get("view")
            if isinstance(view, dict) and view.get("rendered_text"):
                self._write_line(str(view["rendered_text"]))
        elif code == "tasks":
            tasks = payload.get("tasks", [])
            selected = payload.get("selected_task_id")
            self._write_line(
                f"会话内任务数：{len(tasks) if isinstance(tasks, list) else 0}；"
                f"当前任务：{selected or '未选择'}。"
            )
        elif code == "status":
            current = payload.get("current_task")
            if isinstance(current, dict):
                self._write_line(
                    f"当前任务：{current.get('alias', current.get('task_id'))}；"
                    f"状态：{current.get('state', 'unknown')}。"
                )
            else:
                self._write_line("当前没有选中的任务。")
            pending = payload.get("pending_actions", [])
            if isinstance(pending, list) and pending:
                self._write_line(f"待办数：{len(pending)}。")
            if payload.get("block_reason"):
                self._write_line(f"阻塞原因：{payload['block_reason']}")
        elif code in {"session", "new_conversation", "conversation_resumed"}:
            self._write_line(f"会话：{payload.get('conversation_id', self.conversation_id)}")
        elif code not in {None, "session"} and not text:
            self._write_line(f"{code}: {payload.get('error', '')}")

    def _write_line(self, text: str) -> None:
        self.output_stream.write(text + "\n")
        self.output_stream.flush()

    def _notify_conversation_changed(self) -> None:
        if self.on_conversation_changed is not None:
            self.on_conversation_changed(self.conversation_id)

    def _find_task(self, selector: str):
        value = selector.strip().casefold()
        tasks = self.runtime.task.list_tasks(self.conversation_id)
        matches = tuple(
            item
            for item in tasks
            if item.task_id == selector.strip() or item.alias.casefold() == value
        )
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _task_summary(task) -> str:
        request = task.request
        if request is None or request.molecule_value is None:
            return "未定义分子"
        kind = request.molecule_kind.value if request.molecule_kind else "molecule"
        return f"{kind}={request.molecule_value}"

    @staticmethod
    def _block_reason(task: dict[str, object] | None) -> str | None:
        if not isinstance(task, dict):
            return None
        state = task.get("state")
        if state == "reconciliation_required":
            return "下游 P5 启动状态未知；请使用 /reconcile。"
        if state == "execution_pending":
            return "等待用户逐项批准 P5 执行节点。"
        blocked = task.get("execution_blocked")
        if isinstance(blocked, dict):
            reasons = blocked.get("reasons", [])
            return "真实 ORCA 尚未就绪：" + "；".join(str(item) for item in reasons)
        return None

    def _banner(self) -> str:
        config = getattr(self.runtime, "config", None)
        if config is None:
            return "P7 profile=legacy planner=unknown backend=unknown"
        public = config.public_dict()
        return (
            "P7 "
            f"profile={public['profile']} model={public['model']} "
            f"identity={public['identity_provider']} backend={public['backend']} "
            f"budget={public['model_call_budget']} "
            f"ORCA={public['orca_version'] or '未配置'} "
            f"readiness={'ready' if public['execution_ready'] else 'blocked'}"
        )

    @staticmethod
    def _action_text(action: dict[str, object]) -> str:
        labels = {
            "accept_plan": "请明确确认草稿并准备",
            "confirm_identity": "请确认唯一分子身份候选",
            "approve_execution": "请明确确认并开始本次计算（当前 P5 节点）",
        }
        return labels.get(str(action.get("action_type")), "请明确处理这个待办")

    @staticmethod
    def _is_generic_ack(text: str) -> bool:
        collapsed = "".join(
            character for character in text.casefold() if character not in " \t\r\n，。,.!?！？"
        )
        return collapsed in {
            "好",
            "好的",
            "可以",
            "行",
            "继续",
            "这个",
        }

    @staticmethod
    def _error(code: str, error: Exception) -> dict[str, object]:
        return {"accepted": False, "code": code, "error": type(error).__name__, "text": str(error)}


__all__ = ["P7ChatDriver"]
