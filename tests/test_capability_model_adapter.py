from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

from nbtriage.capability_analysis import (
    CapabilityAnalysisRequest,
    CapabilityAnalysisService,
    CapabilityEvidenceUnit,
    CapabilityGateCandidate,
    CapabilityGateKind,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    ConfigProjection,
    SemanticClaimKind,
    TeachingRole,
)
from nbtriage.capability_model_adapter import (
    SYSTEM_INSTRUCTION,
    CapabilityAnalysisToolRuntime,
    CapabilityModelAdapterError,
    CapabilityModelAdapterReason,
    PydanticAICapabilityAnalysisClient,
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
        "answer_markdown": "根据图片查找相似内容。",
        "answer_evidence_ids": [evidence_id],
        "answer_config_reference_ids": [],
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
    assert messages[0].instructions == SYSTEM_INSTRUCTION.strip()
    prompt = cast(UserPromptPart, messages[0].parts[0])
    payload = json.loads(cast(str, prompt.content))
    assert payload["invocations"] == [
        {
            "entry_id": "root",
            "mode": "anchored",
            "command_body": "搜图",
            "canonical_usages": [],
            "aliases": [],
            "requires_mention": False,
        }
    ]
    assert payload["gate_candidates"] == []
    assert "SENTINEL_SOURCE" in payload["evidence_units"][0]["content"]
    info = cast(AgentInfo, observed["info"])
    assert info.model_request_parameters.output_mode == "native"
    assert info.model_request_parameters.function_tools == []


def test_prompt_requires_complete_usage_literal_affix_self_check() -> None:
    assert "固定字面量、成员变量和 parser 参数结构" in SYSTEM_INSTRUCTION
    assert "逐字符保留成员变量前后的全部固定字面量" in SYSTEM_INSTRUCTION
    assert 'f"^{name}图"' in SYSTEM_INSTRUCTION
    assert "即使命中 `^`、`$`、`*` 等看似正则或格式控制的符号" in SYSTEM_INSTRUCTION
    assert "实际注册表达式或变量替换关系无法确认，必须关闭知识" in SYSTEM_INSTRUCTION


def test_prompt_separates_alias_display_from_usage_and_places_repeat_marker_after_slot() -> None:
    assert "usage claim 仍必须使用 command_body" in SYSTEM_INSTRUCTION
    assert "entry.display_trigger" in SYSTEM_INSTRUCTION
    assert "展开后必须恰好等于 command_body 与全部 aliases" in SYSTEM_INSTRUCTION
    assert "`<参数>...` 表示至少一项、`[参数]...` 表示零项或多项" in SYSTEM_INSTRUCTION


def test_prompt_preserves_supported_baseline_retrieval_fields() -> None:
    assert "必须按 entry_id 逐字段对照旧值" in SYSTEM_INSTRUCTION
    assert "不得把这些非空数组改成空数组" in SYSTEM_INSTRUCTION
    assert "保留仍然成立的旧值成员" in SYSTEM_INSTRUCTION
    assert "最终顺序可由模型外做稳定规范化" in SYSTEM_INSTRUCTION
    assert "最终输出自检" in SYSTEM_INSTRUCTION
    assert "最终 claims 必须仍包含它们并引用当前 Evidence" in SYSTEM_INSTRUCTION


def test_agent_uses_profile_selected_output_tool() -> None:
    observed: dict[str, Any] = {}

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        observed["info"] = info
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, _output(), "call-1")],
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


def test_agent_retries_when_model_changes_parser_owned_usage() -> None:
    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return _native_response(usage="搜图 <图片>" if calls == 1 else "搜图 [图片]")

    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "搜图",
                ("搜图 [图片]",),
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
        == "搜图 [图片]"
    )


def test_agent_receives_aliases_and_retries_missing_required_mention() -> None:
    calls = 0
    observed: dict[str, object] = {}

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        observed["messages"] = messages
        output = _output(usage="状态" if calls == 1 else "@bot 状态")
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        entry["answer_markdown"] = "群聊中请发送 @bot 状态。"
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


def test_agent_accepts_exact_nested_alias_display_trigger() -> None:
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


def test_invalid_answer_markdown_falls_back_to_validated_public_claims() -> None:
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

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert result.entries[0].answer_markdown == "根据图片查找相似内容。"


def test_agent_retries_when_complete_usage_enumerates_more_than_four_members() -> None:
    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        usage = "#(摸摸|亲亲|贴贴|白底|波纹) [图片]" if calls == 1 else "#<表情名> [图片]"
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
            parts=[ToolCallPart(info.output_tools[0].name, output, "call-output")],
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


def test_output_entry_ids_must_match_invocation_targets() -> None:
    output = _output()
    cast(dict[str, object], cast(list[object], output["entries"])[0])["entry_id"] = "other"
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

    with pytest.raises(
        CapabilityModelAdapterError,
        match="output validation failed",
    ):
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))


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

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        output = _output()
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        entry["constraints"] = [
            {
                "kind": "role",
                "statement": "仅管理员可用",
                "evidence_ids": ["evidence-handler", "evidence-definition"],
                "config_reference_ids": [],
                "role": "admin",
                "rate_limit_policy": None,
                "rate_limit_scope": None,
                "gate_candidate_ids": [] if calls == 1 else ["gate:admin"],
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
    assert result.entries[0].constraints[0].role is TeachingRole.ADMIN
    assert result.entries[0].constraints[0].gate_candidate_ids == ("gate:admin",)
