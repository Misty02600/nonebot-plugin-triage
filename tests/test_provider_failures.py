import pytest

from nbtriage.provider_failures import (
    ProviderFailureReason,
    classify_provider_http_status,
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
