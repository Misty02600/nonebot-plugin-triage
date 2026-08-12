"""离线评测输入与代码身份的稳定摘要。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path

EVALUATION_CODE_REVISION_PREFIX = "nbtriage-source-sha256:"

_CASE_CORPUS_DOMAIN = b"nbtriage-case-corpus-v1\0"
_SOURCE_ROOTS = (Path("src/nbtriage"), Path("tools/nbtriage_maintainer"))


class EvaluationProvenanceError(ValueError):
    pass


def case_corpus_sha256(
    case_raw_by_id: Mapping[str, bytes],
    case_ids: Iterable[str],
) -> str:
    """计算实际评测 Case 集合的稳定摘要。

    摘要绑定排序后的 Case ID 和每个文件的原始字节 SHA-256，不绑定绝对路径、目录枚举顺序或未使用文件。
    """
    normalized_case_ids = sorted(set(case_ids))
    digest = hashlib.sha256(_CASE_CORPUS_DOMAIN)
    for case_id in normalized_case_ids:
        raw = case_raw_by_id.get(case_id)
        if raw is None:
            raise EvaluationProvenanceError(f"case corpus is missing raw bytes for {case_id}")
        digest.update(case_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def evaluation_code_revision(repository_root: Path) -> str:
    """计算领域核心和维护者工具两棵 Python 源码树的稳定 revision。"""
    normalized_root = repository_root.resolve()
    digest = hashlib.sha256()
    try:
        source_paths: list[Path] = []
        for relative_root in _SOURCE_ROOTS:
            source_root = normalized_root / relative_root
            if not source_root.is_dir():
                raise EvaluationProvenanceError(
                    f"evaluation source directory is unavailable: {source_root}"
                )
            tree_paths = list(source_root.rglob("*.py"))
            if not tree_paths:
                raise EvaluationProvenanceError(
                    f"evaluation source directory contains no Python files: {source_root}"
                )
            source_paths.extend(tree_paths)

        ordered_paths = sorted(
            source_paths,
            key=lambda path: path.relative_to(normalized_root).as_posix(),
        )
        for source_path in ordered_paths:
            relative_path = source_path.relative_to(normalized_root).as_posix()
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(source_path.read_bytes())
            digest.update(b"\0")
    except EvaluationProvenanceError:
        raise
    except (OSError, ValueError) as error:
        raise EvaluationProvenanceError(
            f"failed to hash evaluation source closure: {error}"
        ) from error
    return f"{EVALUATION_CODE_REVISION_PREFIX}{digest.hexdigest()}"
