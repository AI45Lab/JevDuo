"""Controller storage and queue primitives."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from northcua.controller.storage.blob_store import LocalControllerBlobStore
    from northcua.controller.storage.persistence import SQLiteControllerStore, artifact_catalog_id
    from northcua.controller.storage.queue import TaskQueue


def __getattr__(name: str) -> object:
    exports = {
        "LocalControllerBlobStore": ("northcua.controller.storage.blob_store", "LocalControllerBlobStore"),
        "SQLiteControllerStore": ("northcua.controller.storage.persistence", "SQLiteControllerStore"),
        "TaskQueue": ("northcua.controller.storage.queue", "TaskQueue"),
        "artifact_catalog_id": ("northcua.controller.storage.persistence", "artifact_catalog_id"),
    }
    if name not in exports:
        raise AttributeError(name)
    module_name, attr_name = exports[name]
    module = __import__(module_name, fromlist=[attr_name])
    return getattr(module, attr_name)


__all__ = [
    "LocalControllerBlobStore",
    "SQLiteControllerStore",
    "TaskQueue",
    "artifact_catalog_id",
]
