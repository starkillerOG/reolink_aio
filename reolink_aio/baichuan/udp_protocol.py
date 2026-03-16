"""UDP protocol and transport for the Reolink Baichuan API"""

import asyncio
import logging
from collections.abc import Callable
from time import time as time_now

from ..exceptions import (
    ApiError,
    InvalidContentTypeError,
    ReolinkConnectionError,
    ReolinkError,
    UnexpectedDataError,
)

from .base_protocol import BaichuanBaseClientProtocol

HEADER_MAGIC_UDP_CON = "3acf872a"

_LOGGER = logging.getLogger(__name__)


class BaichuanUdpClientProtocol(BaichuanBaseClientProtocol, asyncio.DatagramProtocol):
    """Reolink Baichuan UDP protocol."""

    def __init__(self, loop, host: str, push_callback: Callable[[int, bytes, int, bytes], None] | None = None, close_callback: Callable[[], None] | None = None) -> None:
        super().__init__(loop, host, push_callback, close_callback)
        self._type: str = "UDP"
        self._udp_data: bytes = b""

    def datagram_received(self, data, addr):
        """Data received callback"""
        # parse received header
        if data[0:4].hex() in {HEADER_MAGIC_UDP_CON}:
            if self._udp_data:
                _LOGGER.debug("Baichuan host %s: received UDP magic header while there is still data in the buffer, clearing old data", self._host)
            self._udp_data = data
            self.time_recv = time_now()
        else:
            if self._udp_data:
                # was waiting on more data so append
                self._udp_data = self._udp_data + data
            elif len(data) < 4 and bytes.fromhex(HEADER_MAGIC_UDP_CON).startswith(data):
                self._udp_data = data
                _LOGGER.debug("Baichuan host %s: received start of UDP magic header but less then 4 bytes, waiting for the rest", self._host)
                return
            else:
                self._set_error(f"with invalid magic header: {data[0:4].hex()}", UnexpectedDataError)
                return

        self.parse_udp_data()

    def parse_udp_data(self) -> None:
        """Parse received UDP data"""
        try:
            if self._udp_data[0:4].hex() == HEADER_MAGIC_UDP_CON:
                self.parse_udp_connection()
            elif len(self._udp_data) < 4 and bytes.fromhex(HEADER_MAGIC_UDP_CON).startswith(self._data):
                # do not clear self._data, wait for the rest of the data
                _LOGGER.debug(
                    "Baichuan host %s: received start of UDP magic header but less then 4 bytes, waiting for the rest", self._host
                )
                return
            else:
                LOGGER.debug("Baichuan host %s: received unknown magic header %s, dropping data", self._host, self._udp_data[0:4].hex())
                self._udp_data = b""
                return
        except Exception as exc:
            if f"parse_data" not in self._log_once:
                self._log_once.append(f"parse_data")
                header = self._udp_data[0:20].hex()
                _LOGGER.exception("Baichuan host %s: error during parsing of received data, header %s: %s", self._host, header, str(exc))
            self._udp_data = b""

    def parse_udp_connection(self) -> None:
        """Parse received UDP data"""
        if len(self._udp_data) < 20:
            # do not clear self._udp_data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received start of header but less then 20 bytes, waiting for the rest", self._host)
            return

        rec_len_body = int.from_bytes(self_udp_data[4:8], byteorder="little")
        rec_mess_id = int.from_bytes(self._udp_data[12:16], byteorder="little")
        rec_checksum = int.from_bytes(self._udp_data[16:20], byteorder="little")

        # check message length
        len_body = len(self._udp_data) - 20
        if len_body < rec_len_body:
            # do not clear self._udp_data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received %s bytes in the body, while UDP header specified %s bytes, waiting for the rest", self._host, len_body, rec_len_body)
            return

        # extract data chunk
        len_chunk = rec_len_body + 20
        payload = self._udp_data[20:len_chunk]
        if len_body > rec_len_body:
            _LOGGER.debug("Baichuan host %s: received %s bytes while UDP header specified %s bytes, parsing multiple messages", self._host, len_body, rec_len_body)
            self._udp_data = self._udp_data[len_chunk::]
        else:  # len_body == rec_len_body
            self._udp_data = b""

        # extract receive future
        receive_future = self.receive_futures.get(-1, {}).get(rec_mess_id)

        try:
            # check the checksum
            checksum = calc_crc(payload)
            if checksum != rec_checksum:
                if receive_future is not None and not receive_future.done():
                    exc = UnexpectedDataError(f"Baichuan host {self._host}: received message with header checksum {rec_checksum} and calculated checksum {checksum}")
                    receive_future.set_exception(exc)
                else:
                    _LOGGER.debug("Baichuan host %s: received unrequested message with header checksum %s and calculated checksum %s, dropping", self._host, rec_checksum, checksum)
                return

            if receive_future is None or receive_future.done():
                if self.receive_futures:
                    expected_cmd_ids = ", ".join(map(str, self.receive_futures.keys()))
                    _LOGGER.debug(
                        "Baichuan host %s: received unrequested UDP message with mess_id %s, while waiting on cmd_id %s, dropping and waiting for next data",
                        self._host,
                        rec_mess_id,
                        expected_cmd_ids,
                    )
                else:
                    _LOGGER.debug("Baichuan host %s: received unrequested UDP message, dropping", self._host)
                return

            receive_future.set_result(payloay)
        finally:
            # if multiple messages received, parse the next also
            if self._udp_data:
                parse_udp_data();

    def error_received(self, exc: Exception | None):
        """Error callback, normally packets are silently dropped by UDP"""
        _LOGGER.warning("Baichuan host %s: received UDP error %s", self._host, exc)

