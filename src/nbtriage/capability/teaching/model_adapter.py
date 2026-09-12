from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from time import monotonic_ns
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import Agent, ModelRetry, ToolOutput, UsageLimits, capture_run_messages
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    RunCancelled,
    ToolFailed,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings, merge_model_settings
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset
from pydantic_ai.usage import RunUsage

from nbtriage._model_runtime.diagnostics import (
    MaintenanceResponseCaptureModel,
    captured_retry_reason,
    captured_run_usage,
    diagnostic_message_trace,
    diagnostic_provider_response_trace,
    last_model_response,
    unexpected_behavior_reason,
    usage_limit_name,
)
from nbtriage._model_runtime.telemetry import (
    current_agent_instrumentation,
    record_agent_response_shape,
)
from nbtriage._model_runtime.usage import response_model_matches
from nbtriage.capability.teaching._prompt import (
    ANCHORED_INSTRUCTION as ANCHORED_INSTRUCTION,
)
from nbtriage.capability.teaching._prompt import (
    BASELINE_INSTRUCTION as BASELINE_INSTRUCTION,
)
from nbtriage.capability.teaching._prompt import CORE_INSTRUCTION as CORE_INSTRUCTION
from nbtriage.capability.teaching._prompt import FAMILY_INSTRUCTION as FAMILY_INSTRUCTION
from nbtriage.capability.teaching._prompt import REGEX_INSTRUCTION as REGEX_INSTRUCTION
from nbtriage.capability.teaching._prompt import SYSTEM_INSTRUCTION as SYSTEM_INSTRUCTION
from nbtriage.capability.teaching._prompt import _instructions_for_request
from nbtriage.capability.teaching.analysis import (
    BaselineChangeOperation,
    BaselineMemberChange,
    BaselineMemberField,
    CapabilityAnalysisEntryOutput,
    CapabilityAnalysisError,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityGateKind,
    CapabilityGateResolution,
    CapabilityGateResolutionKind,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    PermissionAlternative,
    RateLimitPolicy,
    RateLimitScope,
    SemanticClaim,
    SemanticClaimKind,
    SemanticConstraint,
    SemanticConstraintKind,
    TeachingRole,
    TeachingScene,
    validate_capability_analysis_output,
)
from nbtriage.capability.teaching.annotations import (
    CAPABILITY_ANNOTATION_PROMPT_ID,
    CAPABILITY_ANNOTATION_TOTAL_TOKEN_LIMIT,
    CapabilityAnnotationError,
    CapabilityAnnotationProjectionError,
    project_capability_annotation,
    validate_capability_public_statement,
    validate_capability_search_term,
    validate_capability_usage_pattern,
    validate_capability_usage_template,
    validate_complete_aggregate_usage,
    validate_keyword_usage,
)
from nbtriage.capability.teaching.usage import (
    MAX_PUBLIC_USAGES,
    CapabilityUsageExpressionError,
    deterministic_usage_selector,
    group_literal_expression_for_usage,
    split_reply_usage,
    usage_command_body_pattern,
    validate_usage_selector,
)


class CapabilityModelAdapterReason(StrEnum):
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    HTTP = "http"
    BUDGET = "budget"
    OUTPUT_TRUNCATED = "output_truncated"
    OUTPUT_VALIDATION = "output_validation"
    SCHEMA = "schema"
    PROVIDER_IDENTITY = "provider_identity"
    SOURCE_CHANGED = "source_changed"
    UNKNOWN = "unknown"


