from __future__ import annotations

import argparse
import importlib.util
import os

import nonebot
from pydantic_ai.models import infer_model

from nbtriage.model_adapters import PydanticAIB1Client
from nbtriage.pydantic_agent_adapter import PydanticAIAgentStepClient


def verify(provider: str) -> None:
    expected_module = provider
    excluded_module = "openai" if provider == "anthropic" else "anthropic"
    for module_name in (expected_module, "pydantic_ai"):
        if importlib.util.find_spec(module_name) is None:
            raise RuntimeError(f"{provider} extra did not install {module_name}")
    if importlib.util.find_spec(excluded_module) is not None:
        raise RuntimeError(f"{provider} extra unexpectedly installed {excluded_module}")

    nonebot.init(driver="~none")
    if nonebot.load_plugin("nonebot_plugin_triage") is None:
        raise RuntimeError(f"plugin failed to load with only the {provider} model extra")

    model_id, key_env = (
        ("anthropic:claude-sonnet-4-5", "ANTHROPIC_API_KEY")
        if provider == "anthropic"
        else ("openai:gpt-4.1-mini", "OPENAI_API_KEY")
    )
    os.environ.setdefault(key_env, "model-extra-isolation-placeholder")
    model = infer_model(model_id)
    PydanticAIB1Client(model, provider=model.system, max_calls=1)
    PydanticAIAgentStepClient(
        model,
        provider=model.system,
        timeout_seconds=60.0,
        max_calls=1,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=("anthropic", "openai"))
    verify(parser.parse_args().provider)
