""" TCP protocol and transport for the Reolink Baichuan API """

import logging
import asyncio

from time import time as time_now
from collections.abc import Callable

from ..exceptions import (
    ApiError,
    UnexpectedDataError,
    ReolinkConnectionError,
    InvalidContentTypeError,
    ReolinkError,
)
from .util import HEADER_MAGIC

_LOGGER = logging.getLogger(__name__)


class BaichuanTcpClientProtocol(asyncio.Protocol):
    """Reolink Baichuan TCP protocol."""

    def __init__(self, loop, host: str, push_callback: Callable[[int, bytes, int], None] | None = None, close_callback: Callable[[], None] | None = None) -> None:
        self._host: str = host
        self._data: bytes = b""
        self._data_chunk: bytes = b""

        self.receive_futures: dict[int, asyncio.Future] = {}  # expected_cmd_id: rec_future
        self.close_future: asyncio.Future = loop.create_future()
        self._close_callback = close_callback
        self._push_callback = push_callback
        self.time_recv: float = 0

        self._log_once: list[str] = []

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Connection callback"""
        _LOGGER.debug("Baichuan host %s: opened connection", self._host)

    def _set_error(self, err_mess: str, exc_class: type[Exception] = ReolinkError, cmd_id: int | None = None) -> None:
        """Set a error message to the future or log the error"""
        self._data = b""
        if self.receive_futures and (cmd_id is None or cmd_id in self.receive_futures):
            exc = exc_class(f"Baichuan host {self._host}: received a message {err_mess}")
            if cmd_id is None:
                for receive_future in self.receive_futures.values():
                    receive_future.set_exception(exc)
            else:
                self.receive_futures[cmd_id].set_exception(exc)
        else:
            _LOGGER.debug("Baichuan host %s: received unrequested message %s, dropping", self._host, err_mess)

    def data_received(self, data: bytes) -> None:
        """Data received callback"""
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
            else:
                self._set_error(f"with invalid magic header: {data[0:4].hex()}", UnexpectedDataError)
                return

        try:
            self.parse_data()
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

    def parse_data(self) -> None:
        """Parse received data"""
        rec_cmd_id = int.from_bytes(self._data[4:8], byteorder="little")
        rec_len_body = int.from_bytes(self._data[8:12], byteorder="little")
        mess_class = self._data[18:20].hex()

        # check message class
        if mess_class == "1466":  # modern 20 byte header
            len_header = 20
        elif mess_class in ["1464", "0000"]:  # modern 24 byte header
            len_header = 24
            rec_payload_offset = int.from_bytes(self._data[20:24], byteorder="little")
            if rec_payload_offset != 0:
                self._set_error("with a non-zero payload offset, parsing not implemented", InvalidContentTypeError, rec_cmd_id)
                return
        elif mess_class == "1465":  # legacy 20 byte header
            len_header = 20
            self._set_error("with legacy message class, parsing not implemented", InvalidContentTypeError, rec_cmd_id)
            return
        else:
            self._set_error(f"with unknown message class '{mess_class}'", InvalidContentTypeError, rec_cmd_id)
            return

        # check message length
        len_body = len(self._data) - len_header
        if len_body < rec_len_body:
            # do not clear self._data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received %s bytes in the body, while header specified %s bytes, waiting for the rest", self._host, len_body, rec_len_body)
            return

        # extract data chunk
        len_chunk = rec_len_body + len_header
        self._data_chunk = self._data[0:len_chunk]
        if len_body > rec_len_body:
            _LOGGER.debug("Baichuan host %s: received %s bytes while header specified %s bytes, parsing multiple messages", self._host, len_body, rec_len_body)
            self._data = self._data[len_chunk::]
        else:  # len_body == rec_len_body
            self._data = b""

        # check status code
        if len_header == 24:
            rec_status_code = int.from_bytes(self._data_chunk[16:18], byteorder="little")
            if rec_status_code != 200:
                if rec_cmd_id in self.receive_futures:
                    exc = ApiError(f"Baichuan host {self._host}: received status code {rec_status_code}", rspCode=rec_status_code)
                    self.receive_futures[rec_cmd_id].set_exception(exc)
                else:
                    _LOGGER.debug("Baichuan host %s: received unrequested message with cmd_id %s and status code %s", self._host, rec_cmd_id, rec_status_code)
                return

        try:
            if rec_cmd_id not in self.receive_futures:
                if self._push_callback is not None:
                    self._push_callback(rec_cmd_id, self._data_chunk, len_header)
                elif self.receive_futures:
                    expected_cmd_ids = ", ".join(map(str, self.receive_futures.keys()))
                    _LOGGER.debug(
                        "Baichuan host %s: received unrequested message with cmd_id %s, while waiting on cmd_id %s, dropping and waiting for next data",
                        self._host,
                        rec_cmd_id,
                        expected_cmd_ids,
                    )
                else:
                    _LOGGER.debug("Baichuan host %s: received unrequested message with cmd_id %s, dropping", self._host, rec_cmd_id)
                return

            self.receive_futures[rec_cmd_id].set_result((self._data_chunk, len_header))
        finally:
            # if multiple messages received, parse the next also
            if self._data:
                if self._data[0:4].hex() == HEADER_MAGIC:
                    self.parse_data()
                else:
                    _LOGGER.debug("Baichuan host %s: got invalid magic header '%s' during parsing of multiple messages, dropping", self._host, self._data[0:4].hex())
                    self._data = b""

    def connection_lost(self, exc: Exception | None) -> None:
        """Connection lost callback"""
        if self.receive_futures:
            if exc is None:
                expected_cmd_ids = ", ".join(map(str, self.receive_futures.keys()))
                exc = ReolinkConnectionError(f"Baichuan host {self._host}: lost connection while waiting for cmd_id {expected_cmd_ids}")
            for receive_future in self.receive_futures.values():
                receive_future.set_exception(exc)
        _LOGGER.debug("Baichuan host %s: closed connection", self._host)
        if self._close_callback is not None:
            self._close_callback()
        self.close_future.set_result(True)
