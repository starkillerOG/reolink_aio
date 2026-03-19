"""Base TCP/UDP protocol and transport for the Reolink Baichuan API"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Callable
from time import time as time_now

from ..const import TIMEOUT
from ..exceptions import ReolinkConnectionError, ReolinkError, ReolinkTimeoutError

_LOGGER = logging.getLogger(__name__)

send_response_t = tuple[bytes, int, bytes]


class BaichuanBaseConnection:
    """Reolink Baichuan base connection."""

    def __init__(self, host: str, port: int, push_callback: Callable[[int, bytes, int, bytes], None] | None = None, close_callback: Callable[[], None] | None = None) -> None:
        self._loop = asyncio.get_event_loop()
        self._host = host
        self._port = port
        self._push_callback = push_callback
        self._close_callback = close_callback

        # Connection
        self._mutex = asyncio.Lock()
        self._transport: asyncio.BaseTransport | None = None
        self._protocol: BaichuanBaseClientProtocol | None = None

    async def connect(self):
        """Initialize the protocol and make the connection if needed."""
        if self.connection_open:
            return  # connection is open

        if self._protocol is not None and self._protocol.receive_futures:
            # Ensure all previous receive futures get the change to throw their exceptions
            _LOGGER.debug("Baichuan host %s: waiting for previous receive futures to finish before opening a new connection", self._host)
            try:
                async with asyncio.timeout(TIMEOUT + 5):
                    while self._protocol.receive_futures:
                        await asyncio.sleep(0)
            except asyncio.TimeoutError:
                _LOGGER.warning("Baichuan host %s: Previous receive futures did not finish before opening a new connection", self._host)

        try:
            async with asyncio.timeout(TIMEOUT):
                async with self._mutex:
                    if self.connection_open:
                        return  # connection already opened in the meantime

                    self._transport, self._protocol = await self._create_connection()
        except asyncio.TimeoutError as err:
            raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error") from err
        except (ConnectionResetError, OSError) as err:
            raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error: {str(err)}") from err

    @abstractmethod
    async def _create_connection(self) -> tuple[asyncio.BaseTransport, BaichuanBaseClientProtocol]:
        """create the connection"""

    @abstractmethod
    def _write(self, data: bytes) -> None:
        """Write data over the transport"""

    async def close(self) -> None:
        """close the connection and wait untill close is complete"""
        if self._transport is not None and self._protocol is not None:
            self._transport.close()
            await self._protocol.close_future

    async def send(self, data: bytes, cmd_id: int, full_mess_id: int, channel: int | None = None, log_mess: str = "") -> send_response_t:
        """Send a message and wait for the response"""
        # check for simultaneous cmd_ids with same full_mess_id
        if (receive_future := self.receive_futures.get(cmd_id, {}).get(full_mess_id)) is not None:
            try:
                async with asyncio.timeout(TIMEOUT):
                    try:
                        await receive_future
                    except Exception:
                        pass
                    while self.receive_futures.get(cmd_id, {}).get(full_mess_id) is not None:
                        await asyncio.sleep(0.010)
            except asyncio.TimeoutError as err:
                raise ReolinkError(
                    f"Baichuan host {self._host}: receive future is already set for cmd_id {cmd_id} "
                    "and timeout waiting for it to finish, cannot receive multiple requests simultaneously"
                ) from err

        # set the receive future
        self.receive_futures.setdefault(cmd_id, {})[full_mess_id] = self._loop.create_future()

        if log_mess:
            _LOGGER.debug(log_mess)

        try:
            async with asyncio.timeout(TIMEOUT):
                async with self._mutex:
                    self._write(data)
                response = await self.receive_futures[cmd_id][full_mess_id]
        except asyncio.TimeoutError as err:
            ch_str = f", ch {channel}" if channel is not None else ""
            err_str = f"Baichuan host {self._host}: Timeout error for cmd_id {cmd_id}{ch_str}"
            raise ReolinkTimeoutError(err_str) from err
        except (ConnectionResetError, OSError) as err:
            ch_str = f", ch {channel}" if channel is not None else ""
            err_str = f"Baichuan host {self._host}: Connection error during read/write of cmd_id {cmd_id}{ch_str}: {str(err)}"
            raise ReolinkConnectionError(err_str) from err
        except asyncio.CancelledError:
            _LOGGER.debug("Baichuan host %s: cmd_id %s mess_id %s got cancelled", self._host, cmd_id, full_mess_id)
            raise
        finally:
            if (receive_future := self.receive_futures.get(cmd_id, {}).get(full_mess_id)) is not None:
                if not receive_future.done():
                    receive_future.cancel()
                self.receive_futures[cmd_id].pop(full_mess_id, None)
                if not self.receive_futures[cmd_id]:
                    self.receive_futures.pop(cmd_id, None)

        return response

    @property
    def connection_open(self) -> bool:
        return self._transport is not None and self._protocol is not None and not self._transport.is_closing()

    @property
    def receive_futures(self) -> dict[int, dict[int, asyncio.Future[send_response_t]]]:
        if self._protocol is None:
            return {}
        return self._protocol.receive_futures

    @property
    def time_recv(self) -> float:
        if self._protocol is None:
            return 0
        return self._protocol.time_recv

    @property
    def time_connect(self) -> float:
        if self._protocol is None:
            return time_now()
        return self._protocol.time_connect


class BaichuanBaseClientProtocol(asyncio.BaseProtocol):
    """Reolink Baichuan base protocol."""

    def __init__(self, loop, host: str, push_callback: Callable[[int, bytes, int, bytes], None] | None = None, close_callback: Callable[[], None] | None = None) -> None:
        self._host: str = host
        self._type: str = "Base"
        self._data: bytes = b""
        self._data_chunk: bytes = b""

        self._transport: asyncio.BaseTransport | None = None

        self.receive_futures: dict[int, dict[int, asyncio.Future[send_response_t]]] = {}  # expected_cmd_id: rec_future
        self.close_future: asyncio.Future = loop.create_future()
        self._close_callback = close_callback
        self._push_callback = push_callback
        self.time_recv: float = 0
        self.time_connect: float = 0

        self._log_once: list[str] = []

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Connection callback"""
        self.time_connect = time_now()
        self._transport = transport
        _LOGGER.debug("Baichuan host %s: opened %s connection", self._host, self._type)

    def connection_lost(self, exc: Exception | None) -> None:
        """Connection lost callback"""
        if self.receive_futures:
            if exc is None:
                expected_cmd_ids = ", ".join(map(str, self.receive_futures.keys()))
                exc = ReolinkConnectionError(f"Baichuan host {self._host}: lost {self._type} connection while waiting for cmd_id {expected_cmd_ids}")
            for val in self.receive_futures.values():
                for receive_future in val.values():
                    if receive_future.done():
                        continue
                    receive_future.set_exception(exc)
        _LOGGER.debug("Baichuan host %s: closed %s connection", self._host, self._type)
        if self._close_callback is not None:
            self._close_callback()
        self.close_future.set_result(True)

    def _set_error(self, err_mess: str, exc_class: type[Exception] = ReolinkError, cmd_id: int | None = None, mess_id: int | None = None) -> None:
        """Set a error message to the future or log the error"""
        self._data = b""
        if self.receive_futures and (cmd_id is None or cmd_id in self.receive_futures):
            exc = exc_class(f"Baichuan host {self._host}: received a message {err_mess}")
            if cmd_id is None:
                for val in self.receive_futures.values():
                    for receive_future in val.values():
                        receive_future.set_exception(exc)
            elif mess_id is None or mess_id not in self.receive_futures[cmd_id]:
                for receive_future in self.receive_futures[cmd_id].values():
                    receive_future.set_exception(exc)
            else:
                self.receive_futures[cmd_id][mess_id].set_exception(exc)
        else:
            _LOGGER.debug("Baichuan host %s: received unrequested message %s, dropping", self._host, err_mess)
