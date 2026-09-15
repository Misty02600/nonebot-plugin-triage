"""由采集器生成故障身份；不解析模型摘要或把证据内容哈希当作故障。"""

from __future__ import annotations

import hashlib
import json
import linecache
import re
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

_INFRASTRUCTURE = frozenset({"asyncio", "anyio", "nonebot", "nonebug", "contextlib"})
_VOLATILE_LABEL = re.compile(r"\b(request_id|correlation_id|trace_id)=([^\s,;]+)")
_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b")


class FailureFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["exception_path", "wait_path", "custom"]
    algorithm_revision: Annotated[str, Field(min_length=1, max_length=64)]
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


@dataclass(frozen=True, slots=True)
class FailureFrame:
    module: str
    function: str
    code: str


def fingerprint_frames(
    frames: tuple[FailureFrame, ...],
    *,
    kind: Literal["exception_path", "wait_path"],
    failure_type: str,
    complete: bool,
    detail: tuple[str, ...] = (),
) -> FailureFingerprint | None:
    """规范化完整采集中的应用路径；采集器负责证明完整性和模块身份。

    Note:
        不包含绝对文件路径、行号或采集时间。保留调用顺序、调用点与错误详情，
        不删除任意数字或参数。没有应用调用点时不生成指纹。
    """
    if not complete or not failure_type or not frames:
        return None
    application = [frame for frame in frames if frame.module.split(".")[0] not in _INFRASTRUCTURE]
    if not application or any(
        not frame.module or not frame.function or not frame.code.strip() for frame in application
    ):
        return None
    payload = {
        "failure_type": failure_type,
        "frames": [(frame.module, frame.function, frame.code.strip()) for frame in application],
        "detail": [
            _TIMESTAMP.sub("<time>", _VOLATILE_LABEL.sub(r"\1=<id>", item)) for item in detail
        ],
    }
    return FailureFingerprint(
        kind=kind,
        algorithm_revision="application-path-v1",
        digest=hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
    )


def fingerprint_exception(exception: BaseException) -> FailureFingerprint | None:
    """读取真实 traceback 和异常链；异常组或不完整现场暂不自动聚合。"""
    frames: list[FailureFrame] = []
    details: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exception
    while current is not None:
        if id(current) in seen or isinstance(current, BaseExceptionGroup):
            return None
        seen.add(id(current))
        traceback = current.__traceback__
        if traceback is None:
            return None
        details.append(f"{type(current).__module__}.{type(current).__qualname__}: {current}")
        while traceback is not None:
            frame = traceback.tb_frame
            frames.append(
                FailureFrame(
                    module=str(frame.f_globals.get("__name__", "")),
                    function=frame.f_code.co_qualname,
                    code=linecache.getline(frame.f_code.co_filename, traceback.tb_lineno).strip(),
                )
            )
            traceback = traceback.tb_next
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    return fingerprint_frames(
        tuple(frames),
        kind="exception_path",
        failure_type=f"{type(exception).__module__}.{type(exception).__qualname__}",
        complete=True,
        detail=tuple(details),
    )
