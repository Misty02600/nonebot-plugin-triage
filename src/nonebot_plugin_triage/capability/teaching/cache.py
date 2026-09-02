from __future__ import annotations

import json
import keyword
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nbtriage.capability.teaching.annotations import (
    CapabilityAnnotationError,
    CapabilityTeachingAnnotation,
)

CAPABILITY_ANNOTATION_CACHE_SCHEMA_VERSION = 3

_MAX_CACHE_FILENAME_LENGTH = 180
_MAX_UNITS_PER_PLUGIN = 4_096
_WINDOWS_RESERVED_FIRST_SEGMENTS = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
_ATTEMPT_STATES = frozenset(
    {
        "generated",
        "disabled",
        "failed",
    }
)
_ATTEMPT_STAGES = frozenset(
    {
        "client_create",
        "agent_run",
        "output_projection",
    }
)
_ATTEMPT_REASONS = frozenset(
    {
        "timeout",
        "transport",
        "http",
        "budget",
        "output_truncated",
        "output_validation",
        "schema",
        "provider_identity",
        "source_changed",
        "evidence_changed",
        "unknown",
    }
)


class CapabilityAnnotationCacheError(ValueError):
    pass


@dataclass(frozen=True)
class CapabilityAnnotationLastAttempt:
    state: str
    stage: str
    request_fingerprint: str
    reason: str | None = None
    detail_code: str | None = None
    attempts: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.state, str) or self.state not in _ATTEMPT_STATES:
            raise CapabilityAnnotationCacheError("last attempt state is invalid")
        if not isinstance(self.stage, str) or self.stage not in _ATTEMPT_STAGES:
            raise CapabilityAnnotationCacheError("last attempt stage is invalid")
        _require_sha256_digest(self.request_fingerprint, "last attempt request fingerprint")
        if self.reason is not None and (
            not isinstance(self.reason, str) or self.reason not in _ATTEMPT_REASONS
        ):
            raise CapabilityAnnotationCacheError("last attempt reason is invalid")
        if self.detail_code is not None and (
            not isinstance(self.detail_code, str)
            or not self.detail_code
            or len(self.detail_code) > 64
            or any(
                not (character.isascii() and (character.isalnum() or character == "_"))
                for character in self.detail_code
            )
        ):
            raise CapabilityAnnotationCacheError("last attempt detail code is invalid")
        if (
            isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or not 1 <= self.attempts <= 2
        ):
            raise CapabilityAnnotationCacheError("last attempt count is invalid")
        if self.state == "failed" and self.reason is None:
            raise CapabilityAnnotationCacheError("failed last attempt must record a reason")
        if self.state != "failed" and self.reason is not None:
            raise CapabilityAnnotationCacheError(
                "successful last attempt must not record a failure reason"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "stage": self.stage,
            "request_fingerprint": self.request_fingerprint,
            "reason": self.reason,
            "detail_code": self.detail_code,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityAnnotationLastAttempt:
        _require_fields(
            payload,
            {
                "state",
                "stage",
                "request_fingerprint",
                "reason",
                "detail_code",
                "attempts",
            },
            "last attempt",
        )
        assert isinstance(payload, dict)
        return cls(
            state=payload["state"],
            stage=payload["stage"],
            request_fingerprint=payload["request_fingerprint"],
            reason=payload["reason"],
            detail_code=payload["detail_code"],
            attempts=payload["attempts"],
        )


@dataclass(frozen=True)
class CapabilityAnnotationCacheUnit:
    analysis_unit_id: str
    last_good: CapabilityTeachingAnnotation | None = None
    pending: CapabilityTeachingAnnotation | None = None
    last_attempt: CapabilityAnnotationLastAttempt | None = None

    def __post_init__(self) -> None:
        _require_bounded_safe_string(self.analysis_unit_id, "analysis unit ID", maximum=128)
        if self.last_good is None and self.pending is None and self.last_attempt is None:
            raise CapabilityAnnotationCacheError(
                "cache unit has neither last-good, pending, nor attempt"
            )
        if self.last_attempt is not None and not isinstance(
            self.last_attempt, CapabilityAnnotationLastAttempt
        ):
            raise CapabilityAnnotationCacheError("unit last attempt is invalid")
        for annotation, label in (
            (self.last_good, "last-good"),
            (self.pending, "pending"),
        ):
            if annotation is None:
                continue
            if not isinstance(annotation, CapabilityTeachingAnnotation):
                raise CapabilityAnnotationCacheError(f"unit {label} annotation is invalid")
            if annotation.capability_id != self.analysis_unit_id:
                raise CapabilityAnnotationCacheError(
                    f"unit {label} capability ID does not match analysis unit ID"
                )
        if self.last_attempt is not None and self.last_attempt.state != "failed":
            annotation = self.pending or self.last_good
            if annotation is None:
                raise CapabilityAnnotationCacheError(
                    "successful last attempt requires a pending or last-good annotation"
                )
            if annotation.request_fingerprint != self.last_attempt.request_fingerprint:
                raise CapabilityAnnotationCacheError(
                    "successful last attempt does not match the candidate fingerprint"
                )
            if self.last_attempt.state == "generated" and not annotation.knowledge_enabled:
                raise CapabilityAnnotationCacheError(
                    "generated last attempt requires enabled candidate knowledge"
                )
            if self.last_attempt.state == "disabled" and annotation.knowledge_enabled:
                raise CapabilityAnnotationCacheError(
                    "disabled last attempt requires disabled candidate knowledge"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "last_good": self.last_good.to_dict() if self.last_good is not None else None,
            "pending": self.pending.to_dict() if self.pending is not None else None,
            "last_attempt": (
                self.last_attempt.to_dict() if self.last_attempt is not None else None
            ),
        }

    @classmethod
    def from_dict(
        cls,
        analysis_unit_id: object,
        payload: object,
    ) -> CapabilityAnnotationCacheUnit:
        _require_fields(payload, {"last_good", "pending", "last_attempt"}, "cache unit")
        assert isinstance(payload, dict)
        unit_id = _require_bounded_safe_string(
            analysis_unit_id,
            "analysis unit ID",
            maximum=128,
        )
        raw_last_good = payload["last_good"]
        raw_pending = payload["pending"]
        raw_last_attempt = payload["last_attempt"]
        try:
            last_good = (
                None
                if raw_last_good is None
                else CapabilityTeachingAnnotation.from_dict(raw_last_good)
            )
            pending = (
                None if raw_pending is None else CapabilityTeachingAnnotation.from_dict(raw_pending)
            )
        except CapabilityAnnotationError as error:
            raise CapabilityAnnotationCacheError("unit annotation is invalid") from error
        return cls(
            analysis_unit_id=unit_id,
            last_good=last_good,
            pending=pending,
            last_attempt=(
                None
                if raw_last_attempt is None
                else CapabilityAnnotationLastAttempt.from_dict(raw_last_attempt)
            ),
        )


@dataclass(frozen=True)
class CapabilityAnnotationPluginCache:
    module_name: str
    plugin_source_revision: str
    published_generation: str | None
    units: tuple[CapabilityAnnotationCacheUnit, ...] = ()
    schema_version: int = CAPABILITY_ANNOTATION_CACHE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        capability_annotation_cache_filename(self.module_name)
        _require_sha256_digest(self.plugin_source_revision, "plugin source revision")
        if self.published_generation is not None:
            _require_sha256_digest(self.published_generation, "published generation")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != CAPABILITY_ANNOTATION_CACHE_SCHEMA_VERSION
        ):
            raise CapabilityAnnotationCacheError("unsupported plugin cache schema version")
        if (
            not isinstance(self.units, tuple)
            or len(self.units) > _MAX_UNITS_PER_PLUGIN
            or any(not isinstance(item, CapabilityAnnotationCacheUnit) for item in self.units)
        ):
            raise CapabilityAnnotationCacheError("plugin cache units are invalid")
        ordered = tuple(sorted(self.units, key=lambda item: item.analysis_unit_id))
        if len({item.analysis_unit_id for item in ordered}) != len(ordered):
            raise CapabilityAnnotationCacheError("plugin cache analysis unit IDs must be unique")
        if self.published_generation is None and any(
            item.last_good is not None for item in ordered
        ):
            raise CapabilityAnnotationCacheError(
                "unpublished plugin cache must not contain last-good annotations"
            )
        object.__setattr__(self, "units", ordered)

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "module_name": self.module_name,
                "plugin_source_revision": self.plugin_source_revision,
                "published_generation": self.published_generation,
                "units": {item.analysis_unit_id: item.to_dict() for item in self.units},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, document: str) -> CapabilityAnnotationPluginCache:
        if not isinstance(document, str):
            raise CapabilityAnnotationCacheError("plugin cache JSON must be text")
        try:
            payload = json.loads(
                document,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, json.JSONDecodeError) as error:
            raise CapabilityAnnotationCacheError("invalid plugin cache JSON") from error
        _require_fields(
            payload,
            {
                "schema_version",
                "module_name",
                "plugin_source_revision",
                "published_generation",
                "units",
            },
            "plugin cache",
        )
        assert isinstance(payload, dict)
        raw_units = payload["units"]
        if not isinstance(raw_units, dict):
            raise CapabilityAnnotationCacheError("plugin cache units must be an object")
        try:
            return cls(
                schema_version=payload["schema_version"],
                module_name=payload["module_name"],
                plugin_source_revision=payload["plugin_source_revision"],
                published_generation=payload["published_generation"],
                units=tuple(
                    CapabilityAnnotationCacheUnit.from_dict(unit_id, unit)
                    for unit_id, unit in raw_units.items()
                ),
            )
        except CapabilityAnnotationCacheError:
            raise
        except (TypeError, ValueError) as error:
            raise CapabilityAnnotationCacheError("plugin cache fields are invalid") from error


