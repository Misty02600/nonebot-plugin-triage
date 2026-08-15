"""从官方 GitHub ZIP 取得固定 revision 的 NoneBot 2.5 文档。"""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

from tools.nbtriage_maintainer.knowledge_pack.models import KnowledgePackError

NONEBOT_RELEASE_REVISIONS = {
    "2.5.0": "c0f9a494bcd308c2ec530ba60c167f9145d21aec",
}
NONEBOT_DOCUMENT_COUNT = 92
NONEBOT_API_DOCUMENT_COUNT = 28
NONEBOT_API_SENTINELS = frozenset(
    {
        PurePosixPath("api/index.md"),
        PurePosixPath("api/matcher.md"),
        PurePosixPath("api/params.md"),
        PurePosixPath("api/permission.md"),
        PurePosixPath("api/plugin/on.md"),
        PurePosixPath("api/rule.md"),
    }
)


def acquire_nonebot_snapshot(
    *,
    version: str,
    output: Path,
) -> dict[str, object]:
    revision = NONEBOT_RELEASE_REVISIONS.get(version)
    if revision is None:
        raise KnowledgePackError(f"unsupported NoneBot documentation version: {version}")
    if output.exists() and not output.is_dir():
        raise KnowledgePackError(f"NoneBot snapshot root must be a directory: {output}")

    target_root = output / "nonebot2"
    if target_root.exists() and (not target_root.is_dir() or any(target_root.iterdir())):
        raise KnowledgePackError(f"NoneBot snapshot target must be empty: {target_root}")

    archive_url = f"https://github.com/nonebot/nonebot2/archive/{revision}.zip"
    archive = _download_archive(archive_url)
    docs, sidebar = _read_snapshot(archive, version, revision)

    target_root.mkdir(parents=True, exist_ok=True)
    for relative_path, content in docs.items():
        target = target_root.joinpath(*relative_path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (target_root / f"version-{version}-sidebars.json").write_bytes(sidebar)

    return {
        "version": version,
        "revision": revision,
        "source_url": (
            f"https://github.com/nonebot/nonebot2/tree/{revision}/"
            f"website/versioned_docs/version-{version}"
        ),
        "document_count": len(docs),
        "api_document_count": sum(path.parts[0] == "api" for path in docs),
    }


def _download_archive(url: str) -> bytes:
    try:
        request = Request(url, headers={"User-Agent": "nonebot-plugin-triage"})
        with urlopen(request, timeout=60) as response:
            return response.read()
    except OSError as error:
        raise KnowledgePackError("failed to download approved NoneBot documentation") from error


def _read_snapshot(
    archive: bytes,
    version: str,
    revision: str,
) -> tuple[dict[PurePosixPath, bytes], bytes]:
    repository_root = f"nonebot2-{revision}/"
    docs_prefix = f"{repository_root}website/versioned_docs/version-{version}/"
    sidebar_name = f"{repository_root}website/versioned_sidebars/version-{version}-sidebars.json"
    docs: dict[PurePosixPath, bytes] = {}
    sidebar: bytes | None = None
    seen_targets: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            for entry in bundle.infolist():
                if entry.is_dir():
                    continue
                if entry.filename.startswith(docs_prefix):
                    relative_path = _safe_relative_path(entry.filename[len(docs_prefix) :])
                    if relative_path.suffix.casefold() not in {".md", ".mdx"}:
                        continue
                    target_key = relative_path.as_posix().casefold()
                    if target_key in seen_targets:
                        raise KnowledgePackError(
                            f"duplicate NoneBot archive path: {relative_path.as_posix()}"
                        )
                    seen_targets.add(target_key)
                    docs[relative_path] = bundle.read(entry)
                elif entry.filename == sidebar_name:
                    if sidebar is not None:
                        raise KnowledgePackError("duplicate NoneBot sidebar in archive")
                    sidebar = bundle.read(entry)
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise KnowledgePackError("invalid NoneBot documentation archive") from error

    if sidebar is None:
        raise KnowledgePackError("NoneBot archive has no versioned sidebar")
    _validate_sidebar(sidebar)
    _validate_document_inventory(docs)
    return docs, sidebar


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
        raise KnowledgePackError(f"unsafe NoneBot archive path: {value!r}")
    return path


def _validate_sidebar(raw: bytes) -> None:
    try:
        sidebar = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnowledgePackError("NoneBot versioned sidebar is invalid") from error
    if not isinstance(sidebar, dict) or not all(
        isinstance(sidebar.get(section), list) for section in ("tutorial", "api")
    ):
        raise KnowledgePackError("NoneBot versioned sidebar is incomplete")


def _validate_document_inventory(docs: dict[PurePosixPath, bytes]) -> None:
    api_count = sum(path.parts[0] == "api" for path in docs)
    if len(docs) != NONEBOT_DOCUMENT_COUNT:
        raise KnowledgePackError(
            f"NoneBot archive has {len(docs)} documents; expected {NONEBOT_DOCUMENT_COUNT}"
        )
    if api_count != NONEBOT_API_DOCUMENT_COUNT:
        raise KnowledgePackError(
            f"NoneBot archive has {api_count} API documents; expected {NONEBOT_API_DOCUMENT_COUNT}"
        )
    missing = NONEBOT_API_SENTINELS.difference(docs)
    if missing:
        rendered = ", ".join(path.as_posix() for path in sorted(missing))
        raise KnowledgePackError(f"NoneBot archive is missing key API documents: {rendered}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acquire an exact NoneBot public documentation snapshot."
    )
    parser.add_argument("--version", default="2.5.0")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata-out", type=Path, required=True)
    args = parser.parse_args()
    metadata = acquire_nonebot_snapshot(version=args.version, output=args.output)
    args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_out.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
