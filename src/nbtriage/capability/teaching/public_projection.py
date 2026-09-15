from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace

from nbtriage.capability.catalog.records import CapabilityRecord, Disclosure
from nbtriage.capability.teaching.analysis import SemanticConstraintKind, TeachingRole
from nbtriage.capability.teaching.annotations import (
    CapabilityTeachingAnnotation,
    CapabilityTeachingEntry,
    CapabilityTeachingRequirement,
)

_PRIVATE_ROLE = re.compile(r"super[\s_-]*users?|超级(?:用户|管理员)|超管", re.IGNORECASE)
_PUBLIC_ROLES = {
    TeachingRole.ADMIN: "群管理员可以使用",
    TeachingRole.OWNER: "群主可以使用",
    TeachingRole.CHANNEL_ADMIN: "频道管理员可以使用",
}


def contains_private_role(text: str) -> bool:
    return _PRIVATE_ROLE.search(text) is not None


def _without_private_exemption(text: str) -> str | None:
    clauses = re.split(r"[；。]", text)
    hidden = [clause for clause in clauses if contains_private_role(clause)]
    if not hidden or any(
        not re.fullmatch(
            r"(?:机器人)?(?:超级用户|超级管理员|超管|superuser)不受[^；。]*限制",
            clause.strip(),
            re.I,
        )
        for clause in hidden
    ):
        return None
    public = "；".join(clause for clause in clauses if clause and clause not in hidden)
    return public or None


def _requirements(
    entry: CapabilityTeachingEntry,
) -> tuple[CapabilityTeachingRequirement, ...] | None:
    result = []
    for requirement in entry.requirements:
        if requirement.role is TeachingRole.SUPERUSER:
            return None
        if any(item.role is TeachingRole.SUPERUSER for item in requirement.alternatives):
            alternatives = tuple(
                replace(item, text=_PUBLIC_ROLES[item.role])
                if item.role is not None and item.role in _PUBLIC_ROLES
                else item
                for item in requirement.alternatives
                if item.role is not TeachingRole.SUPERUSER
            )
            if not alternatives or any(_PRIVATE_ROLE.search(item.text) for item in alternatives):
                return None
            prefix = []
            for clause in re.split(r"[；。]", requirement.text):
                if contains_private_role(clause):
                    break
                if clause:
                    prefix.append(clause)
            requirement = replace(
                requirement,
                text=("；".join(prefix) + "；" if prefix else "")
                + "公开使用条件（满足其一）："
                + "；或".join(item.text for item in alternatives),
                alternatives=alternatives,
            )
        elif _PRIVATE_ROLE.search(requirement.text):
            if requirement.kind is SemanticConstraintKind.RATE_LIMIT and (
                public := _without_private_exemption(requirement.text)
            ):
                result.append(replace(requirement, text=public))
                continue
            # 未结构化的条件不能通过删词变成更宽的权限。
            return None
        result.append(requirement)
    return tuple(result)


def _record_references(record: CapabilityRecord, shared_headers: set[str]) -> set[str]:
    headers = {
        claim.value
        for claim in record.claims
        if claim.field == "command.header" and isinstance(claim.value, str)
    }
    references = headers - shared_headers
    for header in headers & shared_headers:
        for claim in record.claims:
            if claim.field != "command.components" or not isinstance(claim.value, list):
                continue
            for component in claim.value:
                if not isinstance(component, dict) or component.get("kind") != "subcommand":
                    continue
                aliases = component.get("aliases", [])
                if not isinstance(aliases, list):
                    aliases = []
                for name in (component.get("name"), *aliases):
                    if isinstance(name, str):
                        references.add(f"{header} {name}")
    return references


def _entry_references(entry: CapabilityTeachingEntry) -> set[str]:
    return {
        entry.name,
        *(usage.split("<", 1)[0].strip() for usage in entry.usages),
    }


