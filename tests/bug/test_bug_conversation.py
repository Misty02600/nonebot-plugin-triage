from nbtriage.bug_conversation import (
    BugConversationMessage,
    BugConversationPage,
)


def test_conversation_models_keep_visible_chat_content_unchanged() -> None:
    content = "authorization=Bearer visible-group-value cookie=session-value user=123456789"
    message = BugConversationMessage(
        sender_id="123456789",
        sent_at=1,
        sender_name="测试成员",
        sender_roles=("admin",),
        is_bot=False,
        is_request_actor=True,
        content=content,
    )

    page = BugConversationPage(
        page_number=1,
        messages=(message,),
        has_more=False,
        partial=False,
    )

    assert page.messages[0].content == content
    assert page.model_dump(mode="json")["messages"][0]["content"] == content
    assert page.model_dump(mode="json")["messages"][0]["sender_id"] == "123456789"
    assert page.model_dump(mode="json")["messages"][0]["sender_roles"] == ["admin"]
