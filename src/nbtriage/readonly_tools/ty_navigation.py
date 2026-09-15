from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import sysconfig
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import Future
from contextlib import asynccontextmanager, closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .python_navigation import (
    DefinitionBackend,
    NavigationBackendUnavailable,
    NavigationSourceChanged,
    PythonNavigationProfile,
    RawDefinition,
    _decode_python_source,
)

_TIMEOUT = 15.0
_CURRENT: ContextVar[TyDefinitionBackend | None] = ContextVar("ty_navigation", default=None)


class _TyResponseError(RuntimeError):
    def __init__(self, code: int) -> None:
        super().__init__(f"ty definition request failed ({code})")
        self.code = code


def _import_root(path: Path) -> Path:
    while path.parent != path and (
        (path / "__init__.py").is_file() or (path / "__init__.pyi").is_file()
    ):
        path = path.parent
    return path


class _TyClient:
    """只实现定义导航所需的 LSP；reader 独立处理握手和并发响应。"""

    def __init__(self, project_root: Path, source_paths: tuple[Path, ...]) -> None:
        from ty import find_ty_bin

        self._write_lock = Lock()
        self._pending_lock = Lock()
        self._pending: dict[int, Future[Any]] = {}
        self._counter = 0
        self._ready = Event()
        self._configured = False
        self._failure: Exception | None = None
        # extra-paths 优先于 typeshed；运行时 stdlib 会遮蔽 typing 等特殊类型声明。
        # 只排除标准库根本身，保留其下的 site-packages 和宿主源码搜索路径。
        stdlib_roots = {
            Path(sysconfig.get_path(name)).resolve() for name in ("stdlib", "platstdlib")
        }
        roots = (
            _import_root(path.resolve())
            for path in (*source_paths, *(Path(p or os.curdir) for p in sys.path))
            if path.is_dir()
        )
        paths = dict.fromkeys(str(root) for root in roots if root not in stdlib_roots)
        self._settings = {
            "diagnosticMode": "off",
            "configuration": {
                "environment": {
                    "python": sys.prefix,
                    "python-version": f"{sys.version_info.major}.{sys.version_info.minor}",
                    "extra-paths": list(paths),
                }
            },
        }
        self.process = subprocess.Popen(
            [find_ty_bin(), "server"],
            cwd=project_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self._reader = Thread(target=self._read, name="triage-ty", daemon=True)
        self._reader.start()
        try:
            self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": project_root.as_uri(),
                    "workspaceFolders": [{"uri": project_root.as_uri(), "name": "teaching"}],
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-16"]},
                        "workspace": {
                            "configuration": True,
                            "workspaceFolders": True,
                            "didChangeWatchedFiles": {
                                "dynamicRegistration": True,
                                "relativePatternSupport": True,
                            },
                        },
                        "textDocument": {"definition": {"linkSupport": True}},
                    },
                    "initializationOptions": {
                        "untrustedWorkspace": True,
                        "experimental": {"useUv": "off"},
                    },
                },
            )
            self.notify("initialized", {})
            # ty 0.0.80 在工作区配置完成后发出能力注册；不是通用的 LSP ready 事件。
            # 本次刷新要求文件稳定，不安装 watcher，也不模拟热更新。
            if not self._ready.wait(_TIMEOUT):
                raise TimeoutError("ty workspace initialization timed out")
            if self._failure is not None:
                raise self._failure
        except BaseException:
            self.close()
            raise

    def _send(self, message: dict[str, Any]) -> None:
        content = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode("utf-8")
        assert self.process.stdin is not None
        with self._write_lock:
            self.process.stdin.write(f"Content-Length: {len(content)}\r\n\r\n".encode() + content)
            self.process.stdin.flush()

    def notify(self, method: str, params: Any) -> None:
        self._send({"method": method, "params": params})

    def request(self, method: str, params: Any, timeout: float = _TIMEOUT) -> Any:
        future: Future[Any] = Future()
        with self._pending_lock:
            if self._failure is not None:
                raise self._failure
            self._counter += 1
            request_id = self._counter
            self._pending[request_id] = future
        try:
            self._send({"id": request_id, "method": method, "params": params})
            return future.result(timeout)
        except TimeoutError:
            self.notify("$/cancelRequest", {"id": request_id})
            raise
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                length = None
                while (line := self.process.stdout.readline()) != b"\r\n":
                    if not line:
                        raise EOFError("ty process exited")
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.partition(b":")[2])
                if length is None or not 0 <= length <= 16_000_000:
                    raise ValueError("invalid ty response framing")
                body = self.process.stdout.read(length)
                if len(body) != length:
                    raise EOFError("incomplete ty response")
                message = json.loads(body)
                if "method" in message:
                    if "id" in message:
                        self._server_request(message)
                    continue
                with self._pending_lock:
                    future = self._pending.pop(message.get("id"), None)
                if future is not None:
                    if "error" in message:
                        future.set_exception(_TyResponseError(message["error"]["code"]))
                    else:
                        future.set_result(message.get("result"))
        except Exception as error:
            with self._pending_lock:
                self._failure = error
                pending, self._pending = self._pending, {}
            for future in pending.values():
                future.set_exception(error)
            self._ready.set()

    def _server_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if method == "workspace/configuration":
            values = []
            for item in message["params"]["items"]:
                value: Any = {"ty": self._settings}
                for part in item.get("section", "").split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                values.append(value)
            self._send({"id": message["id"], "result": values})
            self._configured = True
        elif method == "client/registerCapability":
            self._send({"id": message["id"], "result": None})
            if self._configured:
                self._ready.set()
        else:
            self._send(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Unsupported client operation",
                    },
                }
            )

    def close(self) -> None:
        try:
            with self._pending_lock:
                busy = bool(self._pending)
            if self.process.poll() is None:
                if busy:
                    self.process.kill()
                else:
                    try:
                        self.request("shutdown", None, timeout=2)
                        self.notify("exit", None)
                        self.process.wait(timeout=2)
                    except (
                        OSError,
                        EOFError,
                        RuntimeError,
                        TimeoutError,
                        subprocess.TimeoutExpired,
                    ):
                        self.process.kill()
                self.process.wait(timeout=2)
        finally:
            self._reader.join(timeout=2)
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()


