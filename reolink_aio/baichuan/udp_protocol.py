"""UDP protocol and transport for the Reolink Baichuan API"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from random import randint
from socket import AF_INET, IPPROTO_UDP
from time import time as time_now
from typing import TYPE_CHECKING

from xml.etree import ElementTree as XML
from ..exceptions import ReolinkConnectionError, ReolinkError, UnexpectedDataError
from . import xmls
from .base_protocol import (
    BaichuanBaseClientProtocol,
    BaichuanBaseConnection,
    send_response_t,
)
from .util import (
    calc_crc,
    decrypt_udp_baichuan,
    encrypt_udp_baichuan,
    get_value_from_xml,
)

MAGIC_UDP_CON = "3acf872a"
MAGIC_UDP_BC = "10cf872a"
MAGIC_UDP_ACK = "20cf872a"
MAGICS_RECV = [MAGIC_UDP_BC, MAGIC_UDP_ACK, MAGIC_UDP_CON]
UDP_CONNECT_PORT = 2015
MTU = 1350  # The maximum transmission unit of the connection. Which is the largest packet size in bytes

_LOGGER = logging.getLogger(__name__)


class BaichuanUdpConnection(BaichuanBaseConnection):
    """Reolink Baichuan UDP connection."""

    _transport: asyncio.DatagramTransport

    def __init__(
        self, host: str, port: int = 0, push_callback: Callable[[int, bytes, int, bytes], None] | None = None, close_callback: Callable[[], None] | None = None
    ) -> None:
        super().__init__(host, UDP_CONNECT_PORT, push_callback, close_callback)
        self._local_port: int = port
        self.uid: str | None = None
        self._client_id: int = 0
        self._host_id: int | None = None
        self._udp_mess_id: int = 0
        self._connect_mutex = asyncio.Lock()

    async def _create_connection(self) -> tuple[asyncio.DatagramTransport, BaichuanUdpClientProtocol]:
        transport, protocol = await self._loop.create_datagram_endpoint(
            lambda: BaichuanUdpClientProtocol(self._loop, self._host, self._push_callback, self._close_callback, self.close()),
            local_addr=("0.0.0.0", self._local_port),
            reuse_port=False,
            family=AF_INET,
            proto=IPPROTO_UDP,
        )

        _, self._local_port = transport.get_extra_info("sockname")
        _LOGGER.debug("Baichuan host %s: using local UDP port %s", self._host, self._local_port)
        return transport, protocol

    async def connect(self):
        """Initialize the protocol and make the connection if needed."""
        if self.udp_connected:
            return  # connection is open

        async with self._connect_mutex:
            await super().connect()

            if self.udp_connected:
                return  # connection already opened in the meantime

            self._udp_mess_id = 0
            self._port = UDP_CONNECT_PORT
            self._client_id = randint(10000, 99999)

            try:
                # Get the UID of the camera
                if self.uid is None:
                    body = xmls.UDP_GET_UID_XML.format(port=self._local_port)
                    mess = await self.send_udp(body)
                    self.uid = get_value_from_xml(mess, "uid")

                # Make the UDP connection
                body = xmls.UDP_CONNECT_XML.format(uid=self.uid, port=self._local_port, client_id=self._client_id, mtu=MTU)
                mess = await self.send_udp(body)
            except ReolinkError as err:
                raise ReolinkConnectionError(f"{err} during UDP connection") from err

            recv_client_id = get_value_from_xml(mess, "cid", int)
            if self._client_id != recv_client_id:
                raise ReolinkConnectionError(f"Baichuan host {self._host}: received client_id {recv_client_id} did not match send client_id {self._client_id}")
            self._host_id = get_value_from_xml(mess, "did", int)
            self._port = self._protocol.remote_port
            self._protocol.host_id = self._host_id
            _LOGGER.debug("Baichuan host %s: using remote UDP port %s", self._host, self._port)

    async def close(self) -> None:
        """close the connection and wait untill close is complete"""
        await super().close()
        self._port = UDP_CONNECT_PORT
        self._host_id = None

    def _write(self, data: bytes) -> None:
        """Write data over the transport"""
        self._transport.sendto(data, (self._host, self._port))

    async def _send(self, data: bytes, cmd_id: int, full_mess_id: int, channel: int | None = None, log_mess: str = "") -> send_response_t:
        """Send a message and wait for the response"""
        return await super().send(data, cmd_id, full_mess_id, channel, log_mess)

    async def send(self, data: bytes, cmd_id: int, full_mess_id: int, channel: int | None = None, log_mess: str = "") -> send_response_t:
        """Wrap the BC message in a UDP header, send the message and wait for the response"""
        if self._host_id is None:
            await self.connect()
            if TYPE_CHECKING:
                assert self._host_id is not None

        mess_id = self._udp_mess_id
        self._udp_mess_id += 1
        self._protocol.send_mess_ids.add(mess_id)

        host_id_bytes = self._host_id.to_bytes(4, byteorder="little")
        mess_id_bytes = mess_id.to_bytes(4, byteorder="little")
        mess_len_bytes = len(data).to_bytes(4, byteorder="little")

        udp_header = bytes.fromhex(MAGIC_UDP_BC) + host_id_bytes + bytes.fromhex("00000000") + mess_id_bytes + mess_len_bytes

        return await self._send(udp_header + data, cmd_id, full_mess_id, channel, log_mess)

    async def send_udp(self, body: str) -> str:
        trans_id = randint(1000, 1000000)

        payload = encrypt_udp_baichuan(body, trans_id)

        mess_id_bytes = trans_id.to_bytes(4, byteorder="little")
        mess_len_bytes = len(payload).to_bytes(4, byteorder="little")
        checksum = calc_crc(payload)

        header = bytes.fromhex(MAGIC_UDP_CON) + mess_len_bytes + bytes.fromhex("01000000") + mess_id_bytes + checksum

        cmd_id = -1
        log_mess = f"Baichuan host {self._host}:{self._local_port}>{self._port}: send UDP message: {body}"
        recv_payload, _, _ = await self._send(header + payload, cmd_id, trans_id, log_mess=log_mess)

        recv_mess = decrypt_udp_baichuan(recv_payload, trans_id)
        _LOGGER.debug("Baichuan host %s:%s<%s: received UDP message:\n%s", self._host, self._local_port, self._port, recv_mess)
        return recv_mess

    @property
    def udp_connected(self) -> bool:
        return self.connection_open and self._port != UDP_CONNECT_PORT and self._host_id is not None


class BaichuanUdpClientProtocol(BaichuanBaseClientProtocol, asyncio.DatagramProtocol):
    """Reolink Baichuan UDP protocol."""

    _transport: asyncio.DatagramTransport

    def __init__(self, loop, host: str, push_callback: Callable[[int, bytes, int, bytes], None] | None = None, close_callback: Callable[[], None] | None = None, close_coroutine: asyncio.coroutine | None = None) -> None:
        super().__init__(loop, host, push_callback, close_callback)
        self._close_coroutine = close_coroutine
        self._loop = loop
        self._type: str = "UDP"
        self._udp_data: bytes = b""
        self.remote_port: int = UDP_CONNECT_PORT
        self.host_id: int | None = None

        self.send_mess_ids: set[int] = set()
        self._last_dev_mess_id: int = -1
        self._last_ack: float = 0

    def send_ack(self) -> None:
        """Send an acknowledgement that a BC message was received"""
        if self._transport is None or self.host_id is None:
            _LOGGER.warning("Baichuan host %s: can not send acknowledgement without a transport and host_id", self._host, mess_id)
            return
        host_id_bytes = self.host_id.to_bytes(4, byteorder="little")
        mess_id = self._last_dev_mess_id
        payload = b""
        if self.send_mess_ids:
            mess_id = min(self.send_mess_ids) - 1
            for j in range(mess_id+1, max(self.send_mess_ids)):
                if j in self.send_mess_ids:
                    payload += b'\x00'
                else:
                    payload += b'\x01'

        if mess_id < 0:
            return
        if _LOGGER.isEnabledFor(logging.DEBUG): 
            _LOGGER.debug("Baichuan host %s:send UDP ACK, mess_id %s  %s", self._host, mess_id, payload.hex())

        mess_id_bytes = mess_id.to_bytes(4, byteorder="little")
        payload_len_bytes = len(payload).to_bytes(4, byteorder="little")
        udp_header = bytes.fromhex(MAGIC_UDP_ACK) + host_id_bytes + bytes.fromhex("0000000000000000") + mess_id_bytes + bytes.fromhex("00000000") + payload_len_bytes
        self._transport.sendto(udp_header + payload, (self._host, self.remote_port))

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Data received callback"""
        # parse received header
        if data[0:4].hex() in MAGICS_RECV:
            if self._udp_data:
                _LOGGER.debug("Baichuan host %s: received UDP magic header while there is still data in the buffer, clearing old data", self._host)
            self._udp_data = data
        else:
            if self._udp_data:
                # was waiting on more data so append
                self._udp_data = self._udp_data + data
            elif len(data) < 4 and any(bytes.fromhex(magic).startswith(data) for magic in MAGICS_RECV):
                self._udp_data = data
                _LOGGER.debug("Baichuan host %s: received start of UDP magic header but less then 4 bytes, waiting for the rest", self._host)
                return
            else:
                self._set_error(f"with invalid magic header: {data[0:4].hex()}", UnexpectedDataError)
                return

        _, port = addr
        self.parse_udp_data(port)

    def parse_udp_data(self, port: int) -> None:
        """Select parsing logic for the UDP packet"""
        try:
            if self._udp_data[0:4].hex() == MAGIC_UDP_BC:
                self.time_recv = time_now()
                self.parse_udp_bc(port)
            elif self._udp_data[0:4].hex() == MAGIC_UDP_ACK:
                self.parse_udp_ack(port)
            elif self._udp_data[0:4].hex() == MAGIC_UDP_CON:
                self.parse_udp_connection(port)
            elif len(self._udp_data) < 4 and any(bytes.fromhex(magic).startswith(self._data) for magic in MAGICS_RECV):
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

    def parse_udp_bc(self, port: int) -> None:
        """Parse received UDP BC message"""
        client_id = int.from_bytes(self._udp_data[4:8], byteorder="little")
        mess_id = int.from_bytes(self._udp_data[12:16], byteorder="little")
        payload_size = int.from_bytes(self._udp_data[16:20], byteorder="little")
        mess_len = 20 + payload_size

        print(f"received UDP BC: {client_id}, {mess_id}, {payload_size}")

        # Send acknowledgement
        self.send_mess_ids.discard(mess_id)
        self.send_ack()

        self._udp_data = self._udp_data[mess_len::]
        if self._udp_data:
            self.parse_udp_data(port)

    def parse_udp_ack(self, port: int) -> None:
        """Parse received UDP acknowledgement"""
        client_id = int.from_bytes(self._udp_data[4:8], byteorder="little")
        last_mess_id = int.from_bytes(self._udp_data[16:20], byteorder="little")
        payload_size = int.from_bytes(self._udp_data[24:28], byteorder="little")
        mess_len = 28 + payload_size
        payload = self._udp_data[28:mess_len]
        if(last_mess_id != self._last_dev_mess_id):
            _LOGGER.debug("Baichuan host %s:received UDP ACK, mess_id %s  %s", self._host, last_mess_id, payload.hex())
        self._last_dev_mess_id = last_mess_id

        now = time_now()
        if now - self._last_ack > 10.0:
            self._last_ack = now
            self.send_ack()

        self._udp_data = self._udp_data[mess_len::]
        if self._udp_data:
            self.parse_udp_data(port)

    def parse_udp_connection(self, port: int) -> None:
        """Parse received UDP connection data"""
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
                root = XML.fromstring(mess)
                if root.tag != "P2P":
                    _LOGGER.debug("Baichuan host %s: received unknown UDP connection message with mess_id %s, dropping:\n%s", self._host, rec_mess_id, mess)
                    return
                for child in root:
                    if child.tag == "D2C_C_R":
                        _LOGGER.debug("Baichuan host %s: received UDP connection message with mess_id %s:\n%s", self._host, rec_mess_id, mess)
                        continue
                    elif child.tag == "D2C_DISC":
                        _LOGGER.debug("Baichuan host %s: received UDP disconnect message with mess_id %s:\n%s", self._host, rec_mess_id, mess)
                        if self._close_coroutine is not None:
                            self._loop.create_task(self._close_coroutine)
                    else:
                        _LOGGER.debug("Baichuan host %s: received unknown UDP connection message with mess_id %s, dropping:\n%s", self._host, rec_mess_id, mess)
                return

            self.remote_port = port
            receive_future.set_result((payload, 0, b""))
        finally:
            # if multiple messages received, parse the next also
            if self._udp_data:
                self.parse_udp_data(port)

    def error_received(self, exc: Exception | None):
        """Error callback, normally packets are silently dropped by UDP"""
        _LOGGER.warning("Baichuan host %s: received UDP error %s", self._host, exc)
