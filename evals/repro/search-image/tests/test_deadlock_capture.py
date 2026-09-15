import asyncio
import hashlib
import importlib.util
import json
import linecache
import platform
import sys
from contextlib import suppress
from datetime import UTC, datetime
from importlib.metadata import version
from itertools import count
from pathlib import Path

import httpx
import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebug import App

ROOT = Path(__file__).resolve().parents[1]
TRIAGE_ROOT = ROOT.parents[2]


def _production_runtime_observer():
    """从当前 triage 工作树装载生产观察器，不加载完整 triage 插件。"""
    source_root = TRIAGE_ROOT / "src"
    source_path = str(source_root)
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    module_path = source_root / "nonebot_plugin_triage" / "nonebot_runtime.py"
    spec = importlib.util.spec_from_file_location("_triage_repro_runtime_observer", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the production runtime observer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from nbtriage.bug.logs import CorrelatedBugLogBuffer
    from nbtriage.runtime_observations import RuntimeObservationBuffer

    sequence = count(1)
    runtime_buffer = RuntimeObservationBuffer(max_entries=64, retention_seconds=60)
    log_buffer = CorrelatedBugLogBuffer(max_entries=64, retention_seconds=60)
    observer = module.NoneBotRuntimeObserver(
        runtime_buffer,
        bug_log_buffer=log_buffer,
        id_factory=lambda: f"repro-production-{next(sequence)}",
    )
    observer.register()
    return observer, runtime_buffer, log_buffer


def _waiting_frames(task: asyncio.Task) -> tuple[list[dict], list[asyncio.Lock]]:
    frames, locks = [], []
    coroutine = task.get_coro()
    while coroutine is not None:
        frame = getattr(coroutine, "cr_frame", None)
        if frame is not None:
            frames.append(
                {
                    "file": frame.f_code.co_filename,
                    "line": frame.f_lineno,
                    "function": frame.f_code.co_name,
                    "code": linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip(),
                }
            )
            lock = frame.f_locals.get("lock")
            if isinstance(lock, asyncio.Lock):
                locks.append(lock)
        coroutine = getattr(coroutine, "cr_await", None)
    return frames, locks


async def test_capture_default_search_deadlock(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    """保存模拟消息及 HTTP 条件下真实执行的日志、Bot 回复和取消前等待快照。"""
    import PicImageSearch.network as network
    import YetAnotherPicSearch

    production_observer, runtime_buffer, log_buffer = _production_runtime_observer()
    production_correlation_id = "corr-repro-production-1"

    started = datetime.now(UTC)
    run_id = started.strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = ROOT / "artifacts" / f"deadlock-{run_id}"
    run_dir.mkdir(parents=True)
    source_root = Path(YetAnotherPicSearch.__file__).parent
    source_hashes = {}
    for source_file in sorted(source_root.rglob("*.py")):
        relative = source_file.relative_to(source_root)
        content = source_file.read_bytes()
        target = run_dir / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        source_hashes[relative.as_posix()] = hashlib.sha256(content).hexdigest()

    logs, messages, http_calls = [], [], []
    logged_exceptions = []
    response_seen = asyncio.Event()
    image_bytes = (source_root / "res" / "usage.jpg").read_bytes()
    message = Message("/搜图 ") + MessageSegment(
        "image", {"file": f"fixture-{run_id}.jpg", "url": "https://fixtures.invalid/image.jpg"}
    )
    event = GroupMessageEvent.model_validate(
        {
            "time": int(started.timestamp()),
            "self_id": 10001,
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": 301,
            "group_id": 20001,
            "user_id": 30001,
            "message": message,
            "original_message": message,
            "raw_message": str(message),
            "font": 0,
            "sender": {"user_id": 30001, "nickname": "tester", "role": "member"},
            "to_me": False,
        }
    )

    def chat(message_id: str, content: str, *, is_bot: bool, reply_to=None, current=False):
        return {
            "schema_version": 2,
            "message_id": message_id,
            "reply_to_message_id": reply_to,
            "sent_at": int(datetime.now(UTC).timestamp()),
            "sender_id": "10001" if is_bot else "30001",
            "is_bot": is_bot,
            "is_request_actor": not is_bot,
            "is_current_request": current,
            "segment_types": ["text", "image"] if message_id == "301" else ["text"],
            "content": content,
        }

    messages.append(
        chat(
            "301",
            "/搜图 [图片附件，测试图片 SHA256=" + hashlib.sha256(image_bytes).hexdigest() + "]",
            is_bot=False,
        )
    )

    def search_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.host == "saucenao.com"
        assert request.url.params["db"] == "999"
        body = {"header": {"status": 429, "message": "4 searches every 30 seconds"}, "results": []}
        http_calls.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "method": request.method,
                "host": request.url.host,
                "path": request.url.path,
                "database": "999",
                "response_status": 429,
                "response_body": body,
                "response_origin": "synthetic",
            }
        )
        response_seen.set()
        return httpx.Response(429, json=body)

    def make_client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(search_response), **kwargs)

    async def download_image(request):
        assert str(request.url) == "https://fixtures.invalid/image.jpg"
        return httpx.Response(200, content=image_bytes)

    monkeypatch.setattr(network, "AsyncClient", make_client)
    adapter = nonebot.get_adapter(Adapter)
    monkeypatch.setattr(adapter, "request", download_image)

    def collect_log(message):
        logs.append(str(message))
        if message.record["exception"] is not None:
            logged_exceptions.append(message.record["exception"].type.__name__)

    log_sink = logger.add(collect_log, level="INFO", diagnose=False)
    plugin = nonebot.get_plugin("YetAnotherPicSearch")
    assert plugin is not None
    captured = None
    request_text = "发了搜图和图片，提示正在搜索，但没有结果，怎么回事？"
    try:
        async with app.test_matcher(list(plugin.matcher)) as ctx:
            ctx.patch_adapter(monkeypatch, adapter)
            bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="10001")
            original_send = ctx.got_call_send

            def record_send(bot, event, message, **kwargs):
                result = original_send(bot, event, message, **kwargs)
                messages.append(
                    chat(
                        str(result["message_id"]),
                        Message(message).extract_plain_text(),
                        is_bot=True,
                        reply_to="301",
                    )
                )
                return result

            def cleanup_api(adapter, api, **data):
                # Cancellation releases RecallContext; this cleanup is outside the capture window.
                assert api == "delete_msg" and data == {"message_id": 302}
                return None

            monkeypatch.setattr(ctx, "got_call_send", record_send)
            monkeypatch.setattr(ctx, "got_call_api", cleanup_api)
            ctx.receive_event(bot, event)
            ctx.should_call_send(
                event,
                Message(MessageSegment.reply(301)) + "正在进行搜索，请稍候",
                result={"message_id": 302},
                bot=bot,
            )
            dispatch = asyncio.create_task(ctx.run())
            blocked_task = None
            try:
                await asyncio.wait_for(response_seen.wait(), timeout=12)
                await asyncio.sleep(0)
                for task in asyncio.all_tasks():
                    frames, locks = _waiting_frames(task)
                    if len(locks) == 2 and locks[0] is locks[1] and locks[0].locked():
                        assert any("YetAnotherPicSearch" in frame["file"] for frame in frames)
                        captured = {"frames": frames, "same_lock": True, "lock_locked": True}
                        blocked_task = task
                        break
                assert captured is not None
                assert len(http_calls) == 1
                assert len(messages) == 2
                messages.append(
                    chat("303", request_text, is_bot=False, reply_to="301", current=True)
                )
                captured.update(
                    {
                        "observed_at": datetime.now(UTC).isoformat(),
                        "dispatch_pending": not dispatch.done(),
                        "http_calls": list(http_calls),
                        "plugin_exception_observed": bool(logged_exceptions),
                        "snapshot_phase": "before_harness_cancellation",
                    }
                )
                production_runtime = runtime_buffer.capture(production_correlation_id)
                production_kinds = [item.kind.value for item in production_runtime.observations]
                assert production_kinds[:2] == ["event_received", "matcher_started"]
                assert "matcher_completed" not in production_kinds
                assert "event_completed" not in production_kinds
                assert production_runtime.buffer_dropped_count == 0
                production_logs = log_buffer.capture(production_correlation_id)
                assert production_logs.logs == ()
                # Seal the window before harness cancellation can generate completion/recall logs.
                (run_dir / "plugin.log").write_text("".join(logs), encoding="utf-8")
            finally:
                if blocked_task is not None:
                    blocked_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await blocked_task
                dispatch.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatch
                await asyncio.sleep(0)
    finally:
        logger.remove(log_sink)

    assert captured is not None
    assert all(
        hashlib.sha256((source_root / name).read_bytes()).hexdigest() == digest
        for name, digest in source_hashes.items()
    )
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "correlation_id": f"repro-{run_id}",
        "request_text": request_text,
        "provenance": {
            "input": "synthetic user messages, attachment download, and HTTP response",
            "execution": "real installed plugin and dependencies under NoneBug",
            "scope": "one simulated group, one search, before harness cancellation",
            "limitation": "body status=429 deliberately triggers the branch; not a captured live service response",
        },
        "deployment": {
            "python": platform.python_version(),
            "versions": {
                name: version(name)
                for name in (
                    "YetAnotherPicSearch",
                    "PicImageSearch",
                    "nonebot2",
                    "nonebot-plugin-alconna",
                    "cookit",
                    "nonebug",
                )
            },
            "key": "nonempty synthetic placeholder; no external search was performed",
            "cache": "isolated test directory; unique image ID; no cache hit",
            "plugin_source_sha256": source_hashes,
            "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        },
        "runtime": captured,
        "production_runtime": production_runtime.to_dict(),
        "production_runtime_observer": {
            "source": "current triage worktree NoneBotRuntimeObserver",
            "dropped_count": production_observer.dropped_count,
            "captured_before_harness_cancellation": True,
            "correlated_exception_logs": len(production_logs.logs),
        },
        "conversation": {
            "schema_version": 2,
            "page_number": 1,
            "availability": "complete",
            "adapter": "OneBot V11",
            "platform": "qq",
            "conversation_type": "group",
            "conversation_id": "20001",
            "bot_id": "10001",
            "request_actor_id": "30001",
            "messages": messages,
            "has_more": False,
            "partial": False,
        },
        "plugin_log_sha256": hashlib.sha256((run_dir / "plugin.log").read_bytes()).hexdigest(),
    }
    (run_dir / "capture.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Capture saved: {run_dir / 'capture.json'}")
