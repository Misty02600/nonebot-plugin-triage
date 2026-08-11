"""尚未接入插件运行路径的 Alconna 能力快照实验。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from arclet.alconna import Alconna, Arparma, Empty, Option, Subcommand, command_manager
from arclet.alconna.base import Completion, Help, Shortcut
from arclet.alconna.exceptions import (
    ArgumentMissing,
    FuzzyMatchSuccess,
    InvalidParam,
    ParamsUnmatched,
    SpecialOptionTriggered,
    UnexpectedElement,
)

from nbtriage.intake import CommandStatus

ALCONNA_CAPABILITY_SCHEMA_VERSION = 1
ALCONNA_PARSE_RECEIPT_SCHEMA_VERSION = 1

BUILTIN_COMPONENT_TYPES = (Help, Completion, Shortcut)


class AlconnaIntegrationError(ValueError):
    pass


class CapabilityComponentKind(StrEnum):
    OPTION = "option"
    SUBCOMMAND = "subcommand"


class AlconnaParseReason(StrEnum):
    MATCHED = "matched"
    BUILTIN_OPTION = "builtin_option"
    HEADER_UNMATCHED = "header_unmatched"
    FUZZY_HEADER_SUGGESTION = "fuzzy_header_suggestion"
    ARGUMENT_MISSING = "argument_missing"
    INVALID_PARAMETER = "invalid_parameter"
    UNMATCHED_PARAMETER = "unmatched_parameter"
    UNEXPECTED_ELEMENT = "unexpected_element"


@dataclass(frozen=True)
class CapabilityArgument:
    path: str
    name: str
    required: bool
    value_type: str | None
    description: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "required": self.required,
            "value_type": self.value_type,
            "description": self.description,
        }


@dataclass(frozen=True)
class CapabilityComponent:
    path: str
    kind: CapabilityComponentKind
    names: tuple[str, ...]
    description: str | None
    arguments: tuple[CapabilityArgument, ...]
    components: tuple[CapabilityComponent, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind.value,
            "names": list(self.names),
            "description": self.description,
            "arguments": [argument.to_dict() for argument in self.arguments],
            "components": [component.to_dict() for component in self.components],
        }


@dataclass(frozen=True)
class AlconnaCapability:
    schema_version: int
    capability_id: str
    namespace: str
    header: str
    description: str
    usage: str | None
    example: str | None
    enabled: bool
    arguments: tuple[CapabilityArgument, ...]
    components: tuple[CapabilityComponent, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "namespace": self.namespace,
            "header": self.header,
            "description": self.description,
            "usage": self.usage,
            "example": self.example,
            "enabled": self.enabled,
            "arguments": [argument.to_dict() for argument in self.arguments],
            "components": [component.to_dict() for component in self.components],
        }


@dataclass(frozen=True)
class AlconnaParseReceipt:
    schema_version: int
    capability_id: str
    status: CommandStatus
    reason: AlconnaParseReason
    head_matched: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "status": self.status.value,
            "reason": self.reason.value,
            "head_matched": self.head_matched,
        }


def snapshot_alconna_capabilities(
    commands: Iterable[Alconna] | None = None,
    *,
    namespace: str = "",
    include_hidden: bool = False,
    include_disabled: bool = False,
) -> tuple[AlconnaCapability, ...]:
    """从已注册 Alconna 命令构造无执行能力的结构化快照。

    `meta.extra`、行为器、executor 和 Matcher 不进入快照。元数据文本来自已安装插件，后续交给模型时
    仍须作为不受信证据处理，不能当作系统指令。

    Args:
        commands: 可选的已注册命令集合；省略时读取全局 `command_manager`。
        namespace: 省略 `commands` 时用于限制 Alconna 命名空间；空字符串表示全部命名空间。
        include_hidden: 是否包含 `CommandMeta.hide=True` 的命令。
        include_disabled: 是否包含命令管理器当前标记为停用的命令。

    Returns:
        按命名空间、能力标识和展示头稳定排序的不可变能力快照。
    """
    if commands is not None and namespace:
        raise AlconnaIntegrationError("namespace cannot be combined with explicit commands")

    source = list(commands) if commands is not None else command_manager.get_commands(namespace)
    capabilities = []
    for command in source:
        if not isinstance(command, Alconna):
            raise AlconnaIntegrationError("commands must contain registered Alconna instances")
        if not include_hidden and command.meta.hide:
            continue
        disabled = command_manager.is_disable(command)
        if disabled and not include_disabled:
            continue
        capabilities.append(_capability_from_command(command, enabled=not disabled))
    return tuple(
        sorted(
            capabilities,
            key=lambda item: (item.namespace, item.capability_id, item.header),
        )
    )


def adapt_alconna_parse_result(
    command: Alconna,
    result: Arparma,
) -> AlconnaParseReceipt:
    """把调用链已经产生的 `Arparma` 转为最小化入口回执。

    本函数不会重新解析原消息，因为 `Alconna.parse()` 可能运行 behavior 或绑定 executor。无法证明来源
    一致或遇到未冻结的异常类型时直接拒绝，避免把框架 / 行为错误猜成用户参数错误。

    Args:
        command: 产生该解析结果的已注册命令。
        result: NoneBot / Alconna 现有解析调用链返回的真实结果。

    Returns:
        不包含 `origin`、`error_data`、异常文本和匹配值的固定状态回执。

    Raises:
        AlconnaIntegrationError: 类型、命令来源或解析失败类型不在冻结契约内。
    """
    if not isinstance(command, Alconna):
        raise AlconnaIntegrationError("command must be an Alconna instance")
    if not isinstance(result, Arparma):
        raise AlconnaIntegrationError("result must be an Arparma instance")
    try:
        source = result.source
    except (KeyError, ValueError) as error:
        raise AlconnaIntegrationError("parse result source is no longer registered") from error
    if source is not command:
        raise AlconnaIntegrationError("parse result belongs to a different command")

    error = result.error_info
    if result.matched:
        return _parse_receipt(command, CommandStatus.PARSED, AlconnaParseReason.MATCHED, True)
    if _is_error(error, SpecialOptionTriggered):
        return _parse_receipt(
            command,
            CommandStatus.PARSED,
            AlconnaParseReason.BUILTIN_OPTION,
            result.head_matched,
        )
    if not result.head_matched:
        reason = (
            AlconnaParseReason.FUZZY_HEADER_SUGGESTION
            if _is_error(error, FuzzyMatchSuccess)
            else AlconnaParseReason.HEADER_UNMATCHED
        )
        return _parse_receipt(command, CommandStatus.UNKNOWN_COMMAND, reason, False)
    if _is_error(error, ArgumentMissing):
        return _parse_receipt(
            command,
            CommandStatus.MISSING_ARGUMENT,
            AlconnaParseReason.ARGUMENT_MISSING,
            True,
        )
    if _is_error(error, ParamsUnmatched):
        return _parse_receipt(
            command,
            CommandStatus.INVALID_ARGUMENT,
            AlconnaParseReason.UNMATCHED_PARAMETER,
            True,
        )
    if _is_error(error, InvalidParam):
        return _parse_receipt(
            command,
            CommandStatus.INVALID_ARGUMENT,
            AlconnaParseReason.INVALID_PARAMETER,
            True,
        )
    if _is_error(error, UnexpectedElement):
        return _parse_receipt(
            command,
            CommandStatus.INVALID_ARGUMENT,
            AlconnaParseReason.UNEXPECTED_ELEMENT,
            True,
        )
    raise AlconnaIntegrationError("unsupported Alconna parse outcome")


def _capability_from_command(command: Alconna, *, enabled: bool) -> AlconnaCapability:
    return AlconnaCapability(
        schema_version=ALCONNA_CAPABILITY_SCHEMA_VERSION,
        capability_id=command.path,
        namespace=command.namespace,
        header=command.header_display,
        description=command.meta.description,
        usage=_optional_text(command.meta.usage),
        example=_optional_text(command.meta.example),
        enabled=enabled,
        arguments=_arguments(command.args.argument, "main"),
        components=tuple(
            _component(option, "")
            for option in command.options
            if not isinstance(option, BUILTIN_COMPONENT_TYPES)
        ),
    )


def _component(node: Option | Subcommand, parent_path: str) -> CapabilityComponent:
    path = f"{parent_path}.{node.dest}" if parent_path else node.dest
    children = (
        tuple(
            _component(child, path)
            for child in node.options
            if not isinstance(child, BUILTIN_COMPONENT_TYPES)
        )
        if isinstance(node, Subcommand)
        else ()
    )
    return CapabilityComponent(
        path=path,
        kind=(
            CapabilityComponentKind.SUBCOMMAND
            if isinstance(node, Subcommand)
            else CapabilityComponentKind.OPTION
        ),
        names=tuple(sorted(str(alias) for alias in node.aliases)),
        description=_optional_text(node.help_text),
        arguments=_arguments(node.args.argument, path),
        components=children,
    )


def _arguments(arguments: Iterable[Any], parent_path: str) -> tuple[CapabilityArgument, ...]:
    return tuple(
        CapabilityArgument(
            path=f"{parent_path}.{argument.name}",
            name=argument.name,
            required=not argument.optional and argument.field.default is Empty,
            value_type=None if argument.hidden else str(argument.value),
            description=_optional_text(argument.notice),
        )
        for argument in arguments
    )


def _parse_receipt(
    command: Alconna,
    status: CommandStatus,
    reason: AlconnaParseReason,
    head_matched: bool,
) -> AlconnaParseReceipt:
    return AlconnaParseReceipt(
        schema_version=ALCONNA_PARSE_RECEIPT_SCHEMA_VERSION,
        capability_id=command.path,
        status=status,
        reason=reason,
        head_matched=head_matched,
    )


def _is_error(error: Any, error_type: type[Exception]) -> bool:
    if isinstance(error, type):
        return issubclass(error, error_type)
    return isinstance(error, error_type)


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
