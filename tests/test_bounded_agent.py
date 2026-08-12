from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import wraps
from typing import Any

import pytest

from nbtriage.bounded_agent import (
    AgentAction,
    AgentActionKind,
    AgentBudget,
    AgentEnvironment,
    AgentPolicyError,
    AgentRunState,
    AgentRunStatus,
    AgentStepClient,
    AgentStepRequest,
    AgentStepResponse,
    AgentStepUsage,
    AgentStopReason,
    BoundedAgentRunner,
    FinishDiagnosisAction,
    ReadRuntimeEvidenceAction,
    RequestEvidenceAction,
    RetrieveSupportEvidenceAction,
    RuntimeEvidenceView,
    SupportEvidenceScope,
    agent_action_envelope_json_schema,
    parse_agent_action,
)
from nbtriage.evidence_receipts import EvidenceReceipt
from nbtriage.rag import TrainCaseRetriever
from nbtriage.runtime_observations import (
    ObservationKind,
    ObservationOutcome,
    RuntimeEvidenceBundle,
    RuntimeObservation,
)


class _Script:
    def __init__(
        self,
        actions: list[AgentAction],
        *,
        usages: list[AgentStepUsage] | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self.actions = actions
        self.usages = usages or [
            AgentStepUsage(
                provider_requests=1,
                input_tokens=10,
                output_tokens=5,
                cost_microusd=10,
            )
            for _ in actions
        ]
        self.delay_seconds = delay_seconds
        self.requests: list[AgentStepRequest] = []

    def factory(self) -> AgentStepClient:
        script = self

        class _SingleStepClient:
            def __init__(self) -> None:
                self.called = False

            async def choose_action(self, request: AgentStepRequest) -> AgentStepResponse:
                assert not self.called
                self.called = True
                script.requests.append(request)
                if script.delay_seconds:
                    await asyncio.sleep(script.delay_seconds)
                index = len(script.requests) - 1
                return AgentStepResponse(
                    action=script.actions[index],
                    usage=script.usages[index],
                    provider_request_id=f"request-{index + 1}",
                    latency_ms=1,
                )

        return _SingleStepClient()


def async_test(function: Callable[..., Any]) -> Callable[..., None]:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        asyncio.run(function(*args, **kwargs))

    return wrapper


def _case(case_id: str = "case-1") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "source": {
            "owner": "nonebot",
            "repository": "plugin-demo",
            "issue_number": 1,
            "title": "plugin fails during startup",
            "body": "Python 3.12 raises DemoError while loading the plugin.",
            "labels": ["bug"],
        },
        "curation": {
            "oracle": {"hidden": "must never enter Agent input"},
            "fault_phase": "boot",
        },
    }


def _train_case() -> dict[str, Any]:
    return {
        "case_id": "train-1",
        "source": {
            "owner": "nonebot",
            "repository": "plugin-demo",
            "issue_number": 2,
            "title": "startup failure",
            "body": "DemoError was fixed by correcting plugin initialization.",
            "labels": ["bug"],
        },
    }


def _runtime_bundle() -> RuntimeEvidenceBundle:
    return RuntimeEvidenceBundle(
        schema_version=1,
        correlation_id="corr-1",
        generated_at="2026-08-09T00:00:00+00:00",
        observations=(
            RuntimeObservation(
                schema_version=1,
                observation_id="obs-1",
                correlation_id="corr-1",
                occurred_at="2026-08-09T00:00:00+00:00",
                kind=ObservationKind.MATCHER_COMPLETED,
                adapter_name="nonebot.adapters.onebot.v11.Adapter",
                event_name=None,
                plugin_name="nonebot_plugin_demo",
                matcher_name="nonebot_plugin_demo.matcher",
                api_name=None,
                outcome=ObservationOutcome.FAILED,
                exception_type="demo.DemoError",
                stack_modules=("nonebot_plugin_demo",),
            ),
        ),
        buffer_dropped_count=0,
    )


def _receipt(run_id: str = "run-1", case_id: str = "case-1") -> EvidenceReceipt:
    return EvidenceReceipt(
        schema_version=1,
        receipt_id="receipt-1",
        session_id=run_id,
        case_id=case_id,
        slot="logs",
        submitted_by="maintainer",
        collected_at="2026-08-09T00:00:00+00:00",
        redacted=True,
        content_sha256="a" * 64,
        byte_count=128,
        facts={
            "exception_type": "demo.DemoError",
            "stack_modules": ["nonebot_plugin_demo"],
            "line_count": 12,
        },
    )


def _finish(*, citations: list[str] | None = None) -> FinishDiagnosisAction:
    return FinishDiagnosisAction(
        version_values=["3.12"],
        missing_evidence=[],
        symptoms=["exception"],
        fault_phase="boot",
        candidate_owners=["plugin"],
        route="verify",
        answer="运行证据与同仓库案例都指向插件初始化失败。",
        citations=citations or [],
        decision_summary="证据足以形成可核验结论",
    )