def _mentions_hidden(value: object, references: set[str]) -> bool:
    if isinstance(value, str):
        if _PRIVATE_ROLE.search(value):
            return True
        normalized = " ".join(value.casefold().split())
        for reference in references:
            marker = " ".join(reference.casefold().split())
            if not marker:
                continue
            # ASCII 指令需完整词匹配，避免隐藏 foo 时误伤 foobar。
            pattern = re.escape(marker)
            if marker[0].isascii() and marker[0].isalnum():
                pattern = r"(?<![\w])" + pattern
            if marker[-1].isascii() and marker[-1].isalnum():
                pattern += r"(?![\w])"
            if re.search(pattern, normalized):
                return True
        return False
    if isinstance(value, Mapping):
        return any(_mentions_hidden(item, references) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_mentions_hidden(item, references) for item in value)
    return False


def _project_entry(
    entry: CapabilityTeachingEntry, references: set[str]
) -> CapabilityTeachingEntry | None:
    requirements = _requirements(entry)
    if requirements is None or _mentions_hidden(entry.name, references):
        return None
    # 自由文本中的身份条件没有结构化依据时，不猜测其是否为必要条件。
    if not entry.requirements and _PRIVATE_ROLE.search(
        "\n".join((entry.summary, *entry.behavior_boundaries))
    ):
        return None
    usages = tuple(text for text in entry.usages if not _mentions_hidden(text, references))
    if not usages:
        return None
    if any(_mentions_hidden(item.text, references) for item in requirements):
        return None
    return replace(
        entry,
        summary=entry.name if _mentions_hidden(entry.summary, references) else entry.summary,
        usages=usages,
        search_terms=tuple(
            text for text in entry.search_terms if not _mentions_hidden(text, references)
        ),
        behavior_boundaries=tuple(
            text for text in entry.behavior_boundaries if not _mentions_hidden(text, references)
        ),
        requirements=requirements,
    )


def project_public_capabilities(
    records: tuple[CapabilityRecord, ...],
    annotations: Mapping[str, CapabilityTeachingAnnotation],
) -> tuple[tuple[CapabilityRecord, ...], dict[str, CapabilityTeachingAnnotation]]:
    """为公开目录、回答和帮助投影同一份资料，保留内部原件。

    Args:
        records: 同一资料集合的记录，受限记录仅用于阻止帮助引用泄露。
        annotations: 按实际记录 ID 绑定的原注释，可来自旧缓存。

    Returns:
        可公开的记录和注释副本。被过滤为空的功能不回退到原始描述。
    """
    hidden: dict[str, set[str]] = {}
    blocked: set[str] = set()
    public_headers: dict[str, set[str]] = {}
    for record in records:
        if record.disclosure is Disclosure.PUBLIC:
            public_headers.setdefault(record.owner, set()).update(
                claim.value
                for claim in record.claims
                if claim.field == "command.header" and isinstance(claim.value, str)
            )
    for record in records:
        references = hidden.setdefault(record.owner, set())
        restricted = record.disclosure is not Disclosure.PUBLIC or any(
            item.kind == "permission" and item.operation == "superuser"
            for item in record.constraints
        )
        annotation = annotations.get(record.capability_id)
        if restricted:
            blocked.add(record.capability_id)
            references.update(_record_references(record, public_headers.get(record.owner, set())))
        if annotation:
            for entry in annotation.entries:
                if restricted or _project_entry(entry, set()) is None:
                    references.update(_entry_references(entry))

    public_records = []
    public_annotations = {}
    for record in records:
        if record.capability_id in blocked:
            continue
        references = hidden[record.owner]
        annotation = annotations.get(record.capability_id)
        if annotation and annotation.knowledge_enabled:
            entries = tuple(
                projected
                for entry in annotation.entries
                if (projected := _project_entry(entry, references)) is not None
            )
            if not entries:
                continue
            public_annotations[record.capability_id] = replace(annotation, entries=entries)
        elif annotation:
            public_annotations[record.capability_id] = annotation
        if any(
            claim.field in {"command.header", "trigger.entries"}
            and _mentions_hidden(claim.value, references)
            for claim in record.claims
        ):
            public_annotations.pop(record.capability_id, None)
            continue
        public_records.append(
            replace(
                record,
                claims=tuple(
                    claim
                    for claim in record.claims
                    if not _mentions_hidden(claim.value, references)
                ),
            )
        )
    return tuple(public_records), public_annotations
