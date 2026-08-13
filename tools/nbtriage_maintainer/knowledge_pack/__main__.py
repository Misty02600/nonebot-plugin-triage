"""公共知识包维护 PoC 的独立命令入口。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .builder import DEFAULT_KNOWLEDGE_INDEX_PATH, build_knowledge_index
from .evaluation import evaluate_knowledge_retrieval
from .models import KnowledgePackError
from .packaging import package_knowledge_index, verify_knowledge_archive
from .search import KnowledgeIndex
from .write_policy import write_snapshot_policy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nbtriage-knowledge-pack",
        description="Build, search, and evaluate a local public knowledge index.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Build a fresh SQLite FTS5 index.")
    build.add_argument("--snapshot-root", type=Path, required=True)
    build.add_argument("--sources", type=Path, required=True)
    build.add_argument("--index", type=Path, default=DEFAULT_KNOWLEDGE_INDEX_PATH)
    build.add_argument("--replace", action="store_true")

    search = commands.add_parser("search", help="Search the local index without network calls.")
    search.add_argument("query")
    search.add_argument("--component", required=True)
    search.add_argument("--version")
    search.add_argument("--index", type=Path, default=DEFAULT_KNOWLEDGE_INDEX_PATH)
    search.add_argument("--limit", type=_positive_int, default=5)

    evaluate = commands.add_parser("evaluate", help="Run a deterministic retrieval fixture.")
    evaluate.add_argument("--fixtures", type=Path, required=True)
    evaluate.add_argument("--index", type=Path, default=DEFAULT_KNOWLEDGE_INDEX_PATH)
    evaluate.add_argument("--report", type=Path)

    prepare = commands.add_parser(
        "prepare-policy",
        help="Bind a small source inventory to the exact contents of a local snapshot.",
    )
    prepare.add_argument("--inventory", type=Path, required=True)
    prepare.add_argument("--snapshot-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)

    package = commands.add_parser("package", help="Package a reviewed index for distribution.")
    package.add_argument("--index", type=Path, default=DEFAULT_KNOWLEDGE_INDEX_PATH)
    package.add_argument("--output", type=Path, required=True)
    package.add_argument("--version", required=True)

    verify_package = commands.add_parser(
        "verify-package",
        help="Verify a release candidate against its tag commit.",
    )
    verify_package.add_argument("--archive", type=Path, required=True)
    verify_package.add_argument("--checksum", type=Path, required=True)
    verify_package.add_argument("--version", required=True)
    verify_package.add_argument("--project-revision", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_knowledge_index(
                args.snapshot_root,
                args.sources,
                args.index,
                replace=args.replace,
            ).to_dict()
        elif args.command == "search":
            hits = KnowledgeIndex(args.index).search(
                args.query,
                component=args.component,
                version=args.version,
                limit=args.limit,
            )
            result = {"query": args.query, "hits": [hit.to_dict() for hit in hits]}
        elif args.command == "evaluate":
            result = evaluate_knowledge_retrieval(args.index, args.fixtures)
            if args.report is not None:
                if args.report.exists():
                    raise KnowledgePackError(f"knowledge report already exists: {args.report}")
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        elif args.command == "prepare-policy":
            policy_path = write_snapshot_policy(
                args.inventory,
                args.snapshot_root,
                args.output,
            )
            result = {"policy": policy_path.resolve().as_posix()}
        elif args.command == "package":
            result = package_knowledge_index(args.index, args.output, args.version)
        else:
            result = verify_knowledge_archive(
                args.archive,
                args.checksum,
                args.version,
                args.project_revision,
            )
    except (KnowledgePackError, OSError) as error:
        print(f"knowledge pack command failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
