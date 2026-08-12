from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from nbtriage.capability_source_evidence import (
    CapabilitySourceEvidencePack,
    HandlerFact,
    RegistrationAnchor,
    SourceSpan,
)


class CapabilityRoleAnalysisError(ValueError):
    pass


class CapabilityRole(StrEnum):
    USER_CAPABILITY = "user_capability"
    SUPPORTING = "supporting"
    UNRESOLVED = "unresolved"


class SourceEffectKind(StrEnum):
    """源码中已被其他提取步骤确认的、与能力角色相关的效果。"""

    USER_OUTPUT = "user_output"
    USER_OBSERVABLE_ACTION = "user_observable_action"
    STATE_READ = "state_read"
    STATE_WRITE = "state_write"


class RoleAnalysisIssue(StrEnum):
    NO_ATTACHED_HANDLER = "no_attached_handler"
    HANDLER_FACT_MISSING = "handler_fact_missing"
    EFFECT_UNOBSERVED = "effect_unobserved"
    PARTIAL_EVIDENCE = "partial_evidence"
    SUPPORT_RELATIONSHIP_UNOBSERVED = "support_relationship_unobserved"


@dataclass(frozen=True)
class SourceEffectFact:
    """一个 handler 或其已知直接 helper 中确认存在的源码效果。"""

    owner_name: str
    kind: SourceEffectKind
    symbol: str
    source: SourceSpan

    def __post_init__(self) -> None:
        if (
            not isinstance(self.owner_name, str)
            or not self.owner_name.strip()
            or len(self.owner_name) > 512
        ):
            raise CapabilityRoleAnalysisError(
                "effect owner_name must be a non-empty string of at most 512 characters"
            )
        if not isinstance(self.kind, SourceEffectKind):
            raise CapabilityRoleAnalysisError("effect kind must be SourceEffectKind")
        if not isinstance(self.symbol, str) or not self.symbol.strip() or len(self.symbol) > 512:
            raise CapabilityRoleAnalysisError(
                "effect symbol must be a non-empty string of at most 512 characters"
            )
        if not isinstance(self.source, SourceSpan):
            raise CapabilityRoleAnalysisError("effect source must be SourceSpan")


@dataclass(frozen=True)
class MatcherRoleRelationship:
    target_matcher_key: str
    shared_symbols: tuple[str, ...]
    source_effects: tuple[SourceEffectFact, ...]
    target_effects: tuple[SourceEffectFact, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.target_matcher_key, str)
            or not self.target_matcher_key.strip()
            or len(self.target_matcher_key) > 1_024
        ):
            raise CapabilityRoleAnalysisError(
                "target_matcher_key must be a non-empty string of at most 1024 characters"
            )
        if not self.shared_symbols or any(
            not isinstance(item, str) or not item.strip() or len(item) > 512
            for item in self.shared_symbols
        ):
            raise CapabilityRoleAnalysisError("shared_symbols must contain bounded strings")
        if tuple(sorted(set(self.shared_symbols))) != self.shared_symbols:
            raise CapabilityRoleAnalysisError("shared_symbols must be sorted and unique")
        if not self.source_effects or not self.target_effects:
            raise CapabilityRoleAnalysisError(
                "role relationships must include source and target effect evidence"
            )
        if any(not isinstance(item, SourceEffectFact) for item in self.source_effects):
            raise CapabilityRoleAnalysisError("source_effects must contain SourceEffectFact values")
        if any(not isinstance(item, SourceEffectFact) for item in self.target_effects):
            raise CapabilityRoleAnalysisError("target_effects must contain SourceEffectFact values")
        shared = set(self.shared_symbols)
        if not shared.issubset(
            {item.symbol for item in self.source_effects}
        ) or not shared.issubset({item.symbol for item in self.target_effects}):
            raise CapabilityRoleAnalysisError(
                "relationship evidence must cover every shared symbol"
            )


