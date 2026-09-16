from __future__ import annotations

import pytest
from pydantic import ValidationError

from nbtriage.behavior.exploration import (
    BehaviorClaimBasis,
    BehaviorEvidenceFact,
    BehaviorEvidenceSnapshot,
)

_CAPTURED_AT = "2026-08-21T08:00:00+00:00"


def _fact() -> BehaviorEvidenceFact:
    return BehaviorEvidenceFact(
        evidence_id="fact-demo",
        source_kind="capability_shadow",
        locator="capability/demo/registration",
        revision="capability-shadow:generation-1",
        captured_at=_CAPTURED_AT,
        text="当前部署存在 demo 命令注册结构。",
        suggested_basis=BehaviorClaimBasis.OBSERVED_STRUCTURE,
    )


def test_evidence_contracts_normalize_bounded_identifiers() -> None:
    fact = BehaviorEvidenceFact(
        **{
            **_fact().model_dump(mode="python"),
            "evidence_id": " fact-demo ",
        }
    )
    snapshot = BehaviorEvidenceSnapshot(
        source_kind=" capability_shadow ",
        generation=" generation-1 ",
        available=True,
        partial=False,
        stale=False,
    )

    assert fact.evidence_id == "fact-demo"
    assert snapshot.source_kind == "capability_shadow"
    assert snapshot.generation == "generation-1"


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456",
        "password=correct-horse-battery-staple",
        "DATABASE_PASSWORD=correct-horse-battery-staple",
        "REDIS_PASSWORD: correct-horse-battery-staple",
        "CLIENT_SECRET_VALUE=abcdefghijklmnop",
        "SSH_PRIVATE_KEY=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef",
        "databasePassword=correct-horse-battery-staple",
        "privateKey=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        "eyJabcdefghijk.eyJ0123456789.abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY=abcdefghijklmnopqrstuvwxyz1234567890ABCD",
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN ENCRYPTED PRIVATE KEY-----",
        "源码位于 C:\\Users\\Misty\\private\\handler.py",
        "源码位于 C:/Users/Misty/private/handler.py",
        "源码位于 \\\\server\\share\\private\\handler.py",
        "源码位于 \\\\?\\C:\\private\\handler.py",
        "源码位于 /home/misty/private/handler.py",
        "日志位于 /tmp/nbtriage/raw.log",
        "日志位于 /123/private/data.log",
        "日志位于 /@scope/private/data.log",
        "配置位于 /usr/local/etc/nbtriage.toml",
        "数据库位于 /private/var/db/x.sqlite3",
        "配置位于 /mnt/c/Users/Misty/.env",
        "配置位于 file:///etc/nbtriage.toml",
        "配置位于 ~/private/.env",
    ],
)
def test_evidence_text_rejects_secrets_and_absolute_paths(unsafe_text: str) -> None:
    with pytest.raises(ValidationError):
        BehaviorEvidenceFact(
            **{
                **_fact().model_dump(mode="python"),
                "text": unsafe_text,
            }
        )


@pytest.mark.parametrize(
    "locator",
    ["C:/Users/Misty/private/handler.py", "capability/../private/handler.py"],
)
def test_evidence_locator_rejects_absolute_or_traversing_path(locator: str) -> None:
    with pytest.raises(ValidationError):
        BehaviorEvidenceFact(
            **{
                **_fact().model_dump(mode="python"),
                "locator": locator,
            }
        )


@pytest.mark.parametrize(
    "captured_at",
    ["2026-08-21T08:00:00", "not-a-timestamp"],
)
def test_evidence_timestamp_requires_timezone_aware_iso_value(captured_at: str) -> None:
    with pytest.raises(ValidationError):
        BehaviorEvidenceFact(
            **{
                **_fact().model_dump(mode="python"),
                "captured_at": captured_at,
            }
        )
