from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from types import ModuleType
from typing import Protocol

from nonebot import require

from nbtriage.bug_agent import BUG_AGENT_PROMPT_ID
from nbtriage.bug_assessment import (
    BUG_EVIDENCE_BODY_MAX_CHARS,
    BugAssessmentAgentClient,
    BugAssessmentCase,
    BugAssessmentCoordinator,
    BugAssessmentDecision,
    BugAssessmentToolbox,
    BugEvidence,
    BugEvidenceKind,
    BugReason,
    ReviewedBugProblemRepository,
    build_bug_case_fingerprint,
    unknown_bug_decision,
)
from nbtriage.bug_conversation import (
    BoundBugConversationReader,
    BugConversationMessage,
    BugConversationPage,
)
from nbtriage.bug_design import BugDesignIndexReader
from nbtriage.bug_logs import (
    CorrelatedBugLogBuffer,
    bug_log_bundle_evidence,
    redact_bug_evidence_text,
)
from nbtriage.bug_source import ApprovedSourceRoot
from nbtriage.capabilities import CapabilityRecord, CapabilitySearchHit
from nbtriage.opencode_go_semantic_adapter import (
    OPENCODE_GO_BUG_ASSESSMENT_BUDGET_PROFILE,
    OPENCODE_GO_BUG_ASSESSMENT_EVALUATION,
    OPENCODE_GO_BUG_ASSESSMENT_MAX_OUTPUT_TOKENS,
    OPENCODE_GO_BUG_ASSESSMENT_PRIVACY_POLICY,
    OPENCODE_GO_BUG_ASSESSMENT_TASK,
    OPENCODE_GO_BUG_ASSESSMENT_TIMEOUT_SECONDS,
    OPENCODE_GO_SEMANTIC_API_FAMILY,
    OPENCODE_GO_SEMANTIC_MODELS,
    create_opencode_go_bug_assessment_agent,
)
from nbtriage.runtime_observations import RuntimeObservationBuffer
from nbtriage.serena_source import BoundedBugSourceBackend, create_bug_source_backend
from nonebot_plugin_triage.bug_problem_store import LocalBugProblemRepository
from nonebot_plugin_triage.capability_shadow import CapabilityShadowService
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.knowledge_pack_runtime import KnowledgePackService

_CONVERSATION_MESSAGE_CONTENT_MAX_CHARS = 2_000
_CONVERSATION_SENDER_NAME_MAX_CHARS = 128
_CONVERSATION_IDENTIFIER_MAX_CHARS = 256
_CONVERSATION_PAGE_MAX_MESSAGES = 30
_CONVERSATION_ROLE_MAX_ITEMS = 8
_CONVERSATION_ROLE_MAX_CHARS = 64
_CONVERSATION_SEGMENT_TYPE_MAX_ITEMS = 16
_CONVERSATION_SEGMENT_TYPE_MAX_CHARS = 32
_DESIGN_EVIDENCE_LIMIT = 5


@dataclass(frozen=True, slots=True)
class BugTaskQualification:
    provider: str
    api_family: str
    model: str
    task: str
    schema_version: int
    prompt_id: str
    privacy_policy: str
    budget_profile: str
    evaluation: str


OPENCODE_GO_BUG_TASK_QUALIFICATION = BugTaskQualification(
    provider="opencode-go",
    api_family=OPENCODE_GO_SEMANTIC_API_FAMILY,
    model="deepseek-v4-flash",
    task=OPENCODE_GO_BUG_ASSESSMENT_TASK,
    schema_version=1,
    prompt_id=BUG_AGENT_PROMPT_ID,
    privacy_policy=OPENCODE_GO_BUG_ASSESSMENT_PRIVACY_POLICY,
    budget_profile=OPENCODE_GO_BUG_ASSESSMENT_BUDGET_PROFILE,
    evaluation=OPENCODE_GO_BUG_ASSESSMENT_EVALUATION,
)
# 中文 Prompt v8 已通过独立 forward-heldout，只准入这一精确组合。
QUALIFIED_BUG_TASKS: frozenset[BugTaskQualification] = frozenset(
    {OPENCODE_GO_BUG_TASK_QUALIFICATION}
)