def capability_annotation_cache_filename(module_name: object) -> str:
    if not isinstance(module_name, str) or not module_name:
        raise CapabilityAnnotationCacheError("plugin module name is invalid")
    segments = module_name.split(".")
    if any(not segment.isidentifier() or keyword.iskeyword(segment) for segment in segments):
        raise CapabilityAnnotationCacheError("plugin module name is not a safe Python module name")
    if segments[0].casefold() in _WINDOWS_RESERVED_FIRST_SEGMENTS:
        raise CapabilityAnnotationCacheError("plugin module name uses a Windows reserved name")
    filename = f"{module_name}.json"
    if len(filename) > _MAX_CACHE_FILENAME_LENGTH:
        raise CapabilityAnnotationCacheError("plugin cache filename is too long")
    return filename


def read_capability_annotation_plugin_cache(
    directory: Path,
    module_name: str,
) -> CapabilityAnnotationPluginCache | None:
    path = Path(directory) / capability_annotation_cache_filename(module_name)
    try:
        document = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    cache = CapabilityAnnotationPluginCache.from_json(document)
    if cache.module_name != module_name:
        raise CapabilityAnnotationCacheError("plugin cache module name does not match its filename")
    return cache


def write_capability_annotation_plugin_cache(
    directory: Path,
    cache: CapabilityAnnotationPluginCache,
) -> Path:
    if not isinstance(cache, CapabilityAnnotationPluginCache):
        raise TypeError("cache must be CapabilityAnnotationPluginCache")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / capability_annotation_cache_filename(cache.module_name)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=root,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(cache.to_json())
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    return path


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapabilityAnnotationCacheError("plugin cache JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise CapabilityAnnotationCacheError(f"plugin cache JSON contains invalid constant {value}")


def _require_fields(payload: object, fields: set[str], label: str) -> None:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise CapabilityAnnotationCacheError(f"{label} fields do not match schema")


def _require_bounded_safe_string(value: object, label: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(not character.isprintable() for character in value)
    ):
        raise CapabilityAnnotationCacheError(f"{label} is not a bounded safe string")
    return value


def _require_sha256_digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CapabilityAnnotationCacheError(f"{label} must be a SHA-256 digest")
    return value


__all__ = (
    "CAPABILITY_ANNOTATION_CACHE_SCHEMA_VERSION",
    "CapabilityAnnotationCacheError",
    "CapabilityAnnotationCacheUnit",
    "CapabilityAnnotationLastAttempt",
    "CapabilityAnnotationPluginCache",
    "capability_annotation_cache_filename",
    "read_capability_annotation_plugin_cache",
    "write_capability_annotation_plugin_cache",
)