@dataclass(frozen=True)
class MatcherRoleAnalysis:
    matcher_key: str
    registration: RegistrationAnchor
    handlers: tuple[HandlerFact, ...]
    effects: tuple[SourceEffectFact, ...]
    role: CapabilityRole
    relationships: tuple[MatcherRoleRelationship, ...] = ()
    issues: tuple[RoleAnalysisIssue, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.matcher_key, str)
            or not self.matcher_key.strip()
            or len(self.matcher_key) > 1_024
        ):
            raise CapabilityRoleAnalysisError(
                "matcher_key must be a non-empty string of at most 1024 characters"
            )
        if not isinstance(self.registration, RegistrationAnchor):
            raise CapabilityRoleAnalysisError("registration must be RegistrationAnchor")
        if any(not isinstance(item, HandlerFact) for item in self.handlers):
            raise CapabilityRoleAnalysisError("handlers must contain HandlerFact values")
        if any(not isinstance(item, SourceEffectFact) for item in self.effects):
            raise CapabilityRoleAnalysisError("effects must contain SourceEffectFact values")
        if not isinstance(self.role, CapabilityRole):
            raise CapabilityRoleAnalysisError("role must be CapabilityRole")
        if any(not isinstance(item, MatcherRoleRelationship) for item in self.relationships):
            raise CapabilityRoleAnalysisError(
                "relationships must contain MatcherRoleRelationship values"
            )
        if any(not isinstance(item, RoleAnalysisIssue) for item in self.issues):
            raise CapabilityRoleAnalysisError("issues must contain RoleAnalysisIssue values")
        if self.role is CapabilityRole.UNRESOLVED and not self.issues:
            raise CapabilityRoleAnalysisError("unresolved analyses must explain their issues")
        if self.role is not CapabilityRole.UNRESOLVED and self.issues:
            raise CapabilityRoleAnalysisError("resolved analyses cannot contain issues")
        if self.role is CapabilityRole.SUPPORTING and not self.relationships:
            raise CapabilityRoleAnalysisError("supporting analyses must contain relationships")
        if self.role is not CapabilityRole.SUPPORTING and self.relationships:
            raise CapabilityRoleAnalysisError("only supporting analyses can contain relationships")


_USER_EFFECTS = frozenset({SourceEffectKind.USER_OUTPUT, SourceEffectKind.USER_OBSERVABLE_ACTION})
_SUPPORTING_EFFECTS = frozenset({SourceEffectKind.STATE_READ, SourceEffectKind.STATE_WRITE})


def analyze_matcher_roles(
    evidence: CapabilitySourceEvidencePack,
    effects: Iterable[SourceEffectFact] = (),
) -> tuple[MatcherRoleAnalysis, ...]:
    """把注册事实与已确认的源码效果组合为 Matcher 角色判断。

    入口工厂和字面量参数本身不会升级为用户能力。明确的用户输出或用户可观察
    动作可以证明用户能力；只有每个关联 handler 都有事实覆盖，且效果均为内部
    状态读写时，才会判为支撑组件。其余情况保持 unresolved。
    """
    if not isinstance(evidence, CapabilitySourceEvidencePack):
        raise TypeError("evidence must be CapabilitySourceEvidencePack")
    effect_facts = tuple(effects)
    if any(not isinstance(item, SourceEffectFact) for item in effect_facts):
        raise TypeError("effects must contain SourceEffectFact values")

    preliminary = tuple(
        _registration_facts(
            registration,
            evidence.handlers,
            effect_facts,
            evidence_is_partial=evidence.is_partial,
        )
        for registration in evidence.registrations
    )
    user_capabilities = tuple(
        facts for facts in preliminary if any(item.kind in _USER_EFFECTS for item in facts.effects)
    )
    return tuple(_classify_registration(facts, user_capabilities) for facts in preliminary)


