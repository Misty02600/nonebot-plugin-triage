from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

RUNTIME_OBSERVATION_SCHEMA_VERSION = 1
RUNTIME_EVIDENCE_BUNDLE_SCHEMA_VERSION = 1

OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
QUALIFIED_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")

OBSERVATION_FIELDS = {
    "schema_version",
    "observation_id",
    "correlation_id",
    "occurred_at",
    "kind",
    "adapter_name",
    "event_name",
    "plugin_name",
    "matcher_name",
    "api_name",
    "outcome",
    "exception_type",
    "stack_modules",
}


class RuntimeObservationError(ValueError):
    pass


class ObservationKind(StrEnum):
    EVENT_RECEIVED = "event_received"
    EVENT_COMPLETED = "event_completed"
    MATCHER_STARTED = "matcher_started"
    MATCHER_COMPLETED = "matcher_completed"
    API_STARTED = "api_started"
    API_COMPLETED = "api_completed"


class ObservationOutcome(StrEnum):
    OBSERVED = "observed"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ALLOWED_OUTCOMES = {
    ObservationKind.EVENT_RECEIVED: {ObservationOutcome.OBSERVED},
    ObservationKind.EVENT_COMPLETED: {
        ObservationOutcome.SUCCEEDED,
        ObservationOutcome.FAILED,
    },
    ObservationKind.MATCHER_STARTED: {ObservationOutcome.STARTED},
    ObservationKind.MATCHER_COMPLETED: {
        ObservationOutcome.SUCCEEDED,
        ObservationOutcome.FAILED,
    },
    ObservationKind.API_STARTED: {ObservationOutcome.STARTED},
    ObservationKind.API_COMPLETED: {
        ObservationOutcome.SUCCEEDED,
        ObservationOutcome.FAILED,
    },
}


@dataclass(frozen=True)
class RuntimeObservation:
    schema_version: int
    observation_id: str
    correlation_id: str
    occurred_at: str
    kind: ObservationKind
    adapter_name: str
    event_name: str | None
    plugin_name: str | None
    matcher_name: str | None
    api_name: str | None
    outcome: ObservationOutcome
    exception_type: str | None
    stack_modules: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "correlation_id": self.correlation_id,
            "occurred_at": self.occurred_at,
            "kind": self.kind.value,
            "adapter_name": self.adapter_name,
            "event_name": self.event_name,
            "plugin_name": self.plugin_name,
            "matcher_name": self.matcher_name,
            "api_name": self.api_name,
            "outcome": self.outcome.value,
            "exception_type": self.exception_type,
            "stack_modules": list(self.stack_modules),
        }


@dataclass(frozen=True)
class RuntimeEvidenceBundle:
    schema_version: int
    correlation_id: str
    generated_at: str
    observations: tuple[RuntimeObservation, ...]
    buffer_dropped_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "correlation_id": self.correlation_id,
            "generated_at": self.generated_at,
            "observations": [item.to_dict() for item in self.observations],
            "buffer_dropped_count": self.buffer_dropped_count,
        }


@dataclass(frozen=True)
class _BufferedObservation:
    observation: RuntimeObservation
    stored_at: datetime


def parse_runtime_observation(payload: Any) -> RuntimeObservation:
    """校验传输适配器提交的最小化运行观察。

    Args:
        payload: 已解析对象；字段集合必须完整且不能包含消息、用户、API 参数等额外数据。

    Returns:
        时间与标识已规范化的不可变运行观察。

    Raises:
        RuntimeObservationError: schema、时间、标识或 kind 对应的字段组合不合法。
    """
    if not isinstance(payload, dict):
        raise RuntimeObservationError("runtime observation must be an object")
    unknown_fields = set(payload) - OBSERVATION_FIELDS
    missing_fields = OBSERVATION_FIELDS - set(payload)
    if unknown_fields:
        raise RuntimeObservationError(f"unsupported observation fields: {sorted(unknown_fields)}")
    if missing_fields:
        raise RuntimeObservationError(f"missing observation fields: {sorted(missing_fields)}")
    if payload.get("schema_version") != RUNTIME_OBSERVATION_SCHEMA_VERSION:
        raise RuntimeObservationError("unsupported runtime observation schema_version")

    kind = _enum_value(payload.get("kind"), ObservationKind, "kind")
    outcome = _enum_value(payload.get("outcome"), ObservationOutcome, "outcome")
    if outcome not in ALLOWED_OUTCOMES[kind]:
        raise RuntimeObservationError(f"outcome is invalid for {kind.value}")

    event_name = _optional_identifier(payload.get("event_name"), "event_name")
    plugin_name = _optional_identifier(payload.get("plugin_name"), "plugin_name")
    matcher_name = _optional_identifier(payload.get("matcher_name"), "matcher_name")
    api_name = _optional_identifier(payload.get("api_name"), "api_name")
    exception_type = _optional_identifier(payload.get("exception_type"), "exception_type")
    stack_modules = _identifier_list(payload.get("stack_modules"), "stack_modules")

    _validate_subject_fields(
        kind=kind,
        event_name=event_name,
        plugin_name=plugin_name,
        matcher_name=matcher_name,
        api_name=api_name,
    )
    if outcome is ObservationOutcome.FAILED:
        if exception_type is None:
            raise RuntimeObservationError("failed observations require exception_type")
    elif exception_type is not None or stack_modules:
        raise RuntimeObservationError("exception evidence is only allowed for failed observations")

    return RuntimeObservation(
        schema_version=RUNTIME_OBSERVATION_SCHEMA_VERSION,
        observation_id=_opaque_id(payload.get("observation_id"), "observation_id"),
        correlation_id=_opaque_id(payload.get("correlation_id"), "correlation_id"),
        occurred_at=_timestamp(payload.get("occurred_at"), "occurred_at"),
        kind=kind,
        adapter_name=_identifier(payload.get("adapter_name"), "adapter_name"),
        event_name=event_name,
        plugin_name=plugin_name,
        matcher_name=matcher_name,
        api_name=api_name,
        outcome=outcome,
        exception_type=exception_type,
        stack_modules=stack_modules,
    )


