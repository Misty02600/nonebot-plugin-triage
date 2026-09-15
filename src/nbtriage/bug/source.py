from __future__ import annotations

import asyncio
import hashlib
import json
import sysconfig
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from nbtriage.bug.assessment import BUG_EVIDENCE_BODY_MAX_CHARS, BugEvidence, BugEvidenceKind
from nbtriage.bug.logs import redact_bug_evidence_text
from nbtriage.readonly_tools.models import (
    ReadOnlyRoot,
    ReadOnlyTaskProfile,
    normalized_locator,
    path_is_allowed,
)
from nbtriage.readonly_tools.pydantic_filesystem import build_read_only_file_toolsets
from nbtriage.readonly_tools.python_navigation import (
    DefinitionNavigator,
    GoToDefinitionRequest,
    PythonNavigationProfile,
)


@dataclass(frozen=True, slots=True)
class ApprovedSourceRoot:
    module_name: str
    root: Path
    file_name: str | None = None

    def __post_init__(self) -> None:
        root = ReadOnlyRoot("plugin", self.root)
        object.__setattr__(self, "root", root.path)
        if self.file_name is not None:
            locator = normalized_locator(self.file_name)
            if "/" in locator or Path(locator).suffix != ".py":
                raise ValueError("single-file plugin must name one Python file")

    def as_read_only_root(self, name: str) -> ReadOnlyRoot:
        patterns = (self.file_name,) if self.file_name is not None else ("*.py", "*.pyi")
        return ReadOnlyRoot(name, self.root, allowed_patterns=patterns)


@dataclass(frozen=True, slots=True)
class _ReadSpan:
    root_name: str
    relative_path: str
    revision: str
    start: int
    end: int


