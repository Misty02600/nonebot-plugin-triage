"""知识索引与查询共用搜索分词，使用独立的 jieba 分词器与默认词典。"""

from __future__ import annotations

import re
import unicodedata

from jieba import Tokenizer

_TOKENIZER = Tokenizer()
_PARTS = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*|[\u3400-\u9fff]+")


def knowledge_search_tokens(text: str) -> list[str]:
    """保留词频用于 BM25；查询端另行去重并限制词数。"""
    tokens: list[str] = []
    for part in _PARTS.findall(unicodedata.normalize("NFKC", text).casefold()):
        if part[0].isascii():
            tokens.append(part)
            if "." in part:
                tokens.extend(part.split("."))
        else:
            tokens.extend(word for word in _TOKENIZER.cut_for_search(part) if len(word) > 1)
    return tokens
