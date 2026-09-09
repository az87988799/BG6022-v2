"""Version identifiers for the P7 conversation and result-delivery layer."""

from __future__ import annotations

P7_SCHEMA_VERSION = 6
P7_CONVERSATION_ENGINE = "p7-conversation-v1"
P7_TASK_ENGINE = "p7-task-v1"
P7_POLICY_VERSION = 7
TURN_SCHEMA = "p7.turn.v1"
OUTPUT_SCHEMA = "p7.output.v1"
CAPABILITY_VIEW_SCHEMA = "p7.capability-view.v1"
PROMPT_VERSION = "p7.intake-plan.v1"
P7_RECORD_ENGINE = "p7-records-v1"

__all__ = [
    "CAPABILITY_VIEW_SCHEMA",
    "OUTPUT_SCHEMA",
    "P7_CONVERSATION_ENGINE",
    "P7_POLICY_VERSION",
    "P7_RECORD_ENGINE",
    "P7_SCHEMA_VERSION",
    "P7_TASK_ENGINE",
    "PROMPT_VERSION",
    "TURN_SCHEMA",
]
