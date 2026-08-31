from __future__ import annotations

from collections.abc import Sequence

import nbtriage.capability_model_prompt as prompt
from nbtriage.capability_analysis import (
    CapabilityAnalysisBaseline,
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
)


def _request(
    modes: Sequence[CapabilityInvocationMode],
    *,
    with_baseline: bool = False,
) -> CapabilityAnalysisRequest:
    invocations: list[CapabilityInvocationTarget] = []
    for index, mode in enumerate(modes):
        if mode is CapabilityInvocationMode.ANCHORED:
            invocation = CapabilityInvocationTarget(
                f"entry-{index}",
                mode,
                command_body="搜图",
            )
        elif mode is CapabilityInvocationMode.REGEX:
            invocation = CapabilityInvocationTarget(
                f"entry-{index}",
                mode,
                regex_pattern=r"^(查图|搜图)$",
            )
        else:
            invocation = CapabilityInvocationTarget(f"entry-{index}", mode)
        invocations.append(invocation)
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
                'search = on_command("搜图")',
                "sha256:source",
                "plugin.demo:search:12",
            ),
        ),
        invocations=tuple(invocations),
        previous_annotation=CapabilityAnalysisBaseline() if with_baseline else None,
    )


def test_prompt_fragments_follow_request_structure() -> None:
    cases = (
        (
            (CapabilityInvocationMode.ANCHORED,),
            False,
            (prompt.CORE_INSTRUCTION, prompt.ANCHORED_INSTRUCTION),
        ),
        (
            (CapabilityInvocationMode.REGEX,),
            False,
            (prompt.CORE_INSTRUCTION, prompt.REGEX_INSTRUCTION),
        ),
        (
            (CapabilityInvocationMode.COMPLETE,),
            False,
            (prompt.CORE_INSTRUCTION, prompt.FAMILY_INSTRUCTION),
        ),
        (
            (CapabilityInvocationMode.ANCHORED,),
            True,
            (
                prompt.CORE_INSTRUCTION,
                prompt.ANCHORED_INSTRUCTION,
                prompt.BASELINE_INSTRUCTION,
            ),
        ),
        (
            (
                CapabilityInvocationMode.ANCHORED,
                CapabilityInvocationMode.REGEX,
                CapabilityInvocationMode.COMPLETE,
            ),
            True,
            (
                prompt.CORE_INSTRUCTION,
                prompt.ANCHORED_INSTRUCTION,
                prompt.REGEX_INSTRUCTION,
                prompt.FAMILY_INSTRUCTION,
                prompt.BASELINE_INSTRUCTION,
            ),
        ),
    )

    for modes, with_baseline, expected_parts in cases:
        request = _request(modes, with_baseline=with_baseline)
        assert prompt._instructions_for_request(request) == "\n\n".join(expected_parts)


def test_prompt_preserves_unique_model_only_contracts() -> None:
    contracts = {
        "core": (
            prompt.CORE_INSTRUCTION,
            (
                "不得为了再次确认而重读整个文件",
                "`navigation_ref` 调用 `python_open_definition`",
                "不得依据预训练知识、库名或符号名",
                "可执行源码和 Runtime 事实是业务语义的主要证据",
                "不得暴露源码路径、Python 符号",
                "密钥、令牌、凭据、认证头、请求参数及其传输方式属于实现机制",
                "实际条件、数据流、赋值、运算、状态更新和调度逻辑优先于",
                "提示文字可以证明用户会看到什么",
                "任一 gate candidate 仍为 unresolved 时",
                "只限制特定 Option、子命令、输入类别、业务对象或结果分支",
                "用户能够通过公开业务操作理解、改变或满足",
                "platform_scope 是模型外拥有的 Runtime 路由事实",
                "不得根据某种身份通常如何获得资格反推 role",
                "原子场景只有 private、group、guild、channel_text、channel_category、channel_voice",
                "内部持久化只有在其用户可观察效果有教学价值时才说明",
                "每条只能是一条可直接成为用户查询的独立短语",
                "entries 的 entry_id 必须与它完全一致",
                "不得添加 payload、output 或 result 包装",
                "mention 是完整输入原子，必须整体放入槽位",
            ),
        ),
        "anchored": (
            prompt.ANCHORED_INSTRUCTION,
            (
                "展开后必须恰好等于全部入口",
                "shortcut usage 可以是完全不同的可调用文字，不要求包含 command_body",
                "Uniseg `At` 是用户直接提供的 `@用户` 输入形式",
                "Alconna `compact` 是 Runtime 已确认的语法",
                "同一 entry 默认只输出一条 usage",
            ),
        ),
        "regex": (
            prompt.REGEX_INSTRUCTION,
            ("只属于某个分支的参数必须留在该分支内部",),
        ),
        "family": (
            prompt.FAMILY_INSTRUCTION,
            (
                "不得试图逐成员阅读，也不得用源码导航代替完整成员复核",
                "usage 必须逐字符保留成员变量前后的全部固定字面量",
                "成员参数数量、直接输入、必选性或精确 usage 可以不同",
            ),
        ),
        "baseline": (
            prompt.BASELINE_INSTRUCTION,
            (
                "遗漏旧成员表示保持不变",
                "每条 remove 或 replace 都必须引用明确推翻旧值的当前 Evidence",
            ),
        ),
    }

    for fragment, (instruction, expected_contracts) in contracts.items():
        missing = [contract for contract in expected_contracts if contract not in instruction]
        assert not missing, f"{fragment} prompt 缺少关键合同：{missing}"


def test_prompt_does_not_embed_case_answers_or_obsolete_output_paths() -> None:
    for forbidden in ("禁言、口他、口她", "payload."):
        assert forbidden not in prompt.SYSTEM_INSTRUCTION
