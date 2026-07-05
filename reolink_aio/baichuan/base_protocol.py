"""Base TCP/UDP protocol and transport for the Reolink Baichuan API"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Callable
from time import time as time_now

from ..const import TIMEOUT
from ..enums import ConnectionEnum
from ..exceptions import (
    ApiError,
    InvalidContentTypeError,
    ReolinkConnectionError,
    ReolinkError,
    ReolinkTimeoutError,
    UnexpectedDataError,
)
from .util import HEADER_MAGIC

_LOGGER = logging.getLogger(__name__)

send_response_t = tuple[bytes, int, bytes]


class BaichuanBaseConnection:
    """Reolink Baichuan base connection."""

    def __init__(self, host: str, port: int, push_callback: Callable[[int, bytes, int, bytes], None] | None = None, close_callback: Callable[[], None] | None = None) -> None:
        self.con_type = ConnectionEnum.unknown
        self._loop = asyncio.get_event_loop()
        self._host = host
        self._port = port
        self._push_callback = push_callback
        self._close_callback = close_callback

        # Connection
        self._mutex = asyncio.Lock()
        self._transport: asyncio.BaseTransport | None = None
        self._protocol: BaichuanBaseClientProtocol | None = None
        self.time_send: float = 0

    async def connect(self):
        """Initialize the protocol and make the connection if needed."""
        if self.connection_open:
            return  # connection is open

        if self._protocol is not None and self._protocol.receive_futures:
            # Ensure all previous receive futures get the change to throw their exceptions
            _LOGGER.debug("Baichuan host %s: waiting for previous receive futures to finish before opening a new connection", self._host)
            try:
                async with asyncio.timeout(TIMEOUT + 5):
                    while self._protocol is not None and self._protocol.receive_futures:
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
    def _write(self, data: bytes, cmd_id: int | None = None, full_mess_id: int | None = None) -> None:
        """Write data over the transport"""

    async def drop_connection(self) -> None:
        """Drop the connection without sending a close message"""
        await self.close()

    async def close(self) -> None:
        """close the connection and wait untill close is complete"""
        if self._transport is not None and self._protocol is not None:
            self._transport.close()
            try:
                async with asyncio.timeout(5):
                    await self._protocol.close_future
            except asyncio.TimeoutError:
                _LOGGER.warning("Baichuan host %s: Timeout waiting on connection close", self._host)
                self._protocol.connection_lost()
        self._transport = None
        self._protocol = None
        self.time_send = 0

    async def send_without_wait(self, data: bytes, cmd_id: int | None = None, timeout: int | float = TIMEOUT) -> None:
        """Send a message without waiting"""
        try:
            async with asyncio.timeout(timeout):
                async with self._mutex:
                    self._write(data)
        except asyncio.TimeoutError as err:
            cmd_str = f"cmd_id {cmd_id}" if cmd_id is not None else "message"
            err_str = f"Baichuan host {self._host}: Timeout sending {cmd_str} without waiting"
            raise ReolinkTimeoutError(err_str) from err
        except (ConnectionResetError, OSError) as err:
            cmd_str = f"cmd_id {cmd_id}" if cmd_id is not None else "message"
            err_str = f"Baichuan host {self._host}: Connection error during write of {cmd_str}: {str(err)}"
            raise ReolinkConnectionError(err_str) from err
        except asyncio.CancelledError:
            cmd_str = f"cmd_id {cmd_id}" if cmd_id is not None else "message"
            _LOGGER.debug("Baichuan host %s: writing %s without waiting got cancelled", self._host, cmd_str)
            raise

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
                    self._write(data, cmd_id, full_mess_id)
                response = await self.receive_futures[cmd_id][full_mess_id]
        except ReolinkTimeoutError:
            if self.con_type == ConnectionEnum.udp:
                # most likely the camera has gone to sleep and the connection is lost, reastablish a fresh connection.
                await self.drop_connection()
            raise
        except asyncio.TimeoutError as err:
            ch_str = f", ch {channel}" if channel is not None else ""
            err_str = f"Baichuan host {self._host}: Timeout error for cmd_id {cmd_id}{ch_str}"
            if self.con_type == ConnectionEnum.udp:
                # most likely the camera has gone to sleep and the connection is lost, reastablish a fresh connection.
                await self.drop_connection()
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
                    self.time_send = self.time_recv
                    self.receive_futures.pop(cmd_id, None)

        return response

    async def _send_heartbeat_response(self, mess_id: int) -> None:
        """Send a heartbear response header"""
        cmd_id = 234
        cmd_id_bytes = (cmd_id).to_bytes(4, byteorder="little")
        mess_id_bytes = (mess_id).to_bytes(4, byteorder="little")
        message_class = "1464"
        mess_len = "00000000"
        payload_offset = "00000000"
        status_code = "0000"
        header = bytes.fromhex(HEADER_MAGIC) + cmd_id_bytes + bytes.fromhex(mess_len) + mess_id_bytes + bytes.fromhex(status_code + message_class + payload_offset)
        _LOGGER.debug("Baichuan host %s: received UDP heartbeat cmd_id %s, sending keepalive", self._host, cmd_id)
        await self.send_without_wait(header, cmd_id)

    def send_heartbeat_response(self, mess_id: int) -> None:
        """Schedule a heartbear response"""
        self._loop.create_task(self._send_heartbeat_response(mess_id))

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

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        host: str,
        push_callback: Callable[[int, bytes, int, bytes], None] | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
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

    def connection_lost(self, exc: Exception | None = None) -> None:
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

    def bc_data_received(self, data: bytes) -> None:
        """New baichuan data received"""
        # parse received header
        if data[0:4].hex() == HEADER_MAGIC:
            if self._data:
                _LOGGER.debug("Baichuan host %s: received magic header while there is still data in the buffer, clearing old data", self._host)
            self._data = data
            self.time_recv = time_now()
        else:
            if self._data:
                # was waiting on more data so append
                self._data = self._data + data
            elif len(data) < 4 and bytes.fromhex(HEADER_MAGIC).startswith(data):
                self._data = data
                _LOGGER.debug("Baichuan host %s: received start of magic header but less then 4 bytes, waiting for the rest", self._host)
                return
            else:
                self._set_error(f"with invalid magic header: {data[0:4].hex()}", UnexpectedDataError)
                return

        try:
            self.parse_bc_data()
        except Exception as exc:
            try:
                if self._data_chunk:
                    cmd_id = int.from_bytes(self._data_chunk[4:8], byteorder="little")
                    header = self._data_chunk[0:24].hex()
                else:
                    cmd_id = int.from_bytes(self._data[4:8], byteorder="little")
                    header = self._data[0:24].hex()
            except Exception:
                cmd_id = 0
                header = "<24"
            if f"parse_data_cmd_id_{cmd_id}" not in self._log_once:
                self._log_once.append(f"parse_data_cmd_id_{cmd_id}")
                _LOGGER.exception("Baichuan host %s: error during parsing of received data, cmd_id %s, header %s: %s", self._host, cmd_id, header, str(exc))
            self._data = b""

    def parse_bc_data(self) -> None:
        """Parse received data"""
        data_len = len(self._data)
        if data_len < 20:
            # do not clear self._data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received start of header but less then 20 bytes, waiting for the rest", self._host)
            return

        rec_cmd_id = int.from_bytes(self._data[4:8], byteorder="little")
        rec_len_body = int.from_bytes(self._data[8:12], byteorder="little")
        rec_payload_offset = 0
        rec_mess_id = int.from_bytes(self._data[12:16], byteorder="little")  # ch_id: 0/251 = push, 250 = host, 1-100 = channel

        mess_class = self._data[18:20].hex()

        # check message class
        if mess_class == "1466":  # modern 20 byte header
            len_header = 20
        elif mess_class in ["1464", "0000"]:  # modern 24 byte header
            len_header = 24
            if data_len < 24:
                # do not clear self._data, wait for the rest of the data
                _LOGGER.debug("Baichuan host %s: received start of modern header with message class %s but less then 24 bytes, waiting for the rest", self._host, mess_class)
                return
            rec_payload_offset = int.from_bytes(self._data[20:24], byteorder="little")
        elif mess_class == "1465":  # legacy 20 byte header
            len_header = 20
            self._set_error("with legacy message class, parsing not implemented", InvalidContentTypeError, rec_cmd_id, rec_mess_id)
            return
        else:
            self._set_error(f"with unknown message class '{mess_class}'", InvalidContentTypeError, rec_cmd_id, rec_mess_id)
            return

        # check message length
        len_body = data_len - len_header
        if len_body < rec_len_body:
            # do not clear self._data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received %s bytes in the body, while header specified %s bytes, waiting for the rest", self._host, len_body, rec_len_body)
            return

        # correct rec_payload_offset
        if rec_payload_offset == 0:
            rec_payload_offset = rec_len_body

        # extract data chunk
        len_chunk = rec_len_body + len_header
        len_message = rec_payload_offset + len_header
        self._data_chunk = self._data[0:len_message]
        payload = self._data[len_message:len_chunk]
        if len_body > rec_len_body:
            _LOGGER.debug("Baichuan host %s: received %s bytes while header specified %s bytes, parsing multiple messages", self._host, len_body, rec_len_body)
            self._data = self._data[len_chunk::]
        else:  # len_body == rec_len_body
            self._data = b""

        # extract receive future
        receive_future = self.receive_futures.get(rec_cmd_id, {}).get(rec_mess_id)

        try:
            # check status code
            if len_header == 24:
                rec_status_code = int.from_bytes(self._data_chunk[16:18], byteorder="little")
                if rec_status_code not in [200, 201, 300]:
                    if receive_future is not None and not receive_future.done():
                        if rec_status_code == 401:
                            exc = ApiError(f"Baichuan host {self._host}: received 401 unauthorized login from cmd_id {rec_cmd_id}", rspCode=rec_status_code)
                        else:
                            exc = ApiError(f"Baichuan host {self._host}: received status code {rec_status_code} from cmd_id {rec_cmd_id}", rspCode=rec_status_code)
                        receive_future.set_exception(exc)
                    else:
                        _LOGGER.debug("Baichuan host %s: received unrequested message with cmd_id %s and status code %s", self._host, rec_cmd_id, rec_status_code)
                    return

            if receive_future is None or receive_future.done():
                if self._push_callback is not None:
                    self._push_callback(rec_cmd_id, self._data_chunk, len_header, payload)
                elif self.receive_futures:
                    expected_cmd_ids = ", ".join(map(str, self.receive_futures.keys()))
                    ch_id = int.from_bytes(self._data_chunk[12:13], byteorder="little")
                    _LOGGER.debug(
                        "Baichuan host %s: received unrequested message with cmd_id %s ch_id %s, mess_id %s, while waiting on cmd_id %s, dropping and waiting for next data",
                        self._host,
                        rec_cmd_id,
                        ch_id,
                        rec_mess_id,
                        expected_cmd_ids,
                    )
                else:
                    _LOGGER.debug("Baichuan host %s: received unrequested message with cmd_id %s, dropping", self._host, rec_cmd_id)
                return

            receive_future.set_result((self._data_chunk, len_header, payload))
        finally:
            # if multiple messages received, parse the next also
            if self._data:
                if self._data[0:4].hex() == HEADER_MAGIC:
                    self.parse_bc_data()
                elif len(self._data) < 4 and bytes.fromhex(HEADER_MAGIC).startswith(self._data):
                    # do not clear self._data, wait for the rest of the data
                    _LOGGER.debug(
                        "Baichuan host %s: received start of magic header but less then 4 bytes, during parsing of multiple messages, waiting for the rest", self._host
                    )
                else:
                    _LOGGER.debug("Baichuan host %s: got invalid magic header '%s' during parsing of multiple messages, dropping", self._host, self._data[0:4].hex())
                    self._data = b""
