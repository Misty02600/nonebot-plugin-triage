"""维护者工具对运行时共享知识索引读取器的兼容导出。"""

from nbtriage.knowledge_index import KnowledgeIndexReader

KnowledgeIndex = KnowledgeIndexReader

__all__ = ("KnowledgeIndex",)
