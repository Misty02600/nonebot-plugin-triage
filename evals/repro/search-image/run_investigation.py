"""在 triage 维护环境运行真实 Bug 调查，沿用复现快照和现有教学注释 mock。"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[3]
REPRO = Path(__file__).resolve().parent


def _dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


async def investigate(capture_path: Path, *, preflight: bool) -> None:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from pydantic_ai.models.wrapper import WrapperModel
    from tools.nbtriage_maintainer.bug_repro_replay import build_repro_toolbox
    from tools.nbtriage_maintainer.model_evaluation_target import create_model_evaluation_binding

    from nbtriage.bug._agent import (
        BUG_AGENT_PROMPT_ID,
        PydanticAIBugAssessmentAgent,
        _build_payload,
    )
    from nbtriage.bug.assessment import (
        BugEvidence,
        BugEvidenceKind,
        format_bug_assessment_reply,
        reconcile_bug_candidate,
    )

    capture_path = capture_path.resolve(strict=True)
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    teaching_path = REPRO / "fixtures/public-teaching.json"
    teaching = json.loads(teaching_path.read_text(encoding="utf-8"))
    plugin_version = capture["deployment"]["versions"]["YetAnotherPicSearch"]
    if teaching["tested_plugin_version"] != plugin_version or not teaching["mock"]:
        raise ValueError("public teaching mock must target this reproduction version")
    source_hash = hashlib.sha256(teaching_path.read_bytes()).hexdigest()
    body = json.dumps(
        {
            "plugin": "YetAnotherPicSearch",
            "source": "existing generated public teaching annotation (evaluation mock)",
            "mock": True,
            "annotation_source_version": teaching["annotation_source_version"],
            "tested_plugin_version": plugin_version,
            "applicability": "For this evaluation, assume this existing annotation is the supplied public contract. This does not verify cross-version compatibility or the teaching-generation pipeline.",
            "annotation": teaching["annotation"],
        },
        ensure_ascii=False,
    )
    public_contract = BugEvidence(
        evidence_id="public-teaching-mock:" + hashlib.sha256(body.encode()).hexdigest()[:24],
        kind=BugEvidenceKind.PUBLIC_CONTRACT,
        source="mock-public-teaching:YetAnotherPicSearch",
        body=body,
        revision=source_hash,
        current=True,  # 测评明确假设注释适用，不表示已验证跨版本知识有效性。
        partial=False,
    )
    case, toolbox = build_repro_toolbox(
        capture_path,
        public_contract=public_contract,
        public_capability_ids=(teaching["annotation"]["capability_id"],),
    )
    await toolbox.preload_public_contract()
    checks = {
        "plugin_version": plugin_version,
        "annotation_source_version": teaching["annotation_source_version"],
        "public_contract_is_mock": True,
        "public_teaching_chars": len(body),
        "public_source": str(teaching_path),
        "public_source_sha256": source_hash,
        "capture_sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
        "prompt_id": BUG_AGENT_PROMPT_ID,
        "initial_payload_chars": len(_build_payload(case, toolbox)),
        "provider_key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
    }
    if preflight:
        print(json.dumps(checks, ensure_ascii=False), flush=True)
        return

    binding = create_model_evaluation_binding(
        backend="pydantic-ai",
        model_name="deepseek:deepseek-v4-flash",
        timeout_seconds=300,
    )
    binding.model.client.max_retries = 0
    output_dir = capture_path.parent / datetime.now(UTC).strftime("investigation-%Y%m%dT%H%M%S%fZ")
    output_dir.mkdir()
    _dump(output_dir / "public-material.json", public_contract.model_dump(mode="json"))
    watched = [
        capture_path,
        teaching_path,
        Path(__file__),
        ROOT / "tools/nbtriage_maintainer/bug_repro_replay.py",
    ]
    watched.extend((ROOT / "src/nbtriage/bug").glob("*.py"))
    watched.extend((capture_path.parent / "source").rglob("*.py"))
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in watched}
    _dump(
        output_dir / "inputs.json",
        {
            **checks,
            "source_hashes": hashes,
            "model": "deepseek:deepseek-v4-flash",
            "model_settings": binding.model_settings,
            "budget": {
                "seconds": 300,
                "output_tokens": 16384,
                "total_tokens": 600000,
                "general_tools": 12,
                "transport_retries": 0,
            },
            "boundary": "Post-handoff Bug Agent only. Existing 2.0.4 teaching annotation mocked as the public contract for the 2.0.10 reproduction; no README input. Captured simulation logs, conversation, runtime and source tools. No repair description or expected verdict in model inputs. No live Bot or real SauceNAO call. Public teaching generation, version compatibility and intake routing are not evaluated. Dependency navigation and design RAG are unavailable. Runtime lock snapshot was collected by the test observer, not a production telemetry collector.",
        },
    )
    report = {"model_calls": [], "output_dir": str(output_dir)}

    class RecordedModel(WrapperModel):
        async def request(self, messages, model_settings, model_request_parameters):
            call = {"request": ModelMessagesTypeAdapter.dump_python(messages, mode="json")}
            report["model_calls"].append(call)
            _dump(output_dir / "result.json", report)
            print(f"MODEL_REQUEST {len(report['model_calls'])}", flush=True)
            try:
                response = await self.wrapped.request(
                    messages, model_settings, model_request_parameters
                )
                call["response"] = ModelMessagesTypeAdapter.dump_python([response], mode="json")
                return response
            finally:
                _dump(output_dir / "result.json", report)

    agent = PydanticAIBugAssessmentAgent(
        RecordedModel(binding.model),
        timeout_seconds=300,
        max_output_tokens=16384,
        total_tokens_limit=600000,
        max_tool_calls=12,
        model_settings=binding.model_settings,
        expected_provider=binding.provider,
        expected_model=binding.model_name,
    )
    started = monotonic()
    try:
        candidate = await agent.assess(case, toolbox)
        decision = reconcile_bug_candidate(candidate, toolbox.evidence)
        report["candidate"] = candidate.model_dump(mode="json")
        report["decision"] = decision.model_dump(mode="json")
        report["formatted_reply"] = format_bug_assessment_reply(decision)
    except Exception as error:
        report["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "kind": getattr(error, "failure_kind", None),
        }
        raise
    finally:
        report["elapsed_seconds"] = monotonic() - started
        report["usage"] = asdict(agent.last_usage) if agent.last_usage else None
        report["evidence"] = [item.model_dump(mode="json") for item in toolbox.evidence]
        report["tool_counts"] = dict(toolbox._tool_call_counts)
        report["messages"] = ModelMessagesTypeAdapter.dump_python(
            list(agent.last_messages), mode="json"
        )
        report["sources_unchanged"] = all(
            hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
            for path, digest in hashes.items()
        )
        _dump(output_dir / "result.json", report)
        print(
            json.dumps(
                {
                    key: report.get(key)
                    for key in (
                        "output_dir",
                        "decision",
                        "formatted_reply",
                        "tool_counts",
                        "usage",
                        "elapsed_seconds",
                        "sources_unchanged",
                        "error",
                    )
                },
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    asyncio.run(investigate(args.capture, preflight=args.preflight))
