"""Strict real control gate classification; no inference from a status label."""

from datetime import datetime

from .local_runner import CONTROL_EXIT_CODE


def control_gate_status(receipt, expected):
    facts = receipt.get("stop_facts")
    if receipt.get("physical_start_count") != 1 or receipt.get("status") == "succeeded":
        return "NOT_EXERCISED"
    if not isinstance(facts, dict):
        return "NOT_EXERCISED"
    reason = "cancel_requested" if expected == "cancelled" else "wall_time_deadline"
    if not facts.get("request_sent"):
        return "NOT_EXERCISED"
    try:
        requested = datetime.fromisoformat(facts["requested_at_utc"])
        confirmed = datetime.fromisoformat(facts["confirmed_at_utc"])
        if requested.tzinfo is None or confirmed.tzinfo is None or confirmed < requested:
            return "FAIL"
    except (KeyError, ValueError, TypeError):
        return "FAIL"
    if (
        receipt.get("status") != expected
        or receipt.get("stop_reason") != reason
        or facts.get("schema") != "p5-stop/v1"
        or facts.get("reason") != reason
        or any(
            facts.get(key) is not True
            for key in (
                "identity_matched",
                "alive_before_request",
                "request_sent",
                "stop_confirmed",
                "tree_stopped",
            )
        )
        or facts.get("pid") != receipt.get("orca_pid")
        or facts.get("expected_created") is None
        or facts.get("expected_created") != receipt.get("orca_created_at")
        or facts.get("observed_created") != facts.get("expected_created")
        or facts.get("exit_code") != receipt.get("exit_code")
        or receipt.get("exit_code") != CONTROL_EXIT_CODE
        or not facts.get("requested_at_utc")
        or not facts.get("confirmed_at_utc")
    ):
        return "FAIL"
    return "PASS"
