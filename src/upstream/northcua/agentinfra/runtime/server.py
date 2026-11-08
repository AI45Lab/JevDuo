"""Shared websocket-server scaffolding for session-scoped agents."""

from __future__ import annotations

import asyncio
from abc import ABC
from typing import Generic, TypeVar

from northcua.agentinfra.runtime.contracts import SessionScopedFacade

TFacade = TypeVar("TFacade", bound=SessionScopedFacade)


class AgentServerBase(ABC, Generic[TFacade]):
    """Shared session/facade/background-task lifecycle for websocket servers.

    Attributes:
        facade: Primary session-scoped facade.
        host: Server host.
        port: Server port.
    """

    def __init__(self, *, facade: TFacade, host: str, port: int) -> None:
        """Initialize shared server lifecycle state.

        Args:
            facade: Primary session-scoped facade.
            host: Server host.
            port: Server port.
        """

        self.facade = facade
        self.host = host
        self.port = port
        self._facades = {facade.session_id: facade}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._admission_tasks: set[asyncio.Task] = set()
        self._closing = False
        self._active_sessions: set[str] = set()
        self._session_connections: dict[str, object] = {}

    def _get_facade(self, session_id: str) -> TFacade:
        facade = self._facades.get(session_id)
        if facade is not None:
            return facade
        spawned = self.facade.spawn_session(session_id)
        self._facades[session_id] = spawned
        return spawned

    def _track_background_task(self, task: asyncio.Task[None], *, session_id: str | None = None) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        if session_id is not None:
            task.add_done_callback(lambda _task, sid=session_id: self._active_sessions.discard(sid))

    async def cancel_background_tasks(self) -> None:
        """Cancel and await tracked background tasks before server shutdown."""

        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def drain_cached_facades(self) -> None:
        """Drain all cached facades exactly once before cache cleanup."""

        seen: set[int] = set()
        errors: list[Exception] = []
        for facade in list(self._facades.values()):
            facade_id = id(facade)
            if facade_id in seen:
                continue
            seen.add(facade_id)
            try:
                await facade.drain_background_tasks()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to drain agent facades", errors)

    async def shutdown_session_cache(self) -> list[str]:
        """Drain facades and clear all session-scoped runtime caches.

        Returns:
            Session ids that were cached before shutdown.
        """

        self._closing = True
        admissions = [task for task in self._admission_tasks if task is not asyncio.current_task()]
        for task in admissions:
            task.cancel()
        if admissions:
            await asyncio.gather(*admissions, return_exceptions=True)
        await self.cancel_background_tasks()
        session_ids = list(self._facades.keys())
        try:
            await self.drain_cached_facades()
        finally:
            self._active_sessions.clear()
            self._session_connections.clear()
            self._facades.clear()
        return session_ids
