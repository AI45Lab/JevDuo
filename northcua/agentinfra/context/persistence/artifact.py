"""Artifact-backed checkpoint repository with cross-process CAS leases."""

from __future__ import annotations

import asyncio

try:
    import fcntl
except ImportError:  # Windows local agents run in one process; batch workers require Linux.
    fcntl = None
import hashlib
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Lock
from typing import AsyncIterator
from weakref import WeakValueDictionary

from northcua.agentkernel.context.domain.models import ContextCheckpoint, ContextOwnerKey

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")
logger = logging.getLogger(__name__)


class ArtifactCheckpointRepository:
    """Persist the latest checkpoint as one atomic JSON document per owner."""

    _locks_guard = Lock()
    _process_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
    _held_owner_leases: ContextVar[frozenset[str]] = ContextVar(
        "northcua_context_held_owner_leases",
        default=frozenset(),
    )

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache: dict[str, ContextCheckpoint] = {}

    async def load(self, owner: ContextOwnerKey) -> ContextCheckpoint | None:
        key = owner.storage_key()
        path = self._checkpoint_path(owner)
        checkpoint = await asyncio.to_thread(self._read_checkpoint, path)
        if checkpoint is not None:
            self._cache[key] = checkpoint
        else:
            self._cache.pop(key, None)
        return checkpoint

    async def compare_and_swap(
        self,
        owner: ContextOwnerKey,
        *,
        expected_revision: int,
        checkpoint: ContextCheckpoint,
    ) -> bool:
        if checkpoint.owner != owner:
            raise ValueError("checkpoint owner does not match repository key")
        key = owner.storage_key()
        if key in self._held_owner_leases.get():
            return await self._compare_and_swap_locked(
                owner,
                expected_revision=expected_revision,
                checkpoint=checkpoint,
            )
        async with self.lease(owner, "checkpoint-cas"):
            return await self._compare_and_swap_locked(
                owner,
                expected_revision=expected_revision,
                checkpoint=checkpoint,
            )

    async def _compare_and_swap_locked(
        self,
        owner: ContextOwnerKey,
        *,
        expected_revision: int,
        checkpoint: ContextCheckpoint,
    ) -> bool:
        """Execute CAS while the owner lease is held by the current task."""

        path = self._checkpoint_path(owner)
        current = await asyncio.to_thread(self._read_checkpoint, path)
        current_revision = current.revision if current is not None else 0
        if current_revision != expected_revision:
            return False
        if current is None and path.exists():
            await asyncio.to_thread(self._preserve_corrupt_checkpoint, path)
        await asyncio.to_thread(self._atomic_write, path, checkpoint)
        self._cache[owner.storage_key()] = checkpoint
        return True

    @asynccontextmanager
    async def lease(self, owner: ContextOwnerKey, source_digest: str) -> AsyncIterator[None]:
        """Serialize summary work for an owner across tasks and worker processes."""

        del source_digest  # Leases serialize by owner; digest is audit context only.
        owner_key = owner.storage_key()
        key = owner_key
        lock = self._process_lock(key)
        await lock.acquire()
        file_handle = None
        try:
            lock_path = self._owner_dir(owner) / "checkpoint.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            file_handle = await asyncio.to_thread(lock_path.open, "a+b")
            # The per-owner asyncio lock serializes this process; flock extends
            # the lease to other worker processes where it exists.
            if fcntl is not None:
                await asyncio.to_thread(fcntl.flock, file_handle.fileno(), fcntl.LOCK_EX)
            # Another process may have committed while this process waited.
            self._cache.pop(owner.storage_key(), None)
            token = self._held_owner_leases.set(self._held_owner_leases.get() | {owner_key})
            try:
                yield
            finally:
                self._held_owner_leases.reset(token)
        finally:
            if file_handle is not None:
                if fcntl is not None:
                    await asyncio.to_thread(fcntl.flock, file_handle.fileno(), fcntl.LOCK_UN)
                file_handle.close()
            lock.release()

    @classmethod
    def _process_lock(cls, key: str) -> asyncio.Lock:
        with cls._locks_guard:
            lock = cls._process_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                cls._process_locks[key] = lock
            return lock

    def _owner_dir(self, owner: ContextOwnerKey) -> Path:
        readable = (
            "-".join(
                filter(
                    None,
                    (
                        _SAFE_COMPONENT.sub("_", owner.logical_agent_path).strip("._")[:48],
                        _SAFE_COMPONENT.sub("_", owner.task_epoch).strip("._")[:32],
                    ),
                )
            )
            or "agent"
        )
        return self._root / "agents" / f"{readable}-{owner.storage_key()[:16]}"

    def _checkpoint_path(self, owner: ContextOwnerKey) -> Path:
        return self._owner_dir(owner) / "checkpoint.json"

    @staticmethod
    def _read_checkpoint(path: Path) -> ContextCheckpoint | None:
        if not path.exists():
            return None
        try:
            return ContextCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring invalid context checkpoint at %s: %s", path, type(exc).__name__)
            return None

    @staticmethod
    def _preserve_corrupt_checkpoint(path: Path) -> None:
        try:
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()[:12]
            preserved = path.with_name(f"checkpoint.corrupt.{digest}.json")
            if not preserved.exists():
                fd = os.open(preserved, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError:
            logger.warning("Unable to preserve corrupt context checkpoint at %s", path, exc_info=True)

    @staticmethod
    def _atomic_write(path: Path, checkpoint: ContextCheckpoint) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        payload = checkpoint.model_dump_json(indent=2)
        fd = os.open(temp_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
