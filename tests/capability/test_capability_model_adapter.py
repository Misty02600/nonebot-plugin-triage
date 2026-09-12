from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic_ai import ModelResponse, TextPart, ThinkingPart, ToolCallPart, models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage

from nbtriage.capability.teaching._prompt import CONFIG_INSTRUCTION
from nbtriage.capability.teaching.analysis import (
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
from nbtriage.capability.teaching.annotations import (
    CapabilityAnnotationError,
    CapabilityAnnotationProjectionCode,
    CapabilityAnnotationProjectionError,
    CapabilityTeachingAnnotation,
    _validated_usage,
    capability_analysis_fingerprint,
    project_capability_annotation,
)
from nbtriage.capability.teaching.model_adapter import (
    ANCHORED_INSTRUCTION,
    CORE_INSTRUCTION,
    SYSTEM_INSTRUCTION,
    CapabilityAnalysisToolRuntime,
    CapabilityModelAdapterError,
    CapabilityModelAdapterReason,
    PydanticAICapabilityAnalysisClient,
    _AnalysisEntryOutput,
    _completed_analysis_output_candidate,
    _validate_analysis_output_contract,
    _validate_entry_usages,
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


@pytest.mark.parametrize(
    ("provider", "requested", "returned", "accepted"),
    [
        ("deepseek", "deepseek-v4-flash", "deepseek-v4-flash", True),
        ("deepseek", "deepseek-v4-flash", "deepseek-flash", True),
        ("deepseek", "deepseek-flash", "deepseek-v4-flash", False),
        ("deepseek", "deepseek-v4-flash", "deepseek-pro", False),
        ("opencode-go", "deepseek-v4-flash", "deepseek-flash", False),
    ],
)
def test_agent_accepts_only_confirmed_provider_model_rename(
    provider: str, requested: str, returned: str, accepted: bool
) -> None:
    response = replace(_native_response(), provider_name=provider, model_name=returned)
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            lambda _messages, _info: response, model_name=returned, profile=_NATIVE_PROFILE
        ),
        max_output_tokens=240,
        expected_provider=provider,
        expected_model=requested,
    )
    if accepted:
        asyncio.run(client.analyze(_request()))
        assert client._last_response is not None
        assert client._last_response.model_name == returned
    else:
        with pytest.raises(CapabilityModelAdapterError, match="model identity mismatch"):
            asyncio.run(client.analyze(_request()))


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
    assert (
        messages[0].instructions
        == "\n\n".join((CORE_INSTRUCTION, CONFIG_INSTRUCTION, ANCHORED_INSTRUCTION)).strip()
    )
    prompt = cast(UserPromptPart, messages[0].parts[0])
    payload = json.loads(cast(str, prompt.content))
    assert payload["invocations"] == [
        {
            "entry_id": "root",
            "mode": "anchored",
            "command_body": "搜图",
            "regex_pattern": None,
            "regex_flags": [],
            "keywords": [],
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


@pytest.mark.parametrize(
    ("target", "usage"),
    [
        (
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.REGEX,
                regex_pattern=r"^(日群友|日群主|日管理|透群友|透群主|透管理)$",
            ),
            "(日|透)(群友 [@用户]|群主|管理)",
        ),
        (
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.KEYWORD,
                keywords=("查找",),
                requires_mention=True,
            ),
            "@bot <范围>查找<对象>",
        ),
    ],
)
def test_text_usage_is_valid_without_command_or_shortcut_contract(
    target: CapabilityInvocationTarget,
    usage: str,
) -> None:
    request = replace(
        _request(),
        invocations=(target,),
    )

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        return _native_response(usage=usage)

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        timeout_seconds=12,
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert result.entries[0].claims[2].statement == usage
    annotation = project_capability_annotation(request, result, analysis_revision="text-test")
    assert annotation.entries[0].usages == (usage,)


