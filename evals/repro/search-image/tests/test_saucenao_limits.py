import asyncio
from contextlib import suppress

import httpx
from PicImageSearch import SauceNAO

LIMIT_MESSAGE = "4 searches every 30 seconds"


async def test_retry_branch_waits_on_its_own_lock() -> None:
    """验证代码分支的自锁；正文 status=429 是人为条件，不声称来自真实服务。"""
    from YetAnotherPicSearch.data_source.saucenao import saucenao_search

    requests: list[httpx.Request] = []
    first_response = asyncio.Event()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "saucenao.com"
        first_response.set()
        return httpx.Response(
            429,
            json={"header": {"status": 429, "message": LIMIT_MESSAGE}, "results": []},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        task = asyncio.create_task(saucenao_search(b"synthetic-transport-fixture", client, "all"))
        try:
            await asyncio.wait_for(first_response.wait(), timeout=10)
            await asyncio.sleep(0)
            assert not task.done()
            waiting_on = []
            held_locks = []
            coroutine = task.get_coro()
            while coroutine is not None:
                waiting_on.append(getattr(coroutine, "__qualname__", type(coroutine).__name__))
                frame = getattr(coroutine, "cr_frame", None)
                if frame is not None and "lock" in frame.f_locals:
                    held_locks.append(frame.f_locals["lock"])
                coroutine = getattr(coroutine, "cr_await", None)
            assert "Lock.acquire" in waiting_on, waiting_on
            assert len(held_locks) == 2
            assert held_locks[0] is held_locks[1]
            assert held_locks[0].locked()
            assert len(requests) == 1
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def test_http_status_and_body_status_are_different_fields() -> None:
    """用合成 HTTP 响应验证状态字段与实际分支，不代表一次线上限流记录。"""
    from YetAnotherPicSearch.data_source.ascii2d import ascii2d_search
    from YetAnotherPicSearch.data_source.saucenao import saucenao_search

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "saucenao.com"
        return httpx.Response(
            429,
            json={"header": {"status": -1, "message": LIMIT_MESSAGE}, "results": []},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        parsed = await SauceNAO(client=client, api_key="fixture-only").search(file=b"fixture")
        assert parsed.status_code == 429
        assert parsed.status == -1
        messages, next_search = await saucenao_search(b"fixture", client, "all")
        assert next_search is ascii2d_search
        assert "自动使用 Ascii2D" in str(messages[0])
        assert len(requests) == 2  # One parser check and one plugin call; no retry.
