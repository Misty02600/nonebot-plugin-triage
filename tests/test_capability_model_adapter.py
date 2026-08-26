from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic_ai import ModelResponse, TextPart, ThinkingPart, ToolCallPart, models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, RetryPromptPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

from nbtriage.capability_analysis import (
    BaselineChangeOperation,
    BaselineMemberField,
    CapabilityAnalysisBaseline,
    CapabilityAnalysisEntryBaseline,
    CapabilityAnalysisRequest,
    CapabilityAnalysisService,
    CapabilityEvidenceUnit,
    CapabilityFamilyMember,
    CapabilityGateCandidate,
    CapabilityGateKind,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    ConfigProjection,
    SemanticClaimKind,
    SemanticConstraint,
    SemanticConstraintKind,
    TeachingRole,
    TeachingScene,
)
from nbtriage.capability_annotations import project_capability_annotation
from nbtriage.capability_model_adapter import (
    ANCHORED_INSTRUCTION,
    BASELINE_INSTRUCTION,
    CORE_INSTRUCTION,
    FAMILY_INSTRUCTION,
    REGEX_INSTRUCTION,
    SYSTEM_INSTRUCTION,
    CapabilityAnalysisToolRuntime,
    CapabilityModelAdapterError,
    CapabilityModelAdapterReason,
    PydanticAICapabilityAnalysisClient,
    _instructions_for_request,
)

models.ALLOW_MODEL_REQUESTS = False

_NATIVE_PROFILE = ModelProfile(
    supports_json_schema_output=True,
    default_structured_output_mode="native",
)
_TOOL_PROFILE = ModelProfile(
    supports_tools=True,
    default_structured_output_mode="tool",
)


def _request() -> CapabilityAnalysisRequest:
    return CapabilityAnalysisRequest(
        capability=CapabilityIdentity(
            "plugin.demo:matcher.search",
            "plugin.demo",
            "command",
            "OneBot V11",
        ),
        evidence_units=(
            CapabilityEvidenceUnit(
                "evidence-handler",
                "python_function",
                'search = on_command("搜图")\n# SENTINEL_SOURCE',
                "sha256:source",
                "plugin.demo:search:12",
            ),
        ),
        config_projections=(
            ConfigProjection("config-enabled", "plugin_config.search_enabled", True),
        ),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "搜图",
            ),
        ),
    )


def _entry(
    *,
    usage: str = "搜图 [图片]",
    evidence_id: str = "evidence-handler",
    summary: str = "根据图片查找相似内容。",
) -> dict[str, object]:
    return {
        "entry_id": "root",
        "claims": [
            {
                "kind": "name",
                "statement": "图片搜索",
                "evidence_ids": ["evidence-handler"],
                "config_reference_ids": [],
            },
            {
                "kind": "summary",
                "statement": summary,
                "evidence_ids": [evidence_id],
                "config_reference_ids": [],
            },
            {
                "kind": "usage",
                "statement": usage,
                "evidence_ids": ["evidence-handler"],
                "config_reference_ids": [],
            },
        ],
        "constraints": [],
    }


def _output(**entry_kwargs: str) -> dict[str, object]:
    return {"knowledge_enabled": True, "entries": [_entry(**entry_kwargs)]}


def _native_response(**entry_kwargs: str) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(json.dumps(_output(**entry_kwargs), ensure_ascii=False))],
        finish_reason="stop",
    )


def test_agent_uses_native_output_and_bounded_source_payload() -> None:
    observed: dict[str, Any] = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        observed.update(messages=messages, info=info)
        return _native_response()

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        timeout_seconds=12,
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert result.entries[0].claims[0].kind is SemanticClaimKind.NAME
    messages = cast(list[ModelRequest], observed["messages"])
    assert messages[0].instructions == "\n\n".join((CORE_INSTRUCTION, ANCHORED_INSTRUCTION)).strip()
    prompt = cast(UserPromptPart, messages[0].parts[0])
    payload = json.loads(cast(str, prompt.content))
    assert payload["invocations"] == [
        {
            "entry_id": "root",
            "mode": "anchored",
            "command_body": "搜图",
            "regex_pattern": None,
            "regex_flags": [],
            "canonical_usages": [],
            "aliases": [],
            "requires_mention": False,
            "shortcut_count": 0,
            "shortcut_evidence_ids": [],
        }
    ]
    assert payload["gate_candidates"] == []
    assert "SENTINEL_SOURCE" in payload["evidence_units"][0]["content"]
    info = cast(AgentInfo, observed["info"])
    assert info.model_request_parameters.output_mode == "native"
    assert info.model_request_parameters.function_tools == []
    output_object = info.model_request_parameters.output_object
    assert output_object is not None
    assert set(output_object.json_schema["properties"]) == {
        "knowledge_enabled",
        "entries",
        "gate_resolutions",
    }


def test_prompt_fragments_follow_request_structure() -> None:
    anchored = _request()
    family = replace(
        anchored,
        invocations=(CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),),
    )
    regex = replace(
        anchored,
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.REGEX,
                regex_pattern=r"^(查图|搜图)$",
                regex_flags=("ignore_case",),
            ),
        ),
    )
    with_baseline = replace(
        anchored,
        previous_annotation=CapabilityAnalysisBaseline(),
    )
    mixed = replace(
        anchored,
        invocations=(
            *anchored.invocations,
            CapabilityInvocationTarget(
                "regex",
                CapabilityInvocationMode.REGEX,
                regex_pattern=r"^(查图|搜图)$",
            ),
            CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),
        ),
        previous_annotation=CapabilityAnalysisBaseline(),
    )

    assert _instructions_for_request(anchored) == "\n\n".join(
        (CORE_INSTRUCTION, ANCHORED_INSTRUCTION)
    )
    assert _instructions_for_request(family) == "\n\n".join((CORE_INSTRUCTION, FAMILY_INSTRUCTION))
    assert _instructions_for_request(regex) == "\n\n".join((CORE_INSTRUCTION, REGEX_INSTRUCTION))
    assert _instructions_for_request(with_baseline) == "\n\n".join(
        (CORE_INSTRUCTION, ANCHORED_INSTRUCTION, BASELINE_INSTRUCTION)
    )
    assert _instructions_for_request(mixed) == SYSTEM_INSTRUCTION


def test_regex_usage_is_valid_without_command_or_shortcut_contract() -> None:
    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.REGEX,
                regex_pattern=r"^(日群友|日群主|日管理|透群友|透群主|透管理)$",
            ),
        ),
    )

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        return _native_response(usage="(日|透)(群友 [@用户]|群主|管理)")

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        timeout_seconds=12,
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert result.entries[0].claims[2].statement == "(日|透)(群友 [@用户]|群主|管理)"