@pytest.mark.parametrize(
    ("usage", "valid"),
    [
        ("@bot 帮我查找<对象>", True),
        ("@bot <范围>查找<对象>", True),
        ("查找<对象> @bot", True),
        ("@bot 搜寻<对象>", True),
        ("@bot <查找对象>", False),
        ("@bot 搜索<对象>", False),
        ("查找<对象>", False),
    ],
)
def test_keyword_usage_contract_matches_final_projection(usage: str, valid: bool) -> None:
    target = CapabilityInvocationTarget(
        "root",
        CapabilityInvocationMode.KEYWORD,
        keywords=("查找", "搜寻"),
        requires_mention=True,
    )
    request = replace(_request(), invocations=(target,))
    entry = _AnalysisEntryOutput.model_validate(_entry(usage=usage))
    if not valid:
        with pytest.raises(CapabilityAnnotationError):
            _validate_entry_usages(entry, target, request)
        with pytest.raises(CapabilityAnnotationError):
            _validated_usage(usage, target=target)
        return
    assert _validate_entry_usages(entry, target, request) == [usage]
    assert _validated_usage(usage, target=target) == usage
    changed = replace(request, invocations=(replace(target, keywords=("查找",)),))
    assert capability_analysis_fingerprint(request, analysis_revision="test") != (
        capability_analysis_fingerprint(changed, analysis_revision="test")
    )


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


@pytest.mark.parametrize(
    ("template", "standard_usage", "reply_usage"),
    [
        ("@bot 搜图 [slot:0]", "@bot 搜图 [图片]", "[回复图片] @bot 搜图"),
        ("@bot 搜图 <slot:0>", "@bot 搜图 <图片>", "<回复图片> @bot 搜图"),
        ("@bot 搜图 <slot:0>...", "@bot 搜图 <图片>...", "<回复图片> @bot 搜图"),
        ("@bot 搜图,<slot:0>", "@bot 搜图,<图片>", "<回复图片> @bot 搜图"),
        ("@bot 搜图[,<slot:0>]", "@bot 搜图[,<图片>]", "[回复图片] @bot 搜图"),
    ],
)
def test_parser_reply_usage_is_not_a_shortcut_and_cannot_replace_standard_usage(
    template: str, standard_usage: str, reply_usage: str
) -> None:
    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "搜图",
                canonical_usages=(template,),
                requires_mention=True,
            ),
        ),
    )
    output = _output(usage=standard_usage)
    entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
    claims = cast(list[dict[str, object]], entry["claims"])
    claims.append(
        {
            "kind": "usage",
            "statement": reply_usage,
            "evidence_ids": ["evidence-handler"],
        }
    )
    calls = 0

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))], finish_reason="stop"
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )
    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))
    annotation = project_capability_annotation(request, result, analysis_revision="reply-test")
    assert calls == 1
    assert set(annotation.entries[0].usages) == {standard_usage, reply_usage}

    from nbtriage.capability.teaching.model_adapter import _AnalysisOutput

    claims.pop(2)
    with pytest.raises(ValueError, match="must be preserved"):
        _validate_analysis_output_contract(
            _AnalysisOutput.model_validate(output), request, (), allow_alias_fallback=False
        )


