"""从官方 Git 仓库取得固定 revision 的 NapCat 资料。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tools.nbtriage_maintainer.knowledge_pack.models import KnowledgePackError

NAPCAT_SOURCE_REPOSITORY = "https://github.com/NapNeko/NapCatQQ.git"
NAPCAT_DOCS_REPOSITORY = "https://github.com/NapNeko/NapCatDocs.git"


def acquire_napcat_snapshot(
    *,
    version: str,
    docs_revision: str,
    output: Path,
    source_checkout: Path | None = None,
) -> dict[str, Any]:
    normalized = version.removeprefix("v")
    if not _is_version(normalized):
        raise KnowledgePackError("NapCat version must be an exact numeric release")
    if not _is_git_revision(docs_revision):
        raise KnowledgePackError("NapCat docs revision must be a full Git commit")
    if output.exists() and any(output.iterdir()):
        raise KnowledgePackError(f"NapCat snapshot output must be empty: {output}")

    with tempfile.TemporaryDirectory(prefix="nbtriage-napcat-") as temporary:
        staging = Path(temporary)
        source = (
            _validated_source_checkout(source_checkout, normalized)
            if source_checkout is not None
            else staging / "source"
        )
        docs = staging / "docs"
        if source_checkout is None:
            _clone(
                NAPCAT_SOURCE_REPOSITORY,
                f"v{normalized}",
                source,
                sparse_paths=("packages",),
            )
        _clone(
            NAPCAT_DOCS_REPOSITORY,
            docs_revision,
            docs,
            sparse_paths=("src/guide", "src/develop", f"src/api/{normalized}"),
        )
        source_revision = _git(source, "rev-parse", "HEAD")
        resolved_docs_revision = _git(docs, "rev-parse", "HEAD")
        openapi = docs / "src" / "api" / normalized / "openapi.json"
        _validate_openapi(openapi, normalized)

        output.mkdir(parents=True, exist_ok=True)
        _copy_tree(source / "packages", output / "napcat-source" / "packages")
        for name in ("guide", "develop"):
            candidate = docs / "src" / name
            if candidate.is_dir():
                _copy_tree(candidate, output / "napcat-docs" / "src" / name)
        api_target = output / "napcat-docs" / "src" / "api" / normalized
        api_target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(openapi, api_target / "openapi.json")

    return {
        "version": normalized,
        "source_revision": source_revision,
        "docs_revision": resolved_docs_revision,
        "source_url": f"https://github.com/NapNeko/NapCatQQ/tree/v{normalized}",
        "docs_url": f"https://github.com/NapNeko/NapCatDocs/tree/{resolved_docs_revision}",
    }


def _clone(
    repository: str,
    revision: str,
    target: Path,
    *,
    sparse_paths: tuple[str, ...],
) -> None:
    try:
        subprocess.run(
            ["git", "init", "--quiet", str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin", repository],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "sparse-checkout", "init", "--cone"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "sparse-checkout", "set", *sparse_paths],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "fetch",
                "--depth",
                "1",
                "--filter=blob:none",
                "origin",
                revision,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "checkout", "--detach", "FETCH_HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise KnowledgePackError(f"failed to clone approved NapCat source {revision}") from error


def _git(repository: Path, *arguments: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise KnowledgePackError("failed to inspect acquired NapCat source") from error


def _validated_source_checkout(path: Path, version: str) -> Path:
    try:
        checkout = path.resolve(strict=True)
    except OSError as error:
        raise KnowledgePackError("NapCat source checkout is unavailable") from error
    if not (checkout / "packages").is_dir():
        raise KnowledgePackError("NapCat source checkout has no packages directory")
    expected_commit = _git(checkout, "rev-list", "-n", "1", f"v{version}")
    actual_commit = _git(checkout, "rev-parse", "HEAD")
    if actual_commit != expected_commit:
        raise KnowledgePackError(
            f"NapCat source checkout is not at tag v{version}: {actual_commit}"
        )
    if _git(checkout, "status", "--porcelain", "--", "packages"):
        raise KnowledgePackError("NapCat source checkout packages directory is dirty")
    return checkout


def _validate_openapi(path: Path, version: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnowledgePackError(f"NapCat {version} has no valid versioned OpenAPI") from error
    info = payload.get("info") if isinstance(payload, dict) else None
    declared = info.get("version") if isinstance(info, dict) else None
    if declared != version:
        raise KnowledgePackError(
            f"NapCat OpenAPI version conflict: directory={version}, info.version={declared!r}"
        )
    if not isinstance(payload.get("paths"), dict) or not payload["paths"]:
        raise KnowledgePackError(f"NapCat {version} OpenAPI has no paths")


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise KnowledgePackError(f"approved NapCat source subtree is unavailable: {source.name}")
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("node_modules", "dist", ".git"),
    )


def _is_version(value: str) -> bool:
    return bool(value) and all(part.isdigit() for part in value.split("."))


def _is_git_revision(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acquire an exact NapCat public knowledge snapshot."
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--docs-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata-out", type=Path, required=True)
    parser.add_argument("--source-checkout", type=Path)
    args = parser.parse_args()
    metadata = acquire_napcat_snapshot(
        version=args.version,
        docs_revision=args.docs_revision,
        output=args.output,
        source_checkout=args.source_checkout,
    )
    args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_out.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
