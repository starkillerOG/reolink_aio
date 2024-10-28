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

        self.expected_cmd_id: int | None = None
        self.receive_future: asyncio.Future | None = None
        self.close_future: asyncio.Future = loop.create_future()
        self._close_callback = close_callback
        self._push_callback = push_callback
        self.time_recv: float = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Connection callback"""
        _LOGGER.debug("Baichuan host %s: opened connection", self._host)

    def _set_error(self, err_mess: str, exc_class: type[Exception] = ReolinkError) -> None:
        """Set a error message to the future or log the error"""
        self._data = b""
        if self.receive_future is not None:
            exc = exc_class(f"Baichuan host {self._host}: received a message {err_mess}")
            self.receive_future.set_exception(exc)
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
            _LOGGER.exception("Baichuan host %s: error during parsing of received data: %s", self._host, str(exc))
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
                self._set_error("with a non-zero payload offset, parsing not implemented", InvalidContentTypeError)
                return
        elif mess_class == "1465":  # legacy 20 byte header
            len_header = 20
            self._set_error("with legacy message class, parsing not implemented", InvalidContentTypeError)
            return
        else:
            self._set_error(f"with unknown message class '{mess_class}'", InvalidContentTypeError)
            return

        # check message length
        len_body = len(self._data) - len_header
        if len_body < rec_len_body:
            # do not clear self._data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received %s bytes in the body, while header specified %s bytes, waiting for the rest", self._host, len_body, rec_len_body)
            return

        # extract data chunk
        len_chunk = rec_len_body + len_header
        data_chunk = self._data[0:len_chunk]
        if len_body > rec_len_body:
            _LOGGER.debug("Baichuan host %s: received %s bytes while header specified %s bytes, parsing multiple messages", self._host, len_body, rec_len_body)
            self._data = self._data[len_chunk::]
        else:
            self._data = b""

        # check status code
        if len_header == 24:
            rec_status_code = int.from_bytes(data_chunk[16:18], byteorder="little")
            if rec_status_code != 200:
                if self.receive_future is not None and rec_cmd_id == self.expected_cmd_id:
                    exc = ApiError(f"Baichuan host {self._host}: received status code {rec_status_code}", rspCode=rec_status_code)
                    self.receive_future.set_exception(exc)
                else:
                    _LOGGER.debug("Baichuan host %s: received unrequested message with cmd_id %s and status code %s", self._host, rec_cmd_id, rec_status_code)
                return

        try:
            if self.receive_future is None or self.expected_cmd_id is None or rec_cmd_id != self.expected_cmd_id:
                if self._push_callback is not None:
                    self._push_callback(rec_cmd_id, data_chunk, len_header)
                elif self.expected_cmd_id is not None:
                    _LOGGER.debug(
                        "Baichuan host %s: received unrequested message with cmd_id %s, while waiting on cmd_id %s, dropping and waiting for next data",
                        self._host,
                        rec_cmd_id,
                        self.expected_cmd_id,
                    )
                else:
                    _LOGGER.debug("Baichuan host %s: received unrequested message with cmd_id %s, dropping", self._host, rec_cmd_id)
                return

            self.receive_future.set_result((data_chunk, len_header))
        finally:
            # if multiple messages received, parse the next also
            if self._data:
                self.parse_data()

    def connection_lost(self, exc: Exception | None) -> None:
        """Connection lost callback"""
        if self.receive_future is not None:
            if exc is None:
                exc = ReolinkConnectionError(f"Baichuan host {self._host}: lost connection while waiting for cmd_id {self.expected_cmd_id}")
            self.receive_future.set_exception(exc)
        _LOGGER.debug("Baichuan host %s: closed connection", self._host)
        if self._close_callback is not None:
            self._close_callback()
        self.close_future.set_result(True)
