from __future__ import annotations

from enum import StrEnum


class ProviderFailureReason(StrEnum):
    REQUEST_REJECTED = "request_rejected"
    PROVIDER_TIMEOUT = "provider_timeout"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TRANSPORT_ERROR = "transport_error"
    UNCLASSIFIED_PROVIDER_ERROR = "unclassified_provider_error"


def classify_provider_http_status(status_code: int) -> ProviderFailureReason:
    if status_code == 408:
        return ProviderFailureReason.PROVIDER_TIMEOUT
    if status_code == 429:
        return ProviderFailureReason.RATE_LIMITED
    if 400 <= status_code < 500:
        return ProviderFailureReason.REQUEST_REJECTED
    if 500 <= status_code < 600:
        return ProviderFailureReason.SERVER_ERROR
    return ProviderFailureReason.UNCLASSIFIED_PROVIDER_ERROR


_TRANSPORT_TIMEOUT_TYPE_NAMES = frozenset(
    {"APITimeoutError", "ConnectTimeout", "PoolTimeout", "ReadTimeout", "WriteTimeout"}
)


def is_transport_timeout(error: BaseException) -> bool:
    """判断单个异常是否为传输超时（含 httpx/httpcore 的非 TimeoutError 子类）。"""
    return isinstance(error, TimeoutError) or type(error).__name__ in _TRANSPORT_TIMEOUT_TYPE_NAMES
