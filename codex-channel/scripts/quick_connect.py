"""Bounded iroh transport for the TokenCoin loopback HTTP gateway.

The public surface is synchronous. A private thread owns the asyncio loop and
all iroh objects. This module never installs or downloads dependencies.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import secrets
import struct
import threading
import urllib.parse
from typing import Callable, Optional

ALPN = b"tokencoin/quick-connect/1"
HANDSHAKE_LIMIT = 1024
HEADER_ALLOWANCE = 64 * 1024
REQUEST_LIMIT = 1024 * 1024 + HEADER_ALLOWANCE
RESPONSE_LIMIT = 8 * 1024 * 1024 + HEADER_ALLOWANCE
CHUNK = 64 * 1024
HANDSHAKE_TIMEOUT = 5.0
CONNECT_TIMEOUT = 15.0
IO_TIMEOUT = 330.0
MAX_ACTIVE_STREAMS = 32
MAX_UNAUTH_CONNECTIONS = 16
MAX_LOCAL_CONNECTIONS = 16

_RUNTIME_LOCK = threading.Lock()
_RUNTIME = None


class _Runtime:
    def __init__(self, iroh):
        self.iroh = iroh
        self.loop = None
        self.error = None
        self.refs = 0
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="tokencoin-quick-connect", daemon=True)
        self.thread.start()
        if not self.ready.wait(5) or self.error is not None or self.loop is None:
            raise DependencyUnavailable("Could not initialize the iroh event loop") from self.error

    def _run(self):
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            self.iroh.iroh_ffi.uniffi_set_event_loop(loop)
        except BaseException as exc:
            self.error = exc
            self.ready.set()
            loop.close()
            return
        self.ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def _acquire_runtime(iroh):
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = _Runtime(iroh)
        elif _RUNTIME.iroh is not iroh:
            raise DependencyUnavailable("All QuickNode instances must use the same verified iroh module")
        _RUNTIME.refs += 1
        return _RUNTIME


def _release_runtime(runtime):
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is not runtime:
            return
        runtime.refs -= 1
        if runtime.refs:
            return
        runtime.loop.call_soon_threadsafe(runtime.loop.stop)
        runtime.thread.join(10)
        if runtime.thread.is_alive():
            raise QuickConnectError("Quick-connect thread did not stop cleanly")
        _RUNTIME = None


class QuickConnectError(RuntimeError):
    """Safe, user-displayable quick-connect failure."""


class DependencyUnavailable(QuickConnectError):
    pass


class InvalidTicket(QuickConnectError):
    pass


class PeerUnavailable(QuickConnectError):
    pass


class _PeerState:
    def __init__(self, server, connection, addr, token: str):
        self.server = server
        self.connection = connection
        self.addr = addr
        self.token = token
        self.tasks: set[asyncio.Task] = set()
        self.reconnect_lock = asyncio.Lock()


class QuickNode:
    """Own one iroh endpoint and expose loopback-only TCP adapters.

    ``relay_mode='default'`` uses iroh's built-in public relay map.
    ``relay_mode='disabled'`` is intended only for loopback tests.
    """

    def __init__(self, secret_key: Optional[bytes] = None,
                 validator: Optional[Callable[[str], bool]] = None, *,
                 relay_mode: str = "default", iroh_module=None):
        if secret_key is not None and (not isinstance(secret_key, bytes) or len(secret_key) != 32):
            raise ValueError("secret_key must be exactly 32 bytes")
        if validator is not None and not callable(validator):
            raise TypeError("validator must be callable")
        if relay_mode not in ("default", "disabled"):
            raise ValueError("relay_mode must be 'default' or 'disabled'")
        self._secret_key = secret_key
        self._validator = validator
        self._relay_mode = relay_mode
        self._iroh = iroh_module
        self._runtime = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._thread_error: Optional[BaseException] = None
        self._endpoint = None
        self._accept_task: Optional[asyncio.Task] = None
        self._supplier_target: Optional[tuple[str, int]] = None
        self._peers: dict[str, _PeerState] = {}
        self._tasks: set[asyncio.Task] = set()
        self._connection_tasks: set[asyncio.Task] = set()
        self._closed = False
        self._state_lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _load_iroh(self):
        if self._iroh is not None:
            return self._iroh
        try:
            from setup_quick_connect import load
            self._iroh = load()
        except Exception as exc:
            raise DependencyUnavailable("The verified iroh runtime is not installed") from exc
        return self._iroh

    def _ensure_thread(self) -> None:
        with self._state_lock:
            if self._closed:
                raise QuickConnectError("QuickNode is closed")
            if self._thread is not None:
                return
            iroh = self._load_iroh()
            self._runtime = _acquire_runtime(iroh)
            self._loop = self._runtime.loop
            self._thread = self._runtime.thread

    def _submit(self, coroutine, timeout: float = 30.0):
        self._ensure_thread()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise PeerUnavailable("Quick-connect operation timed out") from exc
        except (QuickConnectError, ValueError):
            raise
        except Exception as exc:
            raise PeerUnavailable("Quick-connect operation failed") from exc

    async def _get_endpoint(self):
        if self._endpoint is not None:
            return self._endpoint
        iroh = self._iroh
        if self._relay_mode == "disabled":
            options = iroh.EndpointOptions(preset=iroh.preset_minimal(), bind_addr="127.0.0.1:0",
                                           secret_key=self._secret_key, alpns=[ALPN],
                                           relay_mode=iroh.RelayMode.disabled())
        else:
            options = iroh.EndpointOptions(preset=iroh.preset_n0(), secret_key=self._secret_key,
                                           alpns=[ALPN])
        self._endpoint = await asyncio.wait_for(iroh.Endpoint.bind(options), CONNECT_TIMEOUT)
        return self._endpoint

    @staticmethod
    def _loopback_target(host: str, port: int) -> tuple[str, int]:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("supplier target must be loopback")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("supplier port must be 1..65535")
        return host, port

    def start_supplier(self, host: str = "127.0.0.1", port: int = 8781) -> str:
        target = self._loopback_target(host, port)
        if self._validator is None:
            raise ValueError("supplier mode requires a capability validator")
        return self._submit(self._start_supplier(target), 35)

    async def _start_supplier(self, target: tuple[str, int]) -> str:
        if self._supplier_target is not None:
            if self._supplier_target != target:
                raise ValueError("supplier is already bound to a different target")
            return str(self._iroh.EndpointTicket.from_addr(self._endpoint.addr()))
        endpoint = await self._get_endpoint()
        if self._relay_mode == "default":
            await asyncio.wait_for(endpoint.online(), CONNECT_TIMEOUT)
        self._supplier_target = target
        self._accept_task = asyncio.create_task(self._accept_loop())
        return str(self._iroh.EndpointTicket.from_addr(endpoint.addr()))

    async def _accept_loop(self) -> None:
        try:
            while self._supplier_target is not None:
                incoming = await self._endpoint.accept_next()
                if incoming is None:
                    return
                if len(self._connection_tasks) >= MAX_UNAUTH_CONNECTIONS:
                    # Dropping Incoming rejects it before application access.
                    continue
                self._track(asyncio.create_task(self._serve_connection(incoming)),
                            self._connection_tasks)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _track(self, task: asyncio.Task, bucket: Optional[set] = None) -> None:
        target = self._tasks if bucket is None else bucket
        target.add(task)
        task.add_done_callback(target.discard)

    async def _serve_connection(self, incoming) -> None:
        connection = None
        try:
            accepting = await asyncio.wait_for(incoming.accept(), HANDSHAKE_TIMEOUT)
            connection = await asyncio.wait_for(accepting.connect(), HANDSHAKE_TIMEOUT)
            first_stream = True
            while self._supplier_target is not None:
                timeout = HANDSHAKE_TIMEOUT if first_stream else IO_TIMEOUT
                bi = await asyncio.wait_for(connection.accept_bi(), timeout)
                first_stream = False
                if len(self._tasks) >= MAX_ACTIVE_STREAMS:
                    await bi.send().reset(1)
                    await bi.recv().stop(1)
                    continue
                self._track(asyncio.create_task(self._supplier_stream(bi, connection)))
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            if connection is not None:
                try:
                    connection.close(0, b"")
                except Exception:
                    pass

    async def _supplier_stream(self, bi, connection) -> None:
        send, recv = bi.send(), bi.recv()
        writer = None
        authorized = False
        try:
            raw_size = await asyncio.wait_for(recv.read_exact(4), HANDSHAKE_TIMEOUT)
            size = struct.unpack("!I", raw_size)[0]
            if not 1 <= size <= HANDSHAKE_LIMIT:
                raise ValueError("invalid handshake")
            value = json.loads(await asyncio.wait_for(recv.read_exact(size), HANDSHAKE_TIMEOUT))
            token = value.get("token") if isinstance(value, dict) and value.get("v") == 1 else None
            if not isinstance(token, str) or not 20 <= len(token) <= 512:
                raise ValueError("invalid capability")
            try:
                allowed = bool(self._validator(token))
            except Exception:
                allowed = False
            if not allowed:
                raise PermissionError("invalid capability")
            authorized = True
            assert self._supplier_target is not None
            reader, writer = await asyncio.wait_for(asyncio.open_connection(*self._supplier_target),
                                                    CONNECT_TIMEOUT)
            await asyncio.wait_for(send.write_all(b"\x01"), IO_TIMEOUT)
            await self._bridge(reader, writer, recv, send, REQUEST_LIMIT, RESPONSE_LIMIT)
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await asyncio.wait_for(send.write_all(b"\x00"), IO_TIMEOUT)
                await send.finish()
            except Exception:
                try:
                    await send.reset(1)
                except Exception:
                    pass
            try:
                await recv.stop(1)
            except Exception:
                pass
        finally:
            if not authorized:
                try:
                    connection.close(1, b"unauthorized")
                except Exception:
                    pass
            await self._close_writer(writer)

    @staticmethod
    async def _pump_tcp_to_quic(reader, send, limit: int) -> None:
        total = 0
        while True:
            data = await asyncio.wait_for(reader.read(CHUNK), IO_TIMEOUT)
            if not data:
                await send.finish()
                return
            total += len(data)
            if total > limit:
                raise ValueError("stream size limit")
            await asyncio.wait_for(send.write_all(data), IO_TIMEOUT)

    @staticmethod
    async def _pump_quic_to_tcp(recv, writer, limit: int) -> None:
        total = 0
        while True:
            data = await asyncio.wait_for(recv.read(CHUNK), IO_TIMEOUT)
            if not data:
                try:
                    writer.write_eof()
                except (AttributeError, NotImplementedError, OSError):
                    pass
                return
            total += len(data)
            if total > limit:
                raise ValueError("stream size limit")
            writer.write(data)
            await asyncio.wait_for(writer.drain(), IO_TIMEOUT)

    async def _bridge(self, reader, writer, recv, send, inbound_limit: int,
                      outbound_limit: int) -> None:
        left = asyncio.create_task(self._pump_tcp_to_quic(reader, send, outbound_limit))
        right = asyncio.create_task(self._pump_quic_to_tcp(recv, writer, inbound_limit))
        try:
            await asyncio.gather(left, right)
        finally:
            for task in (left, right):
                if not task.done():
                    task.cancel()
            await asyncio.gather(left, right, return_exceptions=True)

    def _validated_addr(self, ticket: str):
        if not isinstance(ticket, str) or not 1 <= len(ticket) <= 8192:
            raise InvalidTicket("Invalid iroh ticket")
        try:
            parsed = self._iroh.EndpointTicket.from_string(ticket)
            addr = parsed.endpoint_addr()
            relay = addr.relay_url()
            if self._relay_mode == "default":
                allowed = set(self._iroh.RelayMode.default_mode().relay_map().urls())
                parts = urllib.parse.urlsplit(relay or "")
                if (relay not in allowed or parts.scheme != "https" or not parts.hostname
                        or parts.username or parts.password or parts.query or parts.fragment
                        or parts.path not in ("", "/")):
                    raise ValueError("untrusted relay")
                # Never dial arbitrary direct addresses supplied by an invitation.
                return self._iroh.EndpointAddr(addr.id(), relay, [])
            direct = addr.direct_addresses()
            if relay is not None or not direct or not all(self._is_loopback_socket(x) for x in direct):
                raise ValueError("test tickets must be loopback-only")
            return self._iroh.EndpointAddr(addr.id(), None, direct)
        except InvalidTicket:
            raise
        except Exception as exc:
            raise InvalidTicket("Invalid or untrusted iroh ticket") from exc

    @staticmethod
    def _is_loopback_socket(value: str) -> bool:
        try:
            parts = urllib.parse.urlsplit("//" + value)
            return bool(parts.port) and ipaddress.ip_address(parts.hostname or "").is_loopback
        except (ValueError, TypeError):
            return False

    def connect_peer(self, ticket: str, upstream_token: str) -> str:
        if not isinstance(upstream_token, str) or not 20 <= len(upstream_token) <= 512:
            raise ValueError("upstream_token must be a scoped high-entropy capability")
        return self._submit(self._connect_peer(ticket, upstream_token), 35)

    async def _connect_peer(self, ticket: str, token: str) -> str:
        endpoint = await self._get_endpoint()
        addr = self._validated_addr(ticket)
        connection = await asyncio.wait_for(endpoint.connect(addr, ALPN), CONNECT_TIMEOUT)
        holder = {}

        async def accepted(reader, writer):
            state = holder["state"]
            task = asyncio.current_task()
            if len(state.tasks) >= MAX_LOCAL_CONNECTIONS:
                await self._close_writer(writer)
                return
            if task is not None:
                state.tasks.add(task)
            try:
                await self._peer_stream(state, reader, writer)
            finally:
                if task is not None:
                    state.tasks.discard(task)

        server = await asyncio.start_server(accepted, "127.0.0.1", 0, limit=CHUNK)
        port = server.sockets[0].getsockname()[1]
        base_url = "http://127.0.0.1:" + str(port)
        state = _PeerState(server, connection, addr, token)
        holder["state"] = state
        self._peers[base_url] = state
        return base_url

    async def _open_peer_bi(self, state: _PeerState):
        original = state.connection
        try:
            return await asyncio.wait_for(original.open_bi(), CONNECT_TIMEOUT)
        except Exception:
            # No HTTP byte has been read or sent yet. This only repairs the
            # transport for a new request; it never replays an accepted request.
            async with state.reconnect_lock:
                if state.connection is original:
                    state.connection = await asyncio.wait_for(
                        self._endpoint.connect(state.addr, ALPN), CONNECT_TIMEOUT)
                return await asyncio.wait_for(state.connection.open_bi(), CONNECT_TIMEOUT)

    async def _peer_stream(self, state: _PeerState, reader, writer) -> None:
        send = recv = None
        try:
            bi = await self._open_peer_bi(state)
            send, recv = bi.send(), bi.recv()
            handshake = json.dumps({"v": 1, "token": state.token}, separators=(",", ":")).encode()
            if len(handshake) > HANDSHAKE_LIMIT:
                raise ValueError("handshake too large")
            await asyncio.wait_for(
                send.write_all(struct.pack("!I", len(handshake)) + handshake), IO_TIMEOUT)
            ack = await asyncio.wait_for(recv.read_exact(1), HANDSHAKE_TIMEOUT)
            if not secrets.compare_digest(ack, b"\x01"):
                raise PermissionError("capability rejected")
            await self._bridge(reader, writer, recv, send, RESPONSE_LIMIT, REQUEST_LIMIT)
        except asyncio.CancelledError:
            raise
        except Exception:
            if send is not None:
                try:
                    await send.reset(1)
                except Exception:
                    pass
            if recv is not None:
                try:
                    await recv.stop(1)
                except Exception:
                    pass
        finally:
            await self._close_writer(writer)

    def disconnect_peer(self, base_url: str) -> None:
        if not isinstance(base_url, str):
            raise ValueError("base_url must be a local adapter URL")
        self._submit(self._disconnect_peer(base_url), 15)

    async def _disconnect_peer(self, base_url: str) -> None:
        state = self._peers.pop(base_url, None)
        if state is None:
            return
        state.server.close()
        await state.server.wait_closed()
        state.connection.close(0, b"")
        tasks = list(state.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop_supplier(self) -> None:
        if self._thread is not None:
            self._submit(self._stop_supplier(), 15)

    async def _stop_supplier(self) -> None:
        self._supplier_target = None
        if self._accept_task is not None:
            self._accept_task.cancel()
            await asyncio.gather(self._accept_task, return_exceptions=True)
            self._accept_task = None
        tasks = list(self._connection_tasks) + list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _close_writer(writer) -> None:
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._async_close(), loop)
        try:
            future.result(20)
        except Exception:
            future.cancel()
        runtime, self._runtime = self._runtime, None
        if runtime is not None:
            _release_runtime(runtime)

    async def _async_close(self) -> None:
        await self._stop_supplier()
        for base_url in list(self._peers):
            await self._disconnect_peer(base_url)
        if self._endpoint is not None:
            await self._endpoint.close()
            self._endpoint = None
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["QuickNode", "QuickConnectError", "DependencyUnavailable",
           "InvalidTicket", "PeerUnavailable"]
