from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nbtriage.live_trials import (
    LiveTrialError,
    RotatingJsonlTrialEventSink,
    TrialFeedback,
    TrialMode,
    TrialOperationResult,
    TrialOperationStatus,
)
from nonebot_plugin_triage.config import NBTriageConfig
from nonebot_plugin_triage.trials import (
    create_trial_service,
    format_trial_feedback_result,
    parse_trial_feedback,
)


def test_trial_runtime_is_default_off_without_resolving_or_creating_log(tmp_path) -> None:
    data_dir = tmp_path / "plugin-data"
    resolved_filenames: list[str] = []

    def resolve(filename: str) -> Path:
        resolved_filenames.append(filename)
        data_dir.mkdir(parents=True)
        return data_dir / filename

    service = create_trial_service(
        NBTriageConfig(),
        trial_log_path_resolver=resolve,
    )

    assert service.mode is TrialMode.OFF
    assert service.sink is None
    assert resolved_filenames == []
    assert not data_dir.exists()


def test_observe_runtime_builds_bounded_rotating_sink(tmp_path) -> None:
    path = tmp_path / "trial-events.jsonl"
    resolved_filenames: list[str] = []

    def resolve(filename: str) -> Path:
        resolved_filenames.append(filename)
        return path

    service = create_trial_service(
        NBTriageConfig(
            nbtriage_trial_mode="observe",
            nbtriage_trial_log_max_bytes=65_536,
            nbtriage_trial_log_backup_count=3,
        ),
        trial_log_path_resolver=resolve,
    )

    assert service.mode is TrialMode.OBSERVE
    assert resolved_filenames == ["trial-events.jsonl"]
    assert isinstance(service.sink, RotatingJsonlTrialEventSink)
    assert service.sink.path == Path(path)
    assert service.sink.max_bytes == 65_536
    assert service.sink.backup_count == 3
    assert not path.exists()


def test_observe_runtime_fails_closed_when_resolver_returns_invalid_path() -> None:
    def resolve(_: str) -> Any:
        return "not-a-path"

    with pytest.raises(LiveTrialError):
        create_trial_service(
            NBTriageConfig(nbtriage_trial_mode="observe"),
            trial_log_path_resolver=resolve,
        )


def test_observe_runtime_fails_closed_when_log_path_cannot_be_resolved() -> None:
    def resolve(_: str) -> Path:
        raise PermissionError("denied")

    with pytest.raises(PermissionError, match="denied"):
        create_trial_service(
            NBTriageConfig(nbtriage_trial_mode="observe"),
            trial_log_path_resolver=resolve,
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("有用", TrialFeedback.USEFUL),
        ("不完整", TrialFeedback.INCOMPLETE),
        ("不正确", TrialFeedback.INCORRECT),
        (" Useful ", TrialFeedback.USEFUL),
        ("free text", None),
    ],
)
def test_trial_feedback_accepts_only_bounded_enum(value, expected) -> None:
    assert parse_trial_feedback(value) is expected


def test_trial_feedback_formatter_does_not_echo_incident_or_free_text() -> None:
    result = TrialOperationResult(
        status=TrialOperationStatus.RECORDED,
        trial_id="trial-safe",
    )

    message = format_trial_feedback_result(result, TrialFeedback.USEFUL)

    assert message == "已记录试运行反馈：有用。"
    assert "trial-safe" not in message
