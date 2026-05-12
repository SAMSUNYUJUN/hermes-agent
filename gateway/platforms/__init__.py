"""Platform adapters for messaging integrations.

Only the shared base types and Feishu/Lark adapter remain in-tree.
"""

from .base import BasePlatformAdapter, MessageEvent, SendResult

__all__ = [
    "BasePlatformAdapter",
    "MessageEvent",
    "SendResult",
]
