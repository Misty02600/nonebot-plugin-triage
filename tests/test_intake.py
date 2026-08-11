import pytest

from nbtriage.intake import (
    IntakeAction,
    IntakeDisposition,
    IntakeError,
    IntakeReason,
    parse_intake_signals,
    route_intake,
)


def intake_payload(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "intake_id": "intake-1",
        "occurred_at": "2026-08-08T12:00:00+00:00",
        "trigger": "mention",
        "correlation_id": None,
        "user_intent": "unknown",
        "bot_relevance": "unknown",
        "command_status": "not_attempted",
        "runtime_status": "not_observed",
        "unsafe_detected": False,
    }
    payload.update(overrides)
    return payload


def decision_for(**overrides):
    return route_intake(parse_intake_signals(intake_payload(**overrides)))


def test_intake_signals_round_trip_without_raw_content() -> None:
    payload = intake_payload(
        trigger="reply_report",
        correlation_id="corr-1",
        user_intent="report_problem",
        bot_relevance="related",
    )

    signals = parse_intake_signals(payload)

    assert signals.to_dict() == payload


def test_capability_request_routes_to_guidance_without_execution() -> None:
    decision = decision_for(
        user_intent="discover_capability",
        bot_relevance="related",
    )

    assert decision.disposition is IntakeDisposition.CAPABILITY_GUIDANCE
    assert decision.action is IntakeAction.SHOW_CAPABILITY
    assert decision.reason is IntakeReason.CAPABILITY_REQUESTED
    assert decision.requires_follow_up is False


@pytest.mark.parametrize(
    "command_status",
    [
        "unknown_command",
        "prefix_error",
        "missing_argument",
        "invalid_argument",
        "permission_denied",
        "context_unavailable",
        "capability_disabled",
    ],
)
def test_command_rejection_routes_to_usage_correction(command_status: str) -> None:
    decision = decision_for(
        user_intent="report_problem",
        bot_relevance="related",
        command_status=command_status,
    )

    assert decision.disposition is IntakeDisposition.USAGE_ERROR
    assert decision.action is IntakeAction.EXPLAIN_COMMAND_ERROR
    assert decision.reason is IntakeReason.COMMAND_REJECTED
    assert decision.requires_follow_up is True


@pytest.mark.parametrize("runtime_status", ["failed", "wrong_behavior", "no_response"])
def test_runtime_failure_routes_to_incident_diagnosis(runtime_status: str) -> None:
    decision = decision_for(
        bot_relevance="related",
        command_status="parsed",
        runtime_status=runtime_status,
    )

    assert decision.disposition is IntakeDisposition.SUSPECTED_INCIDENT
    assert decision.action is IntakeAction.START_DIAGNOSIS
    assert decision.reason is IntakeReason.RUNTIME_FAILURE_OBSERVED


def test_explicit_problem_report_without_runtime_evidence_is_still_suspected() -> None:
    decision = decision_for(
        user_intent="report_problem",
        bot_relevance="related",
    )

    assert decision.disposition is IntakeDisposition.SUSPECTED_INCIDENT
    assert decision.action is IntakeAction.START_DIAGNOSIS
    assert decision.reason is IntakeReason.PROBLEM_REPORTED


def test_unrelated_request_explains_scope() -> None:
    decision = decision_for(bot_relevance="unrelated")

    assert decision.disposition is IntakeDisposition.OUT_OF_SCOPE
    assert decision.action is IntakeAction.EXPLAIN_SCOPE
    assert decision.reason is IntakeReason.EXPLICITLY_UNRELATED


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_intent": "discover_capability", "bot_relevance": "related"},
        {
            "user_intent": "report_problem",
            "bot_relevance": "related",
            "command_status": "missing_argument",
        },
        {
            "user_intent": "report_problem",
            "bot_relevance": "related",
            "command_status": "parsed",
            "runtime_status": "failed",
        },
        {"bot_relevance": "unrelated"},
    ],
)
def test_unsafe_guard_has_absolute_routing_priority(overrides: dict) -> None:
    decision = decision_for(**overrides, unsafe_detected=True)

    assert decision.disposition is IntakeDisposition.UNSAFE
    assert decision.action is IntakeAction.REFUSE
    assert decision.reason is IntakeReason.PRE_MODEL_SAFETY_GUARD
    assert decision.requires_follow_up is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"bot_relevance": "unrelated", "command_status": "parsed"},
        {"bot_relevance": "unrelated", "runtime_status": "failed"},
        {"user_intent": "report_problem", "runtime_status": "succeeded"},
        {"command_status": "missing_argument", "runtime_status": "succeeded"},
    ],
)
def test_conflicting_signals_request_one_question(overrides: dict) -> None:
    decision = decision_for(**overrides)

    assert decision.disposition is None
    assert decision.action is IntakeAction.ASK_ONE_QUESTION
    assert decision.reason is IntakeReason.CONFLICTING_STRUCTURED_SIGNALS
    assert decision.requires_follow_up is True


def test_insufficient_signals_request_one_question_without_claiming_fault() -> None:
    decision = decision_for()

    assert decision.to_dict() == {
        "schema_version": 1,
        "intake_id": "intake-1",
        "disposition": None,
        "action": "ask_one_question",
        "reason": "insufficient_structured_signals",
        "requires_follow_up": True,
    }


@pytest.mark.parametrize(
    "field",
    ["message", "command_text", "user_id", "group_id", "context", "model_prompt"],
)
def test_intake_schema_rejects_raw_or_identity_fields(field: str) -> None:
    payload = intake_payload()
    payload[field] = "must-not-enter-core"

    with pytest.raises(IntakeError, match="unsupported intake signal fields"):
        parse_intake_signals(payload)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"occurred_at": "2026-08-08T12:00:00"}, "timezone"),
        ({"trigger": "silent_monitor"}, "trigger is unsupported"),
        ({"unsafe_detected": 1}, "must be a boolean"),
        ({"correlation_id": "contains space"}, "correlation_id"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_intake_schema_rejects_invalid_contract(overrides: dict, message: str) -> None:
    with pytest.raises(IntakeError, match=message):
        parse_intake_signals(intake_payload(**overrides))


def test_route_revalidates_manually_constructed_signal_object() -> None:
    signals = parse_intake_signals(intake_payload())
    object.__setattr__(signals, "unsafe_detected", "false")

    with pytest.raises(IntakeError, match="must be a boolean"):
        route_intake(signals)
