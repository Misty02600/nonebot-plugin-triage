"""基础框架资料的选段合同；正文只来自当前知识包，不维护 API 解释。"""

from __future__ import annotations

import json
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version

from nbtriage.capability.teaching.analysis import CapabilityAnalysisRequest, CapabilityEvidenceUnit
from nbtriage.knowledge_index import KnowledgeIndexReader, KnowledgePackError

NONEBOT_SECTIONS = (
    ("nonebot2/tutorial/handler.mdx", "事件处理 > 事件处理函数"),
    ("nonebot2/advanced/matcher.md", "事件响应器进阶 > 事件响应器组成"),
    ("nonebot2/advanced/dependency.mdx", "依赖注入 > 类型依赖注入"),
    ("nonebot2/advanced/dependency.mdx", "依赖注入 > 子依赖"),
    ("nonebot2/advanced/dependency.mdx", "依赖注入 > 非依赖参数"),
    ("nonebot2/appendices/overload.md", "事件类型与重载"),
    ("nonebot2/appendices/permission.mdx", "权限控制"),
    ("nonebot2/appendices/session-control.mdx", "会话控制"),
)
# 概述本身包含必要定义；使用独立选取而非整个依赖注入章节，避免预载全部 provider 手册。
NONEBOT_OVERVIEW = ("nonebot2/advanced/dependency.mdx", "依赖注入")
ALCONNA_SECTIONS = (
    ("nonebot2/best-practice/alconna/command.md", "Alconna 本体 > 参数声明(Args)"),
    (
        "nonebot2/best-practice/alconna/command.md",
        "Alconna 本体 > 选项与子命令(Option & Subcommand)",
    ),
    ("nonebot2/best-practice/alconna/command.md", "Alconna 本体 > 解析结果"),
    ("nonebot2/best-practice/alconna/matcher.mdx", "`on_alconna` 响应器 > 声明"),
    ("nonebot2/best-practice/alconna/matcher.mdx", "`on_alconna` 响应器 > 依赖注入"),
    ("nonebot2/best-practice/alconna/matcher.mdx", "`on_alconna` 响应器 > 条件控制"),
)
UNINFO_SECTIONS = (
    ("uninfo/README.md", "nonebot-plugin-uninfo > 使用"),
    ("uninfo/README.md", "nonebot-plugin-uninfo > 模型定义"),
)


def add_bootstrap_docs(
    request: CapabilityAnalysisRequest,
    reader: KnowledgeIndexReader,
    *,
    pack_revision: str,
) -> CapabilityAnalysisRequest:
    """完整选段作为首包 Evidence；资料不完整时保留原证据，不部分替换旧说明。"""
    try:
        nonebot_version = version("nonebot2")
        # 保留概述原文，不把其全部子节都当成必需基础资料。
        overview = reader.read_sections(
            component="nonebot2",
            version=nonebot_version,
            sections=(NONEBOT_OVERVIEW,),
        )
        documents = [item for item in overview if item.locator.endswith("#依赖注入")]
        documents.extend(
            reader.read_sections(
                component="nonebot2",
                version=nonebot_version,
                sections=NONEBOT_SECTIONS,
            )
        )
    except (KnowledgePackError, PackageNotFoundError):
        return request
    replaced = {"framework:nonebot2/dependency-overload"}
    # 使用 Runtime 分类和 family 解析器事实；普通命令也可能有 canonical_usages。
    if (
        request.capability.kind == "alconna"
        or any(
            unit.source_kind == "runtime_family_shapes"
            and any(
                shape.get("parser") == "alconna" for shape in json.loads(unit.content)["shapes"]
            )
            for unit in request.evidence_units
        )
        or any(
            unit.locator == "framework:nonebot-plugin-alconna/dispatch"
            for unit in request.evidence_units
        )
    ):
        try:
            documents.extend(
                reader.read_sections(
                    component="nonebot2",
                    version=nonebot_version,
                    sections=ALCONNA_SECTIONS,
                )
            )
        except KnowledgePackError:
            pass
        else:
            replaced.add("framework:nonebot-plugin-alconna/dispatch")
    unique = {item.evidence_id: item for item in documents}
    if any(
        unit.locator == "framework:nonebot-plugin-uninfo/Session"
        or "/nonebot_plugin_uninfo/" in (unit.locator or "")
        for unit in request.evidence_units
    ):
        try:
            uninfo_docs = reader.read_sections(
                component="nonebot-plugin-uninfo",
                version=version("nonebot-plugin-uninfo"),
                sections=UNINFO_SECTIONS,
            )
        except (KnowledgePackError, PackageNotFoundError):
            pass
        else:
            unique.update((item.evidence_id, item) for item in uninfo_docs)
            replaced.add("framework:nonebot-plugin-uninfo/Session")
    units = tuple(
        CapabilityEvidenceUnit(
            evidence_id=item.evidence_id,
            source_kind="knowledge_user_docs",
            content=item.excerpt,
            revision=f"pack:{pack_revision}:{item.revision}",
            locator=f"knowledge/{item.component}/{item.locator}",
        )
        for item in unique.values()
    )
    ids = set(unique)
    return replace(
        request,
        evidence_units=(
            *units,
            *(
                unit
                for unit in request.evidence_units
                if unit.locator not in replaced and unit.evidence_id not in ids
            ),
        ),
    )
