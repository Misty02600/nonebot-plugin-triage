"""已安装公共框架与工具库的静态源码证据。"""

from .catalog import PUBLIC_FRAMEWORK_SPECS, public_framework_spec
from .models import (
    InstalledComponentSpec,
    InstalledSourceError,
    InstalledSourceFile,
    InstalledSourceRevision,
    InstalledSourceSnapshot,
    RelationPrecision,
    SourceAvailability,
    SourceBinding,
    SourceEvidence,
    SourceOrigin,
    SourceRelation,
    SourceRelationKind,
    SourceSearchHit,
    SourceSpan,
    SourceSymbol,
    SourceSymbolKind,
)
from .reader import (
    SourceReaderLimits,
    build_installed_source_snapshot,
    expand_relations,
    inspect_symbol,
    search_symbols,
)
from .resolver import SourceInventoryLimits, resolve_installed_source

__all__ = [
    "PUBLIC_FRAMEWORK_SPECS",
    "InstalledComponentSpec",
    "InstalledSourceError",
    "InstalledSourceFile",
    "InstalledSourceRevision",
    "InstalledSourceSnapshot",
    "RelationPrecision",
    "SourceAvailability",
    "SourceBinding",
    "SourceEvidence",
    "SourceInventoryLimits",
    "SourceOrigin",
    "SourceReaderLimits",
    "SourceRelation",
    "SourceRelationKind",
    "SourceSearchHit",
    "SourceSpan",
    "SourceSymbol",
    "SourceSymbolKind",
    "build_installed_source_snapshot",
    "expand_relations",
    "inspect_symbol",
    "public_framework_spec",
    "resolve_installed_source",
    "search_symbols",
]