@pytest.mark.parametrize(
    ("parser_template", "invalid_slot"),
    [(True, None), (False, None), (True, "周期"), (True, "<" + "名" * 41 + ">")],
)
def test_agent_preserves_standard_usage_and_accepts_cited_shortcuts_with_aliases(
    parser_template: bool, invalid_slot: str | None
) -> None:
    shortcut_evidence_id = "evidence-shortcuts"
    standard_usage = "搜图 [图片] [(--type|-t) <周期 或日期>]"
    template = "搜图 [slot:0] [(--type|-t) <slot:1>]"
    base_request = _request()
    request = replace(
        base_request,
        evidence_units=(
            *base_request.evidence_units,
            CapabilityEvidenceUnit(
                shortcut_evidence_id,
                "runtime_capability_facts",
                '{"command.shortcuts":[{"pattern":"今日搜图"},{"pattern":"今日找图"}]}',
                "sha256:shortcuts",
            ),
        ),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "搜图",
                canonical_usages=(template,) if parser_template else (),
                aliases=("找图",),
                shortcut_count=2,
                shortcut_evidence_ids=(shortcut_evidence_id,),
            ),
        ),
    )
    output = _output(usage=standard_usage)
    entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
    entry["display_trigger"] = "(搜图|找图)"
    claims = cast(list[dict[str, object]], entry["claims"])
    claims.extend(
        {
            "kind": "usage",
            "statement": shortcut,
            "evidence_ids": [shortcut_evidence_id],
            "config_reference_ids": [],
        }
        for shortcut in ("今日搜图", "今日找图")
    )
    provider_calls = 0
    corrections: list[str] = []

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        corrections.extend(
            str(part.content) for part in messages[-1].parts if isinstance(part, RetryPromptPart)
        )
        claims[2]["statement"] = (
            standard_usage.replace("<周期 或日期>", invalid_slot)
            if invalid_slot is not None and provider_calls == 1
            else standard_usage
        )
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))], finish_reason="stop"
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            respond,
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert provider_calls == (2 if invalid_slot is not None else 1)
    assert result.entries[0].display_trigger == "(搜图|找图)"
    if invalid_slot is not None:
        assert len(corrections) == 1
        assert "entry_id=root, field=usage" in corrections[0]
        assert template in corrections[0]
        assert "此错误不要求修改 display_trigger" in corrections[0]
        if invalid_slot.startswith("<"):
            assert "参数槽位名称须为 1 至 40 个字符" in corrections[0]
    assert [
        claim.statement
        for claim in result.entries[0].claims
        if claim.kind is SemanticClaimKind.USAGE
    ] == [standard_usage, "今日搜图", "今日找图"]
    annotation = project_capability_annotation(
        request,
        result,
        analysis_revision="shortcut-test",
    )
    assert annotation.entries[0].usages == (
        "(搜图|找图) [图片] [(--type|-t) <周期 或日期>]",
        "今日搜图",
        "今日找图",
    )

    from nbtriage.capability.teaching.model_adapter import _AnalysisOutput

    claims[-1]["evidence_ids"] = ["evidence-handler"]
    with pytest.raises(ValueError, match="shortcut usage must cite registered shortcut Evidence"):
        _validate_analysis_output_contract(
            _AnalysisOutput.model_validate(output), request, (), allow_alias_fallback=False
        )
    claims.pop()
    claims.pop(2)
    with pytest.raises(ValueError, match=r"must preserve|must be preserved"):
        _validate_analysis_output_contract(
            _AnalysisOutput.model_validate(output), request, (), allow_alias_fallback=False
        )


def test_agent_preserves_compact_parser_separators_when_naming_slots() -> None:
    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        output = _output(usage="@bot 提醒[时间]")
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        entry["display_trigger"] = "(提醒|叫我)"
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
                "提醒",
                canonical_usages=("@bot 提醒[slot:0]",),
                aliases=("叫我",),
                requires_mention=True,
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))
    annotation = project_capability_annotation(
        request,
        result,
        analysis_revision="compact-test",
    )

    assert annotation.entries[0].usages == ("@bot (提醒|叫我)[时间]",)


@pytest.mark.parametrize("profile", [_NATIVE_PROFILE, _TOOL_PROFILE], ids=["native", "tool"])
def test_output_correction_collects_independent_errors_across_entries(
    profile: ModelProfile,
) -> None:
    calls = 0
    corrections: list[str] = []
    request = replace(
        _request(),
        invocations=(
            CapabilityInvocationTarget(
                "root",
                CapabilityInvocationMode.ANCHORED,
                "搜图",
                canonical_usages=("搜图 <slot:0>",),
                aliases=("找图",),
                shortcut_count=1,
                shortcut_evidence_ids=("evidence-handler",),
            ),
            CapabilityInvocationTarget(
                "other",
                CapabilityInvocationMode.ANCHORED,
                "查图",
                canonical_usages=("查图 <slot:0>",),
            ),
        ),
    )

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        corrections.extend(
            str(part.content) for part in messages[-1].parts if isinstance(part, RetryPromptPart)
        )
        output = _output(usage="搜图 图片" if calls == 1 else "搜图 <图片>")
        entries = cast(list[dict[str, object]], output["entries"])
        entries[0]["display_trigger"] = "(搜图|未知入口)" if calls == 1 else "(搜图|找图)"
        entries.append(
            {**_entry(usage="查图 图片" if calls == 1 else "查图 <图片>"), "entry_id": "other"}
        )
        return ModelResponse(
            parts=(
                [ToolCallPart(info.output_tools[0].name, output, f"output-{calls}")]
                if info.output_tools
                else [TextPart(json.dumps(output, ensure_ascii=False))]
            )
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=profile), max_output_tokens=240
    )
    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert calls == 2
    assert len(corrections) == 1
    assert "entry_id=root, field=usage" in corrections[0]
    assert "entry_id=other, field=usage" in corrections[0]
    assert "field=display_trigger" in corrections[0]
    assert "shortcut usage must cite" not in corrections[0]
    assert "只修正 display_trigger" not in corrections[0]
    assert len(result.entries) == 2
    assert result.entries[0].display_trigger == "(搜图|找图)"


