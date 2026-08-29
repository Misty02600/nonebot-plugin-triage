from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_plugin_runtime(isolate_live_semantic_transport: None) -> None:
    pass
