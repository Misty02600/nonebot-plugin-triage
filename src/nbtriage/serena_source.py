from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
from collections.abc import Callable, Mapping
from contextlib import suppress
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, Self

import yaml

from nbtriage.bug_assessment import BugEvidence, BugEvidenceKind
from nbtriage.bug_source import ApprovedSourceRoot, BoundedSourceReader, BugSourceError

_LOGGER = logging.getLogger(__name__)
_SERENA_REQUIRED_TOOLS = frozenset(
    {
        "find_symbol",
        "find_referencing_symbols",
    }
)
_SERENA_RESULT_MAX_CHARS = 48_000
_SERENA_TOOL_TIMEOUT_SECONDS = 20.0
_SERENA_INIT_TIMEOUT_SECONDS = 60.0
_SERENA_WEBSITE = "https://oraios.github.io/serena"


class SerenaSourceError(RuntimeError):
    pass


class _SerenaToolset(Protocol):
    @property
    def server_info(self) -> object: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *args: object) -> object: ...

    async def list_tools(self) -> list[object]: ...

    async def direct_call_tool(self, name: str, args: dict[str, Any]) -> object: ...


SerenaToolsetFactory = Callable[[ApprovedSourceRoot, Path, str], _SerenaToolset]


class BoundedBugSourceBackend:
    """把同步的有界源码读取器适配成 Bug runtime 使用的异步接口。"""

    def __init__(self, approved_root: ApprovedSourceRoot) -> None:
        self._reader = BoundedSourceReader(approved_root)

    async def search(self, query: str) -> tuple[BugEvidence, ...]:
        return await asyncio.to_thread(self._reader.search, query)

    async def read(self, relative_path: str) -> tuple[BugEvidence, ...]:
        return await asyncio.to_thread(self._reader.read, relative_path)

    async def find_symbol(self, name_path_pattern: str) -> tuple[BugEvidence, ...]:
        return await self.search(name_path_pattern)

    async def find_references(
        self,
        relative_path: str,
        name_path: str,
    ) -> tuple[BugEvidence, ...]:
        del relative_path, name_path
        return ()

    async def aclose(self) -> None:
        return None