class TyDefinitionBackend:
    """刷新内懒启动一个进程；源码出现变化时拒绝复用，不做热同步或自动重启。"""

    def __init__(self, project_root: Path, source_paths: tuple[Path, ...] = ()) -> None:
        self._root = project_root.resolve()
        self._paths = source_paths
        self._lock = Lock()
        self._documents_lock = Lock()
        self._client: _TyClient | None = None
        self._startup_error: Exception | None = None
        self._closed = False
        self._documents: dict[str, str] = {}

    def _connection(self) -> _TyClient:
        with self._lock:
            if self._closed or self._startup_error is not None:
                raise NavigationBackendUnavailable("ty navigation session is unavailable")
            if self._client is None:
                try:
                    self._client = _TyClient(self._root, self._paths)
                except Exception as error:
                    self._startup_error = error
                    raise NavigationBackendUnavailable("ty could not initialize") from error
            return self._client

    def go_to_definition(
        self,
        *,
        code: str,
        path: Path,
        line: int,
        column: int,
        project_root: Path,
        python_executable: Path,
        added_sys_path: tuple[Path, ...],
    ) -> tuple[RawDefinition, ...]:
        client = self._connection()
        uri = path.as_uri()
        revision = hashlib.sha256(code.encode("utf-8")).hexdigest()
        with self._documents_lock:
            previous = self._documents.get(uri)
            if previous is not None and previous != revision:
                raise NavigationSourceChanged("source changed within ty navigation session")
            if previous is None:
                client.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": "python",
                            "version": 1,
                            "text": code,
                        }
                    },
                )
                self._documents[uri] = revision
        params = {
            "textDocument": {"uri": uri},
            "position": {
                "line": line - 1,
                "character": len(code.splitlines()[line - 1][:column].encode("utf-16-le")) // 2,
            },
        }
        deadline = monotonic() + _TIMEOUT
        result: Any = None
        for attempt in range(3):
            try:
                result = client.request(
                    "textDocument/definition",
                    params,
                    timeout=max(0, deadline - monotonic()),
                )
                break
            except _TyResponseError as error:
                # didOpen 也会使 ty 的数据库换代；仅重发未改源码的 ContentModified 查询。
                if error.code != -32801 or attempt == 2 or monotonic() >= deadline:
                    raise
                if _decode_python_source(path.read_bytes()) != code:
                    raise NavigationSourceChanged(
                        "source changed while ty query was canceled"
                    ) from error
        locations = [result] if isinstance(result, dict) else result or []
        definitions = []
        for location in locations:
            uri = location.get("targetUri", location.get("uri", ""))
            parsed = urlsplit(uri)
            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                continue
            target_range = location.get("targetSelectionRange", location.get("range"))
            if not isinstance(target_range, dict):
                raise ValueError("ty returned a definition without its range")
            position = target_range["start"]
            definitions.append(
                RawDefinition(
                    module_path=Path(url2pathname(parsed.path)),
                    name=None,
                    full_name=None,
                    kind=None,
                    line=position["line"] + 1,
                    column=position["character"],
                    utf16_column=True,
                )
            )
        return tuple(definitions)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
        if client is not None:
            client.close()


@asynccontextmanager
async def navigation_session(
    source_paths: tuple[Path, ...] = (),
) -> AsyncIterator[TyDefinitionBackend]:
    """同一任务内共享解析进程，包含 to_thread/工具线程；退出时等待回收。"""
    backend = TyDefinitionBackend(Path.cwd(), source_paths)
    token = _CURRENT.set(backend)
    try:
        yield backend
    finally:
        _CURRENT.reset(token)
        cleanup = asyncio.create_task(asyncio.to_thread(backend.close))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


@contextmanager
def definition_backend(
    profile: PythonNavigationProfile,
    explicit: DefinitionBackend | None,
) -> Iterator[DefinitionBackend]:
    if (backend := explicit or _CURRENT.get()) is not None:
        yield backend
    else:
        # 独立诊断没有刷新生命周期，单次调用也必须自行回收进程。
        with closing(
            TyDefinitionBackend(
                profile.project_root.path,
                tuple(root.path for root in profile.source_roots),
            )
        ) as temporary:
            yield temporary
