from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from nbtriage.capability_analysis import TeachingRole, TeachingScene


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
            ),
            PermissionSemantic(
                "GROUP",
                PublicConstraintKind.SCENE,
                "group_chat",
                teaching_scene=TeachingScene.GROUP,
            ),
            PermissionSemantic(
                "GUILD",
                PublicConstraintKind.SCENE,
                "guild_or_channel",
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
            ),
            PermissionSemantic(
                "GROUP_OWNER",
                PublicConstraintKind.ROLE,
                "owner",
                TeachingRole.OWNER,
            ),
        ),
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
                "NoneBot 依赖函数的 Bot、Event 和 Matcher 参数类型注解参与重载筛选；实际对象不匹配时不会执行该依赖函数。",
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
    "nonebot_dependency_overload_profile",
    "nonebot_permission_profile",
    "onebot_v11_permission_profile",
    "public_permission_statement",
    "uninfo_permission_profile",
    "uninfo_session_field_profile",
)
