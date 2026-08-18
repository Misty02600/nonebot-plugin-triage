from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from nbtriage.capability_analysis import TeachingRole


class PublicConstraintKind(StrEnum):
    ROLE = "role"
    SCENE = "scene"


@dataclass(frozen=True)
class PermissionSemantic:
    symbol: str
    kind: PublicConstraintKind
    operation: str
    teaching_role: TeachingRole | None = None


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
            PermissionSemantic("PRIVATE", PublicConstraintKind.SCENE, "private_chat"),
            PermissionSemantic("GROUP", PublicConstraintKind.SCENE, "group_chat"),
            PermissionSemantic("GUILD", PublicConstraintKind.SCENE, "guild_or_channel"),
        ),
    )


_PUBLIC_PERMISSION_STATEMENTS = {
    (PublicConstraintKind.ROLE, "not_administrator_or_owner"): "仅非管理员、非群主的普通成员可用",
    (PublicConstraintKind.ROLE, "administrator_or_owner"): "仅群管理员或群主可用",
    (PublicConstraintKind.ROLE, "owner"): "仅群主可用",
    (PublicConstraintKind.SCENE, "private_chat"): "仅私聊可用",
    (PublicConstraintKind.SCENE, "group_chat"): "仅群聊可用",
    (PublicConstraintKind.SCENE, "guild_or_channel"): "仅频道或子频道场景可用",
}


def public_permission_statement(kind: PublicConstraintKind, operation: str) -> str:
    try:
        return _PUBLIC_PERMISSION_STATEMENTS[(kind, operation)]
    except KeyError as error:
        raise ValueError("permission semantic has no public statement") from error


__all__ = (
    "PermissionSemantic",
    "PermissionSemanticProfile",
    "PublicConstraintKind",
    "public_permission_statement",
    "uninfo_permission_profile",
)
