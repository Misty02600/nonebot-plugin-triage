from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest

import nbtriage.capability.teaching._prompt as prompt
from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisBaseline,
    CapabilityAnalysisRequest,
    CapabilityEvidenceUnit,
    CapabilityGateCandidate,
    CapabilityGateKind,
    CapabilityIdentity,
    CapabilityInvocationMode,
    CapabilityInvocationTarget,
    ConfigProjection,
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
        elif mode is CapabilityInvocationMode.KEYWORD:
            invocation = CapabilityInvocationTarget(f"entry-{index}", mode, keywords=("查找",))
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
            (CapabilityInvocationMode.KEYWORD,),
            False,
            (prompt.CORE_INSTRUCTION, prompt.KEYWORD_INSTRUCTION),
        ),
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


@pytest.mark.parametrize(
    "features",
    [
        (),
        ("aliases",),
        ("shortcuts",),
        ("canonical",),
        ("config",),
        ("gates",),
        ("aliases", "shortcuts", "canonical", "config", "gates"),
    ],
)
def test_optional_prompt_rules_follow_facts_across_mixed_entries(features):
    request = _request(
        (
            CapabilityInvocationMode.KEYWORD,
            CapabilityInvocationMode.ANCHORED,
            CapabilityInvocationMode.ANCHORED,
        ),
        with_baseline=True,
    )
    invocations = tuple(
        replace(
            item,
            aliases=("查图",) if "aliases" in features else (),
            canonical_usages=("搜图 <slot:0>",) if "canonical" in features else (),
            shortcut_count=1 if "shortcuts" in features else 0,
            shortcut_evidence_ids=("evidence-handler",) if "shortcuts" in features else (),
        )
        if item.mode is CapabilityInvocationMode.ANCHORED
        else item
        for item in request.invocations
    )
    request = replace(
        request,
        invocations=invocations,
        config_projections=(ConfigProjection("config-enabled", "config.enabled", True),)
        if "config" in features
        else (),
        gate_candidates=(
            CapabilityGateCandidate(
                "gate-permission",
                CapabilityGateKind.PERMISSION,
                ("entry-1",),
                ("evidence-handler",),
            ),
        )
        if "gates" in features
        else (),
    )
    instruction = prompt._instructions_for_request(request)
    for feature, fragment in (
        ("aliases", prompt.ALIAS_INSTRUCTION),
        ("shortcuts", prompt.SHORTCUT_INSTRUCTION),
        ("canonical", prompt.CANONICAL_INSTRUCTION),
        ("config", prompt.CONFIG_INSTRUCTION),
        ("gates", prompt.GATE_INSTRUCTION),
    ):
        assert instruction.count(fragment) == (1 if feature in features else 0)
    assert prompt.KEYWORD_INSTRUCTION in instruction
    assert prompt.BASELINE_INSTRUCTION in instruction
    assert "没有 gate candidate 不等于没有执行限制" in instruction
    assert "不得把“不限流”“没有权限限制”等整体无约束结论写进公开字段" in instruction
    assert "配置投影已经关闭的处理分支必须省略" in instruction
    assert "证据或对齐不明确时省略该回复变体" in instruction
    assert "按下一条处理" not in instruction


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
                "以已确认的实际条件、数据流、赋值、运算、状态更新和调度逻辑为准",
                "提示文字可以证明用户会看到什么",
                "没有 gate candidate 不等于没有执行限制",
                "仅限制某个 Option、子命令、输入类别、业务对象或结果分支",
                "用户可通过公开业务操作理解、改变或满足",
                "platform_scope 是模型外拥有的 Runtime 路由事实",
                "不得根据身份通常如何获得资格反推 role",
                "non_private 表达非私聊，它不是互斥原子类型",
                "另有依赖注入等更窄条件时，保留该限制",
                "内部持久化只有在其用户可观察效果有教学价值时才说明",
                "每条只能是一条可直接成为用户查询的独立短语",
                "entries 的 entry_id 必须与它完全一致",
                "不得添加 payload、output 或 result 包装",
                "mention 是完整输入原子，必须整体放入槽位",
                "requires_mention=false 时，不得自行添加必需的 `@bot`",
                "可选回复不能用于省略必填槽位",
                "证据或对齐不明确时省略该回复变体",
                "优先在槽位内部用 `|` 简洁列举；这仍是一个参数",
                "共同场景与分支组为 AND",
                "若存在绕过该场景的允许路径，不得将其提为共同场景",
                "Evidence 已明确证明的嵌套角色 OR 可以展开",
            ),
        ),
        "anchored": (
            prompt.ANCHORED_INSTRUCTION,
            (
                "同一 entry 默认只输出一条 usage",
                "aliases 为空时，display_trigger 使用 null",
            ),
        ),
        "aliases": (prompt.ALIAS_INSTRUCTION, ("展开后必须恰好等于全部入口",)),
        "shortcuts": (
            prompt.SHORTCUT_INSTRUCTION,
            (
                "shortcut usage 可以是完全不同的可调用文字，不要求包含 command_body",
                "shortcut Evidence 的 `compact`",
            ),
        ),
        "canonical": (
            prompt.CANONICAL_INSTRUCTION,
            (
                "Uniseg `At` 是用户直接提供的 `@用户` 输入形式",
                "Alconna `compact` 是 Runtime 已确认的语法",
            ),
        ),
        "gates": (
            prompt.GATE_INSTRUCTION,
            (
                "任一 gate candidate 仍为 unresolved 时",
                "每个 gate resolution 都必须引用 candidate 自己的结构 Evidence",
            ),
        ),
        "regex": (
            prompt.REGEX_INSTRUCTION,
            (
                "只属于某个分支的参数必须留在该分支内部",
                "转换为统一的帮助记法",
                "固定字面字符按实际输入保留",
            ),
        ),
        "family": (
            prompt.FAMILY_INSTRUCTION,
            (
                "不得试图逐成员阅读，也不得用源码导航代替完整成员复核",
                "usage 必须逐字符保留成员变量前后的全部固定字面量",
                "成员参数数量、直接输入、必选性或精确 usage 可以不同",
                "前置回复上下文不计入这些槽位",
                "不要给单个概念槽位再套分组括号，源码本身包含的字面括号除外",
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