@pytest.mark.parametrize(
    ("scenes", "statement"),
    [
        (("group", "guild", "channel_text"), "仅群聊、频道或频道文字场景可用"),
        (("non_private",), "仅非私聊场景可用"),
    ],
)
def test_agent_accepts_typed_scene_and_evidenced_rate_limit_exemption(
    scenes: tuple[str, ...], statement: str
) -> None:
    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        output = _output()
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        entry["constraints"] = [
            {
                "kind": "scene",
                "statement": statement,
                "evidence_ids": ["evidence-handler"],
                "config_reference_ids": [],
                "role": None,
                "allowed_scenes": list(scenes),
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

    assert result.entries[0].constraints[0].allowed_scenes == tuple(
        TeachingScene(scene) for scene in scenes
    )
    assert result.entries[0].constraints[1].statement.endswith("超级用户不受此限制")
    annotation = project_capability_annotation(_request(), result, analysis_revision="fixture-v1")
    assert CapabilityTeachingAnnotation.from_dict(annotation.to_dict()) == annotation
    requirement = next(
        item for item in annotation.entries[0].requirements if item.kind.value == "scene"
    )
    assert requirement.allowed_scenes == tuple(TeachingScene(scene) for scene in scenes)


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


def test_total_token_limit_accepts_received_valid_response_but_blocks_further_correction() -> None:
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

    assert error_info.value.reason_code is CapabilityModelAdapterReason.BUDGET
    assert invalid_calls() == 2


def test_per_request_input_limit_accepts_received_final_result_but_blocks_retry() -> None:
    def run(*, repair_second_response: bool):
        provider_calls = 0

        def read_dependency() -> str:
            return "bounded evidence"

        def respond(_messages, info: AgentInfo) -> ModelResponse:
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls == 1:
                return ModelResponse(
                    parts=[ToolCallPart("read_dependency", {}, "call-read")],
                    usage=RequestUsage(input_tokens=10, output_tokens=5),
                    finish_reason="tool_call",
                )
            output = _output()
            if not repair_second_response:
                entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
                entry["entry_id"] = "other"
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, output, "call-output")],
                usage=RequestUsage(input_tokens=65_000, output_tokens=5),
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
            total_tokens_limit=100_000,
            tool_runtime_factory=lambda _request: runtime,
        )
        return client, lambda: provider_calls

    valid_client, valid_calls = run(repair_second_response=True)
    result = asyncio.run(CapabilityAnalysisService(valid_client).analyze(_request()))

    assert result.entries[0].entry_id == "root"
    assert valid_calls() == 2
    assert valid_client.last_usage is not None
    assert valid_client.last_usage.input_tokens == 65_010

    invalid_client, invalid_calls = run(repair_second_response=False)
    with pytest.raises(CapabilityModelAdapterError) as error_info:
        asyncio.run(CapabilityAnalysisService(invalid_client).analyze(_request()))

    assert error_info.value.reason_code is CapabilityModelAdapterReason.BUDGET
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


