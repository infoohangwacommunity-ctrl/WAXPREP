"""Storage interfaces and implementations."""

from waxprep.storage.memory import InMemoryStorage
from waxprep.storage.protocol import Storage

__all__ = ["InMemoryStorage", "Storage"]
