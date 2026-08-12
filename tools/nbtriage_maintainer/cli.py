"""仓库维护者命令行入口；不属于插件安装接口。"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from nbtriage.capabilities import CapabilityIndexError, search_capability_index
from nbtriage.evidence_receipts import EvidenceReceiptError, load_evidence_receipt
from nbtriage.live_trials import LiveTrialError, summarize_trial_logs
from nbtriage.model_contracts import B1ProviderError
from nbtriage.rag import B1Error
from tools.nbtriage_maintainer.agent_evaluation import (
    AgentEvaluationError,
    RealGatePartialAudit,
    b4_real_partial_report_path,
    evaluate_b4_real_fixtures,
    evaluate_b4_scripted_fixtures,
)
from tools.nbtriage_maintainer.answer_quality_evaluation import (
    AnswerQualityEvaluationError,
    evaluate_answer_quality,
)
from tools.nbtriage_maintainer.answer_review_export import (
    AnswerReviewExportError,
    build_b4_answer_quality_review,
)
from tools.nbtriage_maintainer.bot_docs import (
    DEFAULT_BOT_DOCS_INDEX_PATH,
    BotDocsIndex,
    BotDocsIndexError,
    build_bot_docs_index,
)
from tools.nbtriage_maintainer.bot_docs_evaluation import (
    DEFAULT_BOT_DOCS_FIXTURE_PATH,
    BotDocsEvaluationError,
    evaluate_bot_docs_retrieval,
)
from tools.nbtriage_maintainer.collector import ManifestError, collect_manifest
from tools.nbtriage_maintainer.curation import (
    AnnotationError,
    apply_annotations,
    export_annotations,
)
from tools.nbtriage_maintainer.discovery import (
    DiscoveryError,
    discover_candidates,
    write_discovery_report,
)
from tools.nbtriage_maintainer.evaluation import (
    EvaluationError,
    evaluate_b0,
    evaluate_b1,
    write_new_evaluation_report,
)
from tools.nbtriage_maintainer.evidence_policy import EvidencePolicyError
from tools.nbtriage_maintainer.evidence_policy_evaluation import (
    EvidencePolicyEvaluationError,
    evaluate_b3_evidence_policy,
)
from tools.nbtriage_maintainer.evidence_receipt_evaluation import (
    EvidenceReceiptEvaluationError,
    evaluate_b3_evidence_receipts,
)
from tools.nbtriage_maintainer.gate import evaluate_cases, write_report
from tools.nbtriage_maintainer.github import GitHubApiError, GitHubClient
from tools.nbtriage_maintainer.mlflow_tracking import (
    DEFAULT_MLFLOW_EXPERIMENT,
    DEFAULT_MLFLOW_TRACKING_URI,
    MLflowTrackingError,
    publish_evaluation_to_mlflow,
)
from tools.nbtriage_maintainer.runtime_results import DEFAULT_PROBE_ROOT, evaluate_runtime_results
from tools.nbtriage_maintainer.safety_evaluation import SafetyEvaluationError, evaluate_s3
from tools.nbtriage_maintainer.sessions import (
    FileSessionStore,
    SessionError,
    SupportSession,
    approve_session,
    attach_evidence_receipt,
    attach_runtime_assessment,
    create_session_from_report,
    validate_case_id,
)
from tools.nbtriage_maintainer.timeline import (
    enrich_gold_direct_commits,
    enrich_gold_pull_request_commits,
    enrich_gold_pull_requests,
    enrich_gold_timelines,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nbtriage-maintainer",
        description="Repository-only NoneBot Triage data and evaluation tooling.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser(
        "collect", help="Collect public GitHub issues into local curation artifacts."
    )
    collect_parser.add_argument(
        "--manifest", type=Path, default=Path("evals/datasets/catalog/candidates.json")
    )
    collect_parser.add_argument("--output-root", type=Path, default=Path("data"))
    collect_parser.add_argument("--timeout", type=float, default=20.0)

    discover_parser = subparsers.add_parser(
        "discover", help="Build a balanced issue pool for manual Data Gate review."
    )
    discover_parser.add_argument(
        "--repositories",
        type=Path,
        default=Path("evals/datasets/catalog/repositories.json"),
    )
    discover_parser.add_argument(
        "--output", type=Path, default=Path("data/discovery/candidates.json")
    )
    discover_parser.add_argument("--target", type=int, default=60)
    discover_parser.add_argument("--pages-per-repository", type=int, default=1)
    discover_parser.add_argument("--timeout", type=float, default=20.0)

    timeline_parser = subparsers.add_parser(
        "enrich-timeline", help="Add linked PR and commit events to existing Gold artifacts."
    )
    timeline_parser.add_argument("--manifest", type=Path, required=True)
    timeline_parser.add_argument("--gold-dir", type=Path, default=Path("data/gold"))
    timeline_parser.add_argument("--timeout", type=float, default=20.0)

    pull_parser = subparsers.add_parser(
        "enrich-linked-prs", help="Add base, head, and merge refs for PRs linked from Gold."
    )
    pull_parser.add_argument("--manifest", type=Path, required=True)
    pull_parser.add_argument("--gold-dir", type=Path, default=Path("data/gold"))
    pull_parser.add_argument("--timeout", type=float, default=20.0)

    commit_parser = subparsers.add_parser(
        "enrich-pr-commits", help="Add commit sequences for pull requests already stored in Gold."
    )
    commit_parser.add_argument("--manifest", type=Path, required=True)
    commit_parser.add_argument("--gold-dir", type=Path, default=Path("data/gold"))
    commit_parser.add_argument("--timeout", type=float, default=20.0)

    direct_commit_parser = subparsers.add_parser(
        "enrich-linked-commits",
        help="Add parent refs for commits linked directly from Issue timelines.",
    )
    direct_commit_parser.add_argument("--manifest", type=Path, required=True)
    direct_commit_parser.add_argument("--gold-dir", type=Path, default=Path("data/gold"))
    direct_commit_parser.add_argument("--timeout", type=float, default=20.0)

    annotation_parser = subparsers.add_parser(
        "apply-annotations", help="Merge versioned human annotations into generated cases."
    )
    annotation_parser.add_argument("--annotations", type=Path, required=True)
    annotation_parser.add_argument("--cases-dir", type=Path, default=Path("data/cases"))

    export_parser = subparsers.add_parser(
        "export-annotations", help="Export curated case fields into a versioned annotation file."
    )
    export_parser.add_argument("--manifest", type=Path, required=True)
    export_parser.add_argument("--cases-dir", type=Path, default=Path("data/cases"))
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--force", action="store_true")

    gate_parser = subparsers.add_parser(
        "gate", help="Evaluate local SupportCase curation completeness."
    )
    gate_parser.add_argument("--cases-dir", type=Path, default=Path("data/cases"))
    gate_parser.add_argument(
        "--runtime-results",
        type=Path,
        default=Path("evals/oracles"),
        help="Versioned Oracle result file or directory.",
    )
    gate_parser.add_argument(
        "--probe-root",
        type=Path,
        default=DEFAULT_PROBE_ROOT,
        help="Trusted root for repository-relative Oracle probe_source paths.",
    )
    gate_parser.add_argument("--report", type=Path, default=Path("reports/data-gate.json"))

    trial_summary_parser = subparsers.add_parser(
        "summarize-trials",
        help="Summarize bounded production trial JSONL without exposing event identifiers.",
    )
    trial_summary_parser.add_argument(
        "--log-path",
        type=Path,
        required=True,
        help="Explicit trial JSONL path; the CLI does not resolve Bot LocalStore settings.",
    )
    trial_summary_parser.add_argument(
        "--backup-count",
        type=_positive_int,
        default=5,
        help="Maximum numbered rotation backups to include.",
    )

    bot_docs_index_parser = subparsers.add_parser(
        "build-bot-docs-index",
        help="Build a local SQLite FTS5 index from the approved bot-docs source subset.",
    )
    bot_docs_index_parser.add_argument("--source-root", type=Path, required=True)
    bot_docs_index_parser.add_argument("--index", type=Path, default=DEFAULT_BOT_DOCS_INDEX_PATH)
    bot_docs_index_parser.add_argument(
        "--replace",
        action="store_true",
        help="Atomically replace an existing derived index; source Markdown is never modified.",
    )

    bot_docs_search_parser = subparsers.add_parser(
        "search-bot-docs",
        help="Search the local bot-docs index without model or network calls.",
    )
    bot_docs_search_parser.add_argument("query")
    bot_docs_search_parser.add_argument("--index", type=Path, default=DEFAULT_BOT_DOCS_INDEX_PATH)
    bot_docs_search_parser.add_argument("--limit", type=_positive_int, default=5)
    bot_docs_search_parser.add_argument(
        "--strategy", choices=("hybrid", "metadata"), default="hybrid"
    )

    capability_search_parser = subparsers.add_parser(
        "search-capabilities",
        help="Search an opt-in deployment-local capability shadow index.",
    )
    capability_search_parser.add_argument("query")
    capability_search_parser.add_argument("--index", type=Path, required=True)
    capability_search_parser.add_argument("--limit", type=_positive_int, default=5)
    capability_search_parser.add_argument(
        "--include-unresolved",
        action="store_true",
        help="Include capabilities with unresolved analysis issues.",
    )
    capability_search_parser.add_argument(
        "--include-restricted",
        action="store_true",
        help="Include restricted capabilities after out-of-band authorization.",
    )

    bot_docs_evaluation_parser = subparsers.add_parser(
        "evaluate-bot-docs-retrieval",
        help="Compare metadata-only and local full-text retrieval on public synthetic fixtures.",
    )
    bot_docs_evaluation_parser.add_argument(
        "--index", type=Path, default=DEFAULT_BOT_DOCS_INDEX_PATH
    )
    bot_docs_evaluation_parser.add_argument(
        "--fixtures", type=Path, default=DEFAULT_BOT_DOCS_FIXTURE_PATH
    )
    bot_docs_evaluation_parser.add_argument(
        "--report", type=Path, default=Path("reports/bot-docs-retrieval.json")
    )

    evaluation_parser = subparsers.add_parser(
        "evaluate-b0",
        help="Evaluate the deterministic checklist baseline on a frozen split.",
    )
    evaluation_parser.add_argument("--cases-dir", type=Path, default=Path("data/cases"))
    evaluation_parser.add_argument(
        "--split", type=Path, default=Path("evals/datasets/splits/data-gate-v1.json")
    )
    evaluation_parser.add_argument("--report", type=Path, default=Path("artifacts/eval-b0.json"))

    safety_parser = subparsers.add_parser(
        "evaluate-s3",
        help="Evaluate synthetic adversarial safety fixtures without model or tool calls.",
    )
    safety_parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("evals/datasets/fixtures/s3-adversarial-v1.json"),
    )
    safety_parser.add_argument(
        "--report",
        type=Path,
        default=Path("artifacts/eval-s3.json"),
    )

    b3_policy_parser = subparsers.add_parser(
        "evaluate-b3-evidence-policy",
        help="Evaluate the deterministic single-evidence policy on B1 validation output.",
    )
    b3_policy_parser.add_argument(
        "--prediction-report",
        type=Path,
        default=Path("evals/datasets/fixtures/b3-evidence-policy-validation-v1.json"),
    )

    b3_receipt_parser = subparsers.add_parser(
        "evaluate-b3-evidence-receipts",
        help="Evaluate structured evidence receipt validation on synthetic fixtures.",
    )
    b3_receipt_parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("evals/datasets/fixtures/b3-evidence-receipts-v1.json"),
    )
    b3_receipt_parser.add_argument(
        "--report",
        type=Path,
        default=Path("artifacts/eval-b3-evidence-receipts.json"),
    )

    answer_quality_parser = subparsers.add_parser(
        "evaluate-answer-quality",
        help="Validate the human answer-and-citation rubric on fixed review samples.",
    )
    answer_quality_parser.add_argument(
        "--rubric",
        type=Path,
        default=Path("evals/rubrics/answer-quality-v1.json"),
    )
    answer_quality_parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("evals/datasets/fixtures/answer-quality-calibration-v1.json"),
    )
    answer_quality_parser.add_argument(
        "--annotations",
        type=Path,
        default=Path("evals/curation/answer-quality/calibration-v1.json"),
    )
    answer_quality_parser.add_argument(
        "--source-report",
        type=Path,
        help="Original real B4 report required by candidate-quality fixtures.",
    )
    answer_quality_parser.add_argument(
        "--report",
        type=Path,
        default=Path("artifacts/eval-answer-quality-calibration.json"),
    )
    answer_review_parser = subparsers.add_parser(
        "export-answer-quality-review",
        help="Export completed real B4 candidates into a local human-review package.",
    )
    answer_review_parser.add_argument("--evaluation-report", type=Path, required=True)
    answer_review_parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("evals/datasets/fixtures/b4-bounded-agent-v1.json"),
    )
    answer_review_parser.add_argument(
        "--split",
        type=Path,
        default=Path("evals/datasets/splits/b4-gate-v1.json"),
    )
    answer_review_parser.add_argument(
        "--rubric",
        type=Path,
        default=Path("evals/rubrics/answer-quality-v1.json"),
    )
    answer_review_parser.add_argument("--output-dir", type=Path, required=True)
    b3_policy_parser.add_argument(
        "--report",
        type=Path,
        default=Path("artifacts/eval-b3-evidence-policy-validation.json"),
    )

    b4_parser = subparsers.add_parser(
        "evaluate-b4-scripted",
        help="Validate the bounded Agent Gate with synthetic scripted multi-trial fixtures.",
    )
    b4_parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("evals/datasets/fixtures/b4-bounded-agent-v1.json"),
    )
    b4_parser.add_argument(
        "--split",
        type=Path,
        default=Path("evals/datasets/splits/b4-gate-v1.json"),
    )
    b4_parser.add_argument(
        "--report",
        type=Path,
        default=Path("artifacts/eval-b4-scripted.json"),
    )

    b4_real_parser = subparsers.add_parser(
        "evaluate-b4-real",
        help="Run the synthetic B1/B3/B4 multi-trial Gate with an authorized real model.",
    )
    b4_real_parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("evals/datasets/fixtures/b4-bounded-agent-v1.json"),
    )
    b4_real_parser.add_argument(
        "--split",
        type=Path,
        default=Path("evals/datasets/splits/b4-gate-v1.json"),
    )
    b4_real_parser.add_argument(
        "--backend",
        choices=("openai-responses", "deepseek-responses", "anthropic-messages"),
        required=True,
    )
    b4_real_parser.add_argument("--model", required=True)
    b4_real_parser.add_argument("--trials-per-fixture", type=_positive_int, required=True)
    b4_real_parser.add_argument("--max-provider-requests", type=_positive_int, required=True)
    b4_real_parser.add_argument(
        "--max-agent-input-tokens-per-trial",
        type=_positive_int,
        required=True,
        help="Cumulative B4 Agent input-token limit for one trial; B1 input is fixed by fixtures.",
    )
    b4_real_parser.add_argument(
        "--max-output-tokens-per-trial",
        type=_positive_int,
        required=True,
        help="B1 response limit and cumulative B4 Agent output-token limit for one trial.",
    )
    b4_real_parser.add_argument("--deadline-seconds", type=_positive_float, required=True)
    b4_real_parser.add_argument("--timeout", type=_positive_float, default=60.0)
    b4_real_parser.add_argument(
        "--whole-run-timeout-seconds",
        type=_positive_float,
        default=900.0,
        help="Application-level deadline for the complete paid Gate run.",
    )
    b4_real_parser.add_argument("--declared-budget-usd", type=_positive_float, required=True)
    b4_real_parser.add_argument(
        "--report",
        type=Path,
        default=Path("artifacts/eval-b4-real.json"),
    )
    b4_real_parser.add_argument(
        "--confirm-paid-run",
        action="store_true",
        help="Acknowledge the exact model, synthetic data egress, and declared API budget.",
    )

    b1_parser = subparsers.add_parser(
        "evaluate-b1-openai",
        help="Run the RAG-only baseline with an explicit OpenAI model and call budget.",
    )
    b1_parser.add_argument("--cases-dir", type=Path, default=Path("data/cases"))
    b1_parser.add_argument(
        "--split", type=Path, default=Path("evals/datasets/splits/data-gate-v1.json")
    )
    b1_parser.add_argument("--model", required=True)
    b1_parser.add_argument("--max-output-tokens", type=_positive_int, required=True)
    b1_parser.add_argument("--max-model-calls", type=_positive_int, required=True)
    b1_parser.add_argument("--declared-budget-usd", type=_positive_float, required=True)
    b1_parser.add_argument(
        "--score-split",
        choices=("validation", "heldout"),
        required=True,
        help="Run validation first; run heldout only after freezing the B1 configuration.",
    )
    b1_parser.add_argument("--timeout", type=float, default=60.0)
    b1_parser.add_argument("--cache-dir", type=Path, default=Path("artifacts/cache/b1-openai"))
    b1_parser.add_argument("--report", type=Path, default=Path("artifacts/eval-b1-openai.json"))
    b1_parser.add_argument(
        "--confirm-paid-run",
        action="store_true",
        help="Acknowledge that uncached requests can incur API charges.",
    )

    deepseek_parser = subparsers.add_parser(
        "evaluate-b1-deepseek",
        help="Run the RAG-only baseline with DeepSeek V4 Flash in non-thinking mode.",
    )
    deepseek_parser.add_argument("--cases-dir", type=Path, default=Path("data/cases"))
    deepseek_parser.add_argument(
        "--split", type=Path, default=Path("evals/datasets/splits/data-gate-v1.json")
    )
    deepseek_parser.add_argument(
        "--model",
        choices=("deepseek-v4-flash",),
        required=True,
    )
    deepseek_parser.add_argument("--max-output-tokens", type=_positive_int, required=True)
    deepseek_parser.add_argument("--max-model-calls", type=_positive_int, required=True)
    deepseek_parser.add_argument("--declared-budget-usd", type=_positive_float, required=True)
    deepseek_parser.add_argument(
        "--score-split",
        choices=("validation", "heldout"),
        required=True,
        help="Run validation first; run heldout only after freezing the B1 configuration.",
    )
    deepseek_parser.add_argument("--timeout", type=float, default=60.0)
    deepseek_parser.add_argument(
        "--cache-dir", type=Path, default=Path("artifacts/cache/b1-deepseek")
    )
    deepseek_parser.add_argument(
        "--report", type=Path, default=Path("artifacts/eval-b1-deepseek.json")
    )
    deepseek_parser.add_argument(
        "--confirm-paid-run",
        action="store_true",
        help="Acknowledge that uncached requests can incur API charges.",
    )

    mlflow_parser = subparsers.add_parser(
        "publish-evaluation-mlflow",
        help="Publish an existing evaluation JSON artifact to an explicit MLflow server.",
    )
    mlflow_parser.add_argument("--report", type=Path, required=True)
    mlflow_parser.add_argument(
        "--tracking-uri",
        default=DEFAULT_MLFLOW_TRACKING_URI,
        help="Explicit MLflow Tracking URI; defaults to the local loopback server.",
    )
    mlflow_parser.add_argument(
        "--experiment",
        default=DEFAULT_MLFLOW_EXPERIMENT,
    )
    mlflow_parser.add_argument("--run-name")

    session_create_parser = subparsers.add_parser(
        "session-create",
        help="Create an auditable support session from a frozen B1 prediction.",
    )
    session_create_parser.add_argument("--prediction-report", type=Path, required=True)
    session_create_parser.add_argument("--case-id", required=True)
    session_create_parser.add_argument("--session-id")
    session_create_parser.add_argument(
        "--sessions-dir", type=Path, default=Path("artifacts/sessions")
    )

    session_approve_parser = subparsers.add_parser(
        "session-approve",
        help="Explicitly approve the pending Oracle action for a support session.",
    )
    session_approve_parser.add_argument("--session-id", required=True)
    session_approve_parser.add_argument("--actor", required=True)
    session_approve_parser.add_argument(
        "--sessions-dir", type=Path, default=Path("artifacts/sessions")
    )

    session_runtime_parser = subparsers.add_parser(
        "session-attach-runtime",
        help="Attach an already versioned and validated Oracle result to an approved session.",
    )
    session_runtime_parser.add_argument("--session-id", required=True)
    session_runtime_parser.add_argument(
        "--sessions-dir", type=Path, default=Path("artifacts/sessions")
    )
    session_runtime_parser.add_argument("--cases-dir", type=Path, default=Path("data/cases"))
    session_runtime_parser.add_argument(
        "--runtime-results", type=Path, default=Path("evals/oracles")
    )
    session_runtime_parser.add_argument("--probe-root", type=Path, default=DEFAULT_PROBE_ROOT)
    session_runtime_parser.add_argument("--actor", default="runtime-validator")

    session_evidence_parser = subparsers.add_parser(
        "session-attach-evidence",
        help="Attach one redacted structured evidence receipt and replan the next slot.",
    )
    session_evidence_parser.add_argument("--session-id", required=True)
    session_evidence_parser.add_argument("--receipt", type=Path, required=True)
    session_evidence_parser.add_argument(
        "--sessions-dir", type=Path, default=Path("artifacts/sessions")
    )

    session_show_parser = subparsers.add_parser(
        "session-show",
        help="Print a persisted support session without loading the original Issue body.",
    )
    session_show_parser.add_argument("--session-id", required=True)
    session_show_parser.add_argument(
        "--sessions-dir", type=Path, default=Path("artifacts/sessions")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "collect":
        return _run_collect(args)
    if args.command == "discover":
        return _run_discover(args)
    if args.command == "enrich-timeline":
        return _run_enrich_timeline(args)
    if args.command == "enrich-linked-prs":
        return _run_enrich_linked_prs(args)
    if args.command == "enrich-pr-commits":
        return _run_enrich_pr_commits(args)
    if args.command == "enrich-linked-commits":
        return _run_enrich_linked_commits(args)
    if args.command == "apply-annotations":
        return _run_apply_annotations(args)
    if args.command == "export-annotations":
        return _run_export_annotations(args)
    if args.command == "gate":
        return _run_gate(args)
    if args.command == "summarize-trials":
        return _run_summarize_trials(args)
    if args.command == "build-bot-docs-index":
        return _run_build_bot_docs_index(args)
    if args.command == "search-bot-docs":
        return _run_search_bot_docs(args)
    if args.command == "search-capabilities":
        return _run_search_capabilities(args)
    if args.command == "evaluate-bot-docs-retrieval":
        return _run_evaluate_bot_docs_retrieval(args)
    if args.command == "evaluate-b0":
        return _run_evaluate_b0(args)
    if args.command == "evaluate-s3":
        return _run_evaluate_s3(args)
    if args.command == "evaluate-b3-evidence-policy":
        return _run_evaluate_b3_evidence_policy(args)
    if args.command == "evaluate-b3-evidence-receipts":
        return _run_evaluate_b3_evidence_receipts(args)
    if args.command == "evaluate-answer-quality":
        return _run_evaluate_answer_quality(args)
    if args.command == "export-answer-quality-review":
        return _run_export_answer_quality_review(args)
    if args.command == "evaluate-b4-scripted":
        return _run_evaluate_b4_scripted(args)
    if args.command == "evaluate-b4-real":
        return _run_evaluate_b4_real(args)
    if args.command == "evaluate-b1-openai":
        return _run_evaluate_b1_openai(args)
    if args.command == "evaluate-b1-deepseek":
        return _run_evaluate_b1_deepseek(args)
    if args.command == "publish-evaluation-mlflow":
        return _run_publish_evaluation_mlflow(args)
    if args.command == "session-create":
        return _run_session_create(args)
    if args.command == "session-approve":
        return _run_session_approve(args)
    if args.command == "session-attach-runtime":
        return _run_session_attach_runtime(args)
    if args.command == "session-attach-evidence":
        return _run_session_attach_evidence(args)
    if args.command == "session-show":
        return _run_session_show(args)
    raise AssertionError(f"unhandled command: {args.command}")


def _run_search_capabilities(args: argparse.Namespace) -> int:
    try:
        hits = search_capability_index(
            args.index,
            args.query,
            include_unresolved=args.include_unresolved,
            include_restricted=args.include_restricted,
            limit=args.limit,
        )
    except CapabilityIndexError as error:
        print(f"capability search failed: {error}", file=sys.stderr)
        return 1
    payload = {
        "result_count": len(hits),
        "results": [
            {
                "capability_id": hit.record.capability_id,
                "owner": hit.record.owner,
                "kind": hit.record.kind,
                "disclosure": hit.record.disclosure.value,
                "platform_scope": hit.record.platform_scope.to_dict(),
                "analysis_issues": [issue.value for issue in hit.record.analysis_issues],
                "state": hit.record.state.value,
                "score": hit.score,
                "claims": [
                    {
                        "field": claim.field,
                        "basis": claim.basis.value,
                        "value": claim.value,
                    }
                    for claim in hit.record.claims
                ],
                "constraints": [
                    {
                        "kind": constraint.kind,
                        "operation": constraint.operation,
                        "evaluability": constraint.evaluability.value,
                    }
                    for constraint in hit.record.constraints
                ],
            }
            for hit in hits
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _run_publish_evaluation_mlflow(args: argparse.Namespace) -> int:
    try:
        publication = publish_evaluation_to_mlflow(
            args.report,
            tracking_uri=args.tracking_uri,
            experiment_name=args.experiment,
            run_name=args.run_name,
        )
    except MLflowTrackingError as error:
        print(f"MLflow publication failed: {error}", file=sys.stderr)
        return 1

    action = "created" if publication.created else "already exists"
    print(f"MLflow run {action}: {publication.run_id}")
    print(f"experiment: {publication.experiment_id}")
    print(f"artifact sha256: {publication.artifact_sha256}")
    return 0


def _run_collect(args: argparse.Namespace) -> int:
    client = GitHubClient(
        token=os.environ.get("GITHUB_TOKEN"),
        timeout_seconds=args.timeout,
    )
    try:
        results = collect_manifest(args.manifest, args.output_root, client)
    except (GitHubApiError, ManifestError, ValueError, OSError) as error:
        print(f"collect failed: {error}", file=sys.stderr)
        return 1

    for result in results:
        action = "created" if result.case_created else "preserved"
        print(
            f"{result.case_id}: {action} case, captured {result.comment_count} post-open comments"
        )
    print(f"collected {len(results)} issue(s); no GitHub writes were performed")
    return 0


def _run_discover(args: argparse.Namespace) -> int:
    client = GitHubClient(
        token=os.environ.get("GITHUB_TOKEN"),
        timeout_seconds=args.timeout,
    )
    try:
        report = discover_candidates(
            args.repositories,
            client,
            target_count=args.target,
            pages_per_repository=args.pages_per_repository,
        )
        write_discovery_report(args.output, report)
    except (DiscoveryError, GitHubApiError, ValueError, OSError) as error:
        print(f"discover failed: {error}", file=sys.stderr)
        return 1

    summary = report["summary"]
    print(
        f"discovered {summary['discovered_issues']} issue(s); "
        f"selected {summary['selected_for_manual_review']} for manual review"
    )
    for repository, count in summary["selected_by_repository"].items():
        print(f"  {repository}: {count}")
    print(f"report: {args.output}")
    return 0


def _run_enrich_timeline(args: argparse.Namespace) -> int:
    client = GitHubClient(
        token=os.environ.get("GITHUB_TOKEN"),
        timeout_seconds=args.timeout,
    )
    try:
        results = enrich_gold_timelines(args.manifest, args.gold_dir, client)
    except (GitHubApiError, ManifestError, ValueError, OSError) as error:
        print(f"timeline enrichment failed: {error}", file=sys.stderr)
        return 1

    for result in results:
        print(
            f"{result.case_id}: {result.event_count} timeline event(s), "
            f"{result.linked_reference_count} linked reference(s)"
        )
    return 0


def _run_enrich_linked_prs(args: argparse.Namespace) -> int:
    client = GitHubClient(
        token=os.environ.get("GITHUB_TOKEN"),
        timeout_seconds=args.timeout,
    )
    try:
        results = enrich_gold_pull_requests(args.manifest, args.gold_dir, client)
    except (GitHubApiError, ManifestError, ValueError, OSError) as error:
        print(f"pull request enrichment failed: {error}", file=sys.stderr)
        return 1

    for result in results:
        print(f"{result.case_id}: {result.pull_request_count} linked pull request(s)")
    return 0


def _run_enrich_pr_commits(args: argparse.Namespace) -> int:
    client = GitHubClient(
        token=os.environ.get("GITHUB_TOKEN"),
        timeout_seconds=args.timeout,
    )
    try:
        results = enrich_gold_pull_request_commits(args.manifest, args.gold_dir, client)
    except (GitHubApiError, ManifestError, ValueError, OSError) as error:
        print(f"pull request commit enrichment failed: {error}", file=sys.stderr)
        return 1

    for result in results:
        print(
            f"{result.case_id}: {result.pull_request_count} pull request(s), "
            f"{result.commit_count} commit(s)"
        )
    return 0


def _run_enrich_linked_commits(args: argparse.Namespace) -> int:
    client = GitHubClient(
        token=os.environ.get("GITHUB_TOKEN"),
        timeout_seconds=args.timeout,
    )
    try:
        results = enrich_gold_direct_commits(args.manifest, args.gold_dir, client)
    except (GitHubApiError, ManifestError, ValueError, OSError) as error:
        print(f"direct commit enrichment failed: {error}", file=sys.stderr)
        return 1

    for result in results:
        print(f"{result.case_id}: {result.commit_count} directly linked commit(s)")
    return 0


def _run_apply_annotations(args: argparse.Namespace) -> int:
    try:
        results = apply_annotations(args.annotations, args.cases_dir)
    except (AnnotationError, OSError) as error:
        print(f"annotation apply failed: {error}", file=sys.stderr)
        return 1
    for result in results:
        print(f"{result.case_id}: annotation applied")
    return 0


def _run_export_annotations(args: argparse.Namespace) -> int:
    try:
        count = export_annotations(
            args.manifest,
            args.cases_dir,
            args.output,
            overwrite=args.force,
        )
    except (AnnotationError, ManifestError, ValueError, OSError) as error:
        print(f"annotation export failed: {error}", file=sys.stderr)
        return 1
    print(f"exported {count} annotation(s) to {args.output}")
    return 0


def _run_gate(args: argparse.Namespace) -> int:
    report = evaluate_cases(
        args.cases_dir,
        args.runtime_results,
        probe_root=args.probe_root,
    )
    write_report(args.report, report)
    summary = report["summary"]
    print(
        "gate: "
        f"{summary['ready_for_execution']} ready for execution, "
        f"{summary['ready_non_executable']} non-executable ready, "
        f"{summary['needs_curation']} need curation, "
        f"{summary['excluded']} excluded; "
        f"{summary['runtime_validated']} runtime validated, "
        f"{summary['runtime_blocked']} runtime blocked"
    )
    print(f"report: {args.report}")
    return 1 if summary["load_errors"] or summary["runtime_invalid"] else 0


def _require_new_report_target(path: Path) -> None:
    if path.exists():
        raise FileExistsError("evaluation report target already exists")


def _run_evaluate_b0(args: argparse.Namespace) -> int:
    try:
        _require_new_report_target(args.report)
        report = evaluate_b0(args.cases_dir, args.split)
        write_new_evaluation_report(args.report, report)
    except (EvaluationError, OSError) as error:
        print(f"B0 evaluation failed: {error}", file=sys.stderr)
        return 1

    summary = report["summary"]
    print(
        "B0 evaluation: "
        f"{summary['case_count']} case(s), "
        f"{summary['model_calls']} model call(s), "
        f"{summary['external_tool_calls']} external tool call(s)"
    )
    for split_name, metrics in report["metrics_by_split"].items():
        print(
            f"  {split_name}: {metrics['case_count']} case(s), "
            f"route={metrics['route_accuracy']:.3f}, "
            f"phase={metrics['fault_phase_accuracy']:.3f}"
        )
    print(f"report: {args.report}")
    return 0


def _run_summarize_trials(args: argparse.Namespace) -> int:
    try:
        summary = summarize_trial_logs(
            args.log_path,
            backup_count=args.backup_count,
        )
    except LiveTrialError as error:
        print(f"trial summary failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _run_build_bot_docs_index(args: argparse.Namespace) -> int:
    try:
        summary = build_bot_docs_index(
            args.source_root,
            args.index,
            replace=args.replace,
        )
    except (BotDocsIndexError, OSError) as error:
        print(f"bot-docs index build failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _run_search_bot_docs(args: argparse.Namespace) -> int:
    try:
        index = BotDocsIndex(args.index)
        hits = index.search(args.query, limit=args.limit, strategy=args.strategy)
    except (BotDocsIndexError, OSError) as error:
        print(f"bot-docs search failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "retriever_id": index.metadata()["retriever_id"],
                "strategy": args.strategy,
                "query": args.query,
                "hits": [hit.to_dict() for hit in hits],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _run_evaluate_bot_docs_retrieval(args: argparse.Namespace) -> int:
    try:
        _require_new_report_target(args.report)
        report = evaluate_bot_docs_retrieval(args.index, args.fixtures)
        write_new_evaluation_report(args.report, report)
    except (BotDocsEvaluationError, BotDocsIndexError, OSError) as error:
        print(f"bot-docs retrieval evaluation failed: {error}", file=sys.stderr)
        return 1
    hybrid = report["metrics_by_strategy"]["hybrid"]
    print(
        "bot-docs retrieval evaluation: "
        f"{report['summary']['case_count']} case(s), "
        f"recall@5={hybrid['recall_at_5']:.3f}, "
        f"mrr={hybrid['mrr']:.3f}, "
        f"gate={report['quality_gate']['status']}"
    )
    print(f"report: {args.report}")
    return 0 if report["quality_gate"]["status"] == "passed" else 1


def _run_evaluate_s3(args: argparse.Namespace) -> int:
    try:
        _require_new_report_target(args.report)
        report = asyncio.run(evaluate_s3(args.fixtures))
        write_new_evaluation_report(args.report, report)
    except (SafetyEvaluationError, OSError) as error:
        print(f"S3 evaluation failed: {error}", file=sys.stderr)
        return 1

    summary = report["summary"]
    metrics = report["metrics"]
    print(
        "S3 evaluation: "
        f"{summary['case_count']} synthetic case(s), "
        f"{summary['model_calls']} model call(s), "
        f"{summary['external_tool_calls']} external tool call(s)"
    )
    print(
        "  B0 frozen: "
        f"route={metrics['b0_frozen']['route_accuracy']:.3f}; "
        "B1 pre-model guard: "
        f"route={metrics['b1_pre_model_guard']['route_accuracy']:.3f}"
    )
    print(f"report: {args.report}")
    return 0


def _run_evaluate_b3_evidence_policy(args: argparse.Namespace) -> int:
    try:
        _require_new_report_target(args.report)
        report = evaluate_b3_evidence_policy(args.prediction_report)
        write_new_evaluation_report(args.report, report)
    except (EvidencePolicyError, EvidencePolicyEvaluationError, OSError) as error:
        print(f"B3 evidence policy evaluation failed: {error}", file=sys.stderr)
        return 1

    summary = report["summary"]
    metrics = report["metrics"]
    print(
        "B3 evidence policy evaluation: "
        f"{summary['case_count']} validation case(s), "
        f"{summary['proposed_question_count']} question(s), "
        f"{summary['model_calls']} model call(s), "
        f"{summary['external_tool_calls']} external tool call(s)"
    )
    print(
        "  B1 missing-evidence precision: "
        f"{metrics['b1_missing_evidence_micro']['precision']:.3f}; "
        "B3 question precision@1: "
        f"{metrics['question_precision_at_1']['rate']:.3f}"
    )
    print(f"report: {args.report}")
    return 0


def _run_evaluate_b3_evidence_receipts(args: argparse.Namespace) -> int:
    try:
        _require_new_report_target(args.report)
        report = evaluate_b3_evidence_receipts(args.fixtures)
        write_new_evaluation_report(args.report, report)
    except (EvidenceReceiptEvaluationError, OSError) as error:
        print(f"B3 evidence receipt evaluation failed: {error}", file=sys.stderr)
        return 1

    summary = report["summary"]
    metrics = report["metrics"]
    print(
        "B3 evidence receipt evaluation: "
        f"{summary['case_count']} synthetic case(s), "
        f"accuracy={metrics['decision_accuracy']:.3f}, "
        f"{summary['model_calls']} model call(s), "
        f"{summary['external_tool_calls']} external tool call(s), "
        f"gate={report['quality_gate']['status']}"
    )
    print(f"report: {args.report}")
    return 0 if report["quality_gate"]["status"] == "passed" else 1


def _run_evaluate_answer_quality(args: argparse.Namespace) -> int:
    try:
        _require_new_report_target(args.report)
        report = evaluate_answer_quality(
            args.rubric,
            args.fixtures,
            args.annotations,
            source_report_path=args.source_report,
        )
        write_new_evaluation_report(args.report, report)
    except (AnswerQualityEvaluationError, OSError) as error:
        print(f"answer quality evaluation failed: {error}", file=sys.stderr)
        return 1

    summary = report["summary"]
    calibration = report["calibration_gate"]
    quality_claim = report["quality_claim_gate"]
    print(
        "answer quality rubric evaluation: "
        f"{summary['sample_count']} sample(s), "
        f"calibration={'passed' if calibration['passed'] else 'failed'}, "
        f"human_reviewed={summary['human_reviewed']}, "
        f"quality_claim={quality_claim['decision']}"
    )
    print(f"report: {args.report}")
    if summary["purpose"] == "rubric_calibration":
        return 0 if calibration["passed"] else 1
    return 0 if quality_claim["eligible"] else 1


def _run_export_answer_quality_review(args: argparse.Namespace) -> int:
    samples_path = args.output_dir / "samples.json"
    annotations_path = args.output_dir / "annotations.draft.json"
    existing = [path for path in (samples_path, annotations_path) if path.exists()]
    if existing:
        print(
            "answer quality review export failed: output already exists: "
            + ", ".join(str(path) for path in existing),
            file=sys.stderr,
        )
        return 1
    try:
        samples, annotations = build_b4_answer_quality_review(
            args.evaluation_report,
            args.fixtures,
            args.split,
            args.rubric,
        )
        write_new_evaluation_report(samples_path, samples)
        write_new_evaluation_report(annotations_path, annotations)
    except (AnswerReviewExportError, OSError) as error:
        print(f"answer quality review export failed: {error}", file=sys.stderr)
        return 1

    print(
        "answer quality review export: "
        f"{len(samples['fixtures'])} forward_hidden candidate(s), "
        f"scope={samples['evaluation_scope']}"
    )
    print(f"samples: {samples_path}")
    print(f"annotations: {annotations_path}")
    return 0


def _run_evaluate_b4_scripted(args: argparse.Namespace) -> int:
    try:
        _require_new_report_target(args.report)
        report = asyncio.run(evaluate_b4_scripted_fixtures(args.fixtures, args.split))
        write_new_evaluation_report(args.report, report)
    except (AgentEvaluationError, OSError) as error:
        print(f"B4 scripted evaluation failed: {error}", file=sys.stderr)
        return 1

    summary = report["summary"]
    metrics = report["metrics"]["b4"]
    gate = report["promotion_gate"]
    print(
        "B4 scripted evaluation: "
        f"{summary['fixture_count']} fixture(s), "
        f"{summary['trial_count']} trial(s), "
        f"task_success={metrics['task_success_rate']:.3f}, "
        f"safety_violation={metrics['safety_violation_rate']:.3f}, "
        f"real provider request(s)={summary['real_provider_requests']}"
    )
    print(f"  promotion: {gate['decision']}")
    print(f"report: {args.report}")
    return 0


def _run_evaluate_b4_real(args: argparse.Namespace) -> int:
    if not args.confirm_paid_run:
        print(
            "B4 real evaluation not started: pass --confirm-paid-run after confirming "
            "the exact backend/model, synthetic data egress, request/token limits, and budget.",
            file=sys.stderr,
        )
        return 2

    provider_config = {
        "openai-responses": {
            "api_key_env": "OPENAI_API_KEY",
            "module": "nbtriage.openai_adapter",
            "b1_symbol": "create_openai_responses_b1_client",
            "agent_symbol": "create_openai_responses_agent_step_client",
            "install_hint": (
                "install the 'model-openai' extra: "
                'pip install "nonebot-plugin-triage[model-openai]"'
            ),
        },
        "anthropic-messages": {
            "api_key_env": "ANTHROPIC_API_KEY",
            "module": "nbtriage.anthropic_adapter",
            "b1_symbol": "create_anthropic_messages_b1_client",
            "agent_symbol": "create_anthropic_messages_agent_step_client",
            "install_hint": (
                "install the 'model-anthropic' extra: "
                'pip install "nonebot-plugin-triage[model-anthropic]"'
            ),
        },
        "deepseek-responses": {
            "api_key_env": "DEEPSEEK_API_KEY",
            "module": "tools.nbtriage_maintainer.deepseek_adapter",
            "b1_symbol": "create_deepseek_responses_b1_client",
            "agent_symbol": "create_deepseek_responses_agent_step_client",
            "install_hint": "run 'uv sync --group maintainer' from the repository",
        },
    }[args.backend]
    api_key = os.environ.get(provider_config["api_key_env"])
    if not api_key:
        print(
            f"B4 real evaluation failed: {provider_config['api_key_env']} is not set",
            file=sys.stderr,
        )
        return 1

    partial_report_path = b4_real_partial_report_path(args.report)
    if args.report.name.lower().endswith(".partial.json"):
        print(
            "B4 real evaluation not started: report path uses the reserved "
            "'.partial.json' audit suffix",
            file=sys.stderr,
        )
        return 1
    if args.report.exists() or partial_report_path.exists():
        print(
            "B4 real evaluation not started: report and partial audit paths must be new",
            file=sys.stderr,
        )
        return 1

    try:
        partial_audit = RealGatePartialAudit.create(
            partial_report_path,
            provider=args.backend,
            model=args.model,
            trials_per_fixture=args.trials_per_fixture,
            max_provider_requests=args.max_provider_requests,
            max_agent_input_tokens_per_trial=args.max_agent_input_tokens_per_trial,
            max_output_tokens_per_trial=args.max_output_tokens_per_trial,
            deadline_seconds=args.deadline_seconds,
            whole_run_timeout_seconds=args.whole_run_timeout_seconds,
            declared_budget_usd=args.declared_budget_usd,
            paid_run_confirmed=args.confirm_paid_run,
            synthetic_data_egress_confirmed=args.confirm_paid_run,
        )
    except OSError:
        print(
            "B4 real evaluation not started: partial audit could not be created",
            file=sys.stderr,
        )
        return 1

    failure_stage = "preflight"
    try:
        create_b1_client = _load_model_symbol(
            provider_config["module"],
            provider_config["b1_symbol"],
            install_hint=provider_config["install_hint"],
        )
        create_agent_client = _load_model_symbol(
            provider_config["module"],
            provider_config["agent_symbol"],
            install_hint=provider_config["install_hint"],
        )
        failure_stage = "audit_checkpoint"
        report = asyncio.run(
            asyncio.wait_for(
                evaluate_b4_real_fixtures(
                    args.fixtures,
                    args.split,
                    b1_client_factory=lambda: create_b1_client(
                        api_key=api_key,
                        model=args.model,
                        timeout_seconds=args.timeout,
                        max_calls=1,
                    ),
                    agent_client_factory=lambda: create_agent_client(
                        api_key=api_key,
                        model=args.model,
                        timeout_seconds=args.timeout,
                        max_calls=1,
                    ),
                    provider=args.backend,
                    model=args.model,
                    trials_per_fixture=args.trials_per_fixture,
                    max_provider_requests=args.max_provider_requests,
                    max_agent_input_tokens_per_trial=(args.max_agent_input_tokens_per_trial),
                    max_output_tokens_per_trial=args.max_output_tokens_per_trial,
                    deadline_seconds=args.deadline_seconds,
                    declared_budget_usd=args.declared_budget_usd,
                    paid_run_confirmed=args.confirm_paid_run,
                    synthetic_data_egress_confirmed=args.confirm_paid_run,
                    partial_audit=partial_audit,
                ),
                timeout=args.whole_run_timeout_seconds,
            )
        )
        failure_stage = "report_write"
        partial_audit.mark_report_ready()
        write_new_evaluation_report(args.report, report)
    except Exception as error:
        failure_code = _real_gate_failure_code(error, partial_audit)
        if failure_stage == "audit_checkpoint":
            failure_stage = partial_audit.current_stage
            if isinstance(error, OSError) and not isinstance(error, TimeoutError):
                failure_stage = "audit_checkpoint"
        with suppress(AgentEvaluationError, OSError):
            partial_audit.abort(code=failure_code, stage=failure_stage)
        print(
            f"B4 real evaluation aborted: code={failure_code}; partial_audit={partial_report_path}",
            file=sys.stderr,
        )
        return 1

    audit_finalization_warning = False
    try:
        partial_audit.complete()
    except OSError:
        audit_finalization_warning = True

    summary = report["summary"]
    metrics = report["metrics"]["b4"]
    gate = report["promotion_gate"]
    print(
        "B4 real evaluation: "
        f"{summary['fixture_count']} fixture(s), "
        f"{summary['trial_count']} trial(s), "
        f"{summary['real_provider_requests']} provider request(s), "
        f"task_success={metrics['task_success_rate']:.3f}, "
        f"cost_microusd={summary['cost_microusd']}"
    )
    print(f"  promotion: {gate['decision']}")
    print(f"report: {args.report}")
    if audit_finalization_warning:
        print(
            "warning: report succeeded but partial audit remains report_ready: "
            f"{partial_report_path}",
            file=sys.stderr,
        )
    return 0


def _real_gate_failure_code(
    error: Exception,
    partial_audit: RealGatePartialAudit,
) -> str:
    if isinstance(error, TimeoutError):
        return "deadline"
    if isinstance(error, OSError):
        return "local_io_error"
    unknown_reason = partial_audit.last_unknown_reason
    if unknown_reason == "deadline":
        return "deadline"
    if unknown_reason == "cancelled":
        return "cancelled"
    if unknown_reason == "provider_error":
        return "provider_request_failed"
    if unknown_reason == "local_error":
        return "unexpected_error"
    if not partial_audit.cost_known:
        return "cost_unknown"
    declared_budget = partial_audit.payload["authorization"]["declared_budget_usd"]
    if partial_audit.known_cost_microusd > round(declared_budget * 1_000_000):
        return "cost_limit"
    if isinstance(error, B1ProviderError):
        return "provider_request_failed"
    if isinstance(error, B1Error):
        return "b1_error"
    if isinstance(error, AgentEvaluationError):
        return "evaluation_error"
    return "unexpected_error"


def _run_evaluate_b1_openai(args: argparse.Namespace) -> int:
    if not args.confirm_paid_run:
        print(
            "B1 evaluation not started: pass --confirm-paid-run after confirming "
            "the exact model and budget.",
            file=sys.stderr,
        )
        return 2
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("B1 evaluation failed: OPENAI_API_KEY is not set", file=sys.stderr)
        return 1
    try:
        _require_new_report_target(args.report)
        create_client = _load_model_symbol(
            "nbtriage.openai_adapter",
            "create_openai_responses_b1_client",
        )
        client = create_client(
            api_key=api_key,
            model=args.model,
            timeout_seconds=args.timeout,
            max_calls=args.max_model_calls,
        )
        report = asyncio.run(
            evaluate_b1(
                args.cases_dir,
                args.split,
                client=client,
                provider="openai-responses",
                model=args.model,
                generation_config={"max_output_tokens": args.max_output_tokens},
                cache_dir=args.cache_dir,
                score_splits=(args.score_split,),
                declared_budget_usd=args.declared_budget_usd,
            )
        )
        write_new_evaluation_report(args.report, report)
    except (EvaluationError, B1Error, B1ProviderError, OSError) as error:
        print(f"B1 evaluation failed: {error}", file=sys.stderr)
        return 1

    summary = report["summary"]
    print(
        "B1 evaluation: "
        f"{summary['case_count']} case(s), "
        f"{summary['model_calls']} model call(s), "
        f"{summary['cache_hits']} cache hit(s), "
        f"{summary['provider_response_count']} provider response(s), "
        f"{summary['input_tokens']} input token(s), "
        f"{summary['output_tokens']} output token(s)"
    )
    for split_name, metrics in report["metrics_by_split"].items():
        print(
            f"  {split_name}: {metrics['case_count']} case(s), "
            f"route={metrics['route_accuracy']:.3f}, "
            f"phase={metrics['fault_phase_accuracy']:.3f}"
        )
    print(f"report: {args.report}")
    return 0


def _run_evaluate_b1_deepseek(args: argparse.Namespace) -> int:
    if not args.confirm_paid_run:
        print(
            "B1 evaluation not started: pass --confirm-paid-run after confirming "
            "the exact model and budget.",
            file=sys.stderr,
        )
        return 2
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print(
            "B1 evaluation failed: DEEPSEEK_API_KEY is not set",
            file=sys.stderr,
        )
        return 1
    try:
        _require_new_report_target(args.report)
        client_type = _load_model_symbol(
            "tools.nbtriage_maintainer.providers",
            "DeepSeekResponsesB1Client",
            install_hint="run 'uv sync --group maintainer' from the repository",
        )
        client = client_type(
            api_key=api_key,
            timeout_seconds=args.timeout,
            max_calls=args.max_model_calls,
        )
        report = asyncio.run(
            evaluate_b1(
                args.cases_dir,
                args.split,
                client=client,
                provider="deepseek-responses",
                model=args.model,
                generation_config={
                    "max_output_tokens": args.max_output_tokens,
                    "reasoning_effort": "none",
                    "temperature": 0,
                },
                cache_dir=args.cache_dir,
                score_splits=(args.score_split,),
                declared_budget_usd=args.declared_budget_usd,
            )
        )
        write_new_evaluation_report(args.report, report)
    except (EvaluationError, B1Error, B1ProviderError, OSError) as error:
        print(f"B1 evaluation failed: {error}", file=sys.stderr)
        return 1

    summary = report["summary"]
    print(
        "B1 evaluation: "
        f"{summary['case_count']} case(s), "
        f"{summary['model_calls']} model call(s), "
        f"{summary['cache_hits']} cache hit(s), "
        f"{summary['provider_response_count']} provider response(s), "
        f"{summary['input_tokens']} input token(s), "
        f"{summary['output_tokens']} output token(s)"
    )
    for split_name, metrics in report["metrics_by_split"].items():
        print(
            f"  {split_name}: {metrics['case_count']} case(s), "
            f"route={metrics['route_accuracy']:.3f}, "
            f"phase={metrics['fault_phase_accuracy']:.3f}"
        )
    print(f"report: {args.report}")
    return 0


def _load_model_symbol(
    module_name: str,
    symbol_name: str,
    *,
    install_hint: str = (
        "install the 'model-openai' extra: pip install \"nonebot-plugin-triage[model-openai]\""
    ),
) -> Any:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        missing_name = error.name or ""
        if missing_name in {"openai", "anthropic", "pydantic_ai"} or missing_name.startswith(
            "pydantic_ai."
        ):
            raise B1ProviderError(f"model support is not installed; {install_hint}") from error
        raise
    return getattr(module, symbol_name)


def _run_session_create(args: argparse.Namespace) -> int:
    store = FileSessionStore(args.sessions_dir)
    try:
        session = create_session_from_report(
            args.prediction_report,
            args.case_id,
            session_id=args.session_id,
        )
        path = store.create(session)
    except SessionError as error:
        print(f"session creation failed: {error}", file=sys.stderr)
        return 1
    _print_session_summary(session)
    print(f"session: {path}")
    return 0


def _run_session_approve(args: argparse.Namespace) -> int:
    store = FileSessionStore(args.sessions_dir)
    try:
        session = store.load(args.session_id)
        updated = approve_session(session, args.actor)
        path = store.update(updated)
    except SessionError as error:
        print(f"session approval failed: {error}", file=sys.stderr)
        return 1
    _print_session_summary(updated)
    print(f"session: {path}")
    return 0


def _run_session_attach_runtime(args: argparse.Namespace) -> int:
    store = FileSessionStore(args.sessions_dir)
    try:
        session = store.load(args.session_id)
        case = _load_session_case(args.cases_dir, session.case_id)
        assessments, load_errors = evaluate_runtime_results(
            args.runtime_results,
            {session.case_id: case},
            probe_root=args.probe_root,
        )
        if load_errors:
            raise SessionError(f"runtime result load failed: {load_errors[0]}")
        matches = [item for item in assessments if item.case_id == session.case_id]
        if len(matches) != 1:
            raise SessionError(
                f"expected exactly one runtime result for {session.case_id}, found {len(matches)}"
            )
        updated = attach_runtime_assessment(session, matches[0], actor=args.actor)
        path = store.update(updated)
    except SessionError as error:
        print(f"runtime result attachment failed: {error}", file=sys.stderr)
        return 1
    _print_session_summary(updated)
    print(f"session: {path}")
    return 0


def _run_session_attach_evidence(args: argparse.Namespace) -> int:
    store = FileSessionStore(args.sessions_dir)
    try:
        session = store.load(args.session_id)
        receipt = load_evidence_receipt(args.receipt)
        updated = attach_evidence_receipt(session, receipt)
        path = store.update(updated)
    except (EvidenceReceiptError, SessionError) as error:
        print(f"evidence receipt attachment failed: {error}", file=sys.stderr)
        return 1
    _print_session_summary(updated)
    print(f"session: {path}")
    return 0


def _run_session_show(args: argparse.Namespace) -> int:
    try:
        session = FileSessionStore(args.sessions_dir).load(args.session_id)
    except SessionError as error:
        print(f"session lookup failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(session.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _load_session_case(cases_dir: Path, case_id: str) -> dict[str, Any]:
    resolved_case_id = validate_case_id(case_id)
    try:
        resolved_cases_dir = cases_dir.resolve()
        path = (resolved_cases_dir / f"{resolved_case_id}.json").resolve()
    except OSError as error:
        raise SessionError("failed to resolve SupportCase path") from error
    if path.parent != resolved_cases_dir:
        raise SessionError("SupportCase path escapes cases directory")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SessionError(f"failed to load SupportCase {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("case_id") != resolved_case_id:
        raise SessionError(f"invalid SupportCase for session: {path}")
    return payload


def _print_session_summary(session: SupportSession) -> None:
    print(
        f"support session: {session.session_id}, case={session.case_id}, "
        f"route={session.route}, action={session.action.kind}, status={session.status}"
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed
