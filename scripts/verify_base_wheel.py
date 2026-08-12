from __future__ import annotations

import importlib.util
from importlib.metadata import distribution

import nonebot

MAINTAINER_MODULES = (
    "nbtriage.__main__",
    "nbtriage.alconna_capabilities",
    "nbtriage.agent_evaluation",
    "nbtriage.answer_quality_evaluation",
    "nbtriage.answer_review_export",
    "nbtriage.bot_docs",
    "nbtriage.bot_docs_evaluation",
    "nbtriage.cli",
    "nbtriage.collector",
    "nbtriage.curation",
    "nbtriage.discovery",
    "nbtriage.deepseek_adapter",
    "nbtriage.evaluation",
    "nbtriage.evidence_policy",
    "nbtriage.evidence_policy_evaluation",
    "nbtriage.evidence_receipt_evaluation",
    "nbtriage.gate",
    "nbtriage.github",
    "nbtriage.mlflow_tracking",
    "nbtriage.models",
    "nbtriage.providers",
    "nbtriage.runtime_results",
    "nbtriage.safety_evaluation",
    "nbtriage.sessions",
    "nbtriage.timeline",
)


def _module_exists(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        return False


def verify() -> None:
    for module_name in ("anthropic", "mlflow", "openai", "pydantic_ai"):
        if _module_exists(module_name):
            raise RuntimeError(f"base wheel unexpectedly installed {module_name}")
    if _module_exists("nbtriage.opencode_go_adapter"):
        raise RuntimeError("base wheel unexpectedly includes the OpenCode Go test backend")
    if _module_exists("tools.nbtriage_maintainer"):
        raise RuntimeError("base wheel unexpectedly includes maintainer tooling")
    for module_name in MAINTAINER_MODULES:
        if _module_exists(module_name):
            raise RuntimeError(f"base wheel unexpectedly includes {module_name}")
    if not _module_exists("nonebot_plugin_localstore"):
        raise RuntimeError("base wheel is missing the LocalStore runtime dependency")

    console_scripts = {
        entry_point.name
        for entry_point in distribution("nonebot-plugin-triage").entry_points
        if entry_point.group == "console_scripts"
    }
    if "nbtriage" in console_scripts:
        raise RuntimeError("base wheel unexpectedly installs the nbtriage console script")

    nonebot.init(driver="~none")
    if nonebot.load_plugin("nonebot_plugin_triage") is None:
        raise RuntimeError("base wheel could not load nonebot_plugin_triage")
    import nonebot_plugin_triage

    if nonebot_plugin_triage.handlers.plugin_runtime.model_service is not None:
        raise RuntimeError("base wheel unexpectedly enabled the model service")


if __name__ == "__main__":
    verify()
