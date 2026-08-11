from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from nbtriage.provider_failures import ProviderFailureReason
from nbtriage.rag import B1Error, B1ModelRequest

EvidenceSlot = Literal[
    "python_version",
    "component_versions",
    "operating_system",
    "logs",
    "reproduction_steps",
    "expected_behavior",
    "configuration",
    "deployment_topology",
    "raw_close_evidence",
]
Symptom = Literal[
    "dependency_error",
    "config_error",
    "exception",
    "timeout_or_disconnect",
    "no_event",
    "no_match",
    "wrong_action",
    "resource_problem",
]
FaultPhase = Literal[
    "install",
    "boot",
    "connect",
    "receive",
    "match",
    "handle",
    "call_api",
    "shutdown",
]
CandidateOwner = Literal[
    "environment",
    "toolchain",
    "framework",
    "plugin",
    "adapter",
    "protocol_implementation",
    "platform",
    "external_service",
]
Route = Literal["verify", "needs_evidence", "escalate", "abstain"]
B1_OUTPUT_SCHEMA_ID = "b1-structured-output-v1"
NormalizedVersion = Annotated[
    str,
    Field(pattern=r"^\d+\.\d+(?:\.\d+)?(?:[abrc]\d+)?$"),
]


class B1ProviderError(B1Error):
    pass


class B1ProviderRequestError(B1ProviderError):
    """表示没有可计费用量的 Provider 请求失败，只携带稳定脱敏分类。"""

    def __init__(
        self,
        message: str,
        *,
        failure_reason: ProviderFailureReason,
        http_status: int | None,
    ) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason
        self.http_status = http_status


class B1ResponseRejectionReason(StrEnum):
    FINISH_REASON = "finish_reason"
    NON_TEXT_OUTPUT = "non_text_output"
    MISSING_TEXT_OUTPUT = "missing_text_output"
    SCHEMA_VALIDATION = "schema_validation"
    DOMAIN_VALIDATION = "domain_validation"


class B1ProviderResponseError(B1ProviderError):
    """表示 Provider 已返回响应，但响应未通过本地后验校验。

    该异常只携带计费与审计所需的有界元数据，不保存原始响应文本，供真实评测在失败关闭时仍能
    精确记录已经发生的请求。

    Args:
        message: 项目控制的稳定错误说明，不包含原始模型输出。
        rejection_reason: 不包含原始输出的稳定后验拒绝分类。
        input_tokens: Provider 响应报告的输入 token 数。
        output_tokens: Provider 响应报告的输出 token 数。
        cost_microusd: 按返回 Provider/model 归一化的费用；无法定价时为 ``None``。
        provider_request_id: Provider 返回的有界响应标识。
        provider_name: Provider 返回的有界身份。
        provider_model_name: Provider 返回的有界模型身份。
        provider_fingerprint: Provider 返回的可选有界指纹。
    """

    def __init__(
        self,
        message: str,
        *,
        rejection_reason: B1ResponseRejectionReason,
        input_tokens: int,
        output_tokens: int,
        cost_microusd: int | None,
        provider_request_id: str | None,
        provider_name: str | None,
        provider_model_name: str | None,
        provider_fingerprint: str | None,
    ) -> None:
        super().__init__(message)
        self.rejection_reason = rejection_reason
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_microusd = cost_microusd
        self.provider_request_id = provider_request_id
        self.provider_name = provider_name
        self.provider_model_name = provider_model_name
        self.provider_fingerprint = provider_fingerprint


class B1StructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_values: list[NormalizedVersion]
    missing_evidence: list[EvidenceSlot]
    symptoms: list[Symptom]
    fault_phase: FaultPhase
    candidate_owners: list[CandidateOwner]
    route: Route
    answer: str
    citations: list[str]


def build_b1_user_payload(request: B1ModelRequest) -> str:
    return json.dumps(
        {
            "case_input": request.case_input,
            "retrieved_evidence": [
                {
                    "case_id": evidence.case_id,
                    "repository": evidence.repository,
                    "issue_number": evidence.issue_number,
                    "title": evidence.title,
                    "excerpt": evidence.excerpt,
                    "score": evidence.score,
                }
                for evidence in request.retrieved_evidence
            ],
            "allowed_citation_case_ids": [
                evidence.case_id for evidence in request.retrieved_evidence
            ],
            "response_schema": request.response_schema,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
