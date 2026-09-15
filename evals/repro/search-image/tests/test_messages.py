from pathlib import Path
from unittest.mock import ANY

import httpx
import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message, MessageSegment
from nonebug import App


async def test_help_responds_with_plugin_help_image(app: App) -> None:
    import YetAnotherPicSearch

    event = GroupMessageEvent.model_validate(
        {
            "time": 1_789_200_000,
            "self_id": 10001,
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": 101,
            "group_id": 20001,
            "user_id": 30001,
            "message": Message("/搜图 -h"),
            "raw_message": "/搜图 -h",
            "font": 0,
            "sender": {"user_id": 30001, "nickname": "tester", "role": "member"},
            "to_me": False,
        }
    )
    help_image = (Path(YetAnotherPicSearch.__file__).parent / "res" / "usage.jpg").read_bytes()
    plugin = nonebot.get_plugin("YetAnotherPicSearch")
    assert plugin is not None
    async with app.test_matcher(list(plugin.matcher)) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="10001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message(MessageSegment.reply(101)) + MessageSegment.image(help_image),
            result={"message_id": 102},
            bot=bot,
        )
        ctx.should_finished()


async def test_default_search_reaches_saucenao_and_sends_result(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """平台收发、附件下载与 HTTP 响应为模拟；实际执行插件及搜索响应解析。"""
    import PicImageSearch.network as network
    import YetAnotherPicSearch
    import YetAnotherPicSearch.__main__ as handlers

    image_bytes = (Path(YetAnotherPicSearch.__file__).parent / "res" / "usage.jpg").read_bytes()
    message = Message("/搜图 ") + MessageSegment(
        "image", {"file": "fixture.jpg", "url": "https://fixtures.invalid/search.jpg"}
    )
    event = GroupMessageEvent.model_validate(
        {
            "time": 1_789_200_001,
            "self_id": 10001,
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": 201,
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
    searches = []

    def search_response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "saucenao.com"
        assert request.url.params["db"] == "999"
        searches.append(request)
        return httpx.Response(
            200,
            json={
                "header": {
                    "status": 0,
                    "long_remaining": 100,
                    "query_image_display": "/fixture.jpg",
                },
                "results": [
                    {
                        "header": {
                            "similarity": "95.0",
                            "thumbnail": "https://fixtures.invalid/thumbnail.jpg",
                            "index_id": 5,
                            "index_name": "fixture",
                            "hidden": 1,
                        },
                        "data": {
                            "title": "Fixture artwork",
                            "pixiv_id": 123,
                            "member_id": 456,
                            "member_name": "Fixture author",
                            "source": "Fixture catalog",
                        },
                    }
                ],
            },
        )

    def make_client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(search_response), **kwargs)

    async def download_image(request):
        assert str(request.url) == "https://fixtures.invalid/search.jpg"
        return httpx.Response(200, content=image_bytes)

    monkeypatch.setattr(network, "AsyncClient", make_client)
    # 等待撤回操作完成，避免后台任务跨越 NoneBug 上下文；保留真实撤回实现。
    recall_context = handlers.RecallContext
    monkeypatch.setattr(handlers, "RecallContext", lambda: recall_context(wait=True))
    adapter = nonebot.get_adapter(Adapter)
    monkeypatch.setattr(adapter, "request", download_image)
    api_calls = []
    plugin = nonebot.get_plugin("YetAnotherPicSearch")
    assert plugin is not None
    async with app.test_matcher(list(plugin.matcher)) as ctx:
        ctx.patch_adapter(monkeypatch, adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="10001")
        original_got_api = ctx.got_call_api

        def record_api(adapter, api, **data):
            api_calls.append((api, data))
            return original_got_api(adapter, api, **data)

        monkeypatch.setattr(ctx, "got_call_api", record_api)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message(MessageSegment.reply(201)) + "正在进行搜索，请稍候",
            result={"message_id": 202},
            bot=bot,
        )
        ctx.should_call_api(
            "send_msg",
            {"message_type": "group", "group_id": 20001, "message": ANY},
            result={"message_id": 203},
        )
        ctx.should_call_api("delete_msg", {"message_id": 202})
    assert len(searches) == 1
    result = api_calls[0][1]["message"]
    assert "SauceNAO (95.0%)" in result.extract_plain_text()
    assert "Fixture artwork" in result.extract_plain_text()
    assert "Fixture catalog" in result.extract_plain_text()
    assert result["reply"][0].data["id"] == "201"
