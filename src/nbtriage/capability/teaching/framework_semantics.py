from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from nbtriage.capability.teaching.analysis import TeachingRole, TeachingScene


class PublicConstraintKind(StrEnum):
    ROLE = "role"
    SCENE = "scene"


@dataclass(frozen=True)
class PermissionSemantic:
    symbol: str
    kind: PublicConstraintKind
    operation: str
    teaching_role: TeachingRole | None = None
    teaching_scene: TeachingScene | None = None
    runtime_checkers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PermissionSemanticProfile:
    component: str
    import_roots: tuple[str, ...]
    permissions: tuple[PermissionSemantic, ...]

    @property
    def revision(self) -> str:
        payload = {
            "component": self.component,
            "import_roots": self.import_roots,
            "permissions": [
                {
                    "symbol": item.symbol,
                    "kind": item.kind.value,
                    "operation": item.operation,
                    "teaching_role": (
                        item.teaching_role.value if item.teaching_role is not None else None
                    ),
                    "teaching_scene": (
                        item.teaching_scene.value if item.teaching_scene is not None else None
                    ),
                    "runtime_checkers": list(item.runtime_checkers),
                }
                for item in self.permissions
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"sha256:{digest}"

    def resolve(self, qualified_name: str) -> PermissionSemantic | None:
        module, separator, symbol = qualified_name.rpartition(".")
        if not separator or module not in self.import_roots:
            return None
        return next((item for item in self.permissions if item.symbol == symbol), None)

    def resolve_runtime_checker(self, qualified_name: str) -> PermissionSemantic | None:
        return next(
            (item for item in self.permissions if qualified_name in item.runtime_checkers),
            None,
        )


@dataclass(frozen=True)
class FrameworkFieldSemantic:
    symbol: str
    statement: str


@dataclass(frozen=True)
class FrameworkFieldSemanticProfile:
    component: str
    annotations: tuple[str, ...]
    fields: tuple[FrameworkFieldSemantic, ...]

    @property
    def revision(self) -> str:
        payload = {
            "component": self.component,
            "annotations": self.annotations,
            "fields": [
                {"symbol": item.symbol, "statement": item.statement} for item in self.fields
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"sha256:{digest}"


def uninfo_permission_profile() -> PermissionSemanticProfile:
    return PermissionSemanticProfile(
        component="nonebot-plugin-uninfo",
        import_roots=("nonebot_plugin_uninfo", "nonebot_plugin_uninfo.permission"),
        permissions=(
            PermissionSemantic(
                "MEMBER",
                PublicConstraintKind.ROLE,
                "not_administrator_or_owner",
                TeachingRole.CUSTOM,
            ),
            PermissionSemantic(
                "ADMIN",
                PublicConstraintKind.ROLE,
                "administrator_or_owner",
                TeachingRole.ADMIN,
            ),
            PermissionSemantic(
                "OWNER",
                PublicConstraintKind.ROLE,
                "owner",
                TeachingRole.OWNER,
            ),
            PermissionSemantic(
                "PRIVATE",
                PublicConstraintKind.SCENE,
                "private_chat",
                teaching_scene=TeachingScene.PRIVATE,
                runtime_checkers=("nonebot_plugin_uninfo.permission._private",),
            ),
            PermissionSemantic(
                "GROUP",
                PublicConstraintKind.SCENE,
                "group_chat",
                teaching_scene=TeachingScene.GROUP,
                runtime_checkers=("nonebot_plugin_uninfo.permission._group",),
            ),
            PermissionSemantic(
                "GUILD",
                PublicConstraintKind.SCENE,
                "guild_or_channel",
                runtime_checkers=("nonebot_plugin_uninfo.permission._guild",),
            ),
        ),
    )


def nonebot_permission_profile() -> PermissionSemanticProfile:
    return PermissionSemanticProfile(
        component="nonebot",
        import_roots=("nonebot.permission",),
        permissions=(
            PermissionSemantic(
                "SUPERUSER",
                PublicConstraintKind.ROLE,
                "superuser",
                TeachingRole.SUPERUSER,
                runtime_checkers=("nonebot.permission.SuperUser",),
            ),
        ),
    )


def onebot_v11_permission_profile() -> PermissionSemanticProfile:
    return PermissionSemanticProfile(
        component="nonebot-adapter-onebot-v11",
        import_roots=(
            "nonebot.adapters.onebot.v11",
            "nonebot.adapters.onebot.v11.permission",
        ),
        permissions=(
            PermissionSemantic(
                "GROUP_ADMIN",
                PublicConstraintKind.ROLE,
                "administrator",
                TeachingRole.ADMIN,
                runtime_checkers=("nonebot.adapters.onebot.v11.permission._group_admin",),
            ),
            PermissionSemantic(
                "GROUP_OWNER",
                PublicConstraintKind.ROLE,
                "owner",
                TeachingRole.OWNER,
                runtime_checkers=("nonebot.adapters.onebot.v11.permission._group_owner",),
            ),
        ),
    )


def builtin_permission_semantic_profiles() -> tuple[PermissionSemanticProfile, ...]:
    return (
        nonebot_permission_profile(),
        onebot_v11_permission_profile(),
        uninfo_permission_profile(),
    )


def uninfo_session_field_profile() -> FrameworkFieldSemanticProfile:
    return FrameworkFieldSemanticProfile(
        component="nonebot-plugin-uninfo",
        annotations=(
            "Uninfo",
            "nonebot_plugin_uninfo.Uninfo",
            "QryItrface",
            "nonebot_plugin_uninfo.QryItrface",
        ),
        fields=(
            FrameworkFieldSemantic(
                "Uninfo",
                "NoneBot Handler 可通过 Uninfo 依赖注入取得跨适配器的当前 Session。",
            ),
            FrameworkFieldSemantic(
                "Session.self_id",
                "当前机器人账号 ID，不是触发事件的用户或调用者 ID。",
            ),
            FrameworkFieldSemantic(
                "Session.adapter",
                "当前适配器名称。",
            ),
            FrameworkFieldSemantic(
                "Session.scope",
                "比 adapter 更具体的平台范围，不表示当前调用者。",
            ),
            FrameworkFieldSemantic(
                "Session.scene",
                "当前事件所在的私聊、群聊、频道或子频道场景。",
            ),
            FrameworkFieldSemantic(
                "Session.user",
                "触发当前事件的用户信息。",
            ),
            FrameworkFieldSemantic(
                "Session.user.id",
                "触发当前事件的用户 ID；需要用户级作用域时应确认该值实际进入执行键。",
            ),
            FrameworkFieldSemantic(
                "Session.member",
                "当前用户在群聊或频道场景中的成员信息；私聊场景通常不存在。",
            ),
            FrameworkFieldSemantic(
                "Session.member.id",
                "群聊或频道场景中的成员用户 ID，与机器人 self_id 不同。",
            ),
            FrameworkFieldSemantic(
                "Session.operator",
                "群聊或频道事件的操作者成员信息，并不保证等于触发事件的 user。",
            ),
            FrameworkFieldSemantic(
                "User",
                "用户公开模型；id 是用户 ID，name、nick、avatar、gender 分别描述名称、备注昵称、头像和性别。",
            ),
            FrameworkFieldSemantic(
                "Scene",
                "事件场景模型；id 是场景 ID，type 区分 Private、Group、Guild 与不同 Channel 类型，parent 表示父级场景。",
            ),
            FrameworkFieldSemantic(
                "Session.scene.id",
                "当前群、频道或私聊等场景的 ID，不保证是当前调用者 ID。",
            ),
            FrameworkFieldSemantic(
                "Session.scene.parent",
                "当前会话所属的直接上级，可能为空；具体含义由适配器决定，不能固定称为服务器，也不等于最顶层。"
                "业务代码取 parent.id、无父级时取 scene.id，表示按直接上级或当前会话确定作用范围；"
                "其他键维度相同且选中同一场景 ID 时共享该范围，不是各子频道或话题独立。"
                "仅存在 parent 不证明共享，实际范围仍由业务代码使用的完整执行键决定。",
            ),
            FrameworkFieldSemantic(
                "Member",
                "群聊或频道成员模型；user 是成员用户，role 返回已有角色中权限等级最高者，另含 nick、mute 与 joined_at。",
            ),
            FrameworkFieldSemantic(
                "Session.scene_path",
                "会话场景路径；群聊中通常按群场景共享，私聊中才会包含用户维度，不能一概解释为调用者 ID。",
            ),
            FrameworkFieldSemantic(
                "Session.id",
                "Uninfo 的会话唯一标识；非私聊场景会在 scene_path 后加入 user.id，语义不同于 scene_path。",
            ),
            FrameworkFieldSemantic(
                "QryItrface",
                "按当前适配器查询用户、场景和场景成员的接口；get_users、get_scenes 与 get_members 返回运行时可查询对象。",
            ),
            FrameworkFieldSemantic(
                "built-in permissions",
                "ADMIN、OWNER、MEMBER 与 PRIVATE、GROUP、GUILD 是 Uninfo 的内建 Permission；项目会在注册表达式可归属时另行投影为固定角色或场景约束。",
            ),
        ),
    )


def nonebot_dependency_overload_profile() -> FrameworkFieldSemanticProfile:
    return FrameworkFieldSemanticProfile(
        component="nonebot2",
        annotations=(
            "Bot",
            "Event",
            "Matcher",
            "MessageEvent",
            "PrivateMessageEvent",
            "GroupMessageEvent",
            "NoticeEvent",
            "RequestEvent",
            "MetaEvent",
        ),
        fields=(
            FrameworkFieldSemantic(
                "typed dependency overload",
                "NoneBot 的 Handler 及其依赖函数的 Bot、Event 和 Matcher 参数类型注解都参与运行时检查；"
                "实际对象不匹配时不会执行相应函数；同一 Matcher 的其他 Handler 应分别判断。"
                "例如，nonebot-adapter-onebot 2.4.6 的 nonebot.adapters.onebot.v11.GroupMessageEvent "
                "表示群消息；当前 Evidence 确认参数限定为该类型时，对应 group（群聊）限制，"
                "不只是排除私聊。此例不能仅凭同名套用于其他 Adapter 或自定义类型；"
                "类型来源或含义不明时，不得据此放宽为所有非私聊场景。"
                "Handler 执行先递归预检查依赖及自身参数类型，通过后才求解依赖并调用函数；"
                "预检查依赖不等于执行依赖函数体。标准 .got() 的取参与提示作为该 Handler 的"
                "无参数依赖在求解阶段执行，因此类型预检查失败时也不会发送这条确认提示；"
                "不能把它当成独立于该 Handler 类型限制的前置步骤。",
            ),
        ),
    )


def nepattern_anti_pattern_profile() -> FrameworkFieldSemanticProfile:
    return FrameworkFieldSemanticProfile(
        component="nepattern",
        annotations=(),
        fields=(
            FrameworkFieldSemantic(
                "nepattern.base.AntiPattern",
                "AntiPattern 对基础匹配规则进行反向验证。基础类型不代表允许输入的类型，"
                "也不足以描述完整的匹配规则；具体输入条件及验证失败后的行为，"
                "应结合基础规则、默认值、可选性和实际处理代码判断。"
                "运行时 pattern_type 保留 AntiPattern 的身份，不提供完整基础规则；"
                "不能仅凭这个标记推断具体排除哪些输入。",
            ),
        ),
    )


def alconna_dispatch_profile() -> FrameworkFieldSemanticProfile:
    return FrameworkFieldSemanticProfile(
        component="nonebot-plugin-alconna",
        annotations=(),
        fields=(
            FrameworkFieldSemantic(
                "AlconnaMatcher.dispatch",
                "dispatch 按已经解析的 Alconna path 选择同一命令的 Matcher 分支；"
                "路径存在判断本身是命令路由，不表示角色、资格或场景限制；"
                "未指定 value 时，or_not=True 表示主入口（无 Option/子命令）或目标路径，属于同一 Matcher 的调用形式；"
                "父子 Matcher 独立分析，入口重叠不表示功能互斥或应合并条目；"
                "additional 回调则是独立执行条件，必须依据其实现解释，不能当成纯路由忽略。",
            ),
        ),
    )


_PUBLIC_PERMISSION_STATEMENTS = {
    (PublicConstraintKind.ROLE, "superuser"): "仅超级用户可用",
    (PublicConstraintKind.ROLE, "administrator"): "仅群管理员可用",
    (
        PublicConstraintKind.ROLE,
        "not_administrator_or_owner",
    ): "仅非频道管理员、非群管理员、非群主的普通成员可用",
    (PublicConstraintKind.ROLE, "administrator_or_owner"): "仅群管理员或群主可用",
    (PublicConstraintKind.ROLE, "owner"): "仅群主可用",
    (PublicConstraintKind.SCENE, "private_chat"): "仅私聊可用",
    (PublicConstraintKind.SCENE, "group_chat"): "仅群聊可用",
}


def public_permission_statement(kind: PublicConstraintKind, operation: str) -> str:
    try:
        return _PUBLIC_PERMISSION_STATEMENTS[(kind, operation)]
    except KeyError as error:
        raise ValueError("permission semantic has no public statement") from error


__all__ = (
    "FrameworkFieldSemantic",
    "FrameworkFieldSemanticProfile",
    "PermissionSemantic",
    "PermissionSemanticProfile",
    "PublicConstraintKind",
    "alconna_dispatch_profile",
    "builtin_permission_semantic_profiles",
    "nepattern_anti_pattern_profile",
    "nonebot_dependency_overload_profile",
    "nonebot_permission_profile",
    "onebot_v11_permission_profile",
    "public_permission_statement",
    "uninfo_permission_profile",
    "uninfo_session_field_profile",
)
