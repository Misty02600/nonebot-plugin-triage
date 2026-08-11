from __future__ import annotations

import argparse
import importlib.util

import nonebot


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

    if provider == "anthropic":
        from nbtriage.anthropic_adapter import (
            create_anthropic_messages_agent_step_client,
            create_anthropic_messages_b1_client,
        )

        create_anthropic_messages_b1_client(
            api_key="model-extra-isolation-placeholder",
            model="claude-sonnet-4-5",
            max_calls=1,
        )
        create_anthropic_messages_agent_step_client(
            api_key="model-extra-isolation-placeholder",
            model="claude-sonnet-4-5",
        )
    else:
        from nbtriage.openai_adapter import (
            create_openai_responses_agent_step_client,
            create_openai_responses_b1_client,
        )

        create_openai_responses_b1_client(
            api_key="model-extra-isolation-placeholder",
            model="gpt-4.1-mini",
            max_calls=1,
        )
        create_openai_responses_agent_step_client(
            api_key="model-extra-isolation-placeholder",
            model="gpt-4.1-mini",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=("anthropic", "openai"))
    verify(parser.parse_args().provider)
