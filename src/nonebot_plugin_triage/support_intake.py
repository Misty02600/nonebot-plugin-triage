from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SupportRequest:
    content: str

    @property
    def is_empty(self) -> bool:
        return not self.content


def normalize_support_request(text: str) -> SupportRequest:
    """只规范化当前请求文字，不承担任何语义分类。"""
    return SupportRequest(" ".join(text.split()))


__all__ = ("SupportRequest", "normalize_support_request")
