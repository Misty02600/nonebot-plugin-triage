"""已安装公共框架与工具库的静态源码证据。"""

from .catalog import PUBLIC_FRAMEWORK_SPECS, public_framework_spec
from .models import (
    InstalledComponentSpec,
    InstalledSourceError,
    InstalledSourceFile,
    InstalledSourceRevision,
    SourceAvailability,
    SourceBinding,
    SourceOrigin,
)
from .resolver import (
    ResolvedSourceFile,
    ResolvedSourceInventory,
    SourceInventoryLimits,
    resolve_installed_source,
    resolve_source_inventory,
)

__all__ = [
    "PUBLIC_FRAMEWORK_SPECS",
    "InstalledComponentSpec",
    "InstalledSourceError",
    "InstalledSourceFile",
    "InstalledSourceRevision",
    "ResolvedSourceFile",
    "ResolvedSourceInventory",
    "SourceAvailability",
    "SourceBinding",
    "SourceInventoryLimits",
    "SourceOrigin",
    "public_framework_spec",
    "resolve_installed_source",
    "resolve_source_inventory",
]
