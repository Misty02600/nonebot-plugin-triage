"""将独立复现捕获的数据接入 Bug 工具；不加载测试插件或调用远程模型。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic_ai import ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.profiles import ModelProfile

from nbtriage.bug._agent import PydanticAIBugAssessmentAgent
from nbtriage.bug.assessment import (
    BugAssessmentCase,
    BugAssessmentToolbox,
    BugEvidence,
    BugEvidenceKind,
    BugInvestigationPlugin,
    build_bug_case_fingerprint,
)
from nbtriage.bug.conversation import BugConversationPage
from nbtriage.bug.fingerprints import FailureFingerprint, FailureFrame, fingerprint_frames
from nbtriage.bug.logs import redact_bug_evidence_text
from nbtriage.bug.source import ApprovedSourceRoot, BugSourceTools


def _verified_file(root: Path, relative: str, digest: str) -> Path:
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("capture file is outside its snapshot")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"capture file hash mismatch: {relative}")
    return path


def repro_wait_fingerprint(capture: dict[str, Any]) -> FailureFingerprint | None:
    """复现采集器的等待现场适配；不从文字报告推断死锁或补造帧。"""
    snapshot = capture["runtime"]
    if (
        snapshot.get("dispatch_pending") is not True
        or snapshot.get("snapshot_phase") != "before_harness_cancellation"
    ):
        return None
    frames = []
    for item in snapshot["frames"]:
        _, separator, relative = item["file"].replace("\\", "/").partition("/YetAnotherPicSearch/")
        if not separator:
            continue
        if relative not in capture["deployment"]["plugin_source_sha256"]:
            return None
        frames.append(
            FailureFrame(
                module="YetAnotherPicSearch." + relative.removesuffix(".py").replace("/", "."),
                function=item["function"],
                code=item["code"],
            )
        )
    return fingerprint_frames(
        tuple(frames),
        kind="wait_path",
        failure_type="pending_coroutine",
        complete=True,
    )


def build_repro_toolbox(
    manifest_path: Path,
    *,
    public_contract: BugEvidence | None = None,
    public_capability_ids: tuple[str, ...] = (),
) -> tuple[BugAssessmentCase, BugAssessmentToolbox]:
    """绑定单次捕获，读取已校验快照；不会把历史捕获冒充当前线上状态。"""
    manifest_path = manifest_path.resolve(strict=True)
    root = manifest_path.parent
    capture = json.loads(manifest_path.read_text(encoding="utf-8"))
    if capture["schema_version"] != 1:
        raise ValueError("unsupported reproduction capture version")
    log_path = _verified_file(root, "plugin.log", capture["plugin_log_sha256"])
    for relative, digest in capture["deployment"]["plugin_source_sha256"].items():
        _verified_file(root, f"source/{relative}", digest)
    source_root = (root / "source").resolve(strict=True)
    if not source_root.is_relative_to(root):
        raise ValueError("source snapshot is outside the capture")
    page = BugConversationPage.model_validate_json(json.dumps(capture["conversation"]))
    if not any(message.is_current_request for message in page.messages):
        raise ValueError("capture has no investigation request")
    provenance = {
        "correlation_id": capture["correlation_id"],
        "snapshot_observed_at": capture["runtime"]["observed_at"],
        "provenance": capture["provenance"],
    }

    def evidence(kind: BugEvidenceKind, payload: Any) -> tuple[BugEvidence, ...]:
        body = redact_bug_evidence_text(json.dumps(payload, ensure_ascii=False))
        digest = hashlib.sha256(body.encode()).hexdigest()
        return (
            BugEvidence(
                evidence_id=f"repro:{kind.value}:{digest[:24]}",
                kind=kind,
                source=f"reproduction:{capture['correlation_id']}",
                body=body,
                revision=digest,
                observed_at=(
                    capture["runtime"]["observed_at"]
                    if kind in (BugEvidenceKind.RUNTIME_OBSERVATION, BugEvidenceKind.CORRELATED_LOG)
                    else None
                ),
                current=True,
                partial=False,
                failure_fingerprint=(
                    repro_wait_fingerprint(capture)
                    if kind is BugEvidenceKind.RUNTIME_OBSERVATION
                    else None
                ),
            ),
        )

    async def runtime():
        return evidence(
            BugEvidenceKind.RUNTIME_OBSERVATION, {**provenance, "snapshot": capture["runtime"]}
        )

    async def logs():
        return evidence(
            BugEvidenceKind.CORRELATED_LOG,
            {
                **provenance,
                "log_kind": "actual INFO-and-above logs in the captured simulation window",
                "exception_traceback_available": capture["runtime"]["plugin_exception_observed"],
                "text": log_path.read_text(encoding="utf-8"),
            },
        )

    async def conversation():
        return evidence(BugEvidenceKind.CONVERSATION_CONTEXT, page.model_dump(mode="json"))

    async def deployment():
        return evidence(BugEvidenceKind.DEPLOYMENT_CONTEXT, {**provenance, **capture["deployment"]})

    async def no_evidence(*_args):
        return ()

    async def public_material():
        return (public_contract,) if public_contract is not None else ()

    fingerprint = build_bug_case_fingerprint(
        capture["request_text"],
        subject_id=None,
        failure_signature=None,
        adapter="OneBot V11",
        source_revision=hashlib.sha256(
            json.dumps(capture["deployment"]["plugin_source_sha256"], sort_keys=True).encode()
        ).hexdigest(),
        contract_revision=public_contract.revision if public_contract is not None else None,
        deployment_generation=capture["run_id"],
    )
    case = BugAssessmentCase(
        request_text=capture["request_text"],
        fingerprint=fingerprint,
        plugins=(
            BugInvestigationPlugin(
                plugin_ref="p1",
                owner="YetAnotherPicSearch",
                capability_ids=public_capability_ids,
                source_available=True,
            ),
        ),
    )
    toolbox = BugAssessmentToolbox(
        runtime_loader=runtime,
        log_loader=logs,
        conversation_loader=conversation,
        deployment_loader=deployment,
        design_loader=no_evidence,
        public_contract_loader=public_material,
        source_tools=BugSourceTools(
            {"p1": ApprovedSourceRoot("YetAnotherPicSearch", source_root)},
            dependency_paths=(),
        ),
    )
    return case, toolbox


async def check_repro_tools(manifest_path: Path) -> dict[str, Any]:
    """通过真实 Agent 工具调度检查可读性；FunctionModel 仅指定调用，不评判故障。"""
    case, toolbox = build_repro_toolbox(manifest_path)
    steps = 0
    returns: dict[str, Any] = {}

    def respond(messages, info):
        nonlocal steps
        steps += 1
        for message in messages:
            for part in message.parts:
                if isinstance(part, RetryPromptPart):
                    raise AssertionError(f"tool check retry: {part.content}")
                if isinstance(part, ToolReturnPart):
                    returns[part.tool_name] = part.content
        if steps == 1:
            names = (
                "read_runtime_evidence",
                "read_correlated_logs",
                "read_conversation_context",
                "read_deployment_context",
            )
            assert set(names) <= {tool.name for tool in info.function_tools}
            return ModelResponse(
                parts=[ToolCallPart(name, {}, tool_call_id=name) for name in names]
            )
        if steps == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "p1_read_file",
                        {"path": "data_source/saucenao.py"},
                        tool_call_id="source-search",
                    ),
                    ToolCallPart(
                        "p1_read_file",
                        {"path": "utils.py", "offset": 205, "limit": 25},
                        tool_call_id="source-lock",
                    ),
                ]
            )
        assert steps == 3, "tool access needed unexpected retries"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "occurrence": "unknown",
                        "responsibility_candidates": [],
                        "reason": "insufficient_evidence",
                        "evidence_ids": [],
                        "missing_evidence": ["public_contract"],
                    },
                )
            ]
        )

    agent = PydanticAIBugAssessmentAgent(
        FunctionModel(
            respond,
            profile=ModelProfile(supports_tools=True, default_structured_output_mode="tool"),
        ),
        timeout_seconds=30,
        max_output_tokens=1024,
    )
    await agent.assess(case, toolbox)
    expected = (
        "read_runtime_evidence",
        "read_correlated_logs",
        "read_conversation_context",
        "read_deployment_context",
    )
    assert all(returns.get(name) for name in expected)
    assert toolbox.conversation_exhausted
    assert toolbox.tool_call_count("p1_read_file") == 2
    return {
        "check": "SDK tool access only",
        "live_model_called": False,
        "formal_intake_routing_tested": False,
        "production_log_collector_tested": False,
        "case": case.model_dump(mode="json"),
        "tool_counts": {
            name: toolbox.tool_call_count(name) for name in (*expected, "p1_read_file")
        },
        "evidence": [item.model_dump(mode="json") for item in toolbox.evidence],
        "limitations": [
            "No generated public teaching context or design RAG supplied; no diagnostic verdict evaluated.",
            "Logs are captured simulation logs via evaluation loaders, not the production exception buffer.",
            "Current means valid for this frozen capture, not a claim about a currently running bot.",
            "Plugin source snapshot only; external dependency navigation is not enabled.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    result = asyncio.run(check_repro_tools(args.capture))
    output = args.capture.resolve().with_name("tool-access.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "tool_counts": result["tool_counts"],
                "live_model_called": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