def test_agent_payload_marks_fixed_permission_as_model_external() -> None:
    observed: dict[str, Any] = {}

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        observed["messages"] = messages
        return _native_response()

    request = replace(
        _request(),
        fixed_constraints=(
            SemanticConstraint(
                SemanticConstraintKind.ROLE,
                "仅群管理员或群主可用",
                ("evidence-handler",),
                role=TeachingRole.ADMIN,
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    asyncio.run(CapabilityAnalysisService(client).analyze(request))

    messages = cast(list[ModelRequest], observed["messages"])
    prompt = cast(UserPromptPart, messages[0].parts[0])
    payload = json.loads(cast(str, prompt.content))
    assert payload["fixed_constraints"] == [
        {
            "kind": "role",
            "statement": "仅群管理员或群主可用",
            "evidence_ids": ["evidence-handler"],
            "config_reference_ids": [],
            "role": "admin",
            "allowed_scenes": [],
            "rate_limit_policy": None,
            "rate_limit_scope": None,
            "permission_alternatives": [],
        }
    ]


def test_agent_preserves_parser_usage_and_accepts_cited_shortcut_usage() -> None:
    shortcut_evidence_id = "evidence-shortcuts"
    base_request = _request()
    request = replace(
        base_request,
        evidence_units=(
            *base_request.evidence_units,
            CapabilityEvidenceUnit(
                shortcut_evidence_id,
                "runtime_capability_facts",
                '{"command.shortcuts":[{"pattern":"今日找图"}]}',
                "sha256:shortcuts",
            ),
        ),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "搜图",
                canonical_usages=("搜图 [slot:0]",),
                shortcut_count=1,
                shortcut_evidence_ids=(shortcut_evidence_id,),
            ),
        ),
    )
    output = _output(usage="搜图 [图片]")
    entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
    claims = cast(list[dict[str, object]], entry["claims"])
    claims.append(
        {
            "kind": "usage",
            "statement": "今日找图",
            "evidence_ids": [shortcut_evidence_id],
            "config_reference_ids": [],
        }
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: ModelResponse(
                parts=[TextPart(json.dumps(output, ensure_ascii=False))],
                finish_reason="stop",
            ),
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert [
        claim.statement
        for claim in result.entries[0].claims
        if claim.kind is SemanticClaimKind.USAGE
    ] == ["搜图 [图片]", "今日找图"]
    annotation = project_capability_annotation(
        request,
        result,
        analysis_revision="shortcut-test",
    )
    assert annotation.entries[0].usages == ("搜图 [图片]", "今日找图")


def test_agent_accepts_typed_scene_and_evidenced_rate_limit_exemption() -> None:
    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        output = _output()
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        entry["constraints"] = [
            {
                "kind": "scene",
                "statement": "仅群聊、频道或频道文字场景可用",
                "evidence_ids": ["evidence-handler"],
                "config_reference_ids": [],
                "role": None,
                "allowed_scenes": ["group", "guild", "channel_text"],
                "rate_limit_policy": None,
                "rate_limit_scope": None,
                "gate_candidate_ids": [],
                "permission_alternatives": [],
            },
            {
                "kind": "rate_limit",
                "statement": "每位用户每周有下载次数配额；超级用户不受此限制",
                "evidence_ids": ["evidence-handler"],
                "config_reference_ids": [],
                "role": None,
                "allowed_scenes": [],
                "rate_limit_policy": "quota",
                "rate_limit_scope": "user",
                "gate_candidate_ids": [],
                "permission_alternatives": [],
            },
        ]
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert result.entries[0].constraints[0].allowed_scenes == (
        TeachingScene.GROUP,
        TeachingScene.GUILD,
        TeachingScene.CHANNEL_TEXT,
    )
    assert result.entries[0].constraints[1].statement.endswith("超级用户不受此限制")


def test_analysis_records_last_response_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def capture_shape(response: ModelResponse | None, *, metadata: dict[str, str]) -> None:
        observed.update(response=response, metadata=metadata)

    monkeypatch.setattr(
        "nbtriage.capability_model_adapter.record_agent_response_shape",
        capture_shape,
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: _native_response(),
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    response = observed["response"]
    assert isinstance(response, ModelResponse)
    assert response.finish_reason == "stop"
    assert observed["metadata"] == {
        "nbtriage.task": "capability_annotation",
        "nbtriage.capability_id": "plugin.demo:matcher.search",
        "nbtriage.plugin_module": "plugin.demo",
    }


def test_opt_in_diagnostic_trace_includes_thinking_but_excludes_prompt() -> None:
    response = ModelResponse(
        parts=[
            ThinkingPart(
                "PRIVATE_THINKING",
                id="reasoning_content",
                provider_name="fixture-provider",
            ),
            TextPart(json.dumps(_output(), ensure_ascii=False)),
        ],
        finish_reason="stop",
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: response,
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
        capture_diagnostics=True,
    )

    asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    document = json.dumps(client.diagnostic_trace, ensure_ascii=False)
    assert '"message": "response"' in document
    assert '"kind": "assistant_thinking"' in document
    assert "PRIVATE_THINKING" in document
    assert "SENTINEL_SOURCE" not in document
    assert SYSTEM_INSTRUCTION.strip() not in document
    provider_document = json.dumps(client.diagnostic_provider_responses, ensure_ascii=False)
    assert '"kind": "assistant_thinking"' in provider_document
    assert "PRIVATE_THINKING" in provider_document


def test_unbounded_maintenance_diagnostics_remove_request_limit() -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        return _native_response(
            summary=(
                "根据图片\u200b查找相似内容。" if provider_calls == 1 else "根据图片查找相似内容。"
            )
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
        max_requests=1,
    )
    client.enable_maintenance_diagnostics(unbounded=True)

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert result.entries[0].claims[1].statement == "根据图片查找相似内容。"
    assert provider_calls == 2
    assert client.diagnostic_trace


def test_maintenance_diagnostics_capture_response_before_usage_limit() -> None:
    raw_output = "RAW_TRUNCATED_PROVIDER_OUTPUT"
    response = ModelResponse(
        parts=[TextPart(raw_output)],
        finish_reason="length",
        model_name="fixture-model",
        provider_name="fixture-provider",
        usage=RequestUsage(input_tokens=64_001, output_tokens=240),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: response,
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )
    client.enable_maintenance_diagnostics()

    with pytest.raises(CapabilityModelAdapterError) as error_info:
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert error_info.value.reason_code is CapabilityModelAdapterReason.BUDGET
    captured = client.diagnostic_provider_responses
    assert captured[0]["finish_reason"] == "length"
    assert captured[0]["parts"] == [{"kind": "assistant_text", "content": raw_output}]
    assert client.last_response is response
    assert client.last_usage is not None
    assert client.last_usage.input_tokens == 64_001


def test_maintenance_diagnostics_capture_redacted_http_error() -> None:
    async def fail(_messages, _info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(
            503,
            "fixture-model",
            {
                "error": {"code": "upstream_unavailable", "message": "retry later"},
                "access_token": "private-value",
            },
            headers={
                "authorization": "Bearer private-value",
                "retry-after": "2",
                "x-request-id": "request-123",
            },
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(fail, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )
    client.enable_maintenance_diagnostics()

    with pytest.raises(CapabilityModelAdapterError):
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert client.diagnostic_provider_errors == (
        {
            "request_index": 1,
            "status_code": 503,
            "model_name": "fixture-model",
            "retry_after_seconds": 2.0,
            "response_headers": {
                "retry-after": "2",
                "x-request-id": "request-123",
            },
            "body": {
                "format": "json",
                "content": (
                    '{"access_token":"[REDACTED]","error":'
                    '{"code":"upstream_unavailable","message":"retry later"}}'
                ),
                "truncated": False,
            },
        },
    )


def test_total_token_limit_accepts_received_valid_response_and_output_retry_stays_bounded() -> None:
    def run(*, repair_second_response: bool):
        provider_calls = 0

        def respond(_messages, _info: AgentInfo) -> ModelResponse:
            nonlocal provider_calls
            provider_calls += 1
            output = _output()
            if provider_calls == 1 or not repair_second_response:
                entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
                entry["entry_id"] = "other"
            return ModelResponse(
                parts=[TextPart(json.dumps(output, ensure_ascii=False))],
                usage=RequestUsage(input_tokens=60, output_tokens=5),
                finish_reason="stop",
            )

        client = PydanticAICapabilityAnalysisClient(
            FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
            max_output_tokens=240,
            total_tokens_limit=100,
        )
        return client, lambda: provider_calls

    valid_client, valid_calls = run(repair_second_response=True)
    result = asyncio.run(CapabilityAnalysisService(valid_client).analyze(_request()))

    assert result.entries[0].entry_id == "root"
    assert valid_calls() == 2
    assert valid_client.last_usage is not None
    assert valid_client.last_usage.total_tokens == 130

    invalid_client, invalid_calls = run(repair_second_response=False)
    with pytest.raises(CapabilityModelAdapterError) as error_info:
        asyncio.run(CapabilityAnalysisService(invalid_client).analyze(_request()))

    assert error_info.value.reason_code is CapabilityModelAdapterReason.OUTPUT_VALIDATION
    assert invalid_calls() == 2


def test_navigation_budget_reserves_a_final_submission_before_the_hard_limit() -> None:
    provider_calls = 0
    observed_tools: list[tuple[str, ...]] = []
    observed_instructions: list[str] = []

    def read_dependency() -> str:
        return "bounded evidence"

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        observed_tools.append(tuple(tool.name for tool in info.function_tools))
        observed_instructions.append(info.instructions or "")
        if provider_calls < 3:
            return ModelResponse(
                parts=[ToolCallPart("read_dependency", {}, f"call-read-{provider_calls}")],
                usage=RequestUsage(
                    input_tokens=45 if provider_calls == 1 else 20,
                    output_tokens=5,
                ),
            )
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, _output(), "call-output")],
            usage=RequestUsage(input_tokens=20, output_tokens=5),
            finish_reason="tool_call",
        )

    runtime = CapabilityAnalysisToolRuntime(
        toolsets=(FunctionToolset(tools=[read_dependency]),),
        evidence_units=tuple,
        validate_source_context=lambda: True,
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
        max_requests=10,
        max_tool_calls=7,
        total_tokens_limit=100,
        tool_runtime_factory=lambda _request: runtime,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert result.entries[0].entry_id == "root"
    assert observed_tools == [
        ("read_dependency",),
        ("read_dependency",),
        (),
    ]
    assert "最终提交预留阶段" in observed_instructions[1]
    assert "只读补证阶段已经结束" in observed_instructions[2]
    assert client.last_usage is not None
    assert client.last_usage.total_tokens == 100


def test_prompt_requires_complete_usage_literal_affix_self_check() -> None:
    assert "不得暴露源码路径、Python 符号" in SYSTEM_INSTRUCTION
    assert "密钥、令牌、凭据、认证头、请求参数及其传输方式属于实现机制" in (SYSTEM_INSTRUCTION)
    assert "不能为了满足 behavior_boundary 的分支说明要求而公开" in (SYSTEM_INSTRUCTION)
    assert "不得为了再次确认而重读整个文件" in SYSTEM_INSTRUCTION
    assert "不限于某一种事实" in SYSTEM_INSTRUCTION
    assert "取得足够证据或确认无法唯一判断后停止" in SYSTEM_INSTRUCTION
    assert "需要理解已知 Python 符号的定义时" in SYSTEM_INSTRUCTION
    assert "需要定位目标插件内的出现、调用或状态访问位置时" in SYSTEM_INSTRUCTION
    assert "`navigation_ref` 调用 `python_open_definition`" in SYSTEM_INSTRUCTION
    assert "版本化 framework Evidence" in SYSTEM_INSTRUCTION
    assert "不得依据预训练知识、库名或符号名" in SYSTEM_INSTRUCTION
    assert "入口直接比较调用者身份或角色时使用 role" in SYSTEM_INSTRUCTION
    assert "不得根据某种身份通常如何获得资格反推 role" in SYSTEM_INSTRUCTION
    assert "权限系统内部的默认授予、预分配或动态映射" in SYSTEM_INSTRUCTION
    assert "整个教学 entry 的共同角色、会话场景、使用资格和限流前提" in SYSTEM_INSTRUCTION
    assert "业务准备状态使用 `behavior_boundary` claim" in SYSTEM_INSTRUCTION
    assert "仅因业务准备状态通过 Permission 形式注册" in SYSTEM_INSTRUCTION
    assert "只限制部分业务分支时，不生成全局 permission" in SYSTEM_INSTRUCTION
    assert "用户能够通过公开业务操作理解、改变或满足" in SYSTEM_INSTRUCTION
    assert "运行配置、基础设施或外部服务就绪条件不是业务准备状态" in (SYSTEM_INSTRUCTION)
    assert "只限制特定 Option、子命令、输入类别、业务对象或结果分支" in SYSTEM_INSTRUCTION
    assert "直接从全集排除该原子" in SYSTEM_INSTRUCTION
    assert "不为重新确认这一集合运算继续导航框架或 Adapter 源码" in SYSTEM_INSTRUCTION
    assert "固定字面量、成员变量和 parser 参数结构" in SYSTEM_INSTRUCTION
    assert "逐字符保留成员变量前后的全部固定字面量" in SYSTEM_INSTRUCTION
    assert "看似格式控制的字符" in SYSTEM_INSTRUCTION
    assert "不得自行解释、删除或从示例补充" in SYSTEM_INSTRUCTION
    assert "字面量所有权无法确认时必须关闭知识" in SYSTEM_INSTRUCTION
    assert "源码工具预算有限" in FAMILY_INSTRUCTION
    assert "不得试图逐成员阅读" in FAMILY_INSTRUCTION
    assert "Uniseg `At` 是用户直接提供的 `@用户` 输入形式" in ANCHORED_INSTRUCTION


def test_prompt_separates_alias_display_from_usage_and_places_repeat_marker_after_slot() -> None:
    assert "不要修改 usage claim 中的 command_body" in SYSTEM_INSTRUCTION
    assert "entry.display_trigger" in SYSTEM_INSTRUCTION
    assert "command_body 与全部 aliases 做无损因式分解" in SYSTEM_INSTRUCTION
    assert "展开后必须恰好等于全部入口" in SYSTEM_INSTRUCTION
    assert "必须继续提取各入口重复的共同前缀、后缀或相邻备选位置" in SYSTEM_INSTRUCTION
    assert "不能因为原始入口总数超过四条就直接改成概念槽位" in SYSTEM_INSTRUCTION
    assert "禁言、口他、口她" not in SYSTEM_INSTRUCTION
    assert "不得用 `<指令>`、`<操作>` 等概念槽位覆盖" in SYSTEM_INSTRUCTION
    assert "`<参数>...` 表示至少一项、`[参数]...` 表示零项或多项" in SYSTEM_INSTRUCTION
    assert "同一 entry 默认只输出一条 usage" in ANCHORED_INSTRUCTION
    assert "最多三条只是最终公开展示的容量上限" in ANCHORED_INSTRUCTION
    assert "`检索 [范围] [@用户]`" in ANCHORED_INSTRUCTION


def test_regex_prompt_prefers_lossless_factoring_without_widening_branch_parameters() -> None:
    assert "默认只输出一条" in REGEX_INSTRUCTION
    assert "就必须继续合并" in REGEX_INSTRUCTION
    assert "分别展示更清楚" in REGEX_INSTRUCTION
    assert "只有单条表达无法准确保留" in REGEX_INSTRUCTION
    assert "只属于某个分支的参数必须留在该分支内" in REGEX_INSTRUCTION
    assert "`(查|删)(成员 [@用户]|群主)`" in REGEX_INSTRUCTION


def test_prompt_exempts_evidenced_shortcut_usage_from_canonical_command_body() -> None:
    assert "每条标准 Parser usage 都必须原样包含它一次" in SYSTEM_INSTRUCTION
    assert "shortcut usage 可以是完全不同的可调用文字" in SYSTEM_INSTRUCTION
    assert "不要求包含 command_body" in SYSTEM_INSTRUCTION


def test_prompt_preserves_supported_baseline_retrieval_fields() -> None:
    assert "模型外会按 entry_id 自动带回" in SYSTEM_INSTRUCTION
    assert "遗漏旧成员表示保持不变" in SYSTEM_INSTRUCTION
    assert "不要输出 keep" in SYSTEM_INSTRUCTION
    assert "baseline_changes" in SYSTEM_INSTRUCTION
    assert "每条 remove 或 replace 都必须引用" in SYSTEM_INSTRUCTION
    assert "最终输出自检" in SYSTEM_INSTRUCTION
    assert "其余旧成员不要重复输出" in SYSTEM_INSTRUCTION


def test_prompt_separates_routing_authorization_and_business_readiness() -> None:
    assert "platform_scope 是模型外拥有的 Runtime 路由事实" in SYSTEM_INSTRUCTION
    assert "调用者本人不必是授权者" in SYSTEM_INSTRUCTION
    assert "业务准备状态属于 behavior_boundary" in SYSTEM_INSTRUCTION
    assert "不能证明另一项能力的详细合同" in SYSTEM_INSTRUCTION
    assert "每条只能是一条可直接成为用户查询的独立短语" in SYSTEM_INSTRUCTION
    assert "以实际条件、状态更新和调度逻辑为准" in SYSTEM_INSTRUCTION
    assert "只描述用户看得见、用得上的行为" in SYSTEM_INSTRUCTION
    assert "内部持久化只有在其用户可观察效果有教学价值时才说明" in SYSTEM_INSTRUCTION
    assert "请求 JSON 中的 invocations" in SYSTEM_INSTRUCTION
    assert "payload." not in SYSTEM_INSTRUCTION


def test_agent_accepts_explicit_baseline_member_change() -> None:
    observed: dict[str, object] = {}
    request = replace(
        _request(),
        previous_annotation=CapabilityAnalysisBaseline(
            entries=(
                CapabilityAnalysisEntryBaseline(
                    "root",
                    search_terms=("封面",),
                    requirements=("旧权限文字不应进入新一轮生成",),
                ),
            )
        ),
    )
    output = _output()
    entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
    entry["baseline_changes"] = [
        {
            "op": "replace",
            "field": "search_terms",
            "old_value": "封面",
            "new_value": "短文标题",
            "evidence_ids": ["evidence-handler"],
            "config_reference_ids": [],
        }
    ]

    def respond(messages, _info) -> ModelResponse:
        observed["messages"] = messages
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    change = result.entries[0].baseline_changes[0]
    assert change.operation is BaselineChangeOperation.REPLACE
    assert change.field is BaselineMemberField.SEARCH_TERMS
    assert change.new_value == "短文标题"
    messages = cast(list[ModelRequest], observed["messages"])
    prompt = cast(UserPromptPart, messages[0].parts[0])
    payload = json.loads(cast(str, prompt.content))
    assert "requirements" not in payload["previous_annotation"]["entries"][0]


def test_agent_uses_profile_selected_output_tool() -> None:
    observed: dict[str, Any] = {}

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        observed["info"] = info
        output_tool = info.output_tools[0]
        output = _output()
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    output,
                    "call-1",
                )
            ],
            finish_reason="tool_call",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert len(result.entries) == 1
    info = cast(AgentInfo, observed["info"])
    assert info.model_request_parameters.output_mode == "tool"
    assert info.model_request_parameters.function_tools == []
    output_tool = info.output_tools[0]
    assert output_tool.name == "final_result"
    assert "knowledge_enabled、entries 和 gate_resolutions 三个顶层字段" in (
        output_tool.description or ""
    )
    assert "不得添加 payload、output 或 result 包装" in (output_tool.description or "")
    assert set(output_tool.parameters_json_schema["properties"]) == {
        "entries",
        "gate_resolutions",
        "knowledge_enabled",
    }
    assert output_tool.parameters_json_schema["required"] == ["knowledge_enabled"]


def test_client_allows_only_one_provider_run() -> None:
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: _native_response(),
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    asyncio.run(client.analyze(_request()))
    with pytest.raises(CapabilityModelAdapterError, match="model-call limit reached"):
        asyncio.run(client.analyze(_request()))


def test_agent_retries_when_model_changes_parser_owned_usage_structure() -> None:
    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return _native_response(usage="搜图 <图片>" if calls == 1 else "搜图 [搜索词]")

    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "搜图",
                ("搜图 [slot:0]",),
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert (
        next(
            claim.statement
            for claim in result.entries[0].claims
            if claim.kind is SemanticClaimKind.USAGE
        )
        == "搜图 [搜索词]"
    )


def test_agent_receives_aliases_and_retries_missing_required_mention() -> None:
    calls = 0
    observed: dict[str, object] = {}

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        observed["messages"] = messages
        output = _output(usage="状态" if calls == 1 else "@bot 状态")
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "状态",
                aliases=("运行状态",),
                requires_mention=True,
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    usage = next(
        claim.statement
        for claim in result.entries[0].claims
        if claim.kind is SemanticClaimKind.USAGE
    )
    assert usage == "@bot 状态"
    messages = cast(list[ModelRequest], observed["messages"])
    payload = json.loads(cast(str, cast(UserPromptPart, messages[0].parts[0]).content))
    assert payload["invocations"][0]["aliases"] == ["运行状态"]
    assert payload["invocations"][0]["requires_mention"] is True


def test_agent_receives_every_family_member_invocation() -> None:
    observed: dict[str, object] = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        observed["messages"] = messages
        observed["tools"] = tuple(tool.name for tool in info.function_tools)
        output = {
            "knowledge_enabled": True,
            "entries": [
                {
                    **_entry(usage="<表情操作> [图片|文字]..."),
                    "entry_id": "family",
                }
            ],
        }
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        capability=CapabilityIdentity("family:meme", "plugin.demo", "command_family"),
        evidence_units=(
            *_request().evidence_units,
            CapabilityEvidenceUnit(
                "evidence-family-members",
                "runtime_family_members",
                '{"members":[{"capability_id":"command:touch"},'
                '{"capability_id":"command:text-image"}]}',
                "sha256:family-members",
            ),
        ),
        invocations=(CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),),
        family_members=(
            CapabilityFamilyMember(
                "command:touch",
                (
                    CapabilityInvocationTarget(
                        "root",
                        CapabilityInvocationMode.ANCHORED,
                        "摸摸",
                        ("摸摸 <图片>",),
                    ),
                ),
                ("evidence-family-members",),
            ),
            CapabilityFamilyMember(
                "command:text-image",
                (
                    CapabilityInvocationTarget(
                        "root",
                        CapabilityInvocationMode.ANCHORED,
                        "文字图",
                        ("文字图 [文字]...",),
                    ),
                ),
                ("evidence-family-members",),
            ),
        ),
    )

    def inspect_family_source() -> str:
        return "unused"

    runtime = CapabilityAnalysisToolRuntime(
        toolsets=(FunctionToolset(tools=[inspect_family_source]),),
        evidence_units=lambda: (),
        validate_source_context=lambda: True,
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            respond,
            model_name="fixture-model",
            profile=ModelProfile(
                supports_tools=True,
                supports_json_schema_output=True,
                default_structured_output_mode="native",
            ),
        ),
        max_output_tokens=240,
        tool_runtime_factory=lambda _request: runtime,
    )

    asyncio.run(CapabilityAnalysisService(client).analyze(request))

    messages = cast(list[ModelRequest], observed["messages"])
    payload = json.loads(cast(str, cast(UserPromptPart, messages[0].parts[0]).content))
    assert "family_members" not in payload
    assert payload["family_manifest"] == {
        "member_count": 2,
        "evidence_ids": ["evidence-family-members"],
    }
    assert observed["tools"] == ("inspect_family_source",)
    assert "文字图 [文字]..." not in cast(str, cast(UserPromptPart, messages[0].parts[0]).content)


