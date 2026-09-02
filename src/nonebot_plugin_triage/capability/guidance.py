from __future__ import annotations

import re

from nbtriage.public_guidance import (
    PUBLIC_GUIDANCE_SCHEMA_VERSION,
    PublicGuidanceFact,
    PublicGuidanceFactBasis,
    PublicGuidanceFactField,
    PublicGuidanceRequest,
)
from nonebot_plugin_triage.capability.discovery.registry import PublicCapability

_NON_SEARCH_TEXT = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff_-]+")


def format_capability_guidance(
    query: str,
    capabilities: tuple[PublicCapability, ...],
    *,
    limit: int = 8,
) -> str:
    if not capabilities:
        return "没有找到相关功能。"

    matches = _matching_capabilities(query, capabilities)
    if len(matches) == 1:
        capability = matches[0]
        lines = [
            f"{capability.header}：{capability.description or '暂无说明'}",
            f"用法：{capability.usage}",
        ]
        if capability.example:
            lines.append(f"示例：{capability.example}")
        return "\n".join(lines)

    shown = matches[:limit] if matches else capabilities[:limit]
    lines = ["我目前能说明这些 Alconna 功能："]
    lines.extend(
        f"- {item.header}" + (f"：{item.description}" if item.description else "") for item in shown
    )
    if len(matches if matches else capabilities) > limit:
        lines.append("- ……")
    lines.append("告诉我具体功能名，我再给你用法。")
    return "\n".join(lines)


def matching_public_capabilities(
    query: str,
    capabilities: tuple[PublicCapability, ...],
) -> tuple[PublicCapability, ...]:
    """返回查询明确命中的显式公开能力，供高置信来源优先回答。"""
    return _matching_capabilities(query, capabilities)


def build_explicit_public_guidance_request(
    query: str,
    capabilities: tuple[PublicCapability, ...],
    *,
    conversation_context: str | None = None,
) -> PublicGuidanceRequest | None:
    facts: list[PublicGuidanceFact] = []
    for capability in capabilities[:5]:
        for field, value in (
            (PublicGuidanceFactField.HEADER, capability.header),
            (PublicGuidanceFactField.DESCRIPTION, capability.description),
            (PublicGuidanceFactField.USAGE, capability.usage),
            (PublicGuidanceFactField.EXAMPLE, capability.example),
        ):
            if not value:
                continue
            facts.append(
                PublicGuidanceFact(
                    fact_id=f"f{len(facts) + 1}",
                    capability=capability.header,
                    field=field,
                    text=value,
                    basis=(
                        PublicGuidanceFactBasis.OBSERVED
                        if field is PublicGuidanceFactField.HEADER
                        else PublicGuidanceFactBasis.DECLARED
                    ),
                )
            )
    normalized = " ".join(query.split())
    if not normalized or not facts:
        return None
    return PublicGuidanceRequest(
        schema_version=PUBLIC_GUIDANCE_SCHEMA_VERSION,
        question=normalized,
        conversation_context=conversation_context,
        facts=tuple(facts),
    )


def _matching_capabilities(
    query: str,
    capabilities: tuple[PublicCapability, ...],
) -> tuple[PublicCapability, ...]:
    searchable = _searchable_text(query)
    if not searchable:
        return ()
    scored: list[tuple[int, PublicCapability]] = []
    query_bigrams = _bigrams(searchable)
    for capability in capabilities:
        header = _searchable_text(capability.header)
        description = _searchable_text(capability.description or "")
        score = 0
        if header and header in searchable:
            score += 100 + len(header)
        if searchable and searchable in description:
            score += 50 + len(searchable)
        score += len(query_bigrams.intersection(_bigrams(f"{header}{description}")))
        if score >= 2:
            scored.append((score, capability))
    scored.sort(key=lambda item: (-item[0], item[1].header.casefold()))
    return tuple(item[1] for item in scored)


def _searchable_text(value: str) -> str:
    return _NON_SEARCH_TEXT.sub("", value).casefold()


def _bigrams(value: str) -> set[str]:
    return {value[index : index + 2] for index in range(max(0, len(value) - 1))}


__all__ = (
    "build_explicit_public_guidance_request",
    "format_capability_guidance",
    "matching_public_capabilities",
)
