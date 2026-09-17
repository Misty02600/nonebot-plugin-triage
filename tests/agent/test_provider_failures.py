import pytest

from nbtriage._model_runtime.failures import (
    ProviderFailureReason,
    classify_provider_http_status,
    is_transport_timeout,
)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, ProviderFailureReason.REQUEST_REJECTED),
        (408, ProviderFailureReason.PROVIDER_TIMEOUT),
        (429, ProviderFailureReason.RATE_LIMITED),
        (499, ProviderFailureReason.REQUEST_REJECTED),
        (500, ProviderFailureReason.SERVER_ERROR),
        (599, ProviderFailureReason.SERVER_ERROR),
        (200, ProviderFailureReason.UNCLASSIFIED_PROVIDER_ERROR),
    ],
)
def test_classifies_provider_http_status_without_response_body(
    status_code: int,
    expected: ProviderFailureReason,
) -> None:
    assert classify_provider_http_status(status_code) is expected


def test_is_transport_timeout_recognizes_builtin_and_provider_name_timeouts() -> None:
    class ConnectTimeout(Exception):
        pass

    class ReadTimeout(Exception):
        pass

    assert is_transport_timeout(TimeoutError("boom")) is True
    assert is_transport_timeout(ConnectTimeout()) is True
    assert is_transport_timeout(ReadTimeout()) is True
    assert is_transport_timeout(ValueError("nope")) is False
    assert is_transport_timeout(Exception("nope")) is False