def test_complete_family_accepts_generic_parameter_slot_after_category_correction() -> None:
    calls = 0
    retry_prompts: list[str] = []

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        retry_prompts.extend(
            cast(str, part.content)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, RetryPromptPart)
        )
        usage = "<操作> [数值] <图片>..." if calls == 1 else "<操作> [参数] <图片>..."
        output = {
            "knowledge_enabled": True,
            "entries": [
                {
                    "entry_id": "family",
                    "claims": [
                        {
                            "kind": "name",
                            "statement": "图片操作",
                            "evidence_ids": ["evidence-handler"],
                        },
                        {
                            "kind": "summary",
                            "statement": "执行多种图片处理操作",
                            "evidence_ids": ["evidence-handler"],
                        },
                        {
                            "kind": "usage",
                            "statement": usage,
                            "evidence_ids": ["evidence-family-shapes"],
                        },
                    ],
                    "constraints": [],
                }
            ],
        }
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        capability=CapabilityIdentity("family:image", "plugin.demo", "command_family"),
        evidence_units=(
            *_request().evidence_units,
            CapabilityEvidenceUnit(
                "evidence-family-shapes",
                "runtime_family_shapes",
                json.dumps(
                    {
                        "shapes": [
                            {
                                "arguments": [
                                    {"pattern_type": "builtins.str"},
                                    {"pattern_type": "nonebot_plugin_alconna.uniseg.segment.Image"},
                                ]
                            },
                            {
                                "arguments": [
                                    {"pattern_type": "builtins.float"},
                                    {"pattern_type": "nonebot_plugin_alconna.uniseg.segment.Image"},
                                ]
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                "sha256:family-shapes",
            ),
        ),
        invocations=(CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert any("field=entries[family].usage" in item for item in retry_prompts)
    assert any("同时包含文本与数值类型槽位" in item for item in retry_prompts)
    assert any("不得把 builtins.str 自动解释成“文字”" in item for item in retry_prompts)
    assert all("缺失=文字" not in item for item in retry_prompts)
    assert (
        next(
            claim.statement
            for claim in result.entries[0].claims
            if claim.kind is SemanticClaimKind.USAGE
        )
        == "<操作> [参数] <图片>..."
    )


def test_complete_family_retry_reports_missing_category_without_prescribing_usage() -> None:
    calls = 0
    retry_prompts: list[str] = []

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        retry_prompts.extend(
            cast(str, part.content)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, RetryPromptPart)
        )
        usage = "<操作> <图片>..." if calls == 1 else "<操作> [数值] <图片>..."
        output = {
            "knowledge_enabled": True,
            "entries": [
                {
                    "entry_id": "family",
                    "claims": [
                        {
                            "kind": "name",
                            "statement": "图片操作",
                            "evidence_ids": ["evidence-handler"],
                        },
                        {
                            "kind": "summary",
                            "statement": "执行多种图片处理操作",
                            "evidence_ids": ["evidence-handler"],
                        },
                        {
                            "kind": "usage",
                            "statement": usage,
                            "evidence_ids": ["evidence-family-shapes"],
                        },
                    ],
                    "constraints": [],
                }
            ],
        }
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        capability=CapabilityIdentity("family:image", "plugin.demo", "command_family"),
        evidence_units=(
            *_request().evidence_units,
            CapabilityEvidenceUnit(
                "evidence-family-shapes",
                "runtime_family_shapes",
                json.dumps(
                    {
                        "shapes": [
                            {
                                "arguments": [
                                    {
                                        "pattern_type": (
                                            "nonebot_plugin_alconna.uniseg.segment.Image"
                                        )
                                    }
                                ]
                            },
                            {
                                "arguments": [
                                    {"pattern_type": "builtins.float"},
                                    {
                                        "pattern_type": (
                                            "nonebot_plugin_alconna.uniseg.segment.Image"
                                        )
                                    },
                                ]
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                "sha256:family-shapes",
            ),
        ),
        invocations=(CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert any("family_usage_missing_input_category" in item for item in retry_prompts)
    assert any("缺少对应结构槽位=数值" in item for item in retry_prompts)
    assert all("操作参数" not in item for item in retry_prompts)
    assert (
        next(
            claim.statement
            for claim in result.entries[0].claims
            if claim.kind is SemanticClaimKind.USAGE
        )
        == "<操作> [数值] <图片>..."
    )


def test_complete_family_retry_preserves_uniseg_mention_input() -> None:
    calls = 0
    retry_prompts: list[str] = []

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        retry_prompts.extend(
            cast(str, part.content)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, RetryPromptPart)
        )
        usage = "<表情名> [图片|文字]..." if calls == 1 else "<表情名> [图片|文字|@用户]..."
        output = {
            "knowledge_enabled": True,
            "entries": [
                {
                    "entry_id": "family",
                    "claims": [
                        {
                            "kind": "name",
                            "statement": "表情制作",
                            "evidence_ids": ["evidence-handler"],
                        },
                        {
                            "kind": "summary",
                            "statement": "使用不同模板生成表情",
                            "evidence_ids": ["evidence-handler"],
                        },
                        {
                            "kind": "usage",
                            "statement": usage,
                            "evidence_ids": ["evidence-family-shapes"],
                        },
                    ],
                    "constraints": [],
                }
            ],
        }
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    union_type = (
        "typing.Union[nonebot_plugin_alconna.uniseg.segment.At,"
        "nonebot_plugin_alconna.uniseg.segment.Image,"
        "nonebot_plugin_alconna.uniseg.segment.Text]"
    )
    request = replace(
        _request(),
        capability=CapabilityIdentity("family:meme", "plugin.demo", "command_family"),
        evidence_units=(
            *_request().evidence_units,
            CapabilityEvidenceUnit(
                "evidence-family-shapes",
                "runtime_family_shapes",
                json.dumps(
                    {"shapes": [{"arguments": [{"pattern_type": union_type}]}]},
                    ensure_ascii=False,
                ),
                "sha256:family-shapes",
            ),
        ),
        invocations=(CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert any("缺少对应结构槽位=@用户（Uniseg At）" in item for item in retry_prompts)
    assert (
        next(
            claim.statement
            for claim in result.entries[0].claims
            if claim.kind is SemanticClaimKind.USAGE
        )
        == "<表情名> [图片|文字|@用户]..."
    )


def test_agent_uses_factored_expression_for_more_than_four_fixed_aliases() -> None:
    aliases = ("禁他", "禁她", "口他", "口她", "踩他", "踩她")

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        output = _output(usage="禁言 <用户>")
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        entry["display_trigger"] = "(禁言|(禁|口|踩)(他|她))"
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "禁言",
                aliases=aliases,
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert result.entries[0].display_trigger == "(禁言|(禁|口|踩)(他|她))"


def test_agent_retries_alias_pattern_once_then_uses_deterministic_fallback() -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        output = _output(usage="禁言 <用户>")
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        entry["display_trigger"] = "(禁言|口他)"
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "禁言",
                aliases=("口他", "禁她"),
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert provider_calls == 2
    assert result.entries[0].display_trigger == "(禁言|口他|禁她)"


def test_agent_retries_rate_limit_text_that_omits_cited_numeric_config() -> None:
    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        output = _output()
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        entry["constraints"] = [
            {
                "kind": "rate_limit",
                "statement": "每名用户存在使用冷却" if calls == 1 else "每名用户有 30 秒冷却",
                "evidence_ids": ["evidence-handler"],
                "config_reference_ids": ["config-cooldown"],
                "role": None,
                "rate_limit_policy": "cooldown",
                "rate_limit_scope": "user",
            }
        ]
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        config_projections=(ConfigProjection("config-cooldown", "plugin_config.cooldown", 30),),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert result.entries[0].constraints[0].statement == "每名用户有 30 秒冷却"


def test_agent_retries_public_config_value_that_omits_reference() -> None:
    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        output = _output(summary="最多返回 7 条新闻")
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        summary = cast(list[dict[str, object]], entry["claims"])[1]
        summary["config_reference_ids"] = [] if calls == 1 else ["config-limit"]
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        config_projections=(ConfigProjection("config-limit", "plugin_config.limit", 7),),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert result.entries[0].claims[1].config_reference_ids == ("config-limit",)


def test_agent_retries_entry_without_summary() -> None:
    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        output = _output()
        if calls == 1:
            entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
            entry["claims"] = [
                claim
                for claim in cast(list[dict[str, object]], entry["claims"])
                if claim["kind"] != "summary"
            ]
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert calls == 2
    assert any(claim.kind is SemanticClaimKind.SUMMARY for claim in result.entries[0].claims)


def test_model_output_rejects_removed_answer_markdown_channel() -> None:
    output = _output()
    entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
    entry["answer_markdown"] = "根据证据，这个 handler 可以搜索图片。"
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: ModelResponse(
                parts=[TextPart(json.dumps(output, ensure_ascii=False))],
                finish_reason="stop",
            ),
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    with pytest.raises(CapabilityModelAdapterError, match="output validation failed"):
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))


def test_agent_retries_when_complete_usage_enumerates_more_than_four_members() -> None:
    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        usage = "#(摸摸|亲亲|贴贴|白底|旋转) [图片]" if calls == 1 else "#<表情名> [图片]"
        output = {
            "knowledge_enabled": True,
            "entries": [{**_entry(usage=usage), "entry_id": "family"}],
        }
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        invocations=(CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert (
        next(
            claim.statement
            for claim in result.entries[0].claims
            if claim.kind is SemanticClaimKind.USAGE
        )
        == "#<表情名> [图片]"
    )


def test_agent_retries_when_one_matcher_emits_more_than_three_fixed_usages() -> None:
    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        entry = _entry(usage="调色 红")
        claims = cast(list[dict[str, object]], entry["claims"])
        if calls == 1:
            claims.extend(
                {
                    "kind": "usage",
                    "statement": f"调色 {color}",
                    "evidence_ids": ["evidence-handler"],
                    "config_reference_ids": [],
                }
                for color in ("蓝", "绿", "黄")
            )
        else:
            claims[-1]["statement"] = "调色 <颜色>"
        return ModelResponse(
            parts=[
                TextPart(
                    json.dumps(
                        {"knowledge_enabled": True, "entries": [entry]},
                        ensure_ascii=False,
                    )
                )
            ],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "调色",
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert [
        claim.statement
        for claim in result.entries[0].claims
        if claim.kind is SemanticClaimKind.USAGE
    ] == ["调色 <颜色>"]


def test_agent_can_cite_revision_bound_read_evidence() -> None:
    provider_calls = 0
    dynamic = CapabilityEvidenceUnit(
        "evidence:file:dependency",
        "approved_file_excerpt",
        "def check(): return False",
        f"sha256:{'2' * 64}",
        "python_purelib/package.py",
    )

    def read_dependency() -> dict[str, object]:
        return {
            "citable": True,
            "evidence_id": dynamic.evidence_id,
            "content": dynamic.content,
            "revision": dynamic.revision,
        }

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("read_dependency", {}, "call-read")],
                usage=RequestUsage(input_tokens=100, output_tokens=10),
            )
        output = _output(evidence_id=dynamic.evidence_id)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    output,
                    "call-output",
                )
            ],
            usage=RequestUsage(input_tokens=100, output_tokens=20),
            finish_reason="tool_call",
        )

    runtime = CapabilityAnalysisToolRuntime(
        toolsets=(FunctionToolset(tools=[read_dependency]),),
        evidence_units=lambda: (dynamic,),
        validate_source_context=lambda: True,
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
        tool_runtime_factory=lambda _request: runtime,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert provider_calls == 2
    assert result.evidence_units == (dynamic,)


def test_agent_retries_complete_usage_without_a_family_member_selector() -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        usage = "滤镜 <图片>" if provider_calls == 1 else "<滤镜名> <图片>"
        output = {
            "knowledge_enabled": True,
            "entries": [{**_entry(usage=usage), "entry_id": "family"}],
        }
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        invocations=(CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE),),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert provider_calls == 2
    usage = next(
        item.statement for item in result.entries[0].claims if item.kind is SemanticClaimKind.USAGE
    )
    assert usage == "<滤镜名> <图片>"


@pytest.mark.parametrize(
    "invalid_usage",
    [
        "{command} [图片]",
        "[图片]",
        "搜图 搜图 [图片]",
        "搜图 <图片> 后发送下一页",
        "(查天气 <城市>|翻译 <文本>|随机语录)",
    ],
)
def test_usage_contract_retries_invalid_complete_usages(invalid_usage: str) -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        return _native_response(usage=invalid_usage if provider_calls == 1 else "搜图 [图片]")

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    usage = next(
        item.statement for item in result.entries[0].claims if item.kind is SemanticClaimKind.USAGE
    )
    assert usage == "搜图 [图片]"
    assert provider_calls == 2


@pytest.mark.parametrize(
    ("first_usages", "compact_usage"),
    [
        (("搜图 [图片]", "搜图 [图片] [文字]"), "搜图 [图片] [文字]"),
        (("搜图", "搜图 <图片>"), "搜图 [图片]"),
    ],
)
def test_usage_contract_retries_redundant_optional_variants(
    first_usages: tuple[str, str],
    compact_usage: str,
) -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        output = _output(usage=compact_usage if provider_calls > 1 else first_usages[0])
        if provider_calls == 1:
            entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
            claims = cast(list[object], entry["claims"])
            extra_usage = dict(cast(dict[str, object], claims[-1]))
            extra_usage["statement"] = first_usages[1]
            claims.append(extra_usage)
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    usages = tuple(
        item.statement for item in result.entries[0].claims if item.kind is SemanticClaimKind.USAGE
    )
    assert usages == (compact_usage,)
    assert provider_calls == 2


def test_complete_usage_retries_full_invocations_inside_alternation() -> None:
    provider_calls = 0
    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.COMPLETE,
            ),
        ),
    )

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        usage = (
            "(查天气 <城市>|翻译 <文本>|随机语录)"
            if provider_calls == 1
            else "(旋转|镜像|灰度) [图片]"
        )
        return _native_response(usage=usage)

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    usage = next(
        item.statement for item in result.entries[0].claims if item.kind is SemanticClaimKind.USAGE
    )
    assert usage == "(旋转|镜像|灰度) [图片]"
    assert provider_calls == 2


def test_output_validation_failure_preserves_provider_usage() -> None:
    provider_calls = 0
    output = _output()
    cast(dict[str, object], cast(list[object], output["entries"])[0])["entry_id"] = "other"

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            usage=RequestUsage(input_tokens=100, output_tokens=10),
            finish_reason="stop",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            respond,
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    with pytest.raises(
        CapabilityModelAdapterError,
        match="output validation failed",
    ):
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert provider_calls > 1
    assert client.last_response is not None
    assert client.last_usage is not None
    assert client.last_usage.requests == provider_calls
    assert client.last_usage.input_tokens == 100 * provider_calls
    assert client.last_usage.output_tokens == 10 * provider_calls


def test_public_projection_failure_gets_one_precise_correction() -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        return _native_response(
            summary=(
                "根据图片\u200b查找相似内容。" if provider_calls == 1 else "根据图片查找相似内容。"
            )
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert result.entries[0].claims[1].statement == "根据图片查找相似内容。"
    assert provider_calls == 2


def test_output_validation_failure_preserves_successful_tool_call_count() -> None:
    provider_calls = 0
    output = _output()
    cast(dict[str, object], cast(list[object], output["entries"])[0])["entry_id"] = "other"

    def read_dependency() -> str:
        return "dependency evidence"

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("read_dependency", {}, "call-read")],
                usage=RequestUsage(input_tokens=100, output_tokens=10),
                finish_reason="tool_call",
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    output,
                    f"call-output-{provider_calls}",
                )
            ],
            usage=RequestUsage(input_tokens=100, output_tokens=10),
            finish_reason="tool_call",
        )

    runtime = CapabilityAnalysisToolRuntime(
        toolsets=(FunctionToolset(tools=[read_dependency]),),
        evidence_units=lambda: (),
        validate_source_context=lambda: True,
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
        tool_runtime_factory=lambda _request: runtime,
    )

    with pytest.raises(
        CapabilityModelAdapterError,
        match="output validation failed",
    ):
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert provider_calls > 2
    assert client.last_usage is not None
    assert client.last_usage.requests == provider_calls
    assert client.last_usage.input_tokens == 100 * provider_calls
    assert client.last_usage.output_tokens == 10 * provider_calls
    assert client.last_usage.tool_calls == 1


