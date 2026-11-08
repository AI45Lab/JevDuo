"""Backpressure-safe TCP proxy for exposing a local controller."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import resource
import signal
import socket
import time
from dataclasses import dataclass, field

logger = logging.getLogger("northcua.controller_tcp_proxy")


@dataclass(frozen=True)
class TcpProxyConfig:
    """Runtime configuration for :class:`AsyncTcpProxy`."""

    listen_host: str = "0.0.0.0"
    listen_port: int = 0
    target_host: str = "127.0.0.1"
    target_port: int = 8885
    connect_timeout_seconds: float = 5.0
    idle_timeout_seconds: float = 0.0
    half_close_timeout_seconds: float = 30.0
    close_timeout_seconds: float = 5.0
    drain_timeout_seconds: float = 30.0
    write_timeout_seconds: float = 30.0
    stats_interval_seconds: float = 30.0
    backlog: int = 4096
    max_connections: int = 4096
    fd_reserve: int = 256
    read_size_bytes: int = 65536
    write_buffer_high_bytes: int = 262144
    tcp_keepalive: bool = True
    tcp_keepalive_idle_seconds: int = 60
    tcp_keepalive_interval_seconds: int = 30
    tcp_keepalive_count: int = 5


@dataclass
class TcpProxyStats:
    """Low-cardinality in-process telemetry for the proxy."""

    accepted: int = 0
    active: int = 0
    peak_active: int = 0
    closed: int = 0
    rejected: int = 0
    connect_failures: int = 0
    relay_failures: int = 0
    close_timeouts: int = 0
    write_timeouts: int = 0
    idle_timeouts: int = 0
    client_to_target_bytes: int = 0
    target_to_client_bytes: int = 0
    backpressure_events: int = 0
    backpressure_seconds: float = 0.0
    max_backpressure_seconds: float = 0.0

    def observe_backpressure(self, elapsed: float) -> None:
        """Record writer drain time when it is materially non-zero."""

        if elapsed < 0.001:
            return
        self.backpressure_events += 1
        self.backpressure_seconds += elapsed
        self.max_backpressure_seconds = max(self.max_backpressure_seconds, elapsed)


@dataclass
class _Connection:
    client_socket: socket.socket | None = None
    client_writer: asyncio.StreamWriter | None = None
    target_writer: asyncio.StreamWriter | None = None
    relay_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    last_activity: float = field(default_factory=time.monotonic)


class AsyncTcpProxy:
    """Bounded, async TCP proxy with explicit write-side backpressure."""

    def __init__(self, config: TcpProxyConfig) -> None:
        self.config = config
        self.stats = TcpProxyStats()
        self._listener: socket.socket | None = None
        self._accept_reader_registered = False
        self._connections: dict[asyncio.Task[None], _Connection] = {}
        self._stats_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._stop_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._serve_stopped = asyncio.Event()

    @property
    def bound_port(self) -> int:
        """Return the bound TCP port after :meth:`start`."""

        if self._listener is None:
            raise RuntimeError("proxy is not running")
        return int(self._listener.getsockname()[1])

    async def start(self) -> None:
        """Start accepting connections."""

        async with self._lifecycle_lock:
            if self._listener is not None:
                raise RuntimeError("proxy is already running")
            if self._stop_task is not None and not self._stop_task.done():
                raise RuntimeError("proxy is still stopping")
            _validate_fd_budget(self.config)
            listener = _create_listener(self.config)
            self._stopped = False
            self._serve_stopped = asyncio.Event()
            self._stop_task = None
            self._listener = listener
            self._resume_accepting()
            if self.config.stats_interval_seconds > 0:
                self._stats_task = asyncio.create_task(self._emit_stats(), name="northcua-tcp-proxy-stats")
            logger.info(
                "listening=%s:%s target=%s:%s max_connections=%s backlog=%s",
                self.config.listen_host,
                self.bound_port,
                self.config.target_host,
                self.config.target_port,
                self.config.max_connections,
                self.config.backlog,
            )

    async def serve_forever(self) -> None:
        """Serve until cancelled or stopped."""

        if self._listener is None:
            await self.start()
        await self._serve_stopped.wait()

    async def stop(self) -> None:
        """Stop once; caller cancellation does not abandon the shared drain."""

        async with self._lifecycle_lock:
            if self._stopped:
                return
            stop_task = self._stop_task
            if stop_task is None:
                stop_task = asyncio.create_task(self._stop_impl(), name="northcua-tcp-proxy-drain")
                self._stop_task = stop_task
        await asyncio.shield(stop_task)

    async def _stop_impl(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, self.config.drain_timeout_seconds)
        self._pause_accepting()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._stats_task is not None:
            self._stats_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stats_task
            self._stats_task = None

        handlers = [task for task in self._connections if not task.done()]
        if handlers:
            done, pending = await asyncio.wait(handlers, timeout=max(0.0, deadline - loop.time()))
            del done
            for task in pending:
                connection = self._connections.get(task)
                if connection is not None:
                    _abort_connection_client(connection)
                    if connection.target_writer is not None:
                        _abort_writer(connection.target_writer)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=max(0.0, deadline - loop.time()))
        self._stopped = True
        self._serve_stopped.set()
        logger.info("stopped %s", self.stats_snapshot())

    def _resume_accepting(self) -> None:
        """Register the listener only while an admission slot is available."""

        listener = self._listener
        if (
            listener is None
            or self._accept_reader_registered
            or self.stats.active >= self.config.max_connections
            or self._stopped
        ):
            return
        asyncio.get_running_loop().add_reader(listener.fileno(), self._accept_ready)
        self._accept_reader_registered = True

    def _pause_accepting(self) -> None:
        listener = self._listener
        if listener is None or not self._accept_reader_registered:
            return
        asyncio.get_running_loop().remove_reader(listener.fileno())
        self._accept_reader_registered = False

    def _accept_ready(self) -> None:
        """Accept only as many sockets as can be admitted into the process."""

        listener = self._listener
        if listener is None:
            return
        while self.stats.active < self.config.max_connections:
            try:
                client_socket, _peer = listener.accept()
            except BlockingIOError:
                return
            except OSError:
                if self._listener is not None:
                    logger.exception("listener_accept_failed")
                return
            client_socket.setblocking(False)
            connection = _Connection(client_socket=client_socket)
            self.stats.accepted += 1
            self.stats.active += 1
            self.stats.peak_active = max(self.stats.peak_active, self.stats.active)
            handler = asyncio.create_task(
                self._handle_accepted_socket(connection),
                name="northcua-tcp-proxy-connection",
            )
            self._connections[handler] = connection
            handler.add_done_callback(self._connection_done)
        self._pause_accepting()

    async def _handle_accepted_socket(self, connection: _Connection) -> None:
        client_socket = connection.client_socket
        if client_socket is None:
            return
        try:
            client_reader, client_writer = await asyncio.open_connection(sock=client_socket)
        except BaseException:
            client_socket.close()
            raise
        connection.client_socket = None
        connection.client_writer = client_writer
        await self._handle_client(client_reader, connection)

    def stats_snapshot(self) -> str:
        """Return stable, secret-free key/value telemetry."""

        stats = self.stats
        return (
            f"accepted={stats.accepted} active={stats.active} peak_active={stats.peak_active} "
            f"closed={stats.closed} rejected={stats.rejected} connect_failures={stats.connect_failures} "
            f"relay_failures={stats.relay_failures} client_to_target_bytes={stats.client_to_target_bytes} "
            f"close_timeouts={stats.close_timeouts} "
            f"write_timeouts={stats.write_timeouts} "
            f"idle_timeouts={stats.idle_timeouts} "
            f"target_to_client_bytes={stats.target_to_client_bytes} "
            f"backpressure_events={stats.backpressure_events} "
            f"backpressure_seconds={stats.backpressure_seconds:.6f} "
            f"max_backpressure_seconds={stats.max_backpressure_seconds:.6f}"
        )

    def _accept_client(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        """Admit or reject synchronously inside the accept callback."""

        stopping = self._stop_task is not None and not self._stop_task.done()
        if self._stopped or stopping or self.stats.active >= self.config.max_connections:
            self.stats.rejected += 1
            _abort_writer(client_writer)
            return

        connection = _Connection(client_writer=client_writer)
        self.stats.accepted += 1
        self.stats.active += 1
        self.stats.peak_active = max(self.stats.peak_active, self.stats.active)
        handler = asyncio.create_task(
            self._handle_client(client_reader, connection),
            name="northcua-tcp-proxy-connection",
        )
        self._connections[handler] = connection
        handler.add_done_callback(self._connection_done)

    def _connection_done(self, handler: asyncio.Task[None]) -> None:
        connection = self._connections.pop(handler, None)
        if connection is None:
            return
        self.stats.active -= 1
        self.stats.closed += 1
        self._resume_accepting()
        if not handler.cancelled():
            error = handler.exception()
            if error is not None:
                self.stats.relay_failures += 1
                logger.warning("connection_failed error=%r", error)

    async def _handle_client(self, client_reader: asyncio.StreamReader, connection: _Connection) -> None:
        client_writer = connection.client_writer
        if client_writer is None:
            raise RuntimeError("admitted client stream is unavailable")
        peer = client_writer.get_extra_info("peername")
        idle_task: asyncio.Task[None] | None = None
        try:
            _configure_writer(client_writer, self.config)
            try:
                async with asyncio.timeout(self.config.connect_timeout_seconds):
                    target_reader, target_writer = await asyncio.open_connection(
                        self.config.target_host,
                        self.config.target_port,
                    )
            except (TimeoutError, OSError) as exc:
                self.stats.connect_failures += 1
                logger.warning(
                    "connect_failed peer=%s target=%s:%s error=%r",
                    peer,
                    self.config.target_host,
                    self.config.target_port,
                    exc,
                )
                return

            connection.target_writer = target_writer
            _configure_writer(target_writer, self.config)
            relays = {
                asyncio.create_task(
                    self._relay(
                        client_reader,
                        target_writer,
                        client_to_target=True,
                        connection=connection,
                    ),
                    name="northcua-tcp-proxy-client-to-target",
                ),
                asyncio.create_task(
                    self._relay(target_reader, client_writer, client_to_target=False, connection=connection),
                    name="northcua-tcp-proxy-target-to-client",
                ),
            }
            connection.relay_tasks = relays
            idle_task = asyncio.create_task(self._wait_for_idle(connection), name="northcua-tcp-proxy-idle")
            waiters = {*relays, idle_task}
            done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if idle_task in done:
                self.stats.idle_timeouts += 1
                done.remove(idle_task)
                for task in relays:
                    task.cancel()
                await asyncio.gather(*relays, return_exceptions=True)
                return
            idle_task.cancel()
            await asyncio.gather(idle_task, return_exceptions=True)
            pending.discard(idle_task)
            first_results = await asyncio.gather(*done, return_exceptions=True)
            first_failed = any(isinstance(result, BaseException) for result in first_results)
            if pending and not first_failed:
                more_done, pending = await asyncio.wait(
                    pending,
                    timeout=max(0.0, self.config.half_close_timeout_seconds),
                )
                done.update(more_done)
            for task in pending:
                task.cancel()
            results = await asyncio.gather(*done, *pending, return_exceptions=True)
            for result in results:
                if isinstance(result, asyncio.CancelledError) or result is None:
                    continue
                if isinstance(result, BaseException):
                    self.stats.relay_failures += 1
                    logger.warning("relay_failed peer=%s error=%r", peer, result)
        finally:
            if idle_task is not None and not idle_task.done():
                idle_task.cancel()
                await asyncio.gather(idle_task, return_exceptions=True)
            for task in connection.relay_tasks:
                if not task.done():
                    task.cancel()
            if connection.relay_tasks:
                await asyncio.gather(*connection.relay_tasks, return_exceptions=True)
            stopping = self._stop_task is not None and not self._stop_task.done()
            if stopping:
                if connection.target_writer is not None:
                    _abort_writer(connection.target_writer)
                _abort_writer(client_writer)
            elif connection.target_writer is not None:
                if await _close_writer(
                    connection.target_writer,
                    timeout_seconds=self.config.close_timeout_seconds,
                ):
                    self.stats.close_timeouts += 1
            if not stopping and await _close_writer(client_writer, timeout_seconds=self.config.close_timeout_seconds):
                self.stats.close_timeouts += 1

    async def _relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        client_to_target: bool,
        connection: _Connection | None = None,
    ) -> None:
        while True:
            chunk = await reader.read(self.config.read_size_bytes)
            if not chunk:
                with contextlib.suppress(AttributeError, NotImplementedError, OSError):
                    if writer.can_write_eof():
                        writer.write_eof()
                        await self._drain_writer(writer)
                return
            writer.write(chunk)
            if connection is not None:
                connection.last_activity = time.monotonic()
            started = time.monotonic()
            await self._drain_writer(writer)
            self.stats.observe_backpressure(time.monotonic() - started)
            if client_to_target:
                self.stats.client_to_target_bytes += len(chunk)
            else:
                self.stats.target_to_client_bytes += len(chunk)

    async def _wait_for_idle(self, connection: _Connection) -> None:
        timeout = self.config.idle_timeout_seconds
        if timeout <= 0:
            await asyncio.Event().wait()
            return
        while True:
            remaining = timeout - (time.monotonic() - connection.last_activity)
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def _drain_writer(self, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(max(0.001, self.config.write_timeout_seconds)):
                await writer.drain()
        except TimeoutError:
            self.stats.write_timeouts += 1
            raise TimeoutError(
                f"TCP proxy write drain timed out after {self.config.write_timeout_seconds:.3f}s"
            ) from None

    async def _emit_stats(self) -> None:
        while True:
            await asyncio.sleep(self.config.stats_interval_seconds)
            logger.info("stats %s", self.stats_snapshot())


def _configure_writer(writer: asyncio.StreamWriter, config: TcpProxyConfig) -> None:
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if config.tcp_keepalive:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for option_name, value in (
                ("TCP_KEEPIDLE", config.tcp_keepalive_idle_seconds),
                ("TCP_KEEPINTVL", config.tcp_keepalive_interval_seconds),
                ("TCP_KEEPCNT", config.tcp_keepalive_count),
            ):
                option = getattr(socket, option_name, None)
                if option is not None:
                    with contextlib.suppress(OSError):
                        sock.setsockopt(socket.IPPROTO_TCP, option, value)
    high = max(16384, config.write_buffer_high_bytes)
    writer.transport.set_write_buffer_limits(high=high, low=max(8192, high // 4))


def _create_listener(config: TcpProxyConfig) -> socket.socket:
    """Create a non-blocking listener whose accepts are controlled by capacity."""

    family = socket.AF_INET6 if ":" in config.listen_host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.setblocking(False)
        listener.bind((config.listen_host, config.listen_port))
        listener.listen(max(1, config.backlog))
    except BaseException:
        listener.close()
        raise
    return listener


async def _close_writer(writer: asyncio.StreamWriter, *, timeout_seconds: float) -> bool:
    """Close one stream without allowing a stuck peer to block shutdown."""

    writer.close()
    try:
        async with asyncio.timeout(max(0.001, timeout_seconds)):
            await writer.wait_closed()
    except TimeoutError:
        transport = getattr(writer, "transport", None)
        if transport is not None:
            transport.abort()
        return True
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    return False


def _abort_writer(writer: asyncio.StreamWriter) -> None:
    """Synchronously release an admitted socket without scheduling close work."""

    transport = getattr(writer, "transport", None)
    if transport is not None:
        transport.abort()
    else:
        writer.close()


def _abort_connection_client(connection: _Connection) -> None:
    """Release either the raw accepted socket or its promoted stream."""

    if connection.client_writer is not None:
        _abort_writer(connection.client_writer)
    elif connection.client_socket is not None:
        connection.client_socket.close()


def _validate_fd_budget(config: TcpProxyConfig) -> None:
    required = (2 * config.max_connections) + max(0, config.fd_reserve)
    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit != resource.RLIM_INFINITY and soft_limit < required:
        raise RuntimeError(
            "TCP proxy file-descriptor limit is too low: "
            f"soft_limit={soft_limit} required={required} "
            f"max_connections={config.max_connections} reserve={config.fd_reserve}"
        )


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=8885)
    parser.add_argument("--connect-timeout", type=_positive_float, default=5.0)
    parser.add_argument("--idle-timeout", type=_non_negative_float, default=0.0)
    parser.add_argument("--half-close-timeout", type=_positive_float, default=30.0)
    parser.add_argument("--close-timeout", type=_positive_float, default=5.0)
    parser.add_argument("--drain-timeout", type=_positive_float, default=30.0)
    parser.add_argument(
        "--write-timeout",
        type=_positive_float,
        default=float(os.getenv("NORTHCUA_CONTROLLER_PROXY_WRITE_TIMEOUT_SECONDS", "30")),
    )
    parser.add_argument("--stats-interval", type=_positive_float, default=30.0)
    parser.add_argument(
        "--backlog",
        type=_positive_int,
        default=int(os.getenv("NORTHCUA_CONTROLLER_PROXY_BACKLOG", "4096")),
    )
    parser.add_argument(
        "--max-connections",
        type=_positive_int,
        default=int(os.getenv("NORTHCUA_CONTROLLER_PROXY_MAX_CONNECTIONS", "4096")),
    )
    parser.add_argument(
        "--fd-reserve",
        type=_positive_int,
        default=int(os.getenv("NORTHCUA_CONTROLLER_PROXY_FD_RESERVE", "256")),
    )
    parser.add_argument(
        "--no-tcp-keepalive",
        action="store_false",
        dest="tcp_keepalive",
        help="disable TCP keepalive probes",
    )
    parser.add_argument(
        "--write-buffer-high-bytes",
        type=_positive_int,
        default=int(os.getenv("NORTHCUA_CONTROLLER_PROXY_WRITE_BUFFER_HIGH_BYTES", "262144")),
    )
    return parser


async def _run(config: TcpProxyConfig) -> None:
    proxy = AsyncTcpProxy(config)
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_requested.set)
    await proxy.start()
    serving = asyncio.create_task(proxy.serve_forever(), name="northcua-tcp-proxy-server")
    stopping = asyncio.create_task(stop_requested.wait(), name="northcua-tcp-proxy-stop")
    try:
        done, _ = await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if serving in done:
            await serving
    finally:
        stopping.cancel()
        serving.cancel()
        await asyncio.gather(serving, stopping, return_exceptions=True)
        await proxy.stop()


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    config = TcpProxyConfig(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        target_host=args.target_host,
        target_port=args.target_port,
        connect_timeout_seconds=args.connect_timeout,
        idle_timeout_seconds=args.idle_timeout,
        half_close_timeout_seconds=args.half_close_timeout,
        close_timeout_seconds=args.close_timeout,
        drain_timeout_seconds=args.drain_timeout,
        write_timeout_seconds=args.write_timeout,
        stats_interval_seconds=args.stats_interval,
        backlog=args.backlog,
        max_connections=args.max_connections,
        fd_reserve=args.fd_reserve,
        write_buffer_high_bytes=args.write_buffer_high_bytes,
        tcp_keepalive=args.tcp_keepalive,
    )
    asyncio.run(_run(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