class BugSourceTools:
    """将共享文件工具和 ty 绑定到一次调查的源码范围与已读取证据。

    Note:
        插件根允许搜索和读取；依赖根只允许读取 ty 已定位且版本未变的文件。
        搜索和目录结果只用于导航，不能作为完整实现证据。
    """

    def __init__(
        self,
        roots: dict[str, ApprovedSourceRoot],
        *,
        dependency_paths: tuple[Path, ...] | None = None,
    ) -> None:
        plugin_roots = tuple(root.as_read_only_root(name) for name, root in roots.items())
        if not plugin_roots:
            raise ValueError("Bug source tools require a selected source root")
        if dependency_paths is None:
            dependency_paths = tuple(
                dict.fromkeys(Path(sysconfig.get_path(name)) for name in ("purelib", "platlib"))
            )
        dependencies = []
        for path in dependency_paths:
            if path.is_dir() and path.resolve() not in {r.path for r in plugin_roots}:
                dependencies.append(
                    ReadOnlyRoot(f"dependency{len(dependencies) + 1}", path, ("*.py", "*.pyi"))
                )
        self.access = ReadOnlyTaskProfile("bug-source", (*plugin_roots, *dependencies))
        self.source_paths = tuple(root.path for root in plugin_roots)
        self._plugin_names = frozenset(root.name for root in plugin_roots)
        self._dependency_files: dict[tuple[str, str], str] = {}
        self._spans: dict[str, _ReadSpan] = {}
        self._navigator = DefinitionNavigator(
            PythonNavigationProfile(
                self.access, plugin_roots[0].name, tuple(r.name for r in self.access.roots)
            )
        )
        allowed = {
            root.name: frozenset(
                {"read_file", "search_files", "find_files", "list_directory", "file_info"}
            )
            if root.name in self._plugin_names
            else frozenset({"read_file"})
            for root in self.access.roots
        }
        built = build_read_only_file_toolsets(self.access, tool_names_by_root=allowed)
        self._files = {
            root.name: _BugFileToolset(cast(AbstractToolset[Any], toolset), self, root)
            for root, toolset in zip(
                sorted(self.access.roots, key=lambda r: r.name), built.toolsets, strict=True
            )
        }
        navigation = FunctionToolset(tools=[self.open_source_definition])
        self.toolsets = (*self._files.values(), navigation)

    def _file_state(self, root: ReadOnlyRoot, path: str) -> tuple[str, str]:
        try:
            locator = normalized_locator(path)
            resolved = (root.path / locator).resolve(strict=True)
            relative = resolved.relative_to(root.path).as_posix()
            if not path_is_allowed(self.access, root, relative) or not resolved.is_file():
                raise ValueError("unapproved source file")
            raw = resolved.read_bytes()
            content = raw.decode("utf-8")
        except (OSError, ValueError, RuntimeError) as error:
            raise ModelRetry("Source file is unavailable or outside the approved scope.") from error
        revision = hashlib.sha256(raw).hexdigest()
        if (
            root.name not in self._plugin_names
            and self._dependency_files.get((root.name, relative)) != revision
        ):
            raise ModelRetry("Dependency reads require a current ty definition target.")
        return revision, content

    def _evidence(
        self, source: str, body: str, *, revision: str | None = None, partial: bool = True
    ) -> BugEvidence:
        body = redact_bug_evidence_text(body)
        if len(body) > BUG_EVIDENCE_BODY_MAX_CHARS:
            body = body[: BUG_EVIDENCE_BODY_MAX_CHARS - 32] + "\n[content truncated]"
            partial = True
        digest = hashlib.sha256(repr((source, revision, body, partial)).encode()).hexdigest()
        return BugEvidence(
            evidence_id=f"source:{digest}",
            kind=BugEvidenceKind.SOURCE_CODE,
            source=source,
            body=body or "(empty result)",
            revision=revision,
            current=True,
            partial=partial,
        )

    async def open_source_definition(
        self, ctx: RunContext[Any], evidence_id: str, line: int, column: int
    ) -> list[dict[str, object]]:
        """跳转已读取证据内的位置；行号从 1 开始，列号从 0 开始。

        唯一定义会自动读取；多个候选只返回定位结果，不能据此认定实际调用目标。
        """

        if ctx.deps.toolbox.tool_budget_exhausted:
            return [
                {
                    "status": "unavailable",
                    "reason": "tool_budget_exhausted",
                    "tool_name": "open_source_definition",
                }
            ]

        async def load() -> tuple[BugEvidence, ...]:
            span = self._spans.get(evidence_id)
            if (
                span is None
                or not span.start <= line <= span.end
                or evidence_id not in {item.evidence_id for item in ctx.deps.toolbox.evidence}
            ):
                raise ModelRetry(
                    "Choose a position inside a previously returned source evidence span."
                )
            result = await asyncio.to_thread(
                self._navigator.go_to_definition,
                GoToDefinitionRequest(
                    span.root_name, span.relative_path, line, column, span.revision
                ),
            )
            for definition in result.definitions:
                if definition.root_name not in self._plugin_names:
                    self._dependency_files[(definition.root_name, definition.relative_path)] = (
                        definition.source_revision
                    )
            if len(result.definitions) == 1:
                target = result.definitions[0]
                reader = self._files[target.root_name]
                return (
                    await reader.read(
                        {"path": target.relative_path, "offset": target.line - 1},
                        ctx,
                        expected_revision=target.source_revision,
                    ),
                )
            return (
                self._evidence(
                    "source:definition-navigation",
                    json.dumps(
                        {
                            "citable": False,
                            "definitions": [asdict(item) for item in result.definitions],
                            "failure": result.failure,
                            "ignored_failures": result.ignored_failures,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )

        return [
            item.model_dump(mode="json")
            for item in await ctx.deps.toolbox.source_tool(load, tool_name="open_source_definition")
        ]


class _BugFileToolset(WrapperToolset[Any]):
    def __init__(
        self, wrapped: AbstractToolset[Any], sources: BugSourceTools, root: ReadOnlyRoot
    ) -> None:
        super().__init__(wrapped)
        self._sources = sources
        self._root = root

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        return await super().get_tools(ctx)

    async def _call(self, name: str, args: dict[str, Any], ctx: RunContext[Any]) -> Any:
        tools = await self.wrapped.get_tools(ctx)
        tool = tools[name]

        # Harness 的遍历和文件读取是同步 I/O，在工具线程内执行，避免阻塞调查超时。
        def invoke() -> Any:
            return asyncio.run(self.wrapped.call_tool(name, args, ctx, tool))

        return await asyncio.to_thread(invoke)

    async def read(
        self, args: dict[str, Any], ctx: RunContext[Any], *, expected_revision: str | None = None
    ) -> BugEvidence:
        before, content = await asyncio.to_thread(
            self._sources._file_state, self._root, args["path"]
        )
        if expected_revision is not None and before != expected_revision:
            raise ModelRetry("Definition source changed; navigate again from current evidence.")
        result = await self._call(f"{self._root.name}_read_file", args, ctx)
        after, _ = await asyncio.to_thread(self._sources._file_state, self._root, args["path"])
        if before != after:
            raise ModelRetry("Source changed while reading; no evidence was recorded.")
        start = args.get("offset", 0) + 1
        end = min(len(content.splitlines()), start + self._sources.access.policy.max_read_lines - 1)
        if args.get("limit") is not None:
            end = min(end, start + args["limit"] - 1)
        if end < start or not isinstance(result, str) or result.startswith("[Binary file:"):
            raise ModelRetry("No text exists at this source offset.")
        locator = f"{self._root.name}/{args['path']}"
        evidence = self._sources._evidence(
            f"source:{self._root.name}",
            f"relative_path={locator}\nlines={start}-{end}\n{result}",
            revision=before,
            partial=False,
        )
        if not evidence.partial:
            self._sources._spans[evidence.evidence_id] = _ReadSpan(
                self._root.name, args["path"], before, start, end
            )
        return evidence

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        if ctx.deps.toolbox.tool_budget_exhausted:
            return [
                {
                    "status": "unavailable",
                    "reason": "tool_budget_exhausted",
                    "tool_name": name,
                }
            ]

        async def load() -> tuple[BugEvidence, ...]:
            if name == f"{self._root.name}_read_file":
                return (await self.read(tool_args, ctx),)
            result = await self._call(name, tool_args, ctx)
            return (
                self._sources._evidence(
                    name,
                    f"Navigation result only; read the located file before citing implementation.\n{result}",
                ),
            )

        return [
            item.model_dump(mode="json")
            for item in await ctx.deps.toolbox.source_tool(load, tool_name=name)
        ]
