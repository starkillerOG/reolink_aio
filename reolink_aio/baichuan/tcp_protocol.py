"""TCP protocol and transport for the Reolink Baichuan API"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from time import time as time_now

from ..exceptions import ApiError, InvalidContentTypeError, UnexpectedDataError
from .base_protocol import BaichuanBaseClientProtocol, BaichuanBaseConnection
from .util import HEADER_MAGIC

_LOGGER = logging.getLogger(__name__)


class BaichuanTcpConnection(BaichuanBaseConnection):
    """Reolink Baichuan TCP connection."""

    _transport: asyncio.Transport

    async def _create_connection(self) -> tuple[asyncio.Transport, BaichuanTcpClientProtocol]:
        """create the connection"""
        return await self._loop.create_connection(lambda: BaichuanTcpClientProtocol(self._loop, self._host, self._push_callback, self._close_callback), self._host, self._port)

    def _write(self, data: bytes) -> None:
        """Write data over the transport"""
        self._transport.write(data)


class BaichuanTcpClientProtocol(BaichuanBaseClientProtocol, asyncio.Protocol):
    """Reolink Baichuan TCP protocol."""

    def __init__(self, loop, host: str, push_callback: Callable[[int, bytes, int, bytes], None] | None = None, close_callback: Callable[[], None] | None = None) -> None:
        super().__init__(loop, host, push_callback, close_callback)
        self._type: str = "TCP"

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
            elif len(data) < 4 and bytes.fromhex(HEADER_MAGIC).startswith(data):
                self._data = data
                _LOGGER.debug("Baichuan host %s: received start of magic header but less then 4 bytes, waiting for the rest", self._host)
                return
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
        if len(self._data) < 20:
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
            if len(self._data) < 24:
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
        len_body = len(self._data) - len_header
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
                    self.parse_data()
                elif len(self._data) < 4 and bytes.fromhex(HEADER_MAGIC).startswith(self._data):
                    # do not clear self._data, wait for the rest of the data
                    _LOGGER.debug(
                        "Baichuan host %s: received start of magic header but less then 4 bytes, during parsing of multiple messages, waiting for the rest", self._host
                    )
                else:
                    _LOGGER.debug("Baichuan host %s: got invalid magic header '%s' during parsing of multiple messages, dropping", self._host, self._data[0:4].hex())
                    self._data = b""
