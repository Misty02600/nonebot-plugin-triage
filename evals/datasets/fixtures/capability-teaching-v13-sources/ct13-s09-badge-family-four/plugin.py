from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg


def render_badge(style: str, text: str) -> bytes:
    return f"{style}:{text}".encode()


def create_badge_handler(style: str):
    async def handle_badge(args: Message = CommandArg()):
        text = args.extract_plain_text().strip()
        if not text:
            return "请提供徽记文字"
        return render_badge(style, text)

    return handle_badge


sketch_handler = create_badge_handler("素描")
watercolor_handler = create_badge_handler("水彩")
woodcut_handler = create_badge_handler("版画")
silhouette_handler = create_badge_handler("剪影")

sketch = on_command("生成素描徽记", handlers=[sketch_handler])
watercolor = on_command("生成水彩徽记", handlers=[watercolor_handler])
woodcut = on_command("生成版画徽记", handlers=[woodcut_handler])
silhouette = on_command("生成剪影徽记", handlers=[silhouette_handler])