def _budget(**overrides: Any) -> AgentBudget:
    values = {
        "max_turns": 4,
        "max_tool_calls": 3,
        "max_input_tokens": 1_000,
        "max_output_tokens": 500,
        "deadline_seconds": 5,
        "max_cost_microusd": 1_000,
        "max_no_progress_steps": 2,
    }
    values.update(overrides)
    return AgentBudget(**values)


def _runner(script: _Script, **budget_overrides: Any) -> BoundedAgentRunner:
    return BoundedAgentRunner(
        script.factory,
        provider="fake",
        model="fake-agent",
        budget=_budget(**budget_overrides),
    )


def _environment(
    *,
    runtime: RuntimeEvidenceBundle | None = None,
    receipts: dict[str, EvidenceReceipt] | None = None,
) -> AgentEnvironment:
    return AgentEnvironment(
        case=_case(),
        retriever=TrainCaseRetriever([_train_case()]),
        runtime_evidence=runtime,
        evidence_receipts=receipts,
    )


@async_test
async def test_agent_changes_action_after_normalized_observations() -> None:
    script = _Script(
        [
            ReadRuntimeEvidenceAction(
                view=RuntimeEvidenceView.FAILURE_DETAILS,
                decision_summary="先检查运行失败摘要",
            ),
            RetrieveSupportEvidenceAction(
                scope=SupportEvidenceScope.SAME_REPOSITORY,
                limit=1,
                decision_summary="再寻找同仓库相似案例",
            ),
            _finish(citations=["train-1"]),
        ]
    )

    state = await _runner(script).start(_environment(runtime=_runtime_bundle()), run_id="run-1")

    assert state.status is AgentRunStatus.COMPLETED
    assert state.stop_reason is AgentStopReason.COMPLETED
    assert state.usage.model_turns == 3
    assert state.usage.tool_calls == 2
    assert [step.action.kind for step in state.trajectory] == [
        "read_runtime_evidence",
        "retrieve_support_evidence",
        "finish_diagnosis",
    ]
    assert state.trajectory[0].observation is not None
    assert (
        state.trajectory[0].observation.content["observations"][0]["exception_type"]
        == "demo.DemoError"
    )
    assert state.outcome is not None
    assert state.outcome.citations == ("train-1",)
    assert "oracle" not in json_text(script.requests[0].prompt_payload())
    assert "hidden" not in json_text(script.requests[0].prompt_payload())
    assert AgentActionKind.READ_RUNTIME_EVIDENCE not in script.requests[1].allowed_actions
    assert AgentActionKind.RETRIEVE_SUPPORT_EVIDENCE not in script.requests[2].allowed_actions


@async_test
async def test_same_repository_scope_finds_case_beyond_global_top_twenty() -> None:
    other_repository_cases = [
        {
            "case_id": f"global-{index:02d}",
            "source": {
                "owner": "nonebot",
                "repository": f"other-{index:02d}",
                "issue_number": index + 2,
                "title": "plugin fails during startup",
                "body": "Python 3.12 raises DemoError while loading the plugin.",
                "labels": ["bug"],
            },
        }
        for index in range(20)
    ]
    same_repository_case = _train_case()
    same_repository_case["source"]["title"] = "Different topic"
    same_repository_case["source"]["body"] = "unrelated"
    script = _Script(
        [
            RetrieveSupportEvidenceAction(
                scope=SupportEvidenceScope.SAME_REPOSITORY,
                limit=1,
                decision_summary="寻找同仓库相似案例",
            ),
            _finish(citations=["train-1"]),
        ]
    )
    environment = AgentEnvironment(
        case=_case(),
        retriever=TrainCaseRetriever([*other_repository_cases, same_repository_case]),
    )

    state = await _runner(script).start(environment, run_id="run-repository-scope")

    observation = state.trajectory[0].observation
    assert observation is not None
    assert observation.status == "ok"
    assert observation.content["items"][0]["case_id"] == "train-1"
    assert len(observation.content["items"]) == 1
    assert state.stop_reason is AgentStopReason.COMPLETED


@async_test
async def test_evidence_request_pauses_and_resumes_without_repeating_model_action() -> None:
    script = _Script(
        [
            RequestEvidenceAction(slot="logs", decision_summary="需要结构化异常摘要"),
            _finish(),
        ]
    )
    runner = _runner(script)

    paused = await runner.start(_environment(), run_id="run-1")
    resumed = await runner.resume(
        paused,
        _environment(receipts={"logs": _receipt()}),
    )

    assert paused.status is AgentRunStatus.PAUSED
    assert paused.stop_reason is AgentStopReason.EVIDENCE_REQUIRED
    assert paused.pending_evidence_slot == "logs"
    assert resumed.status is AgentRunStatus.COMPLETED
    assert len(resumed.trajectory) == 2
    assert resumed.trajectory[0].observation is not None
    assert resumed.trajectory[0].observation.status == "ok"
    assert resumed.trajectory[0].observation.content["facts"]["line_count"] == 12
    assert resumed.usage.model_turns == 2