def test_parallel_navigation_batch_respects_budget_then_finalizes() -> None:
    provider_calls = 0
    executed: list[str] = []
    observed_tools: list[tuple[str, ...]] = []
    successful_results: list[ToolReturnPart] = []
    failed_results: list[ToolReturnPart] = []

    async def read_primary() -> str:
        await asyncio.sleep(0.01)
        executed.append("primary")
        return "primary evidence"

    async def read_secondary() -> str:
        executed.append("secondary")
        return "secondary evidence"

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        observed_tools.append(tuple(tool.name for tool in info.function_tools))
        if provider_calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart("read_primary", {}, "call-primary"),
                    ToolCallPart("read_secondary", {}, "call-secondary"),
                    ToolCallPart("read_primary", {}, "call-over-budget"),
                ],
                usage=RequestUsage(input_tokens=40, output_tokens=5),
                finish_reason="tool_call",
            )
        failed_results.extend(
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.outcome == "failed"
        )
        successful_results.extend(
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.outcome == "success"
        )
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, _output(), "call-output")],
            usage=RequestUsage(input_tokens=30, output_tokens=5),
            finish_reason="tool_call",
        )

    runtime = CapabilityAnalysisToolRuntime(
        toolsets=(
            FunctionToolset(tools=[read_primary]),
            FunctionToolset(tools=[read_secondary]),
        ),
        evidence_units=tuple,
        validate_source_context=lambda: True,
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_TOOL_PROFILE),
        max_output_tokens=240,
        max_requests=5,
        max_tool_calls=2,
        total_tokens_limit=1_000,
        tool_runtime_factory=lambda _request: runtime,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert result.entries[0].entry_id == "root"
    assert provider_calls == 2
    assert executed == ["primary", "secondary"]
    assert observed_tools == [
        ("read_primary", "read_secondary"),
        (),
    ]
    assert len(failed_results) == 1
    assert failed_results[0].tool_call_id == "call-over-budget"
    assert "tool_budget_exhausted" in str(failed_results[0].content)
    successful_contents = [cast(dict[str, object], part.content) for part in successful_results]
    assert [content["remaining_navigation_calls"] for content in successful_contents] == [
        1,
        0,
    ]
    assert successful_contents[-1]["navigation_phase"] == "finalize"
    assert client.last_usage is not None
    assert client.last_usage.tool_calls == 2


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
        entry = cast(dict[str, object], cast(list[object], output["entries"])[0])
        entry["display_trigger"] = "(状态|运行状态)"
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


@pytest.mark.parametrize(
    ("usages", "valid"),
    [
        (("#<滤镜名> <图片>...", "<回复图片> #<滤镜名>"), True),
        (("<回复图片> #<滤镜名>", "#<滤镜名> <图片>..."), True),
        (("[回复消息] #<滤镜名> <图片>...",), True),
        (("<回复图片> #<滤镜名>",), False),
        (("[回复图片] #<滤镜名>",), False),
        (("#<滤镜名> [参数]", "<回复图片> #<滤镜名>"), False),
        (("#<滤镜名> <图片>...", "<回复图片> 滤镜"), False),
        (("#<滤镜名> <图片>...", "#<另一个名称> <图片>..."), False),
        (("#<滤镜名> <图片>...", "<回复图片> (红 <图片>|蓝 <图片>)"), False),
    ],
)
def test_family_reply_variants_preserve_standard_input_coverage(
    usages: tuple[str, ...], valid: bool
) -> None:
    target = CapabilityInvocationTarget("family", CapabilityInvocationMode.COMPLETE)
    request = replace(
        _request(),
        invocations=(target,),
        evidence_units=(
            *_request().evidence_units,
            CapabilityEvidenceUnit(
                "evidence-shapes",
                "runtime_family_shapes",
                '{"shapes":[{"arguments":[{"pattern_type":"uniseg.Image"}]}]}',
                "sha256:shapes",
            ),
        ),
    )
    entry = _entry(usage=usages[0])
    entry["entry_id"] = "family"
    claims = cast(list[dict[str, object]], entry["claims"])
    claims.extend(
        {"kind": "usage", "statement": usage, "evidence_ids": ["evidence-handler"]}
        for usage in usages[1:]
    )
    candidate = _AnalysisEntryOutput.model_validate(entry)
    if not valid:
        with pytest.raises(CapabilityAnnotationError):
            _validate_entry_usages(candidate, target, request)
        return
    assert len(_validate_entry_usages(candidate, target, request)) == 1

    def respond(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[TextPart(json.dumps({"knowledge_enabled": True, "entries": [entry]}))],
            finish_reason="stop",
        )

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
    )
    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))
    annotation = project_capability_annotation(request, result, analysis_revision="family-reply")
    assert annotation.entries[0].usages == usages


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