class RuntimeObservationBuffer:
    """保存单进程内已最小化的运行观察，并按显式策略淘汰。

    调用方必须给出容量和保留时长，避免领域层暗中选择生产隐私默认值。缓冲允许异步钩子以非时间顺序
    提交观察；容量淘汰按提交顺序执行，证据包按发生时间稳定排序。它不提供多进程协调或崩溃恢复。
    """

    def __init__(self, *, max_entries: int, retention_seconds: int) -> None:
        if not _is_bounded_positive_int(max_entries, upper_bound=1_000_000):
            raise RuntimeObservationError("max_entries must be between 1 and 1000000")
        if not _is_bounded_positive_int(retention_seconds, upper_bound=604_800):
            raise RuntimeObservationError("retention_seconds must be between 1 and 604800")
        self.max_entries = max_entries
        self.retention_seconds = retention_seconds
        self._entries: deque[_BufferedObservation] = deque()
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, observation: RuntimeObservation, *, now: datetime | None = None) -> bool:
        """加入观察；过期输入会被计为丢弃并返回 `False`。"""
        if not isinstance(observation, RuntimeObservation):
            raise RuntimeObservationError("observation must be a RuntimeObservation")
        normalized = parse_runtime_observation(observation.to_dict())
        current_time = _aware_datetime(now)
        self._prune(current_time)
        cutoff = current_time - timedelta(seconds=self.retention_seconds)
        if _parse_timestamp(normalized.occurred_at) < cutoff:
            self._dropped_count += 1
            return False
        if len(self._entries) == self.max_entries:
            self._entries.popleft()
            self._dropped_count += 1
        self._entries.append(_BufferedObservation(observation=normalized, stored_at=current_time))
        return True

    def capture(
        self,
        correlation_id: str,
        *,
        generated_at: datetime | None = None,
    ) -> RuntimeEvidenceBundle:
        """生成同一关联标识的证据包，并暴露此前缓冲丢弃总数。"""
        normalized_correlation_id = _opaque_id(correlation_id, "correlation_id")
        current_time = _aware_datetime(generated_at)
        self._prune(current_time)
        observations = tuple(
            sorted(
                (
                    item.observation
                    for item in self._entries
                    if item.observation.correlation_id == normalized_correlation_id
                ),
                key=lambda item: (_parse_timestamp(item.occurred_at), item.observation_id),
            )
        )
        return RuntimeEvidenceBundle(
            schema_version=RUNTIME_EVIDENCE_BUNDLE_SCHEMA_VERSION,
            correlation_id=normalized_correlation_id,
            generated_at=current_time.isoformat(),
            observations=observations,
            buffer_dropped_count=self._dropped_count,
        )

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.retention_seconds)
        retained = deque(item for item in self._entries if item.stored_at >= cutoff)
        self._dropped_count += len(self._entries) - len(retained)
        self._entries = retained


def _validate_subject_fields(
    *,
    kind: ObservationKind,
    event_name: str | None,
    plugin_name: str | None,
    matcher_name: str | None,
    api_name: str | None,
) -> None:
    if kind in {ObservationKind.EVENT_RECEIVED, ObservationKind.EVENT_COMPLETED}:
        if event_name is None or any((plugin_name, matcher_name, api_name)):
            raise RuntimeObservationError("event observations require only event_name")
        return
    if kind in {ObservationKind.MATCHER_STARTED, ObservationKind.MATCHER_COMPLETED}:
        if matcher_name is None or event_name is not None or api_name is not None:
            raise RuntimeObservationError(
                "matcher observations require matcher_name and may include plugin_name"
            )
        return
    if api_name is None or any((event_name, plugin_name, matcher_name)):
        raise RuntimeObservationError("api observations require only api_name")


def _opaque_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_PATTERN.fullmatch(value):
        raise RuntimeObservationError(f"{field_name} contains unsupported characters")
    return value


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not QUALIFIED_IDENTIFIER_PATTERN.fullmatch(value):
        raise RuntimeObservationError(f"{field_name} must be a qualified identifier")
    return value


def _optional_identifier(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field_name)


def _identifier_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise RuntimeObservationError(f"{field_name} must be a bounded list")
    normalized = tuple(_identifier(item, f"{field_name}[]") for item in value)
    if len(set(normalized)) != len(normalized):
        raise RuntimeObservationError(f"{field_name} entries must be unique")
    return normalized


def _enum_value(value: Any, enum_type: type[StrEnum], field_name: str) -> Any:
    if not isinstance(value, str):
        raise RuntimeObservationError(f"{field_name} is unsupported")
    try:
        return enum_type(value)
    except ValueError as error:
        raise RuntimeObservationError(f"{field_name} is unsupported") from error


def _timestamp(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeObservationError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = _parse_timestamp(value)
    except ValueError as error:
        raise RuntimeObservationError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise RuntimeObservationError(f"{field_name} must include a timezone")
    return parsed.isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _aware_datetime(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise RuntimeObservationError("buffer time must include a timezone")
    return current.astimezone(UTC)


def _is_bounded_positive_int(value: Any, *, upper_bound: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= upper_bound