class SerenaBugSourceBackend(BoundedBugSourceBackend):
    """通过只读 Serena MCP 做符号导航，失败时保留原有有界文本读取。"""

    def __init__(
        self,
        approved_root: ApprovedSourceRoot,
        *,
        serena_home: Path,
        executable: str,
        source_revision: str | None = None,
        toolset_factory: SerenaToolsetFactory | None = None,
    ) -> None:
        super().__init__(approved_root)
        self._approved_root = approved_root
        self._serena_home = serena_home
        self._executable = executable
        self._source_revision = source_revision
        self._toolset_factory = toolset_factory or _create_serena_toolset
        self._validate_server_identity = toolset_factory is None
        self._toolset: _SerenaToolset | None = None
        self._entered = False
        self._unavailable = False
        self._lock = asyncio.Lock()

    async def find_symbol(self, name_path_pattern: str) -> tuple[BugEvidence, ...]:
        pattern = _bounded_symbol_name(name_path_pattern)
        result = await self._call(
            "find_symbol",
            {
                "name_path_pattern": pattern,
                "include_body": True,
                "substring_matching": True,
                "max_matches": 6,
                "max_answer_chars": 32_000,
            },
        )
        evidence = self._symbol_evidence("find_symbol", result)
        if evidence:
            return evidence
        return await super().find_symbol(pattern)

    async def find_references(
        self,
        relative_path: str,
        name_path: str,
    ) -> tuple[BugEvidence, ...]:
        normalized_path = _approved_relative_python_path(
            self._approved_root.root,
            relative_path,
        )
        normalized_name = _bounded_symbol_name(name_path)
        result = await self._call(
            "find_referencing_symbols",
            {
                "name_path": normalized_name,
                "relative_path": normalized_path,
                "max_answer_chars": 32_000,
            },
        )
        return self._symbol_evidence("find_referencing_symbols", result)

    async def aclose(self) -> None:
        async with self._lock:
            toolset = self._toolset
            entered = self._entered
            self._toolset = None
            self._entered = False
        if toolset is not None and entered:
            try:
                await toolset.__aexit__(None, None, None)
            except Exception as error:
                _LOGGER.warning(
                    "Serena source backend shutdown failed: %s",
                    type(error).__name__,
                )

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> object | None:
        if self._unavailable:
            return None
        try:
            toolset = await self._ensure_started()
            async with asyncio.timeout(_SERENA_TOOL_TIMEOUT_SECONDS):
                return await toolset.direct_call_tool(tool_name, arguments)
        except Exception as error:
            self._unavailable = True
            _LOGGER.warning(
                "Serena source navigation unavailable; using bounded fallback: %s",
                type(error).__name__,
            )
            return None

    async def _ensure_started(self) -> _SerenaToolset:
        async with self._lock:
            if self._toolset is not None and self._entered:
                return self._toolset
            _prepare_serena_home(self._serena_home)
            toolset = self._toolset_factory(
                self._approved_root,
                self._serena_home,
                self._executable,
            )
            entered = False
            try:
                async with asyncio.timeout(_SERENA_INIT_TIMEOUT_SECONDS):
                    await toolset.__aenter__()
                    entered = True
                    if self._validate_server_identity:
                        _validate_serena_server(toolset.server_info)
                    tools = await toolset.list_tools()
                names = {
                    name for tool in tools if isinstance((name := getattr(tool, "name", None)), str)
                }
                missing = _SERENA_REQUIRED_TOOLS.difference(names)
                if missing:
                    raise SerenaSourceError("Serena read-only toolset is incomplete")
            except BaseException:
                if entered:
                    with suppress(Exception):
                        await toolset.__aexit__(None, None, None)
                raise
            self._toolset = toolset
            self._entered = True
            return toolset

    def _symbol_evidence(
        self,
        tool_name: str,
        result: object | None,
    ) -> tuple[BugEvidence, ...]:
        payload = _tool_result_payload(result)
        sanitized = _sanitize_serena_payload(payload, self._approved_root.root)
        if sanitized is None:
            return ()
        body = json.dumps(
            sanitized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        partial = len(body) > _SERENA_RESULT_MAX_CHARS
        bounded = body[:_SERENA_RESULT_MAX_CHARS]
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        evidence_key = f"{tool_name}:{self._source_revision or ''}:{digest}"
        evidence_digest = hashlib.sha256(evidence_key.encode("utf-8")).hexdigest()
        return (
            BugEvidence(
                evidence_id=f"source:{evidence_digest[:32]}",
                kind=BugEvidenceKind.SOURCE_CODE,
                source=f"serena:{tool_name}",
                body=bounded,
                revision=self._source_revision or digest,
                current=True,
                partial=partial,
            ),
        )


def create_bug_source_backend(
    approved_root: ApprovedSourceRoot,
    *,
    serena_home: Path | None,
    executable: str | None = None,
    source_revision: str | None = None,
    toolset_factory: SerenaToolsetFactory | None = None,
) -> BoundedBugSourceBackend:
    command = executable or shutil.which("serena")
    if serena_home is None or command is None:
        return BoundedBugSourceBackend(approved_root)
    if _has_untrusted_serena_config(approved_root.root):
        _LOGGER.warning(
            "Serena source navigation disabled because the approved root contains project config"
        )
        return BoundedBugSourceBackend(approved_root)
    if toolset_factory is None and not _mcp_client_available():
        return BoundedBugSourceBackend(approved_root)
    root_key = f"{approved_root.module_name}:{approved_root.root}:{source_revision or ''}"
    scoped_home = serena_home / hashlib.sha256(root_key.encode("utf-8")).hexdigest()[:16]
    return SerenaBugSourceBackend(
        approved_root,
        serena_home=scoped_home,
        executable=command,
        source_revision=source_revision,
        toolset_factory=toolset_factory,
    )


def _create_serena_toolset(
    approved_root: ApprovedSourceRoot,
    serena_home: Path,
    executable: str,
) -> _SerenaToolset:
    try:
        transports = import_module("fastmcp.client.transports")
        pydantic_mcp = import_module("pydantic_ai.mcp")
        transport_type = transports.StdioTransport
        toolset_type = pydantic_mcp.MCPToolset
    except (AttributeError, ImportError) as error:
        raise SerenaSourceError("Pydantic AI MCP support is not installed") from error
    transport = transport_type(
        command=executable,
        args=[
            "start-mcp-server",
            "--project",
            str(approved_root.root),
            "--context",
            str(Path(__file__).with_name("_serena_readonly_context.yml")),
            "--language-backend",
            "LSP",
            "--enable-web-dashboard",
            "false",
            "--enable-gui-log-window",
            "false",
            "--open-web-dashboard",
            "false",
            "--log-level",
            "ERROR",
            "--tool-timeout",
            str(int(_SERENA_TOOL_TIMEOUT_SECONDS)),
        ],
        env=_serena_environment(serena_home),
        cwd=str(approved_root.root),
        keep_alive=False,
        log_file=Path(os.devnull),
    )
    return toolset_type(
        transport,
        tool_error_behavior="error",
        include_instructions=False,
        init_timeout=_SERENA_INIT_TIMEOUT_SECONDS,
        read_timeout=_SERENA_TOOL_TIMEOUT_SECONDS,
    )


def _prepare_serena_home(path: Path) -> None:
    resolved = path.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    projects = resolved / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    payload = {
        "language_backend": "LSP",
        "line_ending": "native",
        "gui_log_window": False,
        "web_dashboard": False,
        "web_dashboard_open_on_launch": False,
        "log_level": 40,
        "trace_lsp_communication": False,
        "ls_specific_settings": {},
        "ignored_paths": [],
        "read_only_memory_patterns": [],
        "ignored_memory_patterns": [],
        "tool_timeout": int(_SERENA_TOOL_TIMEOUT_SECONDS),
        "excluded_tools": [],
        "included_optional_tools": [],
        "fixed_tools": [],
        "base_modes": [],
        "default_modes": [],
        "default_max_tool_answer_chars": _SERENA_RESULT_MAX_CHARS,
        "token_count_estimator": "CHAR_COUNT",
        "symbol_info_budget": 10,
        "project_serena_folder_location": (projects.as_posix() + "/$projectFolderName/.serena"),
        "trusted_project_path_patterns": [],
        "projects": [],
        "record_tool_usage_stats": False,
    }
    target = resolved / "serena_config.yml"
    temporary = resolved / (f".serena-config-{os.getpid()}-{secrets.token_hex(6)}.tmp")
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _serena_environment(serena_home: Path) -> dict[str, str]:
    allowed = {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["SERENA_HOME"] = str(serena_home.resolve())
    environment["SERENA_USAGE_REPORTING"] = "false"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _tool_result_payload(result: object | None) -> object | None:
    if result is None:
        return None
    if isinstance(result, (list, Mapping)):
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return None
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return None
    texts = [getattr(item, "text", None) for item in content]
    joined = "\n".join(item for item in texts if isinstance(item, str))
    if not joined:
        return None
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return None


def _sanitize_serena_payload(payload: object, root: Path) -> object | None:
    if isinstance(payload, list):
        symbol_results = [_sanitize_symbol(item, root) for item in payload]
        filtered = [item for item in symbol_results if item is not None]
        return filtered or None
    if isinstance(payload, Mapping):
        reference_results: dict[str, object] = {}
        for path, value in payload.items():
            if not isinstance(path, str):
                continue
            try:
                relative = _approved_relative_python_path(root, path)
            except BugSourceError:
                continue
            sanitized = _sanitize_reference_groups(value)
            if sanitized:
                reference_results[relative] = sanitized
        return reference_results or None
    return None


def _sanitize_symbol(value: object, root: Path) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    relative_path = value.get("relative_path")
    name_path = value.get("name_path")
    kind = value.get("kind")
    if not all(isinstance(item, str) for item in (relative_path, name_path, kind)):
        return None
    try:
        relative = _approved_relative_python_path(root, str(relative_path))
    except BugSourceError:
        return None
    result: dict[str, object] = {
        "relative_path": relative,
        "name_path": str(name_path)[:500],
        "kind": str(kind)[:100],
    }
    location = _sanitize_location(value.get("body_location"))
    if location is not None:
        result["body_location"] = location
    body = value.get("body")
    if isinstance(body, str):
        result["body"] = body[:32_000]
    return result


def _sanitize_reference_groups(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, object] = {}
    for kind, records in value.items():
        if not isinstance(kind, str) or not isinstance(records, list):
            continue
        sanitized: list[dict[str, object]] = []
        for record in records[:32]:
            if not isinstance(record, Mapping):
                continue
            name_path = record.get("name_path")
            context = record.get("content_around_reference")
            if not isinstance(name_path, str) or not isinstance(context, str):
                continue
            item: dict[str, object] = {
                "name_path": name_path[:500],
                "content_around_reference": context[:4_000],
            }
            location = _sanitize_location(record.get("body_location"))
            if location is not None:
                item["body_location"] = location
            sanitized.append(item)
        if sanitized:
            result[kind[:100]] = sanitized
    return result or None


def _sanitize_location(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    start = value.get("start_line")
    end = value.get("end_line")
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
        return None
    return {"start_line": start, "end_line": end}


def _approved_relative_python_path(root: Path, value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    relative = Path(normalized)
    if (
        not normalized
        or len(normalized) > 500
        or relative.is_absolute()
        or relative.suffix != ".py"
        or ".." in relative.parts
    ):
        raise BugSourceError("Serena result is outside the approved source root")
    unresolved = root / relative
    if unresolved.is_symlink():
        raise BugSourceError("Serena result is not an approved Python source file")
    try:
        candidate = unresolved.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise BugSourceError("Serena result is outside the approved source root") from error
    if candidate.is_symlink() or not candidate.is_file():
        raise BugSourceError("Serena result is not an approved Python source file")
    return candidate.relative_to(root).as_posix()


def _bounded_symbol_name(value: str) -> str:
    if type(value) is not str:
        raise BugSourceError("symbol name path must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 500
        or any(ord(character) < 32 for character in normalized)
    ):
        raise BugSourceError("symbol name path must contain 1 to 500 visible characters")
    return normalized


def _mcp_client_available() -> bool:
    try:
        import_module("fastmcp.client.transports")
        import_module("pydantic_ai.mcp")
    except ImportError:
        return False
    return True


def _validate_serena_server(server_info: object) -> None:
    name = getattr(server_info, "name", None)
    website = getattr(server_info, "websiteUrl", None)
    if not isinstance(name, str) or name.casefold() != "serena" or website != _SERENA_WEBSITE:
        raise SerenaSourceError("Serena MCP server identity is not qualified")


def _has_untrusted_serena_config(root: Path) -> bool:
    return (root / ".serena").exists()


__all__ = (
    "BoundedBugSourceBackend",
    "SerenaBugSourceBackend",
    "SerenaSourceError",
    "SerenaToolsetFactory",
    "create_bug_source_backend",
)
