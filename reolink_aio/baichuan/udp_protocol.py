"""UDP protocol and transport for the Reolink Baichuan API"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from random import randint
from socket import AF_INET, IPPROTO_UDP
from time import time as time_now

from ..exceptions import UnexpectedDataError
from .base_protocol import BaichuanBaseClientProtocol, BaichuanBaseConnection
from .util import calc_crc, decrypt_udp_baichuan, encrypt_udp_baichuan

HEADER_MAGIC_UDP_CON = "3acf872a"

_LOGGER = logging.getLogger(__name__)


class BaichuanUdpConnection(BaichuanBaseConnection):
    """Reolink Baichuan UDP connection."""

    _transport: asyncio.DatagramTransport

    def __init__(
        self, host: str, port: int = 0, push_callback: Callable[[int, bytes, int, bytes], None] | None = None, close_callback: Callable[[], None] | None = None
    ) -> None:
        remote_port = 2015
        super().__init__(host, remote_port, push_callback, close_callback)
        self._local_port: int = port
        self._udp_mess_id: int = randint(1000, 1000000)

    async def _init_protocol(self) -> tuple[asyncio.DatagramTransport, BaichuanUdpClientProtocol]:
        transport, protocol = await self._loop.create_datagram_endpoint(
            lambda: BaichuanUdpClientProtocol(self._loop, self._host, self._push_callback, self._close_callback),
            local_addr=("0.0.0.0", self._local_port),
            reuse_port=False,
            family=AF_INET,
            proto=IPPROTO_UDP,
        )

        _, self._local_port = transport.get_extra_info("sockname")
        _LOGGER.debug("Baichuan host %s: using local UDP port %s", self._host, self._local_port)
        return transport, protocol

    async def _create_connection(self) -> tuple[asyncio.DatagramTransport, BaichuanUdpClientProtocol]:
        return await self._init_protocol()

    async def send_udp(self, body: str) -> str:
        self._udp_mess_id += 1
        mess_id = self._udp_mess_id

        payload = encrypt_udp_baichuan(body, mess_id)

        mess_id_bytes = mess_id.to_bytes(4, byteorder="little")
        mess_len_bytes = len(payload).to_bytes(4, byteorder="little")
        checksum = calc_crc(payload)

        header = bytes.fromhex(HEADER_MAGIC_UDP_CON) + mess_len_bytes + bytes.fromhex("01000000") + mess_id_bytes + checksum

        self.receive_futures.setdefault(-1, {})[mess_id] = self._loop.create_future()

        _LOGGER.debug("Baichuan host %s:%s>%s: send UDP message: %s", self._host, self._local_port, self._port, body)
        await self.write(header + payload)

        recv_payload = await self.receive_futures[-1][mess_id]
        self.receive_futures.get(-1, {}).pop(mess_id, None)
        if not self.receive_futures.get(-1):
            self.receive_futures.pop(-1, None)
        
        recv_mess = decrypt_udp_baichuan(recv_payload, mess_id)
        _LOGGER.debug("Baichuan host %s:%s<%s: received UDP message:\n%s", self._host, self._local_port, self._port, recv_mess)
        return recv_mess

    def _write(self, data: bytes) -> None:
        """Write data over the transport"""
        self._transport.sendto(data, (self._host, self._port))


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
                _LOGGER.debug("Baichuan host %s: received start of UDP magic header but less then 4 bytes, waiting for the rest", self._host)
                return
            else:
                _LOGGER.debug("Baichuan host %s: received unknown magic header %s, dropping data", self._host, self._udp_data[0:4].hex())
                self._udp_data = b""
                return
        except Exception as exc:
            if "parse_data" not in self._log_once:
                self._log_once.append("parse_data")
                header = self._udp_data[0:20].hex()
                _LOGGER.exception("Baichuan host %s: error during parsing of received data, header %s: %s", self._host, header, str(exc))
            self._udp_data = b""

    def parse_udp_connection(self) -> None:
        """Parse received UDP data"""
        if len(self._udp_data) < 20:
            # do not clear self._udp_data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received start of header but less then 20 bytes, waiting for the rest", self._host)
            return

        rec_len_body = int.from_bytes(self._udp_data[4:8], byteorder="little")
        rec_mess_id = int.from_bytes(self._udp_data[12:16], byteorder="little")
        rec_checksum = self._udp_data[16:20]

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
                    exc = UnexpectedDataError(
                        f"Baichuan host {self._host}: received message with header checksum {rec_checksum.hex()} and calculated checksum {checksum.hex()}"
                    )
                    receive_future.set_exception(exc)
                else:
                    _LOGGER.debug(
                        "Baichuan host %s: received unrequested message with header checksum %s and calculated checksum %s, dropping",
                        self._host,
                        rec_checksum.hex(),
                        checksum.hex(),
                    )
                return

            if receive_future is None or receive_future.done():
                mess = decrypt_udp_baichuan(payload, rec_mess_id)
                if self.receive_futures:
                    expected_cmd_ids = ", ".join(map(str, self.receive_futures.keys()))
                    _LOGGER.debug(
                        "Baichuan host %s: received unrequested UDP message with mess_id %s, while waiting on cmd_id%s, dropping and waiting for next data:\n%s",
                        self._host,
                        rec_mess_id,
                        expected_cmd_ids,
                        mess,
                    )
                else:
                    _LOGGER.debug("Baichuan host %s: received unrequested UDP message with mess_id %s, dropping:\n%s", self._host, rec_mess_id, mess)
                return

            receive_future.set_result(payload)
        finally:
            # if multiple messages received, parse the next also
            if self._udp_data:
                self.parse_udp_data()

    def error_received(self, exc: Exception | None):
        """Error callback, normally packets are silently dropped by UDP"""
        _LOGGER.warning("Baichuan host %s: received UDP error %s", self._host, exc)
