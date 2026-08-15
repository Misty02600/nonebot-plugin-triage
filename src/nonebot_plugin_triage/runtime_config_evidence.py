from __future__ import annotations

import hashlib
import keyword
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import TypeAlias

from pydantic import BaseModel

from nonebot_plugin_triage.config_policy import ConfigValuePolicy, normalize_config_root
from nonebot_plugin_triage.config_projection import (
    ConfigProjectionError,
    JsonValue,
    project_config_values,
)

_REFERENCE_ID_PATTERN = re.compile(r"^config:[0-9a-f]{64}$")
_SOURCE_REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_REFERENCES = 64


class RuntimeConfigEvidenceError(ValueError):
    pass


class RuntimeConfigOmissionReason(StrEnum):
    REFERENCE_NOT_APPROVED = "reference_not_approved"
    MODULE_NOT_ALLOWED = "module_not_allowed"
    MODULE_UNAVAILABLE = "module_unavailable"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_REVISION_MISMATCH = "source_revision_mismatch"
    CONFIG_INSTANCE_UNAVAILABLE = "config_instance_unavailable"
    CONFIG_TYPE_NOT_ALLOWED = "config_type_not_allowed"
    CONFIG_TYPE_MISMATCH = "config_type_mismatch"
    CONFIG_KEY_MISMATCH = "config_key_mismatch"
    INVALID_REFERENCE = "invalid_reference"
    RESTRICTED = "restricted"
    MISSING = "missing"
    OPAQUE = "opaque"
    LIMIT_EXCEEDED = "limit_exceeded"


@dataclass(frozen=True, slots=True)
class RuntimeConfigReference:
    """宿主从静态证据预先准入的一项运行时配置引用。"""

    reference_id: str
    module: str
    binding: str
    field_name: str
    config_key: str
    config_type: str
    source_revision: str

    def __post_init__(self) -> None:
        if not _REFERENCE_ID_PATTERN.fullmatch(self.reference_id):
            raise RuntimeConfigEvidenceError("reference_id must be a config SHA-256 identifier")
        if not _valid_module_name(self.module):
            raise RuntimeConfigEvidenceError("reference module must be a valid Python module name")
        if not self.binding.isidentifier() or keyword.iskeyword(self.binding):
            raise RuntimeConfigEvidenceError("reference binding must be a Python identifier")
        if not self.field_name.isidentifier() or keyword.iskeyword(self.field_name):
            raise RuntimeConfigEvidenceError("reference field must be a Python identifier")
        try:
            normalize_config_root(self.config_key)
        except ValueError as error:
            raise RuntimeConfigEvidenceError("reference config_key is invalid") from error
        if (
            not isinstance(self.config_type, str)
            or not self.config_type
            or len(self.config_type) > 512
            or ":" not in self.config_type
        ):
            raise RuntimeConfigEvidenceError("reference config_type is invalid")
        if not _SOURCE_REVISION_PATTERN.fullmatch(self.source_revision):
            raise RuntimeConfigEvidenceError("source_revision must be a SHA-256 identifier")
        if self.reference_id != runtime_config_reference_id(
            self.module,
            self.binding,
            self.field_name,
        ):
            raise RuntimeConfigEvidenceError("reference_id does not match the referenced binding")

    @property
    def source_symbol(self) -> str:
        return f"{self.module}:{self.binding}.{self.field_name}"


@dataclass(frozen=True, slots=True)
class RuntimeConfigValueEvidence:
    reference_id: str
    source_symbol: str = field(repr=False)
    value: JsonValue = field(repr=False)

    def __repr__(self) -> str:
        return f"RuntimeConfigValueEvidence(reference_id={self.reference_id!r}, value=<redacted>)"


@dataclass(frozen=True, slots=True)
class RuntimeConfigOmission:
    reference_id: str
    reason: RuntimeConfigOmissionReason
    source_symbol: str | None = field(default=None, repr=False)


RuntimeConfigEvidence: TypeAlias = RuntimeConfigValueEvidence | RuntimeConfigOmission