def test_agent_retries_evidence_reference_outside_current_request() -> None:
    provider_calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        return _native_response(
            evidence_id="evidence-missing" if provider_calls == 1 else "evidence-handler"
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert result.entries[0].claims[1].evidence_ids == ("evidence-handler",)
    assert provider_calls == 2


def test_length_finish_reason_is_classified_as_output_truncated() -> None:
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: ModelResponse(
                parts=[TextPart('{"knowledge_enabled":true')],
                finish_reason="length",
            ),
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    with pytest.raises(CapabilityModelAdapterError) as error_info:
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert error_info.value.reason_code is CapabilityModelAdapterReason.OUTPUT_TRUNCATED
    assert "finish_reason:length" in str(error_info.value)


def test_request_timeout_is_classified_separately_from_transport_failure() -> None:
    async def respond(_messages: object, _info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(0.05)
        return _native_response()

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
        timeout_seconds=0.001,
    )

    with pytest.raises(CapabilityModelAdapterError) as error_info:
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert error_info.value.reason_code is CapabilityModelAdapterReason.TIMEOUT
    assert "timed out" in str(error_info.value)


def test_disabled_output_contains_no_entries() -> None:
    response = ModelResponse(
        parts=[TextPart('{"knowledge_enabled":false,"entries":[]}')],
        finish_reason="stop",
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: response,
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert result.knowledge_enabled is False
    assert result.entries == ()


def test_agent_can_resolve_gate_as_no_constraint_with_definition_evidence() -> None:
    output = _output()
    output["gate_resolutions"] = [
        {
            "candidate_id": "gate:allow-all",
            "outcome": "no_constraint",
            "evidence_ids": ["evidence-handler", "evidence-definition"],
            "config_reference_ids": [],
        }
    ]
    request = replace(
        _request(),
        evidence_units=(
            *_request().evidence_units,
            CapabilityEvidenceUnit(
                "evidence-definition",
                "approved_python_definition",
                "def allow_all(): return True",
                "sha256:definition",
            ),
        ),
        gate_candidates=(
            CapabilityGateCandidate(
                "gate:allow-all",
                CapabilityGateKind.PERMISSION,
                ("root",),
                ("evidence-handler",),
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: ModelResponse(
                parts=[TextPart(json.dumps(output, ensure_ascii=False))],
                finish_reason="stop",
            ),
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert result.knowledge_enabled is True
    assert result.gate_resolutions[0].outcome.value == "no_constraint"


def test_agent_retries_enabled_output_with_unresolved_gate_then_closes() -> None:
    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        output = _output() if calls == 1 else {"knowledge_enabled": False, "entries": []}
        output["gate_resolutions"] = [
            {
                "candidate_id": "gate:unknown",
                "outcome": "unresolved",
                "evidence_ids": ["evidence-handler"],
                "config_reference_ids": [],
            }
        ]
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        gate_candidates=(
            CapabilityGateCandidate(
                "gate:unknown",
                CapabilityGateKind.EXECUTION_GUARD,
                ("root",),
                ("evidence-handler",),
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert result.knowledge_enabled is False
    assert result.gate_resolutions[0].outcome.value == "unresolved"


def test_agent_requires_real_constraint_to_link_gate_candidate() -> None:
    calls = 0
    retry_prompts: list[str] = []

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        retry_prompts.extend(
            cast(str, part.content)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, RetryPromptPart)
        )
        output = _output()
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        entry["constraints"] = [
            {
                "kind": "permission",
                "statement": "满足以下任一条件：群管理员或群主",
                "evidence_ids": ["evidence-handler", "evidence-definition"],
                "config_reference_ids": [],
                "role": None,
                "rate_limit_policy": None,
                "rate_limit_scope": None,
                "gate_candidate_ids": [] if calls == 1 else ["gate:admin"],
                "permission_alternatives": [
                    {
                        "kind": "role",
                        "statement": "群管理员或群主",
                        "role": "admin",
                        "scene": None,
                    }
                ],
            }
        ]
        output["gate_resolutions"] = [
            {
                "candidate_id": "gate:admin",
                "outcome": "constraint",
                "evidence_ids": ["evidence-handler", "evidence-definition"],
                "config_reference_ids": [],
            }
        ]
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    request = replace(
        _request(),
        evidence_units=(
            *_request().evidence_units,
            CapabilityEvidenceUnit(
                "evidence-definition",
                "approved_python_definition",
                "def admin_only(session): return session.is_admin",
                "sha256:definition",
            ),
        ),
        gate_candidates=(
            CapabilityGateCandidate(
                "gate:admin",
                CapabilityGateKind.PERMISSION,
                ("root",),
                ("evidence-handler",),
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert any("gate_missing_public_owner" in item for item in retry_prompts)
    assert any("candidate_id=gate:admin" in item for item in retry_prompts)
    assert any("missing_entry_ids=root" in item for item in retry_prompts)
    assert any("gate_candidate_ids" in item for item in retry_prompts)
    assert result.entries[0].constraints[0].permission_alternatives[0].role is TeachingRole.ADMIN
    assert result.entries[0].constraints[0].gate_candidate_ids == ("gate:admin",)


def test_agent_accepts_business_state_permission_gate_as_behavior_boundary() -> None:
    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        output = _output()
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        claims = cast(list[dict[str, object]], entry["claims"])
        claims.append(
            {
                "kind": "behavior_boundary",
                "statement": "使用前需先开始当前业务流程",
                "evidence_ids": ["evidence-handler", "evidence-definition"],
                "config_reference_ids": [],
                "gate_candidate_ids": ["gate:game-started"],
            }
        )
        output["gate_resolutions"] = [
            {
                "candidate_id": "gate:game-started",
                "outcome": "constraint",
                "evidence_ids": ["evidence-handler", "evidence-definition"],
                "config_reference_ids": [],
            }
        ]
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

    base_request = _request()
    request = replace(
        base_request,
        evidence_units=(
            *base_request.evidence_units,
            CapabilityEvidenceUnit(
                "evidence-definition",
                "approved_python_definition",
                "def game_started(group_id): return group_id in active_games",
                "sha256:definition",
            ),
        ),
        gate_candidates=(
            CapabilityGateCandidate(
                "gate:game-started",
                CapabilityGateKind.PERMISSION,
                ("root",),
                ("evidence-handler",),
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    linked = next(
        claim
        for claim in result.entries[0].claims
        if claim.kind is SemanticClaimKind.BEHAVIOR_BOUNDARY
    )
    assert linked.gate_candidate_ids == ("gate:game-started",)
    assert result.entries[0].constraints == ()
