"""仓库维护者使用的评测数据策展流程。"""

from __future__ import annotations

import json
import re
from contextlib import suppress
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.nbtriage_maintainer.collector import load_manifest
from tools.nbtriage_maintainer.github import parse_issue_url
from tools.nbtriage_maintainer.models import CaseCuration, OracleDraft


class AnnotationError(ValueError):
    pass


_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class AppliedAnnotation:
    case_id: str
    case_path: Path


@dataclass(frozen=True)
class _PreparedAnnotation:
    case_id: str
    case_path: Path
    original: bytes
    updated: bytes


def apply_annotations(annotation_path: Path, cases_dir: Path) -> list[AppliedAnnotation]:
    """把可版本管理的人工标注合并到本地生成 Case。

    Args:
        annotation_path: schema v1 标注文件，只能修改 `curation` 字段。
        cases_dir: 已由公开来源采集生成的 Case 目录。

    Returns:
        成功更新的 Case 与路径。

    Raises:
        AnnotationError: 标注结构、字段或目标 Case 不符合约束。
    """
    payload = _read_json(annotation_path, "annotation file")
    if payload.get("schema_version") != 1:
        raise AnnotationError("annotation schema_version must be 1")
    annotations = payload.get("annotations")
    if not isinstance(annotations, list) or not annotations:
        raise AnnotationError("annotations must be a non-empty list")

    allowed_fields = {item.name for item in fields(CaseCuration)}
    allowed_oracle_fields = {item.name for item in fields(OracleDraft)}
    seen = set()
    prepared: list[_PreparedAnnotation] = []
    resolved_cases_dir = cases_dir.resolve()
    for index, annotation in enumerate(annotations):
        if not isinstance(annotation, dict):
            raise AnnotationError(f"annotation {index} must be an object")
        case_id = annotation.get("case_id")
        curation = annotation.get("curation")
        if not isinstance(case_id, str) or _CASE_ID_PATTERN.fullmatch(case_id) is None:
            raise AnnotationError(f"annotation {index} has an invalid case_id")
        if case_id in seen:
            raise AnnotationError(f"duplicate annotation case_id: {case_id}")
        if not isinstance(curation, dict) or not curation:
            raise AnnotationError(f"annotation {case_id} must contain curation fields")
        unknown = set(curation) - allowed_fields
        if unknown:
            raise AnnotationError(f"annotation {case_id} has unknown fields: {sorted(unknown)}")
        oracle = curation.get("oracle")
        if oracle is not None:
            if not isinstance(oracle, dict):
                raise AnnotationError(f"annotation {case_id} oracle must be an object")
            unknown_oracle = set(oracle) - allowed_oracle_fields
            if unknown_oracle:
                raise AnnotationError(
                    f"annotation {case_id} has unknown oracle fields: {sorted(unknown_oracle)}"
                )
        seen.add(case_id)

        case_path = (resolved_cases_dir / f"{case_id}.json").resolve()
        if case_path.parent != resolved_cases_dir:
            raise AnnotationError(f"annotation {index} has an invalid case_id")
        original, case = _read_json_bytes(case_path, "case")
        if case.get("case_id") != case_id or not isinstance(case.get("curation"), dict):
            raise AnnotationError(f"case artifact does not match annotation: {case_path}")
        merged = dict(case["curation"])
        for key, value in curation.items():
            if key == "oracle" and isinstance(value, dict):
                merged_oracle = dict(merged.get("oracle", {}))
                merged_oracle.update(value)
                merged[key] = merged_oracle
            elif key == "field_provenance" and isinstance(value, dict):
                merged_provenance = dict(merged.get("field_provenance", {}))
                merged_provenance.update(value)
                merged[key] = merged_provenance
            else:
                merged[key] = value
        case["curation"] = merged
        prepared.append(
            _PreparedAnnotation(
                case_id=case_id,
                case_path=case_path,
                original=original,
                updated=_json_bytes(case),
            )
        )

    _commit_annotation_batch(prepared)
    return [AppliedAnnotation(item.case_id, item.case_path) for item in prepared]


def export_annotations(
    manifest_path: Path,
    cases_dir: Path,
    output_path: Path,
    overwrite: bool = False,
) -> int:
    """从本地策展 Case 导出可版本管理的最小人工标注。"""
    if output_path.exists() and not overwrite:
        raise AnnotationError(f"annotation output already exists: {output_path}")
    annotations = []
    excluded_keys = {
        "provisional_support_level",
        "provisional_execution_mode",
        "research_note",
    }
    for candidate in load_manifest(manifest_path):
        issue_ref = parse_issue_url(candidate["source_url"])
        case_path = cases_dir / f"{issue_ref.case_id}.json"
        case = _read_json(case_path, "case")
        curation = case.get("curation")
        if not isinstance(curation, dict):
            raise AnnotationError(f"case has no curation object: {case_path}")
        exported = {
            key: value
            for key, value in curation.items()
            if key not in excluded_keys and _has_content(value)
        }
        if not exported:
            raise AnnotationError(f"case has no completed curation fields: {case_path}")
        annotations.append({"case_id": issue_ref.case_id, "curation": exported})

    payload = {
        "schema_version": 1,
        "source_manifest": manifest_path.as_posix(),
        "annotations": annotations,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output_path, payload)
    return len(annotations)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    return _read_json_bytes(path, label)[1]


def _read_json_bytes(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as error:
        raise AnnotationError(f"{label} not found: {path}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AnnotationError(f"{label} is not valid JSON: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise AnnotationError(f"{label} must contain a JSON object: {path}")
    return raw, payload


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return any(_has_content(item) for item in value.values())
    if isinstance(value, list):
        return bool(value)
    return True


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    content = _json_bytes(payload)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _commit_annotation_batch(prepared: list[_PreparedAnnotation]) -> None:
    staged: list[tuple[_PreparedAnnotation, Path, Path]] = []
    replaced: list[tuple[_PreparedAnnotation, Path]] = []
    try:
        for item in prepared:
            token = uuid4().hex
            new_path = item.case_path.with_name(
                f".{item.case_path.name}.annotation-new-{token}.tmp"
            )
            backup_path = item.case_path.with_name(
                f".{item.case_path.name}.annotation-backup-{token}.tmp"
            )
            staged.append((item, new_path, backup_path))
            _write_new_bytes(new_path, item.updated)
            _write_new_bytes(backup_path, item.original)

        for item, new_path, backup_path in staged:
            if item.case_path.read_bytes() != item.original:
                raise AnnotationError("case artifact changed while applying annotation batch")
            new_path.replace(item.case_path)
            replaced.append((item, backup_path))
    except (AnnotationError, OSError) as error:
        rollback_failed = False
        for item, backup_path in reversed(replaced):
            try:
                backup_path.replace(item.case_path)
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise AnnotationError(
                "annotation batch commit failed and rollback was incomplete"
            ) from error
        if isinstance(error, AnnotationError):
            raise
        raise AnnotationError("annotation batch commit failed") from error
    finally:
        for _, new_path, backup_path in staged:
            with suppress(OSError):
                new_path.unlink()
            with suppress(OSError):
                backup_path.unlink()


def _write_new_bytes(path: Path, content: bytes) -> None:
    with path.open("xb") as file:
        file.write(content)
