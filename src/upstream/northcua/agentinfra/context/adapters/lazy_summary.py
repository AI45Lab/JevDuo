"""Lazy lifecycle wrapper for expensive internal summary provider clients."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from northcua.agentkernel.context.domain.models import SummaryInvocationRequest, SummaryInvocationResult
from northcua.agentkernel.context.ports import SummaryClient


class LazySummaryClient:
    """Construct the provider-backed client only when compaction actually runs."""

    def __init__(self, factory: Callable[[], SummaryClient], *, source_capacity_bytes: int | None = None) -> None:
        self._factory = factory
        self._delegate: SummaryClient | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._source_capacity_bytes = source_capacity_bytes

    @property
    def source_capacity_bytes(self) -> int | None:
        return self._source_capacity_bytes

    async def summarize(self, request: SummaryInvocationRequest) -> SummaryInvocationResult:
        delegate = await self._get_or_create()
        return await delegate.summarize(request)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            delegate = self._delegate
            self._delegate = None
        if delegate is not None:
            await delegate.close()

    async def _get_or_create(self) -> SummaryClient:
        async with self._lock:
            if self._closed:
                raise RuntimeError("summary client is closed")
            if self._delegate is None:
                self._delegate = self._factory()
            return self._delegate
