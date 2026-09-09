"""Shared human/JSON terminal driver for the P7 conversation surface."""

from __future__ import annotations

import json
import queue
import sys
import threading
from collections.abc import Callable
from typing import TextIO

from orca_agent.domain.ids import ConversationId


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
        self._seen_pending: set[str] = set()
        self._seen_terminal: set[str] = set()

    def run(self) -> int:
        """Serve stdin until EOF or an explicit exit command."""

        if not self.json_output:
            self._write_line(
                "P7 对话已启动。输入自然语言；/help 查看命令；输入 /exit 退出。"
            )
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
                "text": "请使用 /accept <token> 或明确输入“接受计划/确认身份/批准执行”。",
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
                    "/status 查看当前任务；/accept <token> 接受待办；"
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
                return {
                    "accepted": True,
                    "code": "tasks",
                    "conversation_id": self.conversation_id,
                    "tasks": [item.model_dump(mode="json") for item in tasks],
                }
            except Exception as error:
                return self._error("tasks_error", error)
        if name == "/status":
            try:
                return self.runtime.query.query(
                    self.conversation_id,
                    request={"kind": "conversation", "text": "当前任务状态"},
                )
            except Exception as error:
                return self._error("status_error", error)
        if name in {"/accept", "/approve", "/reject"}:
            if not argument:
                return {"accepted": False, "code": "token_required"}
            return self._apply_action(argument, "accept" if name != "/reject" else "reject")
        if name == "/use":
            return {
                "accepted": True,
                "code": "task_selector",
                "text": (
                    "后续“当前任务”查询会按会话活动任务解析；"
                    "如有多个任务，请使用任务别名或 ID。"
                ),
                "selector": argument or None,
            }
        return {"accepted": False, "code": "unknown_command", "text": "未知命令，请输入 /help。"}

    def _new_conversation(self) -> dict[str, object]:
        try:
            state = self.runtime.conversation.new_conversation()
            self.conversation_id = str(state["conversation_id"])
            self._seen_pending.clear()
            self._seen_terminal.clear()
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
        if result.get("effects") or result.get("activity"):
            self._emit({"accepted": True, "code": "progress", **result})
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
                        self._seen_pending.add(token)
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
            if task.get("state") == "result_ready" and task_id not in self._seen_terminal:
                self._seen_terminal.add(task_id)
                try:
                    query = self.runtime.query.query(self.conversation_id, task_id=task_id)
                except Exception as error:
                    query = self._error("result_query_error", error)
                self._emit({"accepted": True, "code": "result", **query})

    def _has_pending_actions(self) -> bool:
        try:
            return bool(self.runtime.task_view(self.conversation_id))
        except AttributeError:
            try:
                pending = self.runtime.query.query(self.conversation_id).get("pending_actions", [])
                return any(
                    isinstance(item, dict) and item.get("status") == "pending"
                    for item in pending
                )
            except Exception:
                return False
        except Exception:
            return False

    def _emit(self, payload: dict[str, object]) -> None:
        if self.json_output:
            self._write_line(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            return
        text = payload.get("text")
        if isinstance(text, str) and text:
            self._write_line(text)
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
            self._write_line(f"会话内任务数：{len(tasks) if isinstance(tasks, list) else 0}。")
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

    @staticmethod
    def _action_text(action: dict[str, object]) -> str:
        labels = {
            "accept_plan": "请明确接受计算计划",
            "confirm_identity": "请确认唯一分子身份候选",
            "approve_execution": "请逐项批准这个 P5 执行节点",
        }
        return labels.get(str(action.get("action_type")), "请明确处理这个待办")

    @staticmethod
    def _is_generic_ack(text: str) -> bool:
        collapsed = "".join(
            character
            for character in text.casefold()
            if character not in " \t\r\n，。,.!?！？"
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