@pytest.mark.parametrize("profile", [_NATIVE_PROFILE, _TOOL_PROFILE], ids=["native", "tool"])
@pytest.mark.parametrize("repair_third_response", [True, False], ids=["repaired", "exhausted"])
def test_public_projection_uses_two_output_corrections(
    profile: ModelProfile, repair_third_response: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_calls = 0
    corrections: list[str] = []

    def project(*args, **kwargs):
        if provider_calls < 3 or not repair_third_response:
            raise CapabilityAnnotationProjectionError(
                CapabilityAnnotationProjectionCode.PUBLIC_MEMBERS,
                "reconciled behavior_boundaries exceeds its public member limit",
            )
        return project_capability_annotation(*args, **kwargs)

    monkeypatch.setattr(
        "nbtriage.capability.teaching.model_adapter.project_capability_annotation", project
    )

    def respond(messages, info: AgentInfo) -> ModelResponse:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls > 1:
            corrections.extend(
                str(part.content)
                for part in messages[-1].parts
                if isinstance(part, RetryPromptPart)
            )
        output = _output()
        if info.output_tools:
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, output, f"output-{provider_calls}")],
                finish_reason="tool_call",
            )
        return ModelResponse(parts=[TextPart(json.dumps(output, ensure_ascii=False))])

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=profile),
        max_output_tokens=240,
    )

    if repair_third_response:
        result = asyncio.run(CapabilityAnalysisService(client).analyze(_request()))
        assert result.entries[0].claims[1].statement == "根据图片查找相似内容。"
    else:
        with pytest.raises(CapabilityModelAdapterError) as error_info:
            asyncio.run(CapabilityAnalysisService(client).analyze(_request()))
        assert error_info.value.reason_code is CapabilityModelAdapterReason.OUTPUT_VALIDATION
        assert error_info.value.detail_code == "projection_public_members"

    assert provider_calls == 3
    assert len(corrections) == 2
    assert all("projection_public_members" in correction for correction in corrections)


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
        await asyncio.sleep(0.5)
        return _native_response()

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
        timeout_seconds=0.1,
    )
    lifecycle: list[dict[str, object]] = []
    client.enable_maintenance_diagnostics()
    client.set_maintenance_lifecycle_sink(lifecycle.append)

    with pytest.raises(CapabilityModelAdapterError) as error_info:
        asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert error_info.value.reason_code is CapabilityModelAdapterReason.TIMEOUT
    assert "timed out" in str(error_info.value)
    assert [event["phase"] for event in lifecycle] == [
        "provider_request_started",
        "provider_request_cancelled",
    ]


def test_unit_timeout_caps_each_model_request_at_150_seconds() -> None:
    observed_settings: list[dict[str, object]] = []

    def respond(_messages: object, info: AgentInfo) -> ModelResponse:
        observed_settings.append(dict(info.model_settings or {}))
        return _native_response()

    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(respond, model_name="fixture-model", profile=_NATIVE_PROFILE),
        max_output_tokens=240,
        timeout_seconds=300,
    )

    asyncio.run(CapabilityAnalysisService(client).analyze(_request()))

    assert observed_settings[0]["timeout"] == 150
    assert client.diagnostic_timeout_seconds == 300
    assert client.diagnostic_request_timeout_seconds == 150


def test_completed_final_result_can_be_revalidated_after_cancellation() -> None:
    response = ModelResponse(
        parts=[ToolCallPart("final_result", _output(), "call-output")],
        finish_reason="tool_call",
    )

    candidate = _completed_analysis_output_candidate([response])

    assert candidate is not None
    _validate_analysis_output_contract(
        candidate,
        _request(),
        (),
        allow_alias_fallback=False,
    )