@async_test
async def test_resume_requires_receipt_bound_to_run_case_and_slot() -> None:
    script = _Script([RequestEvidenceAction(slot="logs", decision_summary="需要结构化异常摘要")])
    runner = _runner(script)
    paused = await runner.start(_environment(), run_id="run-1")

    with pytest.raises(AgentPolicyError, match="binding"):
        await runner.resume(
            paused,
            _environment(receipts={"logs": _receipt(run_id="other-run")}),
        )


@async_test
async def test_zero_tool_budget_exposes_only_finish_and_blocks_tool_action() -> None:
    script = _Script(
        [
            ReadRuntimeEvidenceAction(
                view=RuntimeEvidenceView.EXECUTION_PATH,
                decision_summary="尝试读取运行路径",
            )
        ]
    )

    state = await _runner(script, max_tool_calls=0).start(
        _environment(runtime=_runtime_bundle()), run_id="run-1"
    )

    assert script.requests[0].allowed_actions == (AgentActionKind.FINISH_DIAGNOSIS,)
    assert state.stop_reason is AgentStopReason.MAX_TOOL_CALLS
    assert state.usage.tool_calls == 0
    assert state.trajectory[0].observation is not None
    assert state.trajectory[0].observation.status == "blocked"


@async_test
async def test_empty_support_index_is_removed_from_allowed_actions() -> None:
    script = _Script([_finish()])
    environment = AgentEnvironment(
        case=_case(),
        retriever=TrainCaseRetriever([]),
        runtime_evidence=_runtime_bundle(),
    )

    state = await _runner(script).start(environment, run_id="run-no-support")

    assert state.status is AgentRunStatus.COMPLETED
    assert script.requests[0].allowed_actions == (
        AgentActionKind.READ_RUNTIME_EVIDENCE,
        AgentActionKind.REQUEST_EVIDENCE,
        AgentActionKind.FINISH_DIAGNOSIS,
    )


def test_action_envelope_schema_narrows_citations_to_observed_case_ids() -> None:
    schema = agent_action_envelope_json_schema(
        (AgentActionKind.FINISH_DIAGNOSIS,),
        allowed_citation_case_ids=("train-1",),
    )

    citations = schema["$defs"]["FinishDiagnosisAction"]["properties"]["citations"]
    assert citations["maxItems"] == 1
    assert citations["items"]["enum"] == ["train-1"]

    no_citations = agent_action_envelope_json_schema((AgentActionKind.FINISH_DIAGNOSIS,))
    assert (
        no_citations["$defs"]["FinishDiagnosisAction"]["properties"]["citations"]["maxItems"] == 0
    )


@async_test
async def test_repeated_action_stops_before_second_tool_execution() -> None:
    action = ReadRuntimeEvidenceAction(
        view=RuntimeEvidenceView.EXECUTION_PATH,
        decision_summary="读取运行路径",
    )
    script = _Script([action, action])

    state = await _runner(script).start(_environment(runtime=_runtime_bundle()), run_id="run-1")

    assert state.stop_reason is AgentStopReason.REPEATED_ACTION
    assert state.usage.tool_calls == 1
    assert len(state.trajectory) == 2
    assert state.trajectory[-1].observation is not None
    assert state.trajectory[-1].observation.content == {"reason": "repeated_action"}


@async_test
async def test_second_runtime_view_is_rejected_after_capability_was_observed() -> None:
    script = _Script(
        [
            ReadRuntimeEvidenceAction(
                view=RuntimeEvidenceView.EXECUTION_PATH,
                decision_summary="读取运行路径",
            ),
            ReadRuntimeEvidenceAction(
                view=RuntimeEvidenceView.FAILURE_DETAILS,
                decision_summary="读取失败摘要",
            ),
        ]
    )

    state = await _runner(script).start(_environment(), run_id="run-1")

    assert state.stop_reason is AgentStopReason.INVALID_ACTION
    assert state.usage.tool_calls == 1
    assert AgentActionKind.READ_RUNTIME_EVIDENCE not in script.requests[1].allowed_actions


