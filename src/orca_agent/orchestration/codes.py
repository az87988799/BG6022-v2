"""Closed sets of codes allowed to cross the durable-kernel boundary."""

from __future__ import annotations

from enum import StrEnum


class CancelReasonCode(StrEnum):
    USER_CANCELLED = "user_cancelled"
    OPERATOR_CANCELLED = "operator_cancelled"
    POLICY_CANCELLED = "policy_cancelled"
    SHUTDOWN_CANCELLED = "shutdown_cancelled"


class HandlerErrorCode(StrEnum):
    HANDLER_FAILED = "handler_failed"
    HANDLER_EXCEPTION = "handler_exception"
    INVALID_HANDLER_RESULT = "invalid_handler_result"
    DISPATCH_BLOCKED = "dispatch_blocked"
    STORAGE_BUSY = "storage_busy"


_HANDLER_MESSAGES = {
    HandlerErrorCode.HANDLER_FAILED: "The handler reported a controlled failure.",
    HandlerErrorCode.HANDLER_EXCEPTION: "The injected handler raised an exception.",
    HandlerErrorCode.INVALID_HANDLER_RESULT: "The handler returned an invalid result.",
    HandlerErrorCode.DISPATCH_BLOCKED: "The effect was blocked by dispatch policy.",
    HandlerErrorCode.STORAGE_BUSY: "The effect could not be persisted because storage was busy.",
}


def handler_error_message(code: HandlerErrorCode) -> str:
    return _HANDLER_MESSAGES[code]


__all__ = ["CancelReasonCode", "HandlerErrorCode", "handler_error_message"]
