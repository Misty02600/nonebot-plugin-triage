from __future__ import annotations

from types import MappingProxyType

from .models import InstalledComponentSpec, InstalledSourceError

_PUBLIC_FRAMEWORK_SPECS = {
    "nonebot2": InstalledComponentSpec("nonebot2", "nonebot2", "nonebot"),
    "nonebot-adapter-onebot": InstalledComponentSpec(
        "nonebot-adapter-onebot",
        "nonebot-adapter-onebot",
        "nonebot.adapters.onebot",
    ),
    "nonebot-plugin-alconna": InstalledComponentSpec(
        "nonebot-plugin-alconna",
        "nonebot-plugin-alconna",
        "nonebot_plugin_alconna",
    ),
    "nonebot-plugin-uninfo": InstalledComponentSpec(
        "nonebot-plugin-uninfo",
        "nonebot-plugin-uninfo",
        "nonebot_plugin_uninfo",
    ),
}

PUBLIC_FRAMEWORK_SPECS = MappingProxyType(_PUBLIC_FRAMEWORK_SPECS)


def public_framework_spec(component: str) -> InstalledComponentSpec:
    """返回获准自动读取的公开框架组件，拒绝任意包名和私有插件。"""
    try:
        return PUBLIC_FRAMEWORK_SPECS[component]
    except KeyError as error:
        raise InstalledSourceError(
            f"public framework component is not approved: {component}"
        ) from error


def is_public_framework_spec(spec: InstalledComponentSpec) -> bool:
    return spec in PUBLIC_FRAMEWORK_SPECS.values()


__all__ = ["PUBLIC_FRAMEWORK_SPECS", "is_public_framework_spec", "public_framework_spec"]