@async_test
async def test_turn_token_cost_deadline_and_cancellation_budgets_fail_closed() -> None:
    read = ReadRuntimeEvidenceAction(
        view=RuntimeEvidenceView.EXECUTION_PATH,
        decision_summary="读取运行路径",
    )
    turn_state = await _runner(_Script([read]), max_turns=1).start(
        _environment(runtime=_runtime_bundle()), run_id="run-turn"
    )
    assert turn_state.stop_reason is AgentStopReason.MAX_TURNS

    token_script = _Script(
        [read],
        usages=[
            AgentStepUsage(
                provider_requests=1,
                input_tokens=10,
                output_tokens=501,
                cost_microusd=10,
            )
        ],
    )
    token_state = await _runner(token_script).start(
        _environment(runtime=_runtime_bundle()), run_id="run-token"
    )
    assert token_state.stop_reason is AgentStopReason.TOKEN_LIMIT
    assert token_state.usage.tool_calls == 0

    cost_script = _Script(
        [read],
        usages=[
            AgentStepUsage(
                provider_requests=1,
                input_tokens=10,
                output_tokens=5,
                cost_microusd=1_001,
            )
        ],
    )
    cost_state = await _runner(cost_script).start(
        _environment(runtime=_runtime_bundle()), run_id="run-cost"
    )
    assert cost_state.stop_reason is AgentStopReason.COST_LIMIT

    deadline_state = await _runner(
        _Script([read], delay_seconds=0.05), deadline_seconds=0.01
    ).start(_environment(runtime=_runtime_bundle()), run_id="run-deadline")
    assert deadline_state.stop_reason is AgentStopReason.DEADLINE

    cancelled = asyncio.Event()
    cancelled.set()
    cancel_script = _Script([read])
    cancel_state = await _runner(cancel_script).start(
        _environment(runtime=_runtime_bundle()),
        run_id="run-cancel",
        cancellation_event=cancelled,
    )
    assert cancel_state.stop_reason is AgentStopReason.CANCELLED
    assert cancel_script.requests == []


@async_test
async def test_unknown_cost_fails_closed_when_cost_budget_is_enabled() -> None:
    script = _Script(
        [_finish()],
        usages=[
            AgentStepUsage(
                provider_requests=1,
                input_tokens=10,
                output_tokens=5,
                cost_microusd=None,
            )
        ],
    )

    state = await _runner(script).start(_environment(), run_id="run-1")

    assert state.stop_reason is AgentStopReason.COST_UNKNOWN
    assert state.outcome is None


@async_test
async def test_safety_guard_stops_before_client_and_invalid_citation_is_rejected() -> None:
    safety_case = _case()
    safety_case["source"]["body"] = "api_key=abcdefghijklmnopqrstuvwxyz123456"
    safety_script = _Script([_finish()])
    safety_state = await _runner(safety_script).start(
        AgentEnvironment(case=safety_case, retriever=TrainCaseRetriever([])),
        run_id="run-safety",
    )
    assert safety_state.stop_reason is AgentStopReason.SAFETY_REJECTED
    assert safety_script.requests == []

    citation_state = await _runner(_Script([_finish(citations=["unseen-case"])])).start(
        _environment(), run_id="run-citation"
    )
    assert citation_state.stop_reason is AgentStopReason.INVALID_ACTION
    assert citation_state.outcome is None


@async_test
async def test_agent_state_round_trips_without_framework_messages_or_private_reasoning() -> None:
    state = await _runner(_Script([_finish()])).start(_environment(), run_id="run-1")

    restored = AgentRunState.model_validate(state.to_dict())

    assert restored == state
    serialized = json_text(state.to_dict())
    assert "ModelRequest" not in serialized
    assert "ToolCallPart" not in serialized
    assert "chain_of_thought" not in serialized


def test_project_action_schema_rejects_unknown_tools_and_extra_arguments() -> None:
    with pytest.raises(AgentPolicyError, match="schema validation"):
        parse_agent_action({"kind": "run_shell", "command": "whoami"})
    with pytest.raises(AgentPolicyError, match="schema validation"):
        parse_agent_action(
            {
                "kind": "request_evidence",
                "slot": "logs",
                "decision_summary": "需要日志",
                "url": "https://example.com",
            }
        )
    with pytest.raises(AgentPolicyError, match="schema validation"):
        parse_agent_action(
            {
                "kind": "finish_diagnosis",
                "version_values": ["Python 3.12"],
                "missing_evidence": [],
                "symptoms": ["made_up_symptom"],
                "fault_phase": "unknown",
                "candidate_owners": ["anyone"],
                "route": "guess",
                "answer": "不能通过领域 schema。",
                "citations": [],
                "decision_summary": "无效枚举必须在工具执行前失败",
            }
        )


@async_test
async def test_persisted_pause_state_must_remain_bound_to_pending_action() -> None:
    script = _Script([RequestEvidenceAction(slot="logs", decision_summary="需要结构化异常摘要")])
    state = await _runner(script).start(_environment(), run_id="run-1")
    payload = state.to_dict()
    payload["trajectory"] = []

    with pytest.raises(ValueError, match="paused Agent state"):
        AgentRunState.model_validate(payload)


def json_text(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