class CapabilityModelAdapterError(CapabilityAnalysisError):
    """携带可安全记录的稳定失败分类，同时保留内部异常链供调用方诊断。"""

    def __init__(
        self,
        message: str,
        *,
        reason_code: CapabilityModelAdapterReason = CapabilityModelAdapterReason.UNKNOWN,
        detail_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.detail_code = detail_code


_MODEL_REQUEST_TIMEOUT_SECONDS = 150.0


class _CapabilityRunDeadline:
    """把单元总时限转换成单调的正常、预留和最终提交阶段。"""

    def __init__(self, total_seconds: float) -> None:
        self._total_seconds = total_seconds
        self._started_ns = monotonic_ns()
        self._finalize_window_seconds = min(
            _MODEL_REQUEST_TIMEOUT_SECONDS,
            total_seconds / 2,
        )
        self._reserve_window_seconds = min(
            total_seconds,
            self._finalize_window_seconds + min(30.0, total_seconds / 10),
        )

    @property
    def remaining_seconds(self) -> float:
        elapsed_seconds = (monotonic_ns() - self._started_ns) / 1_000_000_000
        return max(0.0, self._total_seconds - elapsed_seconds)

    @property
    def request_timeout_seconds(self) -> float:
        return min(_MODEL_REQUEST_TIMEOUT_SECONDS, self._total_seconds)

    def phase(self) -> Literal["normal", "reserve", "finalize"]:
        remaining = self.remaining_seconds
        if remaining <= self._finalize_window_seconds:
            return "finalize"
        if remaining <= self._reserve_window_seconds:
            return "reserve"
        return "normal"


class _NavigationToolBudget:
    def __init__(self, max_tool_calls: int) -> None:
        self._max_tool_calls = max_tool_calls
        self._claimed = 0
        self._lock = asyncio.Lock()

    @property
    def claimed(self) -> int:
        return self._claimed

    @property
    def limit(self) -> int:
        return self._max_tool_calls

    @property
    def remaining(self) -> int:
        return max(0, self._max_tool_calls - self._claimed)

    async def claim(self) -> bool:
        async with self._lock:
            if self._claimed >= self._max_tool_calls:
                return False
            self._claimed += 1
            return True


class _BoundedNavigationToolset(WrapperToolset[Any]):
    def __init__(
        self,
        wrapped: AbstractToolset[Any],
        *,
        budget: _NavigationToolBudget,
        max_requests: int,
        total_tokens_limit: int,
        deadline: _CapabilityRunDeadline,
        announce_budget: bool,
    ) -> None:
        super().__init__(wrapped=wrapped)
        self._budget = budget
        self._max_requests = max_requests
        self._total_tokens_limit = total_tokens_limit
        self._deadline = deadline
        self._announce_budget = announce_budget

    def _budget_phase(self, usage: RunUsage) -> Literal["normal", "reserve", "finalize"]:
        time_phase = self._deadline.phase()
        if time_phase == "finalize":
            return "finalize"
        if (
            self._budget.claimed >= self._budget.limit
            or usage.requests >= self._max_requests - 1
            or usage.total_tokens * 4 >= self._total_tokens_limit * 3
        ):
            return "finalize"
        if time_phase == "reserve":
            return "reserve"
        if (
            self._budget.claimed >= max(0, self._budget.limit - 1)
            or usage.requests >= max(0, self._max_requests - 2)
            or usage.total_tokens * 2 >= self._total_tokens_limit
        ):
            return "reserve"
        return "normal"

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        if self._budget_phase(ctx.usage) == "finalize" or not await self._budget.claim():
            raise ToolFailed(
                "tool_budget_exhausted: remaining_navigation_calls=0；"
                "源码导航或时间预算已进入收尾阶段；"
                "停止补证并使用现有 Evidence "
                "提交 final_result。必要 gate 或可执行用法仍无法确认时应安全关闭知识。"
            )
        result = await super().call_tool(name, tool_args, ctx, tool)
        budget_state: dict[str, object] = {
            "remaining_navigation_calls": self._budget.remaining,
        }
        phase = self._budget_phase(ctx.usage)
        if phase != "normal":
            budget_state["navigation_phase"] = phase
        if phase == "finalize":
            budget_state["navigation_instruction"] = (
                "源码导航预算已耗尽；下一轮只使用已有 Evidence 提交 final_result，"
                "必要事实仍无法确认时安全关闭知识。"
            )
        if isinstance(result, dict):
            return {**result, **budget_state}
        return {"result": result, **budget_state}

    async def get_tools(
        self,
        ctx: RunContext[Any],
    ) -> dict[str, ToolsetTool[Any]]:
        if self._budget_phase(ctx.usage) == "finalize":
            return {}
        return await super().get_tools(ctx)

    async def get_instructions(
        self,
        ctx: RunContext[Any],
    ) -> str | InstructionPart | Sequence[str | InstructionPart] | None:
        instructions = await super().get_instructions(ctx)
        if not self._announce_budget:
            return instructions
        phase = self._budget_phase(ctx.usage)
        if phase == "normal":
            return instructions
        if phase == "reserve":
            budget_instruction = (
                "当前单元已经进入最终提交预留阶段。若 name、summary、至少一条可执行 usage "
                "以及全部执行 gate 已有充分 Evidence，请立即提交最小完整 final_result；"
                "不得再为 search term、持久化方式、示例、返回措辞或文字润色导航源码。"
                "只有仍缺少会影响可执行用法或必要 gate 结论的一项明确事实时，才做最后一次定向补证。"
            )
        else:
            budget_instruction = (
                "只读补证阶段已经结束，源码工具不再可用；现在必须使用已有 Evidence 提交 "
                "final_result。已有证据充分时提交最小完整教学结果；必要 gate 或可执行用法仍无法"
                "确认时，使用 unresolved 并安全关闭当前知识，不得猜测，也不得继续丰富可选事实。"
            )
        if instructions is None:
            return budget_instruction
        if isinstance(instructions, (str, InstructionPart)):
            return (instructions, budget_instruction)
        return (*instructions, budget_instruction)


@dataclass(frozen=True)
class CapabilityAnalysisToolRuntime:
    toolsets: tuple[AbstractToolset[Any], ...]
    evidence_units: Callable[[], tuple[CapabilityEvidenceUnit, ...]]
    validate_source_context: Callable[[], bool]


CapabilityAnalysisToolRuntimeFactory = Callable[
    [CapabilityAnalysisRequest], CapabilityAnalysisToolRuntime | None
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ClaimOutput(_StrictModel):
    kind: Annotated[
        Literal["name", "summary", "usage", "search_term", "behavior_boundary"],
        Field(
            description=(
                "公开能力事实类型：name=简短能力名称；summary=一句话用途；"
                "usage=完整调用形式；search_term=一条独立的同义检索词或支持对象，"
                "不得在一个 statement 中拼接多个检索词；"
                "behavior_boundary=usage 无法表达的输入格式、后续交互、处理或结果边界，"
                "以及能力所需的业务准备状态；不得重复参数结构、调用者身份、会话场景、"
                "可配置权限、名单、开放资格或限流。"
            )
        ),
    ]
    statement: Annotated[str, Field(min_length=1, max_length=1_000)]
    evidence_ids: Annotated[list[str], Field(min_length=1, max_length=16)]
    config_reference_ids: Annotated[list[str], Field(max_length=16)] = []
    gate_candidate_ids: Annotated[
        list[str],
        Field(
            max_length=16,
            description=(
                "内部覆盖关联：仅 behavior_boundary 可以列出其实际解释的业务准备状态 "
                "gate candidate；其他 claim 必须为空。"
            ),
        ),
    ] = []

    @model_validator(mode="after")
    def validate_public_statement(self) -> _ClaimOutput:
        if self.kind == "usage":
            self.statement = _normalize_usage_statement(self.statement)
        validate_capability_public_statement(
            self.statement,
        )
        if self.kind == "search_term":
            validate_capability_search_term(self.statement)
        if self.gate_candidate_ids and self.kind != "behavior_boundary":
            raise ValueError("only behavior_boundary may reference gate candidates")
        return self


class _BaselineChangeOutput(_StrictModel):
    op: Literal["remove", "replace"]
    field: Literal["search_terms", "behavior_boundaries"]
    old_value: Annotated[str, Field(min_length=1, max_length=1_000)]
    new_value: Annotated[str | None, Field(max_length=1_000)] = None
    evidence_ids: Annotated[list[str], Field(min_length=1, max_length=16)]
    config_reference_ids: Annotated[list[str], Field(max_length=16)] = []

    @model_validator(mode="after")
    def validate_change(self) -> _BaselineChangeOutput:
        self.old_value = validate_capability_public_statement(self.old_value)
        if self.op == "replace":
            if self.new_value is None:
                raise ValueError("replace baseline change requires new_value")
            self.new_value = validate_capability_public_statement(self.new_value)
            if self.new_value == self.old_value:
                raise ValueError("replace baseline change must change the value")
            if self.field == "search_terms":
                validate_capability_search_term(self.new_value)
        elif self.new_value is not None:
            raise ValueError("remove baseline change must not define new_value")
        return self


def _normalize_usage_statement(value: str) -> str:
    return " ".join(value.replace("`", "").split())


_OPTIONAL_USAGE_TAIL_RE = re.compile(r"(?: \[[^\[\]]+\])+$")
_REQUIRED_USAGE_TAIL_RE = re.compile(r"(?: <[^<>]+>)+$")
_USAGE_ALTERNATION_RE = re.compile(r"\(([^()]*\|[^()]*)\)")


def _has_redundant_anchored_usage(
    usages: Sequence[str],
    command_body: str,
) -> bool:
    usage_set = set(usages)
    for shorter in usage_set:
        for longer in usage_set - {shorter}:
            if not longer.startswith(f"{shorter} "):
                continue
            tail = longer[len(shorter) :]
            if _OPTIONAL_USAGE_TAIL_RE.fullmatch(tail):
                return True
            if shorter == command_body and _REQUIRED_USAGE_TAIL_RE.fullmatch(tail):
                return True
    return False


def _complete_usage_embeds_distinct_invocations(usage: str) -> bool:
    for match in _USAGE_ALTERNATION_RE.finditer(usage):
        alternatives = match.group(1)
        if any(character.isspace() or character in "<>[]" for character in alternatives):
            return True
    return False


_FAMILY_INPUT_CATEGORY_LABELS = {
    "image": "图片",
    "mention": "@用户（Uniseg At）",
    "number": "数值",
    "string": "builtins.str（公开名称需依据 Evidence 确定）",
    "text": "Uniseg Text（公开名称需依据 Evidence 确定）",
}
_FAMILY_IMAGE_TERMS = ("图片", "图像")
_FAMILY_EXPLICIT_NUMBER_ONLY_TERMS = frozenset({"数值", "数字", "整数", "数量"})


def _family_argument_pattern_types(argument: dict[str, object]) -> tuple[str, ...]:
    pattern_type = argument.get("pattern_type")
    if not isinstance(pattern_type, str):
        return ()
    if pattern_type.startswith("typing.Union[") and pattern_type.endswith("]"):
        return tuple(
            item.strip()
            for item in pattern_type.removeprefix("typing.Union[").removesuffix("]").split(",")
            if item.strip()
        )
    return (pattern_type,)


def _family_parser_input_categories(request: CapabilityAnalysisRequest) -> frozenset[str]:
    categories: set[str] = set()
    for evidence in request.evidence_units:
        if evidence.source_kind != "runtime_family_shapes":
            continue
        try:
            document = json.loads(evidence.content)
        except (TypeError, ValueError):
            continue
        shapes = document.get("shapes") if isinstance(document, dict) else None
        if not isinstance(shapes, list):
            continue
        for shape in shapes:
            arguments = shape.get("arguments") if isinstance(shape, dict) else None
            if not isinstance(arguments, list):
                continue
            for argument in arguments:
                if not isinstance(argument, dict):
                    continue
                for pattern_type in _family_argument_pattern_types(argument):
                    if pattern_type.endswith(".Image"):
                        categories.add("image")
                    elif pattern_type.endswith(".At"):
                        categories.add("mention")
                    elif pattern_type.endswith(".Text"):
                        categories.add("text")
                    elif pattern_type in {"builtins.float", "builtins.int"}:
                        categories.add("number")
                    elif pattern_type == "builtins.str":
                        categories.add("string")
    return frozenset(categories)


def _family_usage_input_slots(value: str) -> tuple[str, ...]:
    _reply, value = split_reply_usage(value)
    slots = [
        (match.group(1), match.group(2))
        for match in re.finditer(r"<([^<>]+)>|\[([^\[\]]+)\]", value)
    ]
    normalized_slots = [
        ("required" if required is not None else "optional", required or optional)
        for required, optional in slots
    ]
    if (
        normalized_slots
        and normalized_slots[0][0] == "required"
        and not _USAGE_ALTERNATION_RE.search(value)
    ):
        normalized_slots = normalized_slots[1:]
    return tuple(
        alternative.strip()
        for _kind, slot in normalized_slots
        for alternative in slot.split("|")
        if alternative.strip()
    )


def _complete_family_usage_category_error(
    entry: _AnalysisEntryOutput,
    request: CapabilityAnalysisRequest,
    *,
    usage: str,
) -> str | None:
    parser_categories = _family_parser_input_categories(request)
    if not parser_categories:
        return None
    slots = _family_usage_input_slots(usage)
    image_slots = tuple(slot for slot in slots if any(term in slot for term in _FAMILY_IMAGE_TERMS))
    mention_slots = tuple(slot for slot in slots if "@" in slot)
    non_media_slots = tuple(
        slot for slot in slots if slot not in image_slots and slot not in mention_slots
    )
    missing: set[str] = set()
    if "image" in parser_categories and not image_slots:
        missing.add("image")
    if "mention" in parser_categories and not mention_slots:
        missing.add("mention")
    non_media_categories = parser_categories.intersection({"number", "string", "text"})
    if non_media_categories and not non_media_slots:
        missing.update(non_media_categories)
    if missing:
        required_labels = "、".join(
            _FAMILY_INPUT_CATEGORY_LABELS[item] for item in sorted(parser_categories)
        )
        missing_labels = "、".join(_FAMILY_INPUT_CATEGORY_LABELS[item] for item in sorted(missing))
        return (
            "family_usage_missing_input_category："
            f"field=entries[{entry.entry_id}].usage；"
            f"Parser 已确定的输入类别={required_labels}；"
            f"当前聚合 usage 缺少对应结构槽位={missing_labels}。"
            "请保持成员选择位、必选性、可选性、重复性和现有槽位结构不变，"
            "只修正聚合 usage 及必要的公开解释以覆盖缺失类别；"
            "builtins.str 只证明字符串结构槽位存在，公开槽位名必须依据当前 Evidence 确定，"
            "不得直接把类型名改写成固定的用户文案；"
            "不要假设所有成员具有相同的精确参数语义，也不要重复 previous_annotation"
        )
    if (
        "number" in parser_categories
        and parser_categories.intersection({"string", "text"})
        and non_media_slots
    ):
        alternatives = {
            slot.removesuffix("...").strip()
            for slot in non_media_slots
            if slot.removesuffix("...").strip()
        }
        if alternatives and alternatives.issubset(_FAMILY_EXPLICIT_NUMBER_ONLY_TERMS):
            return (
                "family_usage_missing_input_category："
                f"field=entries[{entry.entry_id}].usage；"
                "Parser 成员 shape 同时包含文本与数值类型槽位，"
                "当前聚合 usage 却把共同输入窄化成纯数值。"
                "请依据当前 Evidence 命名或概括这些成员输入；"
                "correction 不指定公开槽位成品，也不得把 builtins.str 自动解释成“文字”"
            )
    return None


class _PermissionAlternativeOutput(_StrictModel):
    kind: Annotated[
        Literal["scene", "role", "access"],
        Field(
            description=(
                "同一 NoneBot Permission 中的一条 OR 分支，与其余 alternatives 为 OR，"
                "与父 permission 的非空 allowed_scenes 为 AND；scene=会话场景条件；"
                "role=能力入口直接检查的当前调用者角色；access=能力入口查询的可配置权限、"
                "ACL、名单或开放资格。只按入口实际判断分类；权限系统内部把某个角色预先授予"
                "一项资格，不得反向展开成该能力的 role 分支。"
            )
        ),
    ]
    statement: Annotated[str, Field(min_length=1, max_length=1_000)]
    role: Literal["channel_admin", "admin", "owner", "superuser", "custom"] | None = None
    scene: Annotated[
        Literal[
            "private",
            "non_private",
            "group",
            "guild",
            "channel_text",
            "channel_category",
            "channel_voice",
        ]
        | None,
        Field(
            description=(
                "这一条 Permission OR 分支的会话场景条件：private=私聊，group=群聊，"
                "guild=频道，channel_text=频道文字，channel_category=频道分类，"
                "channel_voice=频道语音；non_private=非私聊，不要求枚举其他场景，"
                "不替代其他独立限制。只有非私聊本身构成允许分支时才填入此处。"
            )
        ),
    ] = None

    @model_validator(mode="after")
    def validate_alternative(self) -> _PermissionAlternativeOutput:
        validate_capability_public_statement(self.statement)
        if self.kind == "role":
            if self.role is None:
                raise ValueError("role alternative requires role metadata")
        elif self.role is not None:
            raise ValueError("only role alternatives may define role metadata")
        if self.kind == "scene":
            if self.scene is None:
                raise ValueError("scene alternative requires scene metadata")
        elif self.scene is not None:
            raise ValueError("only scene alternatives may define scene metadata")
        return self


class _ConstraintOutput(_StrictModel):
    kind: Annotated[
        Literal["permission", "scene", "role", "access", "rate_limit"],
        Field(
            description=(
                "影响能力能否执行的公开前提：permission=一个 Permission 的 OR 分支组，"
                "可用 allowed_scenes 附加全部允许路径共同要求的场景；"
                "scene/role/access 仅用于非 Permission 的前提，其中 scene 用 allowed_scenes "
                "完整表达允许的会话场景条件，role 是调用者身份，"
                "access 是可配置权限、名单或开放资格。按能力入口实际执行的判断分类，"
                "不得把权限系统内部对角色的预授权反向写成 role；只保留 Evidence 支持的资格事实，"
                "不补充未证明的主体、原因或控制方式；业务准备状态属于 behavior_boundary，"
                "platform_scope 属于模型外 Runtime 路由事实；"
                "rate_limit=冷却、配额或并发。"
                "普通参数、回复上下文和 @bot 不属于 constraint。"
            )
        ),
    ]
    statement: Annotated[str, Field(min_length=1, max_length=1_000)]
    evidence_ids: Annotated[list[str], Field(min_length=1, max_length=16)]
    config_reference_ids: Annotated[list[str], Field(max_length=16)] = []
    role: Literal["channel_admin", "admin", "owner", "superuser", "custom"] | None = None
    allowed_scenes: Annotated[
        list[
            Literal[
                "private",
                "non_private",
                "group",
                "guild",
                "channel_text",
                "channel_category",
                "channel_voice",
            ]
        ],
        Field(
            max_length=7,
            description=(
                "scene constraint 的完整允许场景，或 permission 全部允许路径共同要求的场景集合；"
                "集合内部为 OR，与 permission_alternatives 为 AND。permission 中为空表示不附加"
                "共同场景条件，不表示 entry 适用所有场景，也不代替未知条件。"
                "private=私聊，group=群聊，"
                "guild=频道，channel_text=频道文字，channel_category=频道分类，"
                "channel_voice=频道语音；non_private=非私聊，不是互斥原子类型。"
                "只证明排除私聊时直接使用 non_private，不必枚举其余场景；"
                "另有仅群聊等更窄条件时不得用它扩大范围。必须与 statement 的允许范围一致。"
            ),
        ),
    ] = []
    rate_limit_policy: Literal["cooldown", "quota", "concurrency", "custom"] | None = None
    rate_limit_scope: Literal["user", "scene", "bot", "global", "custom", "unknown"] | None = None
    gate_candidate_ids: Annotated[
        list[str],
        Field(
            max_length=16,
            description=(
                "内部覆盖关联：列出本条公开 constraint 实际解释的 gate candidate 精确 ID；"
                "只能引用本轮 resolution=constraint 的候选。它不是 Evidence、entry 或 "
                "Permission alternative，也不表达布尔关系；由其他 Evidence 直接证明的限制留空。"
            ),
        ),
    ] = []
    permission_alternatives: Annotated[
        list[_PermissionAlternativeOutput],
        Field(
            max_length=16,
            description=(
                "Permission 的非空 OR 允许分支；共同场景由同条 constraint 的 allowed_scenes "
                "附加。不要求与函数调用一一对应，Evidence 明确证明的嵌套角色 OR 可以展开。"
            ),
        ),
    ] = []

    @model_validator(mode="after")
    def validate_public_statement(self) -> _ConstraintOutput:
        validate_capability_public_statement(self.statement)
        if self.kind == "role":
            if self.role is None:
                raise ValueError("role constraint requires role metadata")
        elif self.role is not None:
            raise ValueError("only role constraints may define role metadata")
        if self.kind in {"scene", "permission"}:
            if self.kind == "scene" and not self.allowed_scenes:
                raise ValueError("scene constraint requires allowed scenes")
            if len(self.allowed_scenes) != len(set(self.allowed_scenes)):
                raise ValueError("constraint allowed scenes must be unique")
        elif self.allowed_scenes:
            raise ValueError("only scene or permission constraints may define allowed scenes")
        if self.kind == "rate_limit":
            if self.rate_limit_policy is None or self.rate_limit_scope is None:
                raise ValueError("rate-limit constraint requires policy and scope")
        elif self.rate_limit_policy is not None or self.rate_limit_scope is not None:
            raise ValueError("only rate-limit constraints may define rate metadata")
        if self.kind == "permission":
            if not self.permission_alternatives:
                raise ValueError("permission constraint requires OR alternatives")
        elif self.permission_alternatives:
            raise ValueError("only permission constraints may define alternatives")
        return self


class _GateResolutionOutput(_StrictModel):
    candidate_id: Annotated[str, Field(min_length=1, max_length=128)]
    outcome: Literal["constraint", "no_constraint", "unresolved"]
    evidence_ids: Annotated[list[str], Field(min_length=1, max_length=16)]
    config_reference_ids: Annotated[list[str], Field(max_length=16)] = []


class _AnalysisEntryOutput(_StrictModel):
    entry_id: Annotated[str, Field(min_length=1, max_length=128)]
    display_trigger: Annotated[str | None, Field(max_length=256)] = None
    claims: Annotated[list[_ClaimOutput], Field(max_length=64)] = []
    baseline_changes: Annotated[list[_BaselineChangeOutput], Field(max_length=64)] = []
    constraints: Annotated[list[_ConstraintOutput], Field(max_length=64)] = []

    @model_validator(mode="after")
    def validate_entry_output(self) -> _AnalysisEntryOutput:
        if sum(item.kind == "name" for item in self.claims) != 1:
            raise ValueError("teaching entry requires exactly one name claim")
        if sum(item.kind == "summary" for item in self.claims) != 1:
            raise ValueError("teaching entry requires exactly one summary claim")
        usage_count = sum(item.kind == "usage" for item in self.claims)
        if usage_count == 0:
            raise ValueError("teaching entry requires at least one usage claim")
        return self


class _AnalysisOutput(_StrictModel):
    knowledge_enabled: bool
    entries: Annotated[list[_AnalysisEntryOutput], Field(max_length=32)] = []
    gate_resolutions: Annotated[list[_GateResolutionOutput], Field(max_length=32)] = []

    @model_validator(mode="after")
    def validate_enabled_output(self) -> _AnalysisOutput:
        if self.knowledge_enabled != bool(self.entries):
            raise ValueError("knowledge_enabled must match whether entries exist")
        entry_ids = [item.entry_id for item in self.entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("entry IDs must be unique")
        return self


_SUPPORTED_STRUCTURED_OUTPUT_MODES = frozenset({"native", "tool"})


def _alias_literals(target: CapabilityInvocationTarget) -> tuple[str, ...]:
    if target.mode is not CapabilityInvocationMode.ANCHORED or target.command_body is None:
        return ()
    return (target.command_body, *target.aliases)


def _alias_pattern_failures(
    entries: Sequence[_AnalysisEntryOutput],
    targets: Mapping[str, CapabilityInvocationTarget],
    standard_usages: Mapping[str, Sequence[str]],
) -> tuple[tuple[_AnalysisEntryOutput, CapabilityInvocationTarget, str], ...]:
    failures: list[tuple[_AnalysisEntryOutput, CapabilityInvocationTarget, str]] = []
    for entry in entries:
        target = targets[entry.entry_id]
        literals = _alias_literals(target)
        fallback = deterministic_usage_selector(literals)
        if len(literals) <= 1:
            entry.display_trigger = None
            continue
        if entry.display_trigger is None:
            if fallback is not None:
                failures.append((entry, target, "缺少可无损合并的 display_trigger"))
            continue
        try:
            validate_usage_selector(entry.display_trigger, literals)
        except CapabilityUsageExpressionError as error:
            failures.append((entry, target, str(error)))
            continue
        usage_error = _display_trigger_usage_error(
            standard_usages.get(entry.entry_id, ()), target, entry.display_trigger
        )
        if usage_error is not None:
            failures.append((entry, target, usage_error))
    return tuple(failures)


def _display_trigger_usage_error(
    standard_usages: Sequence[str],
    target: CapabilityInvocationTarget,
    display_trigger: str,
) -> str | None:
    assert target.command_body is not None
    pattern = usage_command_body_pattern(
        target.command_body, canonical_usages=target.canonical_usages
    )
    grouped_trigger = group_literal_expression_for_usage(display_trigger)
    for usage in standard_usages:
        rendered, substitutions = re.subn(
            pattern,
            lambda _match: grouped_trigger,
            usage,
            count=1,
        )
        if substitutions != 1:
            return "usage 未包含唯一的 command_body"
        try:
            validate_capability_usage_pattern(
                rendered,
                allow_verified_aliases=True,
                allow_separated_slots=bool(target.canonical_usages),
            )
        except CapabilityAnnotationError as error:
            return f"替换后的 usage 不可展示：{error}"
    return None


class _NextRequestTokenLimits(UsageLimits):
    """允许已付费响应完成校验，把 token 超限延迟到下一请求前。"""

    _received_oversized_input = False

    def check_tokens(self, usage: RunUsage) -> None:
        response_limits = replace(self, total_tokens_limit=None)
        UsageLimits.check_tokens(response_limits, usage)

    def check_per_request_input_tokens(self, request_input_tokens: int) -> None:
        limit = self.per_request_input_tokens_limit
        if limit is not None and request_input_tokens > limit:
            self._received_oversized_input = True

    def check_before_request(self, usage: RunUsage) -> None:
        if self._received_oversized_input:
            limit = self.per_request_input_tokens_limit
            raise UsageLimitExceeded(
                "The next request would follow a response whose input exceeded "
                f"the per_request_input_tokens_limit of {limit}"
            )
        UsageLimits.check_before_request(self, usage)


def _error_chain_contains_timeout(error: BaseException) -> bool:
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        current_id = id(current)
        if current_id in visited:
            continue
        visited.add(current_id)
        if isinstance(current, TimeoutError) or type(current).__name__ in {
            "APITimeoutError",
            "ConnectTimeout",
            "PoolTimeout",
            "ReadTimeout",
            "WriteTimeout",
        }:
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _completed_analysis_output_candidate(
    messages: Sequence[ModelMessage],
) -> _AnalysisOutput | None:
    """从已完成响应中提取一份完整但尚待领域校验的最终候选。

    Args:
        messages: Pydantic AI 在取消前保存的完整消息快照。

    Returns:
        可通过结构解析的最终候选；不存在或参数不完整时返回 ``None``。
    """

    for message in reversed(messages):
        if not isinstance(message, ModelResponse) or message.state != "complete":
            continue
        if message.finish_reason not in (None, "stop", "tool_call"):
            continue
        for part in reversed(message.parts):
            if not isinstance(part, ToolCallPart) or part.tool_name != "final_result":
                continue
            try:
                return _AnalysisOutput.model_validate(part.args_as_dict(raise_if_invalid=True))
            except (AssertionError, TypeError, ValueError):
                return None
        text = "".join(part.content for part in message.parts if isinstance(part, TextPart))
        if text:
            try:
                return _AnalysisOutput.model_validate_json(text)
            except ValueError:
                return None
    return None


def _analysis_metadata(request: CapabilityAnalysisRequest) -> dict[str, str]:
    return {
        "nbtriage.task": "capability_annotation",
        "nbtriage.capability_id": request.capability.capability_id,
        "nbtriage.plugin_module": (
            request.source_context.module_name
            if request.source_context is not None
            else request.capability.owner
        ),
    }


class PydanticAICapabilityAnalysisClient:
    """通过一次有界 Pydantic AI Agent 运行生成公开能力注释候选。"""

    def __init__(
        self,
        model: Model,
        *,
        timeout_seconds: float = 60.0,
        max_output_tokens: int,
        model_settings: ModelSettings | None = None,
        expected_provider: str | None = None,
        expected_model: str | None = None,
        tool_runtime_factory: CapabilityAnalysisToolRuntimeFactory | None = None,
        max_requests: int = 10,
        max_tool_calls: int = 10,
        total_tokens_limit: int = CAPABILITY_ANNOTATION_TOTAL_TOKEN_LIMIT,
        cost_limit_usd: Decimal = Decimal("0.05"),
        capture_diagnostics: bool = False,
    ) -> None:
        if timeout_seconds <= 0:
            raise CapabilityModelAdapterError(
                "timeout_seconds must be positive",
                reason_code=CapabilityModelAdapterReason.BUDGET,
            )
        if max_output_tokens < 1:
            raise CapabilityModelAdapterError(
                "max_output_tokens must be positive",
                reason_code=CapabilityModelAdapterReason.BUDGET,
            )
        if max_requests < 1 or max_tool_calls < 0 or total_tokens_limit < 1:
            raise CapabilityModelAdapterError(
                "capability Agent budgets are invalid",
                reason_code=CapabilityModelAdapterReason.BUDGET,
            )
        if cost_limit_usd <= 0:
            raise CapabilityModelAdapterError(
                "cost_limit_usd must be positive",
                reason_code=CapabilityModelAdapterReason.BUDGET,
            )
        if tool_runtime_factory is not None and not model.profile.get("supports_tools", False):
            raise CapabilityModelAdapterError("capability navigation requires model tool support")
        output_mode = model.profile.get("default_structured_output_mode", "tool")
        if output_mode not in _SUPPORTED_STRUCTURED_OUTPUT_MODES:
            raise CapabilityModelAdapterError(
                "capability annotation task does not support the model profile output mode"
            )
        self._max_output_tokens: int | None = max_output_tokens
        self._expected_provider = expected_provider
        self._expected_model = expected_model
        self._timeout_seconds = timeout_seconds
        self._request_timeout_seconds = min(
            timeout_seconds,
            _MODEL_REQUEST_TIMEOUT_SECONDS,
        )
        self._tool_runtime_factory = tool_runtime_factory
        self._max_requests: int | None = max_requests
        self._max_tool_calls: int | None = max_tool_calls
        self._total_tokens_limit: int | None = total_tokens_limit
        self._cost_limit_usd: Decimal | None = cost_limit_usd
        self._last_validation_failure: str | None = None
        self._last_validation_detail_code: str | None = None
        self._alias_retry_used = False
        self._called = False
        self._active_tool_runtime: CapabilityAnalysisToolRuntime | None = None
        self._last_response: ModelResponse | None = None
        self._last_usage: RunUsage | None = None
        self._capture_diagnostics = capture_diagnostics
        self._diagnostic_trace: tuple[dict[str, Any], ...] = ()
        self._diagnostic_model = (
            MaintenanceResponseCaptureModel(model) if capture_diagnostics else None
        )
        output_type: type[_AnalysisOutput] | ToolOutput[_AnalysisOutput] = (
            ToolOutput(
                _AnalysisOutput,
                name="final_result",
                description=(
                    "直接填写 knowledge_enabled、entries 和 gate_resolutions 三个顶层字段；"
                    "不得添加 payload、output 或 result 包装，也不得把对象序列化成 JSON 字符串"
                ),
            )
            if output_mode == "tool"
            else _AnalysisOutput
        )
        self._agent: Agent[CapabilityAnalysisRequest, _AnalysisOutput] = Agent(
            model,
            output_type=output_type,
            deps_type=CapabilityAnalysisRequest,
            name="capability_teaching_annotation",
            model_settings=merge_model_settings(
                model_settings,
                ModelSettings(
                    max_tokens=max_output_tokens,
                    parallel_tool_calls=False,
                    timeout=self._request_timeout_seconds,
                ),
            ),
            retries={"tools": 0, "output": 2},
            end_strategy="early",
            tool_timeout=min(timeout_seconds, 15.0),
        )
        self._agent.instrument = current_agent_instrumentation()

        @self._agent.output_validator
        def validate_usage_contract(
            ctx: RunContext[CapabilityAnalysisRequest],
            output: _AnalysisOutput,
        ) -> _AnalysisOutput:
            self._last_validation_detail_code = None
            captured_evidence = (
                self._active_tool_runtime.evidence_units()
                if self._active_tool_runtime is not None
                else ()
            )
            try:
                _validate_analysis_output_contract(
                    output,
                    ctx.deps,
                    captured_evidence,
                    allow_alias_fallback=self._alias_retry_used,
                )
            except (CapabilityAnalysisError, CapabilityAnnotationError, ExceptionGroup) as error:
                failures = error.exceptions if isinstance(error, ExceptionGroup) else (error,)
                corrections: list[str] = []
                for failure in failures:
                    if isinstance(failure, _AliasPatternValidationError):
                        self._alias_retry_used = True
                        corrections.append(
                            "field=display_trigger: 必须无损合并 Runtime 已确认的全部固定入口，"
                            "展开集合必须完全一致且每个局部备选位置最多四项；"
                            "无法满足时使用 null 并保留标准可执行 usage，不得使用概念槽位。"
                            f"{failure.detail}"
                        )
                    elif isinstance(failure, CapabilityAnnotationProjectionError):
                        self._last_validation_detail_code = f"projection_{failure.code.value}"
                        corrections.append(
                            f"公开教学投影失败；错误码：{self._last_validation_detail_code}；"
                            f"原因：{failure}"
                        )
                    else:
                        corrections.append(str(failure))
                self._last_validation_failure = "\n".join(corrections)
                if len(failures) > 1:
                    self._last_validation_detail_code = "multiple_output_validation"
                raise ModelRetry(
                    "请一并修正以下已确认错误，并重新提交完整对象。"
                    "仅修改所列错误涉及的字段；依赖前置条件的检查可能在修正后继续进行。\n"
                    + self._last_validation_failure
                ) from error
            return output

    @property
    def last_response(self) -> ModelResponse | None:
        return self._last_response

    @property
    def last_usage(self) -> RunUsage | None:
        return self._last_usage

    @property
    def diagnostic_trace(self) -> tuple[dict[str, Any], ...]:
        return self._diagnostic_trace

    @property
    def diagnostic_provider_responses(self) -> tuple[dict[str, Any], ...]:
        model = self._diagnostic_model
        return diagnostic_provider_response_trace(
            model.responses if model is not None else (),
            request_indexes=(model.response_request_indexes if model is not None else ()),
        )

    @property
    def diagnostic_provider_errors(self) -> tuple[dict[str, Any], ...]:
        model = self._diagnostic_model
        return tuple(model.errors) if model is not None else ()

    @property
    def diagnostic_timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def diagnostic_request_timeout_seconds(self) -> float:
        return self._request_timeout_seconds

    def set_maintenance_lifecycle_sink(
        self,
        sink: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        model = self._diagnostic_model
        if model is None:
            raise CapabilityModelAdapterError(
                "maintenance diagnostics must be enabled before setting a lifecycle sink"
            )
        model.set_lifecycle_sink(sink)

    def enable_maintenance_diagnostics(self, *, unbounded: bool = False) -> None:
        """在首次调用前启用本地维护诊断，并可移除项目侧用量止损。"""
        if self._called:
            raise CapabilityModelAdapterError(
                "maintenance diagnostics must be enabled before analysis"
            )
        self._capture_diagnostics = True
        if self._diagnostic_model is None:
            model = self._agent.model
            if not isinstance(model, Model):
                raise CapabilityModelAdapterError(
                    "maintenance diagnostics require a resolved model"
                )
            self._diagnostic_model = MaintenanceResponseCaptureModel(model)
        if not unbounded:
            return
        self._max_output_tokens = None
        self._max_requests = None
        self._max_tool_calls = None
        self._total_tokens_limit = None
        self._cost_limit_usd = None
        model_settings = self._agent.model_settings
        if callable(model_settings):
            raise CapabilityModelAdapterError(
                "unbounded maintenance diagnostics require static model settings"
            )
        unbounded_settings = dict(model_settings or {})
        unbounded_settings.pop("max_tokens", None)
        self._agent.model_settings = cast(ModelSettings, unbounded_settings)

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        if not isinstance(request, CapabilityAnalysisRequest):
            raise TypeError("request must be CapabilityAnalysisRequest")
        if self._called:
            raise CapabilityModelAdapterError(
                "capability model-call limit reached: 1",
                reason_code=CapabilityModelAdapterReason.BUDGET,
            )
        self._called = True
        self._alias_retry_used = False
        self._last_validation_detail_code = None
        run_deadline = _CapabilityRunDeadline(self._timeout_seconds)
        tool_runtime = (
            self._tool_runtime_factory(request) if self._tool_runtime_factory is not None else None
        )
        self._active_tool_runtime = tool_runtime
        if tool_runtime is None:
            analysis_toolsets = None
        elif self._max_tool_calls is None:
            analysis_toolsets = tool_runtime.toolsets
        else:
            navigation_budget = _NavigationToolBudget(self._max_tool_calls)
            analysis_toolsets = tuple(
                _BoundedNavigationToolset(
                    toolset,
                    budget=navigation_budget,
                    max_requests=cast(int, self._max_requests),
                    total_tokens_limit=cast(int, self._total_tokens_limit),
                    deadline=run_deadline,
                    announce_budget=index == 0,
                )
                for index, toolset in enumerate(tool_runtime.toolsets)
            )
        cancelled_run: RunCancelled | None = None
        recovered_output: _AnalysisOutput | None = None
        normal_output: _AnalysisOutput | None = None
        normal_usage: RunUsage | None = None
        with capture_run_messages() as captured_messages:
            try:
                async with asyncio.timeout(run_deadline.remaining_seconds):
                    with self._agent.parallel_tool_call_execution_mode("sequential"):
                        result = await self._agent.run(
                            _build_payload(request),
                            model=self._diagnostic_model,
                            instructions=_instructions_for_request(request),
                            deps=request,
                            metadata=_analysis_metadata(request),
                            retries={"tools": 1, "output": 2},
                            toolsets=analysis_toolsets,
                            usage_limits=_NextRequestTokenLimits(
                                cost_limit=self._cost_limit_usd,
                                request_limit=self._max_requests,
                                output_tokens_limit=(
                                    None
                                    if self._max_output_tokens is None or self._max_requests is None
                                    else self._max_output_tokens * self._max_requests
                                ),
                                total_tokens_limit=self._total_tokens_limit,
                                per_request_input_tokens_limit=(
                                    None if self._max_requests is None else 64_000
                                ),
                            ),
                        )
                        normal_output = result.output
                        normal_usage = result.usage
            except asyncio.CancelledError as error:
                cancelled_run = RunCancelled.from_cancellation(error)
                if cancelled_run is not None:
                    captured_messages[:] = cancelled_run.all_messages()
                raise
            except ModelHTTPError as error:
                raise CapabilityModelAdapterError(
                    f"capability model request failed with HTTP {error.status_code}",
                    reason_code=CapabilityModelAdapterReason.HTTP,
                    detail_code=f"http_{error.status_code}",
                ) from error
            except TimeoutError as error:
                cancelled_run = RunCancelled.from_cancellation(error)
                if cancelled_run is not None:
                    captured_messages[:] = cancelled_run.all_messages()
                    candidate = _completed_analysis_output_candidate(captured_messages)
                    if candidate is not None:
                        captured_evidence = (
                            tool_runtime.evidence_units() if tool_runtime is not None else ()
                        )
                        try:
                            _validate_analysis_output_contract(
                                candidate,
                                request,
                                captured_evidence,
                                allow_alias_fallback=False,
                            )
                        except Exception:
                            candidate = None
                    recovered_output = candidate
                if recovered_output is None:
                    raise CapabilityModelAdapterError(
                        "capability model request timed out",
                        reason_code=CapabilityModelAdapterReason.TIMEOUT,
                        detail_code=(
                            "unit_deadline"
                            if run_deadline.remaining_seconds <= 0
                            else "model_request_timeout"
                        ),
                    ) from error
            except ModelAPIError as error:
                if _error_chain_contains_timeout(error):
                    raise CapabilityModelAdapterError(
                        "capability model request timed out",
                        reason_code=CapabilityModelAdapterReason.TIMEOUT,
                        detail_code="model_request_timeout",
                    ) from error
                raise CapabilityModelAdapterError(
                    "capability model request failed during transport",
                    reason_code=CapabilityModelAdapterReason.TRANSPORT,
                ) from error
            except UsageLimitExceeded as error:
                raise CapabilityModelAdapterError(
                    f"capability model request exceeded the {usage_limit_name(error)} budget",
                    reason_code=CapabilityModelAdapterReason.BUDGET,
                ) from error
            except UnexpectedModelBehavior as error:
                truncated = any(
                    isinstance(message, ModelResponse) and message.finish_reason == "length"
                    for message in captured_messages
                ) or any(
                    response.finish_reason == "length"
                    for response in (
                        self._diagnostic_model.responses
                        if self._diagnostic_model is not None
                        else ()
                    )
                )
                detail = (
                    "finish_reason:length"
                    if truncated
                    else (
                        self._last_validation_failure
                        or captured_retry_reason(captured_messages)
                        or unexpected_behavior_reason(error)
                    )
                )
                raise CapabilityModelAdapterError(
                    f"capability model output validation failed: {detail}",
                    reason_code=(
                        CapabilityModelAdapterReason.OUTPUT_TRUNCATED
                        if truncated
                        else CapabilityModelAdapterReason.OUTPUT_VALIDATION
                    ),
                    detail_code=(
                        "finish_reason_length"
                        if truncated
                        else self._last_validation_detail_code or "agent_output_validation"
                    ),
                ) from error
            except Exception as error:
                raise CapabilityModelAdapterError("capability model request failed") from error
            finally:
                provider_responses = (
                    self._diagnostic_model.responses if self._diagnostic_model is not None else ()
                )
                self._last_response = last_model_response(captured_messages) or next(
                    iter(reversed(provider_responses)),
                    None,
                )
                self._last_usage = (
                    cancelled_run.usage
                    if cancelled_run is not None
                    else captured_run_usage(
                        captured_messages,
                        provider_responses=provider_responses,
                    )
                )
                self._diagnostic_trace = (
                    diagnostic_message_trace(captured_messages) if self._capture_diagnostics else ()
                )
                record_agent_response_shape(
                    self._last_response,
                    metadata=_analysis_metadata(request),
                )
        if recovered_output is None:
            if normal_output is None or normal_usage is None:
                raise CapabilityModelAdapterError(
                    "capability model request returned no validated output",
                    reason_code=CapabilityModelAdapterReason.OUTPUT_VALIDATION,
                )
            self._last_usage = normal_usage
            analysis_output = normal_output
        else:
            analysis_output = recovered_output

        response = self._last_response
        if response is None:
            raise CapabilityModelAdapterError(
                "capability model request returned no provider response",
                reason_code=CapabilityModelAdapterReason.TRANSPORT,
            )
        if (
            self._expected_provider is not None
            and response.provider_name != self._expected_provider
        ):
            raise CapabilityModelAdapterError(
                "capability model response provider identity mismatch",
                reason_code=CapabilityModelAdapterReason.PROVIDER_IDENTITY,
            )
        if not response_model_matches(
            response,
            expected_provider=self._expected_provider,
            expected_model=self._expected_model,
        ):
            raise CapabilityModelAdapterError(
                "capability model response model identity mismatch",
                reason_code=CapabilityModelAdapterReason.PROVIDER_IDENTITY,
            )
        if response.finish_reason not in (None, "stop", "tool_call"):
            raise CapabilityModelAdapterError(
                "capability model response did not finish normally",
                reason_code=CapabilityModelAdapterReason.OUTPUT_VALIDATION,
            )
        if (
            self._max_requests is not None
            and self._last_usage is not None
            and not 1 <= self._last_usage.requests <= self._max_requests
        ):
            raise CapabilityModelAdapterError(
                "capability Agent exceeded the qualified provider request budget",
                reason_code=CapabilityModelAdapterReason.BUDGET,
            )
        if type(analysis_output) is not _AnalysisOutput:
            raise CapabilityModelAdapterError(
                "capability model response failed schema validation",
                reason_code=CapabilityModelAdapterReason.SCHEMA,
            )
        if tool_runtime is not None and not tool_runtime.validate_source_context():
            raise CapabilityModelAdapterError(
                "capability plugin source changed during Agent analysis",
                reason_code=CapabilityModelAdapterReason.SOURCE_CHANGED,
            )
        captured_evidence = tool_runtime.evidence_units() if tool_runtime is not None else ()
        return _to_domain_output(analysis_output, captured_evidence)


class _AliasPatternValidationError(CapabilityAnnotationError):
    def __init__(
        self,
        failures: Sequence[tuple[_AnalysisEntryOutput, CapabilityInvocationTarget, str]],
    ) -> None:
        self.failures = tuple(failures)
        self.detail = "；".join(
            f"entry_id={entry.entry_id}: {reason}" for entry, _target, reason in self.failures
        )
        super().__init__(self.detail)


def _validate_entry_usages(
    entry: _AnalysisEntryOutput,
    target: CapabilityInvocationTarget,
    request: CapabilityAnalysisRequest,
) -> list[str]:
    usage_claims = [claim for claim in entry.claims if claim.kind == "usage"]
    usages = [claim.statement for claim in usage_claims]
    if len(usages) > MAX_PUBLIC_USAGES:
        raise CapabilityAnnotationError(
            "teaching entry allows at most three usages; larger fixed "
            "alternatives must be merged without changing their structure"
        )
    standard_usage_indexes: set[int] = set()
    reply_usage_indexes: set[int] = set()
    if target.mode is CapabilityInvocationMode.COMPLETE:
        contexts = [split_reply_usage(usage)[0] for usage in usages]
        standard_usage_indexes = {
            index for index, context in enumerate(contexts) if context is None
        }
        if not standard_usage_indexes:
            standard_usage_indexes = {
                index
                for index, context in enumerate(contexts)
                if context and context.startswith("[")
            }
        if len(standard_usage_indexes) != 1:
            raise CapabilityAnnotationError(
                "complete invocation requires exactly one standard aggregate usage; "
                "additional usages must be reply variants"
            )
        reply_usage_indexes = set(range(len(usages))) - standard_usage_indexes
        for usage in usages:
            validate_complete_aggregate_usage(usage)
            if _complete_usage_embeds_distinct_invocations(usage):
                raise CapabilityAnnotationError(
                    "参数化聚合的圆括号只能枚举简短成员值；"
                    "请保留共同成员选择位，不得借回复变体逐成员列举命令"
                )
        standard_usage = usages[next(iter(standard_usage_indexes))]
        category_error = _complete_family_usage_category_error(entry, request, usage=standard_usage)
        if category_error is not None:
            raise CapabilityAnnotationError(category_error)
    elif target.mode in {
        CapabilityInvocationMode.REGEX,
        CapabilityInvocationMode.KEYWORD,
    }:
        standard_usage_indexes = set(range(len(usages)))
    elif target.canonical_usages:
        for template in target.canonical_usages:
            usage_errors: list[str] = []
            for index, usage in enumerate(usages):
                if index in standard_usage_indexes:
                    continue
                try:
                    validate_capability_usage_template(usage, template)
                except CapabilityAnnotationError as error:
                    usage_errors.append(f"usage[{index}]: {error}")
                    continue
                standard_usage_indexes.add(index)
                break
            else:
                raise CapabilityAnnotationError(
                    "every parser-provided structural template must be preserved; "
                    f"entry_id={entry.entry_id}, field=usage, required_template={template!r}, "
                    f"submitted_usages={usages!r}; details={'; '.join(usage_errors)}。"
                    "请按具体原因修正槽位命名或标准 usage 的参数结构，"
                    "保留命令、括号、顺序、Option、别名与重复标记，仅槽位名称可改写；"
                    "shortcut 不能替代标准用法。此错误不要求修改 display_trigger。"
                )
    elif target.mode is CapabilityInvocationMode.ANCHORED and target.command_body is not None:
        standard_usage_indexes = {
            index
            for index, usage in enumerate(usages)
            if len(
                re.findall(
                    usage_command_body_pattern(
                        target.command_body, canonical_usages=target.canonical_usages
                    ),
                    usage,
                )
            )
            == 1
        }
        if not standard_usage_indexes:
            raise CapabilityAnnotationError(
                "anchored entry must preserve a standard command usage; "
                f"entry_id={entry.entry_id}, field=usage, "
                f"command_body={target.command_body!r}, submitted_usages={usages!r}"
            )
    for index, usage in enumerate(usages):
        if (
            index in standard_usage_indexes
            or index in reply_usage_indexes
            or not split_reply_usage(usage)[0]
        ):
            continue
        reply_errors: list[str] = []
        for template in target.canonical_usages:
            try:
                validate_capability_usage_template(usage, template, allow_reply_context=True)
            except CapabilityAnnotationError as error:
                reply_errors.append(str(error))
                continue
            reply_usage_indexes.add(index)
            break
        else:
            if target.canonical_usages:
                raise CapabilityAnnotationError(
                    "reply usage must uniquely align with parser slots and preserve commands, "
                    "options and retained slot structure; only a required reply context may "
                    "supply required positional slots. Omit uncertain reply variants, not the "
                    f"standard usage; details={'; '.join(reply_errors)}"
                )
    shortcut_usage_indexes = set(range(len(usages))) - standard_usage_indexes - reply_usage_indexes
    if shortcut_usage_indexes:
        if len(shortcut_usage_indexes) > target.shortcut_count:
            raise CapabilityAnnotationError("shortcut usages exceed the registered shortcut count")
        shortcut_evidence_ids = set(target.shortcut_evidence_ids)
        for index in shortcut_usage_indexes:
            if not shortcut_evidence_ids.intersection(usage_claims[index].evidence_ids):
                raise CapabilityAnnotationError(
                    "shortcut usage must cite registered shortcut Evidence; "
                    f"entry_id={entry.entry_id}, field=usage[{index}].evidence_ids, "
                    f"statement={usages[index]!r}"
                )
    if (
        not target.canonical_usages
        and target.mode is CapabilityInvocationMode.ANCHORED
        and target.command_body is not None
        and _has_redundant_anchored_usage(
            [usages[index] for index in sorted(standard_usage_indexes)],
            target.command_body,
        )
    ):
        raise CapabilityAnnotationError(
            "同一 entry 中可省略的参数必须用一条方括号用法表示，不得同时输出省略版和带参数版"
        )
    for index, usage in enumerate(usages):
        validate_capability_usage_pattern(
            usage,
            allow_separated_slots=bool(target.canonical_usages)
            and index not in shortcut_usage_indexes,
        )
        if target.mode is CapabilityInvocationMode.KEYWORD:
            validate_keyword_usage(usage, target)
        is_shortcut = index in shortcut_usage_indexes
        if (
            not is_shortcut
            and not target.canonical_usages
            and target.mode is CapabilityInvocationMode.ANCHORED
            and target.command_body is not None
            and len(
                re.findall(
                    usage_command_body_pattern(
                        target.command_body, canonical_usages=target.canonical_usages
                    ),
                    usage,
                )
            )
            != 1
        ):
            raise CapabilityAnnotationError("anchored usage must contain command_body exactly once")
        if (
            target.requires_mention
            and target.command_body is not None
            and (
                len(re.findall(r"(?<!\S)@bot(?=\s)", usage)) != 1
                if is_shortcut
                else len(
                    re.findall(
                        usage_command_body_pattern(
                            target.command_body,
                            requires_mention=True,
                            canonical_usages=target.canonical_usages,
                        ),
                        usage,
                    )
                )
                != 1
            )
        ):
            raise CapabilityAnnotationError(
                "mention-required usage must place @bot before command_body"
            )
        if (
            target.requires_mention
            and target.mode
            in {
                CapabilityInvocationMode.COMPLETE,
                CapabilityInvocationMode.REGEX,
            }
            and len(re.findall(r"(?<!\S)@bot(?=\s)", usage)) != 1
        ):
            raise CapabilityAnnotationError(
                "mention-required non-anchored usage must contain one @bot placeholder"
            )
    return [usages[index] for index in sorted(standard_usage_indexes)]


def _validate_analysis_output_contract(
    output: _AnalysisOutput,
    request: CapabilityAnalysisRequest,
    captured_evidence: tuple[CapabilityEvidenceUnit, ...],
    *,
    allow_alias_fallback: bool,
) -> None:
    """用正常输出路径的全部合同校验候选，供在线校验与超时恢复共用。

    汇总不同入口及独立检查的错误；前置条件失败时不执行依赖检查。

    Args:
        output: 已通过 Pydantic 结构解析的模型候选。
        request: 当前教学单元请求及其静态 Evidence。
        captured_evidence: 本轮只读工具新增的可引用 Evidence。
        allow_alias_fallback: 是否允许在已经用完一次别名纠错后应用确定性回退。

    Raises:
        CapabilityAnalysisError: Evidence、gate 或领域合同不成立。
        CapabilityAnnotationError: usage、别名或公开投影合同不成立。
        ExceptionGroup: 同一候选存在多个已确认的独立合同错误。
    """

    targets = {item.entry_id: item for item in request.invocations}
    if output.knowledge_enabled and {item.entry_id for item in output.entries} != set(targets):
        raise CapabilityAnnotationError("entries must exactly match the request invocations")

    errors: list[Exception] = []
    try:
        _validate_gate_resolution_output(output, request)
    except CapabilityAnnotationError as error:
        errors.append(CapabilityAnnotationError(f"field=gate_resolutions: {error}"))
    gate_valid = not errors

    if output.knowledge_enabled:
        standard_usages: dict[str, list[str]] = {}
        for entry in output.entries:
            target = targets[entry.entry_id]
            try:
                standard_usages[entry.entry_id] = _validate_entry_usages(entry, target, request)
            except (CapabilityAnalysisError, CapabilityAnnotationError) as error:
                errors.append(
                    CapabilityAnnotationError(f"entry_id={entry.entry_id}, field=usage: {error}")
                )
            try:
                _validate_rate_limit_config_values(entry, request)
            except (CapabilityAnalysisError, CapabilityAnnotationError) as error:
                errors.append(
                    CapabilityAnnotationError(
                        f"entry_id={entry.entry_id}, field=constraints: {error}"
                    )
                )

        # 表达式自身可独立校验；只有标准 usage 合法时才检查替换后的展示。
        alias_failures = _alias_pattern_failures(output.entries, targets, standard_usages)
        if alias_failures and not allow_alias_fallback:
            errors.append(_AliasPatternValidationError(alias_failures))
        elif alias_failures:
            for entry, target, _reason in alias_failures:
                fallback = deterministic_usage_selector(_alias_literals(target))
                entry.display_trigger = (
                    fallback
                    if fallback is not None
                    and _display_trigger_usage_error(
                        standard_usages.get(entry.entry_id, ()), target, fallback
                    )
                    is None
                    else None
                )

    # 领域校验依赖 gate 结构；公开投影依赖前述全部检查，避免连带误报。
    domain_output: CapabilityAnalysisOutput | None = None
    if gate_valid:
        try:
            domain_output = _to_domain_output(output, captured_evidence)
            validate_capability_analysis_output(request, domain_output)
        except (CapabilityAnalysisError, CapabilityAnnotationError) as error:
            errors.append(error)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("output contract validation failed", errors)
    assert domain_output is not None
    project_capability_annotation(
        request,
        domain_output,
        analysis_revision="projection-validation",
    )


def _build_payload(request: CapabilityAnalysisRequest) -> str:
    invocation_targets = {item.entry_id: item for item in request.invocations}
    payload = {
        "schema_version": 9,
        "prompt_id": CAPABILITY_ANNOTATION_PROMPT_ID,
        "capability": {
            "capability_id": request.capability.capability_id,
            "owner": request.capability.owner,
            "kind": request.capability.kind,
            "adapter": request.capability.adapter,
        },
        "invocations": [
            {
                "entry_id": item.entry_id,
                "mode": item.mode.value,
                "command_body": item.command_body,
                "regex_pattern": item.regex_pattern,
                "regex_flags": list(item.regex_flags),
                "keywords": list(item.keywords),
                "canonical_usages": list(item.canonical_usages),
                "aliases": list(item.aliases),
                "requires_mention": item.requires_mention,
                "shortcut_count": item.shortcut_count,
                "shortcut_evidence_ids": list(item.shortcut_evidence_ids),
            }
            for item in request.invocations
        ],
        "family_manifest": (
            {
                "member_count": len(request.family_members),
                "evidence_ids": sorted(
                    {
                        evidence_id
                        for member in request.family_members
                        for evidence_id in member.evidence_ids
                    }
                ),
            }
            if request.family_members
            else None
        ),
        "gate_candidates": [
            {
                "candidate_id": item.candidate_id,
                "kind": item.kind.value,
                "entry_ids": list(item.entry_ids),
                "evidence_ids": list(item.evidence_ids),
                "owner": item.owner,
                "symbol": item.symbol,
            }
            for item in request.gate_candidates
        ],
        "fixed_constraints": [
            {
                "kind": item.kind.value,
                "statement": item.statement,
                "evidence_ids": list(item.evidence_ids),
                "config_reference_ids": list(item.config_reference_ids),
                "role": item.role.value if item.role is not None else None,
                "allowed_scenes": [scene.value for scene in item.allowed_scenes],
                "rate_limit_policy": (
                    item.rate_limit_policy.value if item.rate_limit_policy is not None else None
                ),
                "rate_limit_scope": (
                    item.rate_limit_scope.value if item.rate_limit_scope is not None else None
                ),
                "permission_alternatives": [
                    {
                        "kind": alternative.kind.value,
                        "statement": alternative.statement,
                        "role": alternative.role.value if alternative.role is not None else None,
                        "scene": (
                            alternative.scene.value if alternative.scene is not None else None
                        ),
                    }
                    for alternative in item.permission_alternatives
                ],
            }
            for item in request.fixed_constraints
        ],
        "source_context": (
            {
                "module_name": request.source_context.module_name,
                "plugin_source_revision": request.source_context.plugin_source_revision,
            }
            if request.source_context is not None
            else None
        ),
        "evidence_units": [
            {
                "evidence_id": unit.evidence_id,
                "source_kind": unit.source_kind,
                "content": unit.content,
                "revision": unit.revision,
                "locator": unit.locator,
            }
            for unit in sorted(request.evidence_units, key=lambda item: item.evidence_id)
        ],
        "config_projections": [
            {
                "reference_id": projection.reference_id,
                "source_symbol": projection.source_symbol,
                "value": projection.value,
            }
            for projection in sorted(request.config_projections, key=lambda item: item.reference_id)
        ],
        "unknown_config": [
            {
                "reference_id": reference.reference_id,
                "source_symbol": reference.source_symbol,
                "reason": reference.reason,
            }
            for reference in sorted(request.unknown_config, key=lambda item: item.reference_id)
        ],
        "allowed_evidence_ids": sorted(unit.evidence_id for unit in request.evidence_units),
        "allowed_config_reference_ids": [
            projection.reference_id
            for projection in sorted(request.config_projections, key=lambda item: item.reference_id)
        ],
        "previous_annotation": (
            {
                "entries": [
                    {
                        "entry_id": entry.entry_id,
                        "name": entry.name,
                        "summary": entry.summary,
                        "usages": (
                            []
                            if (
                                (target := invocation_targets.get(entry.entry_id)) is not None
                                and target.aliases
                            )
                            else list(entry.usages)
                        ),
                        "search_terms": list(entry.search_terms),
                        "behavior_boundaries": list(entry.behavior_boundaries),
                    }
                    for entry in request.previous_annotation.entries
                ],
            }
            if request.previous_annotation is not None
            else None
        ),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _to_domain_output(
    output: _AnalysisOutput,
    captured_evidence: tuple[CapabilityEvidenceUnit, ...] = (),
) -> CapabilityAnalysisOutput:
    entries = tuple(_to_domain_entry(item) for item in output.entries)
    referenced = {
        evidence_id
        for entry in entries
        for item in (*entry.claims, *entry.constraints)
        for evidence_id in item.evidence_ids
    }
    referenced.update(
        evidence_id
        for entry in entries
        for change in entry.baseline_changes
        for evidence_id in change.evidence_ids
    )
    referenced.update(
        evidence_id
        for resolution in output.gate_resolutions
        for evidence_id in resolution.evidence_ids
    )
    return CapabilityAnalysisOutput(
        knowledge_enabled=output.knowledge_enabled,
        entries=entries,
        evidence_units=tuple(item for item in captured_evidence if item.evidence_id in referenced),
        gate_resolutions=tuple(
            CapabilityGateResolution(
                candidate_id=item.candidate_id,
                outcome=CapabilityGateResolutionKind(item.outcome),
                evidence_ids=tuple(item.evidence_ids),
                config_reference_ids=tuple(item.config_reference_ids),
            )
            for item in output.gate_resolutions
        ),
    )


def _validate_rate_limit_config_values(
    entry: _AnalysisEntryOutput,
    request: CapabilityAnalysisRequest,
) -> None:
    projections = {item.reference_id: item.value for item in request.config_projections}
    for constraint in entry.constraints:
        if constraint.kind != "rate_limit":
            continue
        for reference_id in constraint.config_reference_ids:
            value = projections.get(reference_id)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            expected = {str(value)}
            if isinstance(value, float) and value.is_integer():
                expected.add(str(int(value)))
            if not any(candidate in constraint.statement for candidate in expected):
                raise CapabilityAnnotationError(
                    "rate-limit statement must include every cited numeric config value"
                )


def _validate_gate_resolution_output(
    output: _AnalysisOutput,
    request: CapabilityAnalysisRequest,
) -> None:
    candidates = {item.candidate_id: item for item in request.gate_candidates}
    resolutions = {item.candidate_id: item for item in output.gate_resolutions}
    if len(resolutions) != len(output.gate_resolutions) or set(resolutions) != set(candidates):
        raise CapabilityAnnotationError("每个 gate candidate 必须且只能返回一个 gate resolution")
    public_owners_by_candidate: dict[str, dict[str, str]] = {}

    def register_public_owner(candidate_id: str, entry_id: str, owner: str) -> None:
        if candidate_id not in candidates:
            raise CapabilityAnnotationError(f"{owner} 引用了不存在的 gate candidate")
        owners = public_owners_by_candidate.setdefault(candidate_id, {})
        if entry_id in owners:
            raise CapabilityAnnotationError(
                "同一 gate candidate 在一个 entry 中只能选择一个公开语义所有者"
            )
        owners[entry_id] = owner

    for entry in output.entries:
        for claim in entry.claims:
            for candidate_id in claim.gate_candidate_ids:
                register_public_owner(candidate_id, entry.entry_id, "behavior_boundary")
        for constraint in entry.constraints:
            for candidate_id in constraint.gate_candidate_ids:
                register_public_owner(candidate_id, entry.entry_id, "constraint")
                if (
                    candidates[candidate_id].kind is CapabilityGateKind.PERMISSION
                    and constraint.kind != "permission"
                ):
                    raise CapabilityAnnotationError(
                        "permission gate 必须输出一条包含 OR alternatives 的 permission constraint"
                    )
    for candidate_id, candidate in candidates.items():
        resolution = resolutions[candidate_id]
        if not set(candidate.evidence_ids).issubset(resolution.evidence_ids):
            raise CapabilityAnnotationError("gate resolution 必须引用候选本身的结构 Evidence")
        support = set(resolution.evidence_ids).difference(candidate.evidence_ids)
        if (
            resolution.outcome != "unresolved"
            and not support
            and not resolution.config_reference_ids
        ):
            raise CapabilityAnnotationError(
                "已解释的 gate candidate 必须引用定义、框架事实或运行配置 Evidence"
            )
        linked_entries = set(public_owners_by_candidate.get(candidate_id, {}))
        if resolution.outcome == "constraint":
            missing_entry_ids = sorted(set(candidate.entry_ids).difference(linked_entries))
            if missing_entry_ids:
                raise CapabilityAnnotationError(
                    "gate_missing_public_owner："
                    f"candidate_id={candidate_id}；"
                    f"missing_entry_ids={','.join(missing_entry_ids)}；"
                    "调用者身份、会话场景、使用资格或限流使用对应 constraint 关联；"
                    "能力自身的业务准备状态使用 behavior_boundary claim 关联。"
                    "只选择一个语义所有者并在其 gate_candidate_ids 中填写上述 candidate_id"
                )
        elif linked_entries:
            raise CapabilityAnnotationError(
                "no_constraint 或 unresolved 不能关联公开 constraint 或 behavior_boundary"
            )
    if output.knowledge_enabled and any(
        item.outcome == "unresolved" for item in output.gate_resolutions
    ):
        raise CapabilityAnnotationError("仍有 unresolved gate candidate 时必须关闭知识")


def _to_domain_entry(output: _AnalysisEntryOutput) -> CapabilityAnalysisEntryOutput:
    return CapabilityAnalysisEntryOutput(
        entry_id=output.entry_id,
        display_trigger=output.display_trigger,
        claims=tuple(
            SemanticClaim(
                kind=SemanticClaimKind(item.kind),
                statement=item.statement,
                evidence_ids=tuple(item.evidence_ids),
                config_reference_ids=tuple(item.config_reference_ids),
                gate_candidate_ids=tuple(item.gate_candidate_ids),
            )
            for item in output.claims
        ),
        baseline_changes=tuple(
            BaselineMemberChange(
                operation=BaselineChangeOperation(item.op),
                field=BaselineMemberField(item.field),
                old_value=item.old_value,
                new_value=item.new_value,
                evidence_ids=tuple(item.evidence_ids),
                config_reference_ids=tuple(item.config_reference_ids),
            )
            for item in output.baseline_changes
        ),
        constraints=tuple(
            SemanticConstraint(
                kind=SemanticConstraintKind(item.kind),
                statement=item.statement,
                evidence_ids=tuple(item.evidence_ids),
                config_reference_ids=tuple(item.config_reference_ids),
                role=TeachingRole(item.role) if item.role is not None else None,
                allowed_scenes=tuple(TeachingScene(scene) for scene in item.allowed_scenes),
                rate_limit_policy=(
                    RateLimitPolicy(item.rate_limit_policy)
                    if item.rate_limit_policy is not None
                    else None
                ),
                rate_limit_scope=(
                    RateLimitScope(item.rate_limit_scope)
                    if item.rate_limit_scope is not None
                    else None
                ),
                gate_candidate_ids=tuple(item.gate_candidate_ids),
                permission_alternatives=tuple(
                    PermissionAlternative(
                        kind=SemanticConstraintKind(alternative.kind),
                        statement=alternative.statement,
                        role=(
                            TeachingRole(alternative.role) if alternative.role is not None else None
                        ),
                        scene=(
                            TeachingScene(alternative.scene)
                            if alternative.scene is not None
                            else None
                        ),
                    )
                    for alternative in item.permission_alternatives
                ),
            )
            for item in output.constraints
        ),
    )


__all__ = (
    "SYSTEM_INSTRUCTION",
    "CapabilityAnalysisToolRuntime",
    "CapabilityAnalysisToolRuntimeFactory",
    "CapabilityModelAdapterError",
    "CapabilityModelAdapterReason",
    "PydanticAICapabilityAnalysisClient",
)
