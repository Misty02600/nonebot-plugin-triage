"""仓库维护数据与评测工件的内部模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class SupportLevel(StrEnum):
    S1_VERIFY = "s1_verify"
    S2_DIAGNOSE = "s2_diagnose"
    S3_ABSTAIN = "s3_abstain"


class ExecutionMode(StrEnum):
    NONEBUG_EXEC = "nonebug_exec"
    SANDBOX_EXEC = "sandbox_exec"
    CONTRACT_EXEC = "contract_exec"
    DIAGNOSE_ONLY = "diagnose_only"
    ESCALATE = "escalate"


class ResponsibilityLayer(StrEnum):
    ENVIRONMENT = "environment"
    TOOLCHAIN = "toolchain"
    FRAMEWORK = "framework"
    PLUGIN = "plugin"
    ADAPTER = "adapter"
    PROTOCOL_IMPLEMENTATION = "protocol_implementation"
    PLATFORM = "platform"
    EXTERNAL_SERVICE = "external_service"


class FaultPhase(StrEnum):
    INSTALL = "install"
    BOOT = "boot"
    CONNECT = "connect"
    RECEIVE = "receive"
    MATCH = "match"
    HANDLE = "handle"
    CALL_API = "call_api"
    SHUTDOWN = "shutdown"


class Symptom(StrEnum):
    DEPENDENCY_ERROR = "dependency_error"
    CONFIG_ERROR = "config_error"
    EXCEPTION = "exception"
    TIMEOUT_OR_DISCONNECT = "timeout_or_disconnect"
    NO_EVENT = "no_event"
    NO_MATCH = "no_match"
    WRONG_ACTION = "wrong_action"
    RESOURCE_PROBLEM = "resource_problem"


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repository: str
    number: int
    source_url: str

    @property
    def slug(self) -> str:
        return f"{self.owner}--{self.repository}--{self.number}"

    @property
    def case_id(self) -> str:
        return f"gh-{self.owner}-{self.repository}-{self.number}".lower()


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repository: str
    number: int
    source_url: str


@dataclass(frozen=True)
class OracleDraft:
    buggy_ref: str | None = None
    fixed_ref: str | None = None
    failure_signature: str | None = None
    success_assertion: str | None = None


@dataclass
class CaseCuration:
    provisional_support_level: str | None = None
    provisional_execution_mode: str | None = None
    research_note: str | None = None
    field_provenance: dict[str, list[str]] = field(default_factory=dict)
    support_level: str | None = None
    execution_mode: str | None = None
    root_cause_cluster: str | None = None
    environment: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    deployment_topology: str | None = None
    redacted_config_evidence: list[str] = field(default_factory=list)
    observed_behavior: str | None = None
    expected_behavior: str | None = None
    reproduction_steps: list[str] = field(default_factory=list)
    fault_phase: str | None = None
    symptoms: list[str] = field(default_factory=list)
    candidate_owners: list[str] = field(default_factory=list)
    required_evidence_gaps: list[str] = field(default_factory=list)
    ruled_out: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    escalation_target: str | None = None
    safety_or_scope_reason: str | None = None
    exclusion_reason: str | None = None
    oracle: OracleDraft = field(default_factory=OracleDraft)


@dataclass(frozen=True)
class SourceEvidence:
    platform: str
    owner: str
    repository: str
    issue_number: int
    issue_url: str
    api_url: str
    opened_at: str
    captured_at: str
    author_login: str | None
    title: str
    body: str | None
    labels: list[str]
    raw_snapshot_path: str
    raw_snapshot_sha256: str
    temporal_integrity: str = "body_edit_history_unavailable"


@dataclass
class SupportCaseDraft:
    schema_version: int
    case_id: str
    visibility_boundary: str
    source: SourceEvidence
    curation: CaseCuration

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
