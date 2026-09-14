from __future__ import annotations

import json
from dataclasses import replace

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext
from pydantic_ai_harness.compaction import (
    estimate_context_tokens,
    estimate_token_count,
    resolve_context_window,
)
from pydantic_core import to_json

from nbtriage.capability.teaching.analysis import CapabilityAnalysisRequest


def estimate_request_tokens(request: ModelRequestContext) -> int:
    """估算完整文本输入；不是 Provider tokenizer 的精确计数。

    Harness 的纯文本估算不包含普通工具 Schema，因此单独补入当前可见工具、
    原生输出 Schema 和输出指令。已有响应时同时采用上游基于 Provider 用量的
    上下文估算，避免密集文本一直按字符估算而持续低估。
    """
    parameters = request.model_request_parameters
    schemas = to_json(
        {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_json_schema,
                }
                for tool in parameters.declared_tool_defs.values()
            ],
            "output": parameters.output_object,
            "output_instructions": parameters.prompted_output_instructions,
        }
    ).decode()
    return max(
        estimate_token_count([*request.messages, ModelRequest(parts=[UserPromptPart(schemas)])]),
        _estimate_anchored_input(request),
    )


def _estimate_anchored_input(request: ModelRequestContext) -> int:
    """沿用上次实际用量，指令替换只增加估算增量，不因缩短而扣减基数。"""
    latest_instructions = ""
    anchored_instructions: str | None = None
    for message in request.messages:
        if isinstance(message, ModelRequest) and message.instructions:
            latest_instructions = message.instructions
        elif isinstance(message, ModelResponse) and message.usage.input_tokens:
            anchored_instructions = latest_instructions
    if anchored_instructions is None or anchored_instructions == latest_instructions:
        return estimate_context_tokens(
            request.messages, model_request_parameters=request.model_request_parameters
        )

    # 上游在指令改变时会整份加到实际用量之上。仅在估算副本中去掉指令，
    # 让它继续计算历史、工具结果及 Schema 增量，再单独加入指令增长量。
    history = [
        replace(message, instructions=None) if isinstance(message, ModelRequest) else message
        for message in request.messages
    ]
    growth = estimate_token_count(
        [ModelRequest(parts=[], instructions=latest_instructions)]
    ) - estimate_token_count([ModelRequest(parts=[], instructions=anchored_instructions)])
    return estimate_context_tokens(
        history, model_request_parameters=request.model_request_parameters
    ) + max(0, growth)


class TeachingInputPreparation(AbstractCapability[CapabilityAnalysisRequest]):
    """整理首包可选预载，并按模型窗口和输出预留检查完整请求；不截断必要资料。"""

    def __init__(self, target: int | None, *, context_window: int | None = None) -> None:
        self.target = target
        self.context_window = context_window
        self.estimates: list[dict[str, int | None]] = []

    async def before_model_request(
        self,
        ctx: RunContext[CapabilityAnalysisRequest],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        target = self.target
        estimated = estimate_request_tokens(request_context)
        estimated_before = estimated
        removed = 0
        if target is not None and estimated > target and ctx.usage.requests == 0:
            removed = self._trim_preloads(ctx.deps, request_context, target)
        estimated = estimate_request_tokens(request_context) if removed else estimated
        window = (
            self.context_window
            or resolve_context_window(request_context.model)
            # OpenAI 兼容网关的 provider ID 不一定收录在模型目录中。
            or resolve_context_window(request_context.model.model_name)
        )
        output_reserve = (request_context.model_settings or {}).get("max_tokens") or 0
        self.estimates.append(
            {
                "request_index": ctx.usage.requests + 1,
                "estimated_input_tokens_before": estimated_before,
                "estimated_input_tokens": estimated,
                "preload_token_target": target,
                "removed_optional_preloads": removed,
                "context_window": window,
                "output_reserve": output_reserve,
            }
        )
        if window is None and any(
            unit.source_kind.startswith("knowledge_") for unit in ctx.deps.evidence_units
        ):
            raise UsageLimitExceeded("expanded framework context requires a known context window")
        if window is not None and estimated + output_reserve > window * 0.9:
            raise UsageLimitExceeded("teaching context estimate exceeds window with output reserve")
        return request_context

    @staticmethod
    def _trim_preloads(
        request: CapabilityAnalysisRequest, context: ModelRequestContext, target: int
    ) -> int:
        prompt = next(
            (
                part
                for message in context.messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, UserPromptPart) and isinstance(part.content, str)
            ),
            None,
        )
        if prompt is None or not isinstance(prompt.content, str):
            return 0
        payload = json.loads(prompt.content)
        # 注册、Runtime、配置及已被结构化事实引用的材料不能作为预载移除。
        protected = {
            evidence_id
            for item in (
                *request.family_members,
                *request.gate_candidates,
                *request.fixed_constraints,
            )
            for evidence_id in item.evidence_ids
        } | {
            evidence_id
            for item in request.invocations
            for evidence_id in item.shortcut_evidence_ids
        }
        optional = [
            unit.evidence_id
            for unit in reversed(request.evidence_units)
            if unit.preload_optional and unit.evidence_id not in protected
        ]
        removed = 0
        for evidence_id in optional:
            payload["evidence_units"] = [
                unit for unit in payload["evidence_units"] if unit["evidence_id"] != evidence_id
            ]
            payload["allowed_evidence_ids"].remove(evidence_id)
            removed += 1
            # 原地更新原始消息，诊断和后续请求都保留实际发送的首包。
            prompt.content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if estimate_request_tokens(context) <= target:
                break
        return removed