def analyze_runtime_matcher_roles(
    matchers: Iterable[tuple[str, tuple[SourceEffectFact, ...], bool]],
) -> tuple[MatcherRoleAnalysis, ...]:
    """按运行时 Matcher 锚点组合效果事实，供跨模块 handler 使用。"""
    facts: list[tuple[str, RegistrationAnchor, tuple[SourceEffectFact, ...], bool]] = []
    seen_keys: set[str] = set()
    for matcher_key, effects, coverage_complete in matchers:
        if not isinstance(matcher_key, str) or not matcher_key:
            raise CapabilityRoleAnalysisError("runtime matcher key must be a non-empty string")
        if matcher_key in seen_keys:
            raise CapabilityRoleAnalysisError("runtime matcher keys must be unique")
        seen_keys.add(matcher_key)
        effect_tuple = tuple(effects)
        if any(not isinstance(item, SourceEffectFact) for item in effect_tuple):
            raise CapabilityRoleAnalysisError("runtime effects must contain SourceEffectFact")
        locator, line = _runtime_key_source(matcher_key)
        registration = RegistrationAnchor(
            matcher_name=matcher_key,
            factory="runtime_matcher",
            entries=(),
            aliases=(),
            handlers=tuple(sorted({item.owner_name for item in effect_tuple})),
            opaque_fields=(),
            source=SourceSpan(locator=locator, line=line, end_line=line, digest="0" * 64),
        )
        facts.append((matcher_key, registration, effect_tuple, coverage_complete))
    users = tuple(item for item in facts if any(effect.kind in _USER_EFFECTS for effect in item[2]))
    results: list[MatcherRoleAnalysis] = []
    for key, registration, effects, coverage_complete in facts:
        if any(effect.kind in _USER_EFFECTS for effect in effects):
            results.append(
                MatcherRoleAnalysis(
                    matcher_key=key,
                    registration=registration,
                    handlers=(),
                    effects=effects,
                    role=CapabilityRole.USER_CAPABILITY,
                )
            )
            continue
        relationships: list[MatcherRoleRelationship] = []
        if (
            coverage_complete
            and effects
            and all(effect.kind in _SUPPORTING_EFFECTS for effect in effects)
        ):
            own_symbols = {effect.symbol for effect in effects}
            for target_key, _, target_effects, _ in users:
                target_state_effects = tuple(
                    effect for effect in target_effects if effect.kind in _SUPPORTING_EFFECTS
                )
                shared = tuple(
                    sorted(
                        own_symbols.intersection(effect.symbol for effect in target_state_effects)
                    )
                )
                if shared:
                    relationships.append(
                        MatcherRoleRelationship(
                            target_matcher_key=target_key,
                            shared_symbols=shared,
                            source_effects=tuple(
                                effect for effect in effects if effect.symbol in shared
                            ),
                            target_effects=tuple(
                                effect for effect in target_state_effects if effect.symbol in shared
                            ),
                        )
                    )
        if relationships:
            results.append(
                MatcherRoleAnalysis(
                    matcher_key=key,
                    registration=registration,
                    handlers=(),
                    effects=effects,
                    role=CapabilityRole.SUPPORTING,
                    relationships=tuple(
                        sorted(relationships, key=lambda item: item.target_matcher_key)
                    ),
                )
            )
            continue
        issue = (
            RoleAnalysisIssue.SUPPORT_RELATIONSHIP_UNOBSERVED
            if coverage_complete and effects
            else RoleAnalysisIssue.EFFECT_UNOBSERVED
        )
        results.append(
            MatcherRoleAnalysis(
                matcher_key=key,
                registration=registration,
                handlers=(),
                effects=effects,
                role=CapabilityRole.UNRESOLVED,
                issues=(issue,),
            )
        )
    return tuple(results)


def _runtime_key_source(value: str) -> tuple[str, int]:
    import hashlib

    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"runtime/{digest[:16]}.py", 1


@dataclass(frozen=True)
class _RegistrationFacts:
    registration: RegistrationAnchor
    handlers: tuple[HandlerFact, ...]
    effects: tuple[SourceEffectFact, ...]
    issues: tuple[RoleAnalysisIssue, ...]


