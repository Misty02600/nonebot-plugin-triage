"""教学冷测的薄宿主：复用正式刷新流程，只在模型边界做预检与记录。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import subprocess
import tomllib
import traceback
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic_ai import models
from pydantic_ai.models import Model, ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel

from nbtriage.capability.teaching._input_budget import estimate_request_tokens
from nbtriage.capability.teaching._prompt import stable_instruction_prefix
from nbtriage.capability.teaching.analysis import CapabilityAnalysisRequest
from nbtriage.capability.teaching.annotations import CAPABILITY_ANNOTATION_REQUEST_REVISION
from nbtriage.capability.teaching.model_adapter import PydanticAICapabilityAnalysisClient
from nbtriage.knowledge_index import KnowledgeIndexReader

from .capability_teaching import (
    CapabilityTeachingMaintenanceError,
    CapabilityTeachingMaintenanceResult,
    _CapturedCapabilityClient,
    _install_model_output_capture,
)


class _PreflightComplete(Exception):
    pass


class _RequestGate(WrapperModel):
    """正式请求准备后核对工具；预检截停，运行模式交回原模型。"""

    def __init__(
        self, wrapped: Model, record: dict[str, Any], *, phase: str, knowledge: str
    ) -> None:
        super().__init__(wrapped)
        self.record = record
        self.phase = phase
        self.knowledge = knowledge

    async def request(
        self, messages: Any, model_settings: Any, model_request_parameters: ModelRequestParameters
    ) -> Any:
        # 诊断复用库原生准备以解析 auto 输出；实际发送仍传入原参数，避免重复变换。
        prepared_settings, prepared_parameters = self.prepare_request(
            model_settings, model_request_parameters
        )
        names = sorted(tool.name for tool in prepared_parameters.function_tools)
        first_request = "first_request" not in self.record
        self.record.setdefault(
            "first_request",
            {
                "model": self.model_name,
                "system": self.system,
                "profile": dict(self.profile),
                "model_settings": {
                    key: value
                    for key, value in (prepared_settings or {}).items()
                    if key in {"max_tokens", "temperature", "top_p", "timeout", "seed"}
                },
                "function_tools": names,
                "output_mode": prepared_parameters.output_mode,
                "output_tools": [tool.name for tool in prepared_parameters.output_tools],
                "tool_schema_sha256": hashlib.sha256(
                    json.dumps(
                        {
                            name: asdict(tool)
                            for name, tool in prepared_parameters.declared_tool_defs.items()
                        },
                        sort_keys=True,
                        default=str,
                    ).encode()
                ).hexdigest(),
                "estimated_input_tokens": estimate_request_tokens(
                    ModelRequestContext(
                        model=self,
                        messages=messages,
                        model_settings=prepared_settings,
                        model_request_parameters=prepared_parameters,
                    )
                ),
            },
        )
        # required 对普通与 family 单元使用同一首请求合同；后续可按正常预算撤下工具。
        if (
            first_request and self.knowledge == "required" and "framework_search_docs" not in names
        ) or (self.knowledge == "off" and "framework_search_docs" in names):
            self.record["outcome"] = "knowledge_tool_mismatch"
            raise CapabilityTeachingMaintenanceError("knowledge tool exposure does not match mode")
        if self.phase == "preflight":
            self.record["outcome"] = "preflight_ready"
            raise _PreflightComplete
        self.record["provider_requests"] += 1
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        self.record.setdefault("request_usage", []).append(
            {
                "request_index": self.record["provider_requests"],
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": response.usage.cache_read_tokens,
                "cache_write_tokens": response.usage.cache_write_tokens,
            }
        )
        return response

    async def count_tokens(self, *args: Any, **kwargs: Any) -> Any:
        if self.phase == "preflight":
            raise CapabilityTeachingMaintenanceError("preflight forbids provider token counting")
        return await self.wrapped.count_tokens(*args, **kwargs)


class _EvaluationClient:
    def __init__(
        self, inner: Any, records: list[dict[str, Any]], *, phase: str, knowledge: str
    ) -> None:
        self.inner = inner
        self.records = records
        self.phase = phase
        self.knowledge = knowledge

    async def analyze(self, request: CapabilityAnalysisRequest) -> Any:
        client = (
            self.inner._inner if isinstance(self.inner, _CapturedCapabilityClient) else self.inner
        )
        if not isinstance(client, PydanticAICapabilityAnalysisClient):
            raise CapabilityTeachingMaintenanceError(
                "evaluation requires the production capability client"
            )
        record: dict[str, Any] = {
            "unit_id": request.capability.capability_id,
            "outcome": "preparing_request",
            "family": bool(request.family_members),
            "provider_requests": 0,
            "stable_instruction_prefix_sha256": hashlib.sha256(
                stable_instruction_prefix(request).encode()
            ).hexdigest(),
            "stable_instruction_prefix_chars": len(stable_instruction_prefix(request)),
            "bootstrap_documents": [
                {
                    "evidence_id": unit.evidence_id,
                    "locator": unit.locator,
                    "revision": unit.revision,
                    "content_chars": len(unit.content),
                }
                for unit in request.evidence_units
                if unit.source_kind.startswith("knowledge_")
            ],
            "request_sha256": hashlib.sha256(
                json.dumps(asdict(request), sort_keys=True, default=str).encode()
            ).hexdigest(),
            "budgets": {
                name.removeprefix("_"): str(getattr(client, name))
                for name in (
                    "_max_output_tokens",
                    "_max_requests",
                    "_max_tool_calls",
                    "_total_tokens_limit",
                    "_cost_limit_usd",
                    "_timeout_seconds",
                )
            },
        }
        self.records.append(record)
        model = client._diagnostic_model or client._agent.model
        if not isinstance(model, Model):
            raise CapabilityTeachingMaintenanceError("resolved model is unavailable")
        gate = _RequestGate(model, record, phase=self.phase, knowledge=self.knowledge)
        try:
            with client._agent.override(model=gate):
                async with client._agent:
                    output = await self.inner.analyze(request)
            record["outcome"] = "generated"
            return output
        except Exception as error:
            if record["outcome"] not in {"preflight_ready", "knowledge_tool_mismatch"}:
                record["outcome"] = "failed"
                record["reason"] = str(getattr(error, "reason_code", type(error).__name__))
                record["detail"] = getattr(error, "detail_code", None)
            raise
        finally:
            record["input_preparation"] = client.diagnostic_input_estimates


def initialize_host(
    project: Path,
    plugins: tuple[str, ...],
    state: Path,
    *,
    knowledge: str | None,
    isolate_all: bool = True,
) -> Any:
    """只初始化一次宿主；不执行插件启动钩子或连接适配器。"""
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        pass
    else:
        raise CapabilityTeachingMaintenanceError("use a fresh process for teaching maintenance")
    settings: dict[str, Any] = {
        "log_level": "INFO",
        "localstore_cache_dir": state / "cache",
        "localstore_config_dir": state / "config",
        "localstore_data_dir": state / "data",
        "localstore_plugin_cache_dir": {},
        "localstore_plugin_config_dir": {},
        "localstore_plugin_data_dir": {},
        "nbtriage_agent_trace_enabled": False,
    }
    if not isolate_all:
        # 原有维护模式支持重试已持久化的 Triage 注释；冷测始终使用完整隔离。
        settings = {
            "driver": "~none",
            "log_level": "INFO",
            **{
                f"localstore_plugin_{kind}_dir": dict.fromkeys(plugins, state / kind)
                for kind in ("cache", "config", "data")
            },
        }
    if knowledge is not None:
        settings.update(
            nbtriage_knowledge_pack_auto_update=knowledge == "required",
            nbtriage_knowledge_pack_url=None,
            nbtriage_knowledge_pack_sha256=None,
        )
    nonebot.init(**settings)
    config = tomllib.loads(project.read_text(encoding="utf-8")).get("tool", {}).get("nonebot", {})
    adapters = config.get("adapters", [])
    if isinstance(adapters, dict):
        adapters = [item for group in adapters.values() for item in group]
    for adapter in adapters:
        module = importlib.import_module(adapter["module_name"])
        nonebot.get_driver().register_adapter(module.Adapter)
    for name in (*plugins, "nonebot_plugin_triage"):
        if nonebot.load_plugin(name) is None:
            raise CapabilityTeachingMaintenanceError(f"requested plugin failed to load: {name}")
    from nonebot_plugin_triage.handlers import plugin_runtime

    return plugin_runtime


async def _prepare_knowledge(
    runtime: Any, archive: Path | None, sha256: str | None
) -> dict[str, Any]:
    if archive is None or sha256 is None or runtime.knowledge_pack is None:
        raise CapabilityTeachingMaintenanceError(
            "required knowledge needs a local archive and SHA256"
        )
    await runtime.knowledge_pack.install_local_archive(archive, sha256)
    status = runtime.knowledge_pack.status
    if not status.ready or status.index_path is None or status.archive_sha256 != sha256:
        raise CapabilityTeachingMaintenanceError("required knowledge pack is not ready")
    reader = KnowledgeIndexReader(status.index_path)
    if not reader.has_user_docs(component="nonebot2", version=version("nonebot2")):
        raise CapabilityTeachingMaintenanceError(
            "knowledge pack has no applicable NoneBot user docs"
        )
    return {**asdict(status), "metadata": reader.metadata()}


def _code_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    files = [
        *root.glob("src/**/*.py"),
        *root.glob("tools/nbtriage_maintainer/**/*.py"),
        root / "pyproject.toml",
    ]
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    return {
        "git_head": head.stdout.strip() if head.returncode == 0 else None,
        "source_sha256": digest.hexdigest(),
        "dependencies": {
            name: version(name) for name in ("nonebot2", "pydantic-ai-slim", "pydantic-ai-harness")
        },
    }


def evaluate_teaching(
    project: Path,
    plugin: str | None,
    *,
    phase: Literal["preflight", "run"],
    knowledge: str,
    run_dir: Path,
    archive: Path | None,
    sha256: str | None,
) -> CapabilityTeachingMaintenanceResult:
    """用正式刷新管线执行隔离评测，逐插件保留状态与请求边界记录。

    Note:
        调用方负责切换到宿主目录并校验参数。预检中断产生的内部失败缓存随
        临时目录清除；只有运行模式保留生成状态，预检就绪不代表语义评审通过。
    """
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = run_dir / "manifest.json"
    records: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "knowledge_mode": knowledge,
        "started_at": datetime.now(UTC).isoformat(),
        "outcome": "preparing",
        "units": records,
        "skipped_plugins": [],
        "startup_hooks_executed": False,
        "state_reused": False,
    }
    capture = None
    previous_allow = models.ALLOW_MODEL_REQUESTS
    try:
        manifest["code"] = _code_identity()
        if phase == "preflight":
            models.ALLOW_MODEL_REQUESTS = False
        config = tomllib.loads(project.read_text(encoding="utf-8"))["tool"]["nonebot"]
        declared = config.get("plugins", [])
        if isinstance(declared, dict):
            declared = [name for group in declared.values() for name in group]
        plugins = (
            (plugin,)
            if plugin
            else tuple(dict.fromkeys(name for name in declared if name != "nonebot_plugin_triage"))
        )
        if not plugins:
            raise CapabilityTeachingMaintenanceError("no target plugins declared")
        manifest["plugins"] = plugins
        # 预检中断会被正式管线记为失败；这些内部工件随临时目录清除，不能作为注释缓存。
        state_context = (
            TemporaryDirectory(prefix="nbtriage-preflight-")
            if phase == "preflight"
            else nullcontext(str(run_dir / "state"))
        )
        with state_context as state_name:
            state = Path(state_name).resolve()
            runtime = initialize_host(project, plugins, state, knowledge=knowledge)
            shadow = runtime.capability_shadow
            if shadow is None:
                raise CapabilityTeachingMaintenanceError(
                    "capability teaching runtime is unavailable"
                )
            output_root = shadow._teaching_output_writer._resolved_root().resolve()
            if not output_root.is_relative_to(state):
                raise CapabilityTeachingMaintenanceError("teaching output escaped isolated state")
            if phase == "run":
                capture = _install_model_output_capture(
                    shadow,
                    plugin_module=plugin or "*",
                    diagnostic_output=run_dir / "model-output.json",
                    unbounded=False,
                )
            service = shadow._annotation_service
            manifest["analysis_revision"] = service._analysis_revision
            manifest["request_revision"] = CAPABILITY_ANNOTATION_REQUEST_REVISION
            factory = service._client_factory
            service.__dict__["_client_factory"] = lambda: _EvaluationClient(
                factory(), records, phase=phase, knowledge=knowledge
            )

            async def refresh() -> Any:
                if knowledge == "required":
                    manifest["knowledge_pack"] = await _prepare_knowledge(runtime, archive, sha256)
                else:
                    manifest["knowledge_pack"] = None
                result = await shadow.refresh_teaching(plugin_modules=plugins, force=True)
                status = service.status.to_dict()
                planned_plugins = {unit["plugin_module"] for unit in status["units"]}
                manifest["skipped_plugins"] = [
                    {"plugin": target, "reason": "no_teaching_unit"}
                    for target in plugins
                    if target not in planned_plugins
                ]
                return result, status

            result, refresh_status = asyncio.run(refresh())
            status = {
                **refresh_status,
                "global_failure_reasons": (
                    [refresh_status["global_failure_reason"]]
                    if refresh_status["global_failure_reason"]
                    else []
                ),
            }
            ready = sum(item["outcome"] == "preflight_ready" for item in records)
            failed = (
                max(0, result.failed_count - ready) if phase == "preflight" else result.failed_count
            )
            if not records:
                failed = max(1, failed)
                manifest["error_code"] = "no_model_units_prepared"
            manifest.update(
                outcome="passed" if not failed else "failed",
                preflight_ready=ready,
                refresh_status=status,
                provider_requests=sum(item["provider_requests"] for item in records),
            )
            # 只把原始管线状态作为诊断保留；顶层明确区分预检就绪与生成成功。
            return CapabilityTeachingMaintenanceResult(
                plugin=plugin or "*",
                units=result.unit_count,
                active=result.active_count,
                cached=result.cached_count,
                generated=result.generated_count,
                disabled=result.disabled_count,
                skipped=result.skipped_count,
                failed=failed,
                stale=result.stale_count,
                family_eligible=result.family_eligible_count,
                family_disabled=result.family_disabled_count,
                family_failed=max(
                    0,
                    result.family_failed_count
                    - sum(
                        item["family"] and item["outcome"] == "preflight_ready" for item in records
                    ),
                ),
                diagnostics_file=str(run_dir / "model-output.json") if capture else None,
                files=tuple(str(path) for path in result.files) if phase == "run" else (),
                phase=phase,
                preflight_ready=ready,
                manifest_file=str(manifest_path),
            )
    except Exception as error:
        manifest.update(outcome="failed", error_code=getattr(error, "code", type(error).__name__))
        manifest["error_stack"] = [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(error.__traceback__)
        ]
        manifest["error_context"] = []
        context = error.__context__
        while context is not None:
            manifest["error_context"].append(
                {
                    "type": type(context).__name__,
                    "frames": [
                        {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                        for frame in traceback.extract_tb(context.__traceback__)
                    ],
                }
            )
            context = context.__context__
        if isinstance(error, CapabilityTeachingMaintenanceError):
            raise
        raise CapabilityTeachingMaintenanceError(
            f"teaching evaluation failed: {manifest['error_code']}"
        ) from error
    finally:
        models.ALLOW_MODEL_REQUESTS = previous_allow
        if capture is not None:
            capture.write()
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