def test_agent_can_resolve_gate_as_no_constraint_with_definition_evidence() -> None:
    observed: dict[str, Any] = {}

    def respond(messages, _info: AgentInfo) -> ModelResponse:
        observed["messages"] = messages
        return ModelResponse(
            parts=[TextPart(json.dumps(output, ensure_ascii=False))],
            finish_reason="stop",
        )

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
                owner="search",
                symbol="allow_all",
            ),
        ),
    )
    client = PydanticAICapabilityAnalysisClient(
        FunctionModel(
            respond,
            model_name="fixture-model",
            profile=_NATIVE_PROFILE,
        ),
        max_output_tokens=240,
    )

    result = asyncio.run(CapabilityAnalysisService(client).analyze(request))

    assert result.knowledge_enabled is True
    assert result.gate_resolutions[0].outcome.value == "no_constraint"
    prompt = cast(UserPromptPart, observed["messages"][0].parts[0])
    payload = json.loads(cast(str, prompt.content))
    assert payload["gate_candidates"] == [
        {
            "candidate_id": "gate:allow-all",
            "kind": "permission",
            "entry_ids": ["root"],
            "evidence_ids": ["evidence-handler"],
            "owner": "search",
            "symbol": "allow_all",
        }
    ]
    without_location = replace(
        request,
        gate_candidates=(replace(request.gate_candidates[0], owner=None, symbol=None),),
    )
    assert capability_analysis_fingerprint(
        request, analysis_revision="test"
    ) != capability_analysis_fingerprint(without_location, analysis_revision="test")


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


@pytest.mark.parametrize(
    ("scenes", "branches", "restricted"),
    [
        ((), ("admin", "owner"), False),
        (("group",), ("superuser", "admin", "owner"), False),
        (("group",), ("superuser", "superuser"), True),
        (("non_private",), ("superuser",), True),
        ((), ("superuser", "private"), False),
        ((), ("superuser", "access"), False),
        ((), ("private",), False),
        ((), ("non_private",), False),
    ],
)
def test_agent_requires_real_constraint_to_link_gate_candidate(
    scenes: tuple[str, ...], branches: tuple[str, ...], restricted: bool
) -> None:
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
                "statement": "满足当前场景与使用资格要求",
                "evidence_ids": ["evidence-handler", "evidence-definition"],
                "config_reference_ids": [],
                "role": None,
                "allowed_scenes": list(scenes),
                "rate_limit_policy": None,
                "rate_limit_scope": None,
                "gate_candidate_ids": [] if calls == 1 else ["gate:admin"],
                "permission_alternatives": [
                    {
                        "kind": (
                            "scene"
                            if branch in {"private", "non_private"}
                            else "access"
                            if branch == "access"
                            else "role"
                        ),
                        "statement": "使用资格",
                        "role": branch
                        if branch not in {"private", "non_private", "access"}
                        else None,
                        "scene": branch if branch in {"private", "non_private"} else None,
                    }
                    for branch in branches
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
    constraint = result.entries[0].constraints[0]
    assert constraint.allowed_scenes == tuple(TeachingScene(scene) for scene in scenes)
    assert constraint.gate_candidate_ids == ("gate:admin",)
    annotation = project_capability_annotation(request, result, analysis_revision="fixture-v1")
    assert CapabilityTeachingAnnotation.from_dict(annotation.to_dict()) == annotation
    requirement = annotation.entries[0].requirements[0]
    assert requirement.allowed_scenes == constraint.allowed_scenes
    assert len(requirement.alternatives) == len(branches)
    assert tuple(
        item.scene.value for item in requirement.alternatives if item.scene is not None
    ) == tuple(branch for branch in branches if branch in {"private", "non_private"})
    assert annotation.entries[0].superuser_only is restricted
    old_payload = annotation.to_dict()
    old_payload["schema_version"] = annotation.schema_version - 1
    with pytest.raises(CapabilityAnnotationError):
        CapabilityTeachingAnnotation.from_dict(old_payload)


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