class RuntimeConfigEvidenceReader:
    """读取宿主已准入且已构造完成的 Pydantic 配置实例。

    本接口只接受 ``reference_id``，不会枚举进程内对象、导入模块、重建
    NoneBot 配置或读取环境文件。值仅作为一次性返回对象存在，且从 ``repr``
    中隐藏；持久化边界由调用方保持禁止。
    """

    def __init__(
        self,
        *,
        owner_module: str,
        references: Iterable[RuntimeConfigReference],
        policy: ConfigValuePolicy,
    ) -> None:
        if not _valid_module_name(owner_module):
            raise RuntimeConfigEvidenceError("owner_module must be a valid Python module name")
        if not isinstance(policy, ConfigValuePolicy):
            raise RuntimeConfigEvidenceError("policy must be a ConfigValuePolicy")
        approved: dict[str, RuntimeConfigReference] = {}
        for reference in references:
            if not isinstance(reference, RuntimeConfigReference):
                raise RuntimeConfigEvidenceError(
                    "references must contain RuntimeConfigReference values"
                )
            if reference.reference_id in approved:
                raise RuntimeConfigEvidenceError("references contain duplicate reference IDs")
            approved[reference.reference_id] = reference
            if len(approved) > _MAX_REFERENCES:
                raise RuntimeConfigEvidenceError("reference count exceeds the allowed limit")

        self._owner_module = owner_module
        self._references = MappingProxyType(approved)
        self._policy = policy

    def __repr__(self) -> str:
        return (
            "RuntimeConfigEvidenceReader("
            f"owner_module={self._owner_module!r}, reference_count={len(self._references)})"
        )

    def read(self, reference_id: str) -> RuntimeConfigEvidence:
        if not isinstance(reference_id, str) or not _REFERENCE_ID_PATTERN.fullmatch(reference_id):
            raise RuntimeConfigEvidenceError("reference_id must be a config SHA-256 identifier")
        reference = self._references.get(reference_id)
        if reference is None:
            return RuntimeConfigOmission(
                reference_id,
                RuntimeConfigOmissionReason.REFERENCE_NOT_APPROVED,
            )
        return self._read_approved(reference)

    def _read_approved(self, reference: RuntimeConfigReference) -> RuntimeConfigEvidence:
        if not _module_belongs_to_owner(reference.module, self._owner_module):
            return _omission(reference, RuntimeConfigOmissionReason.MODULE_NOT_ALLOWED)
        owner_root = _owner_source_root(self._owner_module)
        module = sys.modules.get(reference.module)
        if owner_root is None or not isinstance(module, ModuleType):
            return _omission(reference, RuntimeConfigOmissionReason.MODULE_UNAVAILABLE)
        path = _resolved_python_file(module)
        if path is None or not _path_belongs_to_owner(path, owner_root):
            return _omission(reference, RuntimeConfigOmissionReason.MODULE_NOT_ALLOWED)
        revision = _source_revision(path)
        if revision is None:
            return _omission(reference, RuntimeConfigOmissionReason.SOURCE_UNAVAILABLE)
        if revision != reference.source_revision:
            return _omission(reference, RuntimeConfigOmissionReason.SOURCE_REVISION_MISMATCH)

        config = vars(module).get(reference.binding)
        if not isinstance(config, BaseModel):
            return _omission(reference, RuntimeConfigOmissionReason.CONFIG_INSTANCE_UNAVAILABLE)
        config_class = type(config)
        if not _module_belongs_to_owner(config_class.__module__, self._owner_module):
            return _omission(reference, RuntimeConfigOmissionReason.CONFIG_TYPE_NOT_ALLOWED)
        if _qualified_type_name(config_class) != reference.config_type:
            return _omission(reference, RuntimeConfigOmissionReason.CONFIG_TYPE_MISMATCH)
        if not _reference_matches_model(reference, config):
            return _omission(reference, RuntimeConfigOmissionReason.CONFIG_KEY_MISMATCH)
        try:
            projection = project_config_values(
                config=config,
                key_to_field={reference.config_key: reference.field_name},
                policy=self._policy,
            )
        except ConfigProjectionError:
            return _omission(reference, RuntimeConfigOmissionReason.INVALID_REFERENCE)
        if projection.entries:
            return RuntimeConfigValueEvidence(
                reference.reference_id,
                reference.source_symbol,
                projection.entries[0].value,
            )
        try:
            reason = RuntimeConfigOmissionReason(projection.omissions[0].reason.value)
        except (IndexError, ValueError):
            reason = RuntimeConfigOmissionReason.INVALID_REFERENCE
        return _omission(reference, reason)


def runtime_config_reference_id(module: str, binding: str, field_name: str) -> str:
    payload = "\0".join((module, binding, field_name))
    digest = hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"config:{digest}"


def _omission(
    reference: RuntimeConfigReference,
    reason: RuntimeConfigOmissionReason,
) -> RuntimeConfigOmission:
    return RuntimeConfigOmission(reference.reference_id, reason, reference.source_symbol)


def _valid_module_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and all(part.isidentifier() and not keyword.iskeyword(part) for part in value.split("."))
    )


def _module_belongs_to_owner(module_name: str, owner_module: str) -> bool:
    return module_name == owner_module or module_name.startswith(f"{owner_module}.")


def _owner_source_root(owner_module: str) -> tuple[Path, bool] | None:
    module = sys.modules.get(owner_module)
    if not isinstance(module, ModuleType):
        return None
    path = _resolved_python_file(module)
    if path is None:
        return None
    is_package = path.name == "__init__.py"
    return (path.parent if is_package else path, is_package)


def _resolved_python_file(module: ModuleType) -> Path | None:
    file_name = vars(module).get("__file__")
    if not isinstance(file_name, str):
        return None
    try:
        path = Path(file_name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not path.is_file() or path.suffix.casefold() != ".py":
        return None
    return path


def _path_belongs_to_owner(path: Path, source_root: tuple[Path, bool]) -> bool:
    root, is_package = source_root
    return path.is_relative_to(root) if is_package else path == root


def _source_revision(path: Path) -> str | None:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    digest = hashlib.sha256(source.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"sha256:{digest}"


def _qualified_type_name(value: type[object]) -> str:
    return f"{value.__module__}:{value.__qualname__}"


def _reference_matches_model(reference: RuntimeConfigReference, config: BaseModel) -> bool:
    fields = type(config).__dict__.get("__pydantic_fields__")
    if type(fields) is not dict:
        return False
    field_info = fields.get(reference.field_name)
    if field_info is None:
        return False
    try:
        validation_alias = object.__getattribute__(field_info, "validation_alias")
        field_alias = object.__getattribute__(field_info, "alias")
    except AttributeError:
        return False
    alias = validation_alias or field_alias or reference.field_name
    if not isinstance(alias, str):
        return False
    try:
        return normalize_config_root(alias) == normalize_config_root(reference.config_key)
    except ValueError:
        return False


__all__ = (
    "RuntimeConfigEvidence",
    "RuntimeConfigEvidenceError",
    "RuntimeConfigEvidenceReader",
    "RuntimeConfigOmission",
    "RuntimeConfigOmissionReason",
    "RuntimeConfigReference",
    "RuntimeConfigValueEvidence",
    "runtime_config_reference_id",
)
