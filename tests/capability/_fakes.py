from __future__ import annotations

from nbtriage.capability.teaching.analysis import (
    CapabilityAnalysisError,
    CapabilityAnalysisOutput,
    CapabilityAnalysisRequest,
)


class FakeCapabilityAnalysisClient:
    def __init__(self, output: CapabilityAnalysisOutput) -> None:
        self.output = output
        self.requests: list[CapabilityAnalysisRequest] = []
        self._called = False

    async def analyze(self, request: CapabilityAnalysisRequest) -> CapabilityAnalysisOutput:
        if self._called:
            raise CapabilityAnalysisError("capability analysis client only permits one request")
        self._called = True
        self.requests.append(request)
        return self.output
