"""Dependency injection for the P7 conversation, task, and query surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orca_agent.application.p7_conversation_service import P7ConversationService
from orca_agent.application.p7_query_service import P7QueryService
from orca_agent.application.p7_task_service import P7TaskService
from orca_agent.llm.baseline import BaselinePlanner
from orca_agent.llm.fake import FakePlanner
from orca_agent.llm.ports import PlannerPort


@dataclass(frozen=True)
class P7Runtime:
    """The complete P7 application graph for one state root."""

    conversation: P7ConversationService
    task: P7TaskService
    query: P7QueryService
    planner: PlannerPort


def build_p7_runtime(
    state_root: str | Path,
    *,
    planner_name: str = "baseline",
    allow_llm: bool = False,
    fallback: str = "none",
    allow_network: bool = False,
    backend_kind: str = "fake",
    allow_real_orca: bool = False,
    planner: PlannerPort | None = None,
) -> P7Runtime:
    """Build P7 with explicit, testable adapters and no hidden global state."""

    if fallback not in {"none", "baseline"}:
        raise ValueError("fallback must be none or baseline")
    if planner is None:
        if planner_name == "baseline":
            planner = BaselinePlanner()
        elif planner_name == "fake":
            planner = FakePlanner()
        elif planner_name == "deepseek_chat":
            from orca_agent.llm.deepseek_chat import DeepSeekChatAdapter

            planner = DeepSeekChatAdapter()
        else:
            raise ValueError("planner must be baseline, fake, or deepseek_chat")

    task = P7TaskService(
        state_root,
        allow_network=allow_network,
        backend_kind=backend_kind,
        allow_real_orca=allow_real_orca,
    )
    query = P7QueryService(state_root, task_service=task)
    conversation = P7ConversationService(
        state_root,
        task_service=task,
        query_service=query,
        planner=planner,
        fallback=fallback,
        allow_llm=allow_llm,
        planner_name=planner_name,
    )
    return P7Runtime(conversation=conversation, task=task, query=query, planner=planner)


__all__ = ["P7Runtime", "build_p7_runtime"]
