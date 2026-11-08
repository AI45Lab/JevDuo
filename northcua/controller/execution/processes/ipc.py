"""Length-prefixed JSON IPC helpers for controller process workers."""

from __future__ import annotations

import asyncio
import struct

from pydantic import ValidationError

from northcua.controller.execution.processes.contracts import WorkerEnvelope

_HEADER = struct.Struct("!I")
_MAX_FRAME_BYTES = 64_000_000


class WorkerIPCError(RuntimeError):
    """Raised when the worker IPC stream violates the framing protocol."""


async def read_envelope(reader: asyncio.StreamReader, *, max_frame_bytes: int = _MAX_FRAME_BYTES) -> WorkerEnvelope:
    """Read one typed worker envelope from a stream.

    Args:
        reader: Async stream reader connected to the worker IPC socket.
        max_frame_bytes: Maximum accepted JSON frame size.

    Returns:
        Validated worker IPC envelope.

    Raises:
        WorkerIPCError: If the frame size or JSON payload is invalid.
        asyncio.IncompleteReadError: If the stream closes mid-frame.
    """

    header = await reader.readexactly(_HEADER.size)
    (size,) = _HEADER.unpack(header)
    if size <= 0 or size > max_frame_bytes:
        raise WorkerIPCError(f"invalid worker IPC frame size: {size}")
    data = await reader.readexactly(size)
    try:
        return WorkerEnvelope.model_validate_json(data)
    except ValidationError as exc:
        raise WorkerIPCError(f"invalid worker IPC envelope: {exc}") from exc


async def write_envelope(writer: asyncio.StreamWriter, envelope: WorkerEnvelope) -> None:
    """Write one typed worker envelope to a stream.

    Args:
        writer: Async stream writer connected to the worker IPC socket.
        envelope: Validated envelope to serialize and send.

    Raises:
        WorkerIPCError: If the serialized envelope exceeds the frame budget.
    """

    data = envelope.model_dump_json().encode("utf-8")
    if len(data) > _MAX_FRAME_BYTES:
        raise WorkerIPCError(f"worker IPC frame exceeds max size: {len(data)}")
    writer.write(_HEADER.pack(len(data)))
    writer.write(data)
    await writer.drain()