@dataclass(frozen=True, slots=True)
class BugAssessmentRuntimeRequest:
    request_text: str
    adapter_name: str
    adapter_type: type[object]
    correlation_id: str | None
    conversation_context: str | None = None
    reply_message: BugConversationMessage | None = None
    conversation_reader: BoundBugConversationReader | None = None


class BugAssessmentServiceLike(Protocol):
    async def assess(self, request: BugAssessmentRuntimeRequest) -> BugAssessmentDecision: ...


class UnavailableBugAssessmentService:
    async def assess(self, request: BugAssessmentRuntimeRequest) -> BugAssessmentDecision:
        del request
        return unknown_bug_decision(BugReason.ANALYSIS_UNAVAILABLE)


class _PublicContractPrechecker:
    async def check(
        self,
        case: BugAssessmentCase,
        toolbox: BugAssessmentToolbox,
    ) -> BugAssessmentDecision | None:
        del case
        await toolbox.preload_reply_context()
        await toolbox.preload_public_contract()
        return None


class BugAssessmentRuntimeService:
    def __init__(
        self,
        *,
        repository: ReviewedBugProblemRepository,
        capability_shadow: CapabilityShadowService | None,
        knowledge_pack: KnowledgePackService | None,
        runtime_buffer: RuntimeObservationBuffer,
        log_buffer: CorrelatedBugLogBuffer,
        agent_client_factory: Callable[[], BugAssessmentAgentClient] | None,
        serena_home: Path | None,
        design_component_versions: Mapping[str, str],
    ) -> None:
        self._repository = repository
        self._capability_shadow = capability_shadow
        self._knowledge_pack = knowledge_pack
        self._runtime_buffer = runtime_buffer
        self._log_buffer = log_buffer
        self._agent_client_factory = agent_client_factory
        self._serena_home = serena_home
        self._design_component_versions = dict(design_component_versions)

    async def assess(self, request: BugAssessmentRuntimeRequest) -> BugAssessmentDecision:
        subject_query = _subject_query(request)
        subject_hit = await self._select_subject(subject_query)
        subject = subject_hit.record if subject_hit is not None else None
        source_revision = _record_source_revision(subject)
        contract_revision = _record_revision(subject)
        deployment_generation = (
            self._capability_shadow.status.deployment_generation
            if self._capability_shadow is not None
            else None
        )
        request_digest = hashlib.sha256(
            json.dumps(
                {
                    "request_text": request.request_text,
                    "conversation_context": request.conversation_context,
                    "reply_content": (
                        request.reply_message.content if request.reply_message is not None else None
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        fingerprint = build_bug_case_fingerprint(
            request.request_text,
            subject_id=subject.capability_id if subject is not None else None,
            failure_signature=request_digest,
            adapter=request.adapter_name,
            source_revision=source_revision,
            contract_revision=contract_revision,
            deployment_generation=deployment_generation,
        )
        case = BugAssessmentCase(request_text=request.request_text, fingerprint=fingerprint)
        source_backend = _source_backend(
            subject,
            serena_home=self._serena_home,
            source_revision=source_revision,
        )

        async def runtime_loader() -> tuple[BugEvidence, ...]:
            if request.correlation_id is None:
                return ()
            bundle = self._runtime_buffer.capture(request.correlation_id)
            body = json.dumps(bundle.to_dict(), ensure_ascii=False, separators=(",", ":"))
            evidence_id = "runtime:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
            return (
                BugEvidence(
                    evidence_id=evidence_id,
                    kind=BugEvidenceKind.RUNTIME_OBSERVATION,
                    source="runtime:correlated",
                    body=body[:48_000],
                    revision=bundle.generated_at,
                    current=True,
                    partial=bundle.buffer_dropped_count > 0 or len(body) > 48_000,
                ),
            )

        async def log_loader() -> tuple[BugEvidence, ...]:
            if request.correlation_id is None:
                return ()
            return bug_log_bundle_evidence(self._log_buffer.capture(request.correlation_id))

        async def reply_context_loader() -> tuple[BugEvidence, ...]:
            evidence: list[BugEvidence] = []
            if request.conversation_context:
                evidence.append(_conversation_text_evidence(request.conversation_context))
            if request.reply_message is not None:
                evidence.append(_conversation_message_evidence(request.reply_message))
            return tuple(evidence)

        async def conversation_loader() -> tuple[BugEvidence, ...]:
            if request.conversation_reader is None:
                return ()
            page = await request.conversation_reader.read_next()
            return (_conversation_page_evidence(page),)

        async def source_loader(query: str) -> tuple[BugEvidence, ...]:
            if source_backend is None:
                return ()
            evidence = await source_backend.find_symbol(query)
            return _redacted_evidence(evidence)

        async def source_read_loader(relative_path: str) -> tuple[BugEvidence, ...]:
            if source_backend is None:
                return ()
            evidence = await source_backend.read(relative_path)
            return _redacted_evidence(evidence)

        async def design_loader(query: str) -> tuple[BugEvidence, ...]:
            if self._knowledge_pack is None or not self._knowledge_pack.status.ready:
                return ()
            path = self._knowledge_pack.status.index_path
            if path is None:
                return ()
            reader = BugDesignIndexReader(path)
            evidence = await asyncio.to_thread(
                _search_design_knowledge,
                reader,
                query,
                self._design_component_versions,
            )
            return _redacted_evidence(evidence)

        async def deployment_loader() -> tuple[BugEvidence, ...]:
            if self._capability_shadow is None:
                return ()
            status = self._capability_shadow.status
            body = json.dumps(
                {
                    "adapter": request.adapter_name,
                    "subject_id": subject.capability_id if subject is not None else None,
                    "source_revision": source_revision,
                    "contract_revision": contract_revision,
                    "deployment_generation": status.deployment_generation,
                    "declared_plugin_count": status.declared_plugin_count,
                    "registered_plugin_count": status.registered_plugin_count,
                    "not_observed_plugin_count": status.not_observed_plugin_count,
                    "runtime_only_plugin_count": status.runtime_only_plugin_count,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return (
                BugEvidence(
                    evidence_id=(
                        "deployment:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
                    ),
                    kind=BugEvidenceKind.DEPLOYMENT_CONTEXT,
                    source="deployment:capability-shadow",
                    body=body,
                    revision=status.deployment_generation,
                    current=not status.stale,
                    partial=status.deployment_partial is not False,
                ),
            )

        async def public_contract_loader() -> tuple[BugEvidence, ...]:
            if self._capability_shadow is None:
                return ()
            result = await self._capability_shadow.search_public(
                subject_query,
                request.adapter_type,
                limit=5,
            )
            if result is None:
                return ()
            return tuple(_public_record_evidence(hit.record) for hit in result.hits)

        toolbox = BugAssessmentToolbox(
            runtime_loader=runtime_loader,
            log_loader=log_loader,
            source_loader=source_loader,
            source_read_loader=source_read_loader,
            design_loader=design_loader,
            deployment_loader=deployment_loader,
            public_contract_loader=public_contract_loader,
            reply_context_loader=reply_context_loader,
            conversation_loader=(
                conversation_loader if request.conversation_reader is not None else None
            ),
        )
        coordinator = BugAssessmentCoordinator(
            self._repository,
            _PublicContractPrechecker(),
            self._agent_client_factory,
        )
        try:
            return await coordinator.assess(case, toolbox)
        finally:
            if source_backend is not None:
                await source_backend.aclose()

    async def _select_subject(self, query: str) -> CapabilitySearchHit | None:
        if self._capability_shadow is None:
            return None
        result = await self._capability_shadow.search_for_bug_assessment(query, limit=3)
        if result is None or result.stale or result.partial is not False or not result.hits:
            return None
        first = result.hits[0]
        if len(result.hits) == 1:
            return first
        second = result.hits[1]
        if first.score >= 50 or first.score >= second.score * 1.5:
            return first
        return None


def create_bug_assessment_agent_factory(
    config: NBTriageConfig,
    *,
    environ: Mapping[str, str] | None = None,
    qualified_tasks: frozenset[BugTaskQualification] = QUALIFIED_BUG_TASKS,
) -> Callable[[], BugAssessmentAgentClient] | None:
    if config.nbtriage_model_backend != "opencode-go-chat":
        return None
    model = config.nbtriage_model_name
    if model is None or model not in OPENCODE_GO_SEMANTIC_MODELS:
        return None
    qualification = BugTaskQualification(
        provider="opencode-go",
        api_family=OPENCODE_GO_SEMANTIC_API_FAMILY,
        model=model,
        task=OPENCODE_GO_BUG_ASSESSMENT_TASK,
        schema_version=1,
        prompt_id=BUG_AGENT_PROMPT_ID,
        privacy_policy=OPENCODE_GO_BUG_ASSESSMENT_PRIVACY_POLICY,
        budget_profile=OPENCODE_GO_BUG_ASSESSMENT_BUDGET_PROFILE,
        evaluation=OPENCODE_GO_BUG_ASSESSMENT_EVALUATION,
    )
    if qualification not in qualified_tasks:
        return None
    environment = os.environ if environ is None else environ
    api_key = environment.get("OPENCODE_API_KEY", "")
    if not api_key.strip():
        return None

    def create_client() -> BugAssessmentAgentClient:
        return create_opencode_go_bug_assessment_agent(
            api_key=api_key,
            model=model,
            timeout_seconds=OPENCODE_GO_BUG_ASSESSMENT_TIMEOUT_SECONDS,
            max_output_tokens=OPENCODE_GO_BUG_ASSESSMENT_MAX_OUTPUT_TOKENS,
        )

    return create_client


def create_bug_assessment_runtime_service(
    config: NBTriageConfig,
    *,
    repository: LocalBugProblemRepository,
    capability_shadow: CapabilityShadowService | None,
    knowledge_pack: KnowledgePackService | None,
    runtime_buffer: RuntimeObservationBuffer,
    log_buffer: CorrelatedBugLogBuffer,
) -> BugAssessmentRuntimeService:
    return BugAssessmentRuntimeService(
        repository=repository,
        capability_shadow=capability_shadow,
        knowledge_pack=knowledge_pack,
        runtime_buffer=runtime_buffer,
        log_buffer=log_buffer,
        agent_client_factory=create_bug_assessment_agent_factory(config),
        serena_home=(
            _resolve_serena_home() if config.nbtriage_bug_source_backend == "serena" else None
        ),
        design_component_versions=_installed_design_component_versions(),
    )


def _installed_design_component_versions() -> dict[str, str]:
    try:
        nonebot_version = distribution_version("nonebot2")
    except PackageNotFoundError:
        return {}
    return {"nonebot2": nonebot_version}


def _search_design_knowledge(
    reader: BugDesignIndexReader,
    query: str,
    component_versions: Mapping[str, str],
) -> tuple[BugEvidence, ...]:
    selected: list[BugEvidence] = []
    seen: set[str] = set()

    def add(evidence: tuple[BugEvidence, ...]) -> None:
        for item in evidence:
            if item.evidence_id in seen:
                continue
            seen.add(item.evidence_id)
            selected.append(item)

    for component, version in sorted(component_versions.items()):
        add(
            reader.search(
                query,
                component=component,
                version=version,
                limit=_DESIGN_EVIDENCE_LIMIT,
            )
        )
        if len(selected) >= _DESIGN_EVIDENCE_LIMIT:
            return tuple(selected[:_DESIGN_EVIDENCE_LIMIT])

    add(reader.search(query, limit=_DESIGN_EVIDENCE_LIMIT - len(selected)))
    return tuple(selected[:_DESIGN_EVIDENCE_LIMIT])


def _resolve_serena_home() -> Path:
    require("nonebot_plugin_localstore")
    from nonebot_plugin_localstore import get_cache_dir

    return get_cache_dir("nonebot_plugin_triage") / "serena"


def _source_backend(
    record: CapabilityRecord | None,
    *,
    serena_home: Path | None,
    source_revision: str | None,
) -> BoundedBugSourceBackend | None:
    if record is None:
        return None
    module_name = _record_module_name(record)
    if module_name is None:
        return None
    module = sys.modules.get(module_name)
    if module is None:
        module = sys.modules.get(module_name.partition(".")[0])
    root = _module_source_root(module)
    if root is None:
        return None
    try:
        return create_bug_source_backend(
            ApprovedSourceRoot(module_name, root),
            serena_home=serena_home,
            source_revision=source_revision,
        )
    except (OSError, ValueError):
        return None


def _module_source_root(module: ModuleType | None) -> Path | None:
    if module is None:
        return None
    search_paths = getattr(module, "__path__", None)
    if search_paths is not None:
        for value in search_paths:
            if isinstance(value, str):
                return Path(value)
    source_file = getattr(module, "__file__", None)
    if isinstance(source_file, str):
        return Path(source_file).parent
    return None


def _record_module_name(record: CapabilityRecord) -> str | None:
    return next(
        (
            claim.value
            for claim in record.claims
            if claim.field == "plugin.module_name" and isinstance(claim.value, str)
        ),
        None,
    )


def _record_source_revision(record: CapabilityRecord | None) -> str | None:
    if record is None:
        return None
    return next(
        (
            evidence.content_hash
            for evidence in record.evidence_refs
            if evidence.kind == "plugin_source" and evidence.content_hash is not None
        ),
        next(
            (
                evidence.content_hash
                for evidence in record.evidence_refs
                if evidence.content_hash is not None
            ),
            None,
        ),
    )


def _record_revision(record: CapabilityRecord | None) -> str | None:
    if record is None:
        return None
    payload = json.dumps(
        record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _public_record_evidence(record: CapabilityRecord) -> BugEvidence:
    body = json.dumps(
        {
            "capability_id": record.capability_id,
            "owner": record.owner,
            "kind": record.kind,
            "state": record.state.value,
            "platform_scope": record.platform_scope.to_dict(),
            "claims": [claim.to_dict() for claim in record.claims],
            "constraints": [constraint.to_dict() for constraint in record.constraints],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    revision = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return BugEvidence(
        evidence_id=f"public:{record.capability_id}",
        kind=BugEvidenceKind.PUBLIC_CONTRACT,
        source=f"public-capability:{record.capability_id}",
        body=body[:48_000],
        revision=revision,
        current=True,
        partial=len(body) > 48_000,
    )


def _subject_query(request: BugAssessmentRuntimeRequest) -> str:
    parts = [request.conversation_context or ""]
    if request.reply_message is not None:
        parts.append(request.reply_message.content)
    parts.append(request.request_text)
    return "\n".join(part for part in parts if part)[:16_000]


def _conversation_text_evidence(value: str) -> BugEvidence:
    body = value[:48_000]
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return BugEvidence(
        evidence_id=f"conversation:support-context:{digest[:32]}",
        kind=BugEvidenceKind.CONVERSATION_CONTEXT,
        source="conversation:support-thread",
        body=body,
        revision=digest,
        current=True,
        partial=len(value) > 48_000,
    )


def _conversation_message_evidence(message: BugConversationMessage) -> BugEvidence:
    body = json.dumps(
        message.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return BugEvidence(
        evidence_id=f"conversation:reply:{digest[:32]}",
        kind=BugEvidenceKind.CONVERSATION_CONTEXT,
        source="conversation:explicit-reply",
        body=body[:48_000],
        revision=digest,
        current=True,
        partial=len(body) > 48_000,
    )


def _conversation_page_evidence(page: BugConversationPage) -> BugEvidence:
    payload, projection_partial = _bounded_conversation_page_payload(page)
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(body) > BUG_EVIDENCE_BODY_MAX_CHARS:
        raise ValueError("bounded conversation page exceeds the evidence body limit")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return BugEvidence(
        evidence_id=f"conversation:page:{page.page_number}:{digest[:24]}",
        kind=BugEvidenceKind.CONVERSATION_CONTEXT,
        source="conversation:latest-window",
        body=body,
        revision=digest,
        current=True,
        partial=projection_partial,
    )


def _bounded_conversation_page_payload(
    page: BugConversationPage,
) -> tuple[dict[str, object], bool]:
    messages: list[dict[str, object]] = []
    partial = page.partial or len(page.messages) > _CONVERSATION_PAGE_MAX_MESSAGES
    for message in page.messages[:_CONVERSATION_PAGE_MAX_MESSAGES]:
        payload = message.model_dump(mode="json")
        content = message.content[:_CONVERSATION_MESSAGE_CONTENT_MAX_CHARS]
        if len(content) < len(message.content):
            partial = True
        for field, limit in (
            ("message_id", _CONVERSATION_IDENTIFIER_MAX_CHARS),
            ("reply_to_message_id", _CONVERSATION_IDENTIFIER_MAX_CHARS),
            ("sender_id", _CONVERSATION_IDENTIFIER_MAX_CHARS),
            ("sender_name", _CONVERSATION_SENDER_NAME_MAX_CHARS),
        ):
            value = payload.get(field)
            if isinstance(value, str):
                bounded = value[:limit]
                partial = partial or len(bounded) < len(value)
                payload[field] = bounded
        payload["sender_roles"], roles_partial = _bounded_string_list(
            payload.get("sender_roles"),
            max_items=_CONVERSATION_ROLE_MAX_ITEMS,
            max_chars=_CONVERSATION_ROLE_MAX_CHARS,
        )
        payload["sender_current_roles"], current_roles_partial = _bounded_string_list(
            payload.get("sender_current_roles"),
            max_items=_CONVERSATION_ROLE_MAX_ITEMS,
            max_chars=_CONVERSATION_ROLE_MAX_CHARS,
        )
        payload["segment_types"], segments_partial = _bounded_string_list(
            payload.get("segment_types"),
            max_items=_CONVERSATION_SEGMENT_TYPE_MAX_ITEMS,
            max_chars=_CONVERSATION_SEGMENT_TYPE_MAX_CHARS,
        )
        partial = partial or roles_partial or current_roles_partial or segments_partial
        payload["content"] = content
        messages.append(payload)
    result = page.model_dump(mode="json", exclude={"messages"})
    for field in (
        "adapter",
        "platform",
        "conversation_type",
        "conversation_id",
        "bot_id",
        "request_actor_id",
    ):
        value = result.get(field)
        if isinstance(value, str):
            bounded = value[:_CONVERSATION_IDENTIFIER_MAX_CHARS]
            partial = partial or len(bounded) < len(value)
            result[field] = bounded
    result["request_actor_roles"], roles_partial = _bounded_string_list(
        result.get("request_actor_roles"),
        max_items=_CONVERSATION_ROLE_MAX_ITEMS,
        max_chars=_CONVERSATION_ROLE_MAX_CHARS,
    )
    partial = partial or roles_partial
    result["messages"] = messages
    result["has_more"] = False
    while (
        messages
        and len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        > BUG_EVIDENCE_BODY_MAX_CHARS
    ):
        messages.pop(0)
        partial = True
    if partial:
        result["availability"] = "partial"
    result["partial"] = partial
    return result, partial


def _bounded_string_list(
    value: object,
    *,
    max_items: int,
    max_chars: int,
) -> tuple[list[str], bool]:
    if not isinstance(value, (list, tuple)):
        return [], bool(value)
    items = [item for item in value if isinstance(item, str)]
    bounded = [item[:max_chars] for item in items[:max_items]]
    partial = len(items) != len(value) or len(items) > max_items
    partial = partial or any(len(item) > max_chars for item in items[:max_items])
    return bounded, partial


def _redacted_evidence(evidence: tuple[BugEvidence, ...]) -> tuple[BugEvidence, ...]:
    return tuple(
        item.model_copy(update={"body": redact_bug_evidence_text(item.body)}) for item in evidence
    )


__all__ = (
    "OPENCODE_GO_BUG_TASK_QUALIFICATION",
    "QUALIFIED_BUG_TASKS",
    "BugAssessmentRuntimeRequest",
    "BugAssessmentRuntimeService",
    "BugAssessmentServiceLike",
    "BugTaskQualification",
    "UnavailableBugAssessmentService",
    "create_bug_assessment_agent_factory",
    "create_bug_assessment_runtime_service",
)