def _registration_facts(
    registration: RegistrationAnchor,
    handler_facts: tuple[HandlerFact, ...],
    effect_facts: tuple[SourceEffectFact, ...],
    *,
    evidence_is_partial: bool,
) -> _RegistrationFacts:
    handlers = tuple(item for item in handler_facts if item.name in registration.handlers)
    effects_by_handler = {
        handler: tuple(
            effect for effect in effect_facts if _effect_belongs_to_handler(effect, handler)
        )
        for handler in handlers
    }
    relevant_effects = tuple(
        effect
        for effect in effect_facts
        if any(_effect_belongs_to_handler(effect, handler) for handler in handlers)
    )

    issues: list[RoleAnalysisIssue] = []
    if not registration.handlers:
        issues.append(RoleAnalysisIssue.NO_ATTACHED_HANDLER)
    elif {item.name for item in handlers} != set(registration.handlers):
        issues.append(RoleAnalysisIssue.HANDLER_FACT_MISSING)
    if handlers and any(not effects_by_handler[handler] for handler in handlers):
        issues.append(RoleAnalysisIssue.EFFECT_UNOBSERVED)
    if evidence_is_partial:
        issues.append(RoleAnalysisIssue.PARTIAL_EVIDENCE)

    if not issues and not relevant_effects:
        issues.append(RoleAnalysisIssue.EFFECT_UNOBSERVED)
    return _RegistrationFacts(
        registration=registration,
        handlers=handlers,
        effects=relevant_effects,
        issues=tuple(issues),
    )


def _classify_registration(
    facts: _RegistrationFacts,
    user_capabilities: tuple[_RegistrationFacts, ...],
) -> MatcherRoleAnalysis:
    key = _matcher_key(facts.registration)
    if any(effect.kind in _USER_EFFECTS for effect in facts.effects):
        return MatcherRoleAnalysis(
            matcher_key=key,
            registration=facts.registration,
            handlers=facts.handlers,
            effects=facts.effects,
            role=CapabilityRole.USER_CAPABILITY,
        )

    relationships = _supporting_relationships(facts, user_capabilities)
    if not facts.issues and relationships:
        return MatcherRoleAnalysis(
            matcher_key=key,
            registration=facts.registration,
            handlers=facts.handlers,
            effects=facts.effects,
            role=CapabilityRole.SUPPORTING,
            relationships=relationships,
        )

    issues = facts.issues
    if not issues:
        issues = (RoleAnalysisIssue.SUPPORT_RELATIONSHIP_UNOBSERVED,)
    return MatcherRoleAnalysis(
        matcher_key=key,
        registration=facts.registration,
        handlers=facts.handlers,
        effects=facts.effects,
        role=CapabilityRole.UNRESOLVED,
        issues=issues,
    )


def _supporting_relationships(
    facts: _RegistrationFacts,
    user_capabilities: tuple[_RegistrationFacts, ...],
) -> tuple[MatcherRoleRelationship, ...]:
    if not facts.effects or any(effect.kind not in _SUPPORTING_EFFECTS for effect in facts.effects):
        return ()
    own_symbols = {effect.symbol for effect in facts.effects}
    relationships: list[MatcherRoleRelationship] = []
    for target in user_capabilities:
        if target is facts:
            continue
        target_symbols = {
            effect.symbol for effect in target.effects if effect.kind in _SUPPORTING_EFFECTS
        }
        shared = tuple(sorted(own_symbols.intersection(target_symbols)))
        if shared:
            source_effects = tuple(item for item in facts.effects if item.symbol in shared)
            target_effects = tuple(
                item
                for item in target.effects
                if item.kind in _SUPPORTING_EFFECTS and item.symbol in shared
            )
            relationships.append(
                MatcherRoleRelationship(
                    target_matcher_key=_matcher_key(target.registration),
                    shared_symbols=shared,
                    source_effects=source_effects,
                    target_effects=target_effects,
                )
            )
    return tuple(sorted(relationships, key=lambda item: item.target_matcher_key))


def _effect_belongs_to_handler(effect: SourceEffectFact, handler: HandlerFact) -> bool:
    return effect.source.locator == handler.source.locator and effect.owner_name in {
        handler.name,
        *handler.direct_helpers,
    }


def _matcher_key(registration: RegistrationAnchor) -> str:
    name = registration.matcher_name or "<anonymous>"
    return f"{registration.source.locator}:{registration.source.line}:{registration.factory}:{name}"


__all__ = (
    "CapabilityRole",
    "CapabilityRoleAnalysisError",
    "MatcherRoleAnalysis",
    "MatcherRoleRelationship",
    "RoleAnalysisIssue",
    "SourceEffectFact",
    "SourceEffectKind",
    "analyze_matcher_roles",
    "analyze_runtime_matcher_roles",
)
