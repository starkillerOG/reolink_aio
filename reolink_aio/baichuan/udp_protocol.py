"""UDP protocol and transport for the Reolink Baichuan API"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from inspect import CORO_CREATED, getcoroutinestate
from math import ceil
from random import randint
from socket import AF_INET, IPPROTO_UDP
from time import time as time_now
from typing import TYPE_CHECKING, Coroutine
from xml.etree import ElementTree as XML

from ..const import RETRY_ATTEMPTS, TIMEOUT, UNKNOWN
from ..enums import ConnectionEnum
from ..exceptions import (
    ReolinkConnectionError,
    ReolinkError,
    ReolinkTimeoutError,
    UnexpectedDataError,
)
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
MAGICS_UDP = [MAGIC_UDP_BC, MAGIC_UDP_ACK, MAGIC_UDP_CON]
UDP_CONNECT_PORT = 2015
MTU = 1350  # The maximum transmission unit of the connection. Which is the largest packet size in bytes
UDP_SEND_ACK_TIMEOUT = 2

_LOGGER = logging.getLogger(__name__)


class BaichuanUdpConnection(BaichuanBaseConnection):
    """Reolink Baichuan UDP connection."""

    _transport: asyncio.DatagramTransport
    _protocol: BaichuanUdpClientProtocol

    def __init__(
        self,
        host: str,
        port: int = 0,
        push_callback: Callable[[int, bytes, int, bytes], None] | None = None,
        close_callback: Callable[[], None] | None = None,
        uid: str = UNKNOWN,
    ) -> None:
        super().__init__(host, UDP_CONNECT_PORT, push_callback, close_callback)
        self.con_type = ConnectionEnum.udp
        self._local_port: int = port
        self._random_local_port: bool = port == 0
        self.uid: str = uid
        self._udp_mess_id: int = 0
        self._connect_mutex = asyncio.Lock()
        self._send_ack_timeout: dict[int, asyncio.TimerHandle] = {}

    async def _create_connection(self) -> tuple[asyncio.DatagramTransport, BaichuanUdpClientProtocol]:
        if self._random_local_port:
            self._local_port = 0

        transport, protocol = await self._loop.create_datagram_endpoint(
            lambda: BaichuanUdpClientProtocol(self._loop, self._host, self.drop_connection(), self.cancel_ack_timeout, self._push_callback, self._close_callback),
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
            self._protocol.client_id = randint(10000, 99999)

            try:
                # Get the UID of the camera
                if self.uid == UNKNOWN:
                    body = xmls.UDP_GET_UID_XML.format(port=self._local_port)
                    mess = await self.send_udp(body)
                    self.uid = get_value_from_xml(mess, "uid")

                # Make the UDP connection
                body = xmls.UDP_CONNECT_XML.format(uid=self.uid, port=self._local_port, client_id=self._protocol.client_id, mtu=MTU)
                mess = await self.send_udp(body)
            except ReolinkError as err:
                raise ReolinkConnectionError(f"{err} during UDP connection") from err

            recv_client_id = get_value_from_xml(mess, "cid", int)
            if self._protocol.client_id != recv_client_id:
                raise ReolinkConnectionError(f"Baichuan host {self._host}: received client_id {recv_client_id} did not match send client_id {self._protocol.client_id}")
            self._protocol.host_id = get_value_from_xml(mess, "did", int)
            self._port = self._protocol.remote_port
            _LOGGER.debug("Baichuan host %s: using remote UDP port %s", self._host, self._port)

    async def drop_connection(self) -> None:
        """Drop the connection without sending a close message"""
        if self._protocol is not None:
            self._protocol.host_id = None  # Prevent sending another close message which will block
        await self.close()

    def cancel_ack_timeout(self, recv_seq_id: int) -> None:
        """Called when the send ACK has passed to prevent a timeout"""
        if not self._send_ack_timeout:
            return

        for seq_id in range(min(self._send_ack_timeout), recv_seq_id + 1):
            handler = self._send_ack_timeout.pop(seq_id, None)
            if handler is not None:
                handler.cancel()

    def _timeout_send_ack(self, seq_id: int, cmd_id: int | None, full_mess_id: int | None) -> None:
        """Called when the timeout for receiving a send ACK has passed to set a exception on the waiting future"""
        self._send_ack_timeout.pop(seq_id, None)
        if cmd_id is None or full_mess_id is None:
            return
        if (receive_future := self.receive_futures.get(cmd_id, {}).get(full_mess_id)) is not None:
            receive_future.set_exception(ReolinkTimeoutError(f"Baichuan host {self._host}: Timeout waiting on send ACK of cmd_id {cmd_id} seq_id {seq_id}"))

    async def close(self) -> None:
        """close the connection and wait untill close is complete"""
        # send close message
        if self._protocol is not None and self._protocol.client_id is not None and self._protocol.host_id is not None:
            body = xmls.UDP_DISCONNECT_XML.format(client_id=self._protocol.client_id, host_id=self._protocol.host_id)
            mess, _ = self._construct_udp_mess(body)
            _LOGGER.debug("Baichuan host %s:%s>%s: send UDP disconnect message: %s", self._host, self._local_port, self._port, body)
            await super().send_without_wait(mess, timeout=5)

        if self._protocol is not None and self._protocol.drop_coroutine is not None and getcoroutinestate(self._protocol.drop_coroutine) == CORO_CREATED:
            # cleanup the coroutine in case it was not awaited
            self._protocol.drop_coroutine.close()

        for send_seq_id, handler in self._send_ack_timeout.items():
            handler.cancel()
            _LOGGER.debug("Baichuan host %s: canceled send ack timeout for seq_id %s", self._host, send_seq_id)
        self._send_ack_timeout.clear()

        await super().close()
        self._port = UDP_CONNECT_PORT

    def _write(self, data: bytes, cmd_id: int | None = None, full_mess_id: int | None = None) -> None:
        """Write data over the transport."""
        BC = data[0:4].hex() == MAGIC_UDP_BC
        if BC:
            seq_id = int.from_bytes(data[12:16], byteorder="little")
            self._send_ack_timeout[seq_id] = self._loop.call_later(UDP_SEND_ACK_TIMEOUT, self._timeout_send_ack, seq_id, cmd_id, full_mess_id)

        if not BC or len(data) <= MTU - 20:
            self._transport.sendto(data, (self._host, self._port))
            return

        # data bigger then MTU, split BC message in several UDP packets
        header_prefix = data[0:12]
        payload = data[20:]
        max_payload_size = max(1, MTU - 20)

        for offset in range(0, len(payload), max_payload_size):
            chunk_end = offset + max_payload_size
            chunk = payload[offset:chunk_end]
            chunk_header = header_prefix + seq_id.to_bytes(4, byteorder="little") + len(chunk).to_bytes(4, byteorder="little")
            self._transport.sendto(chunk_header + chunk, (self._host, self._port))
            seq_id += 1

    async def _construct_udp_header(self, data_len: int) -> bytes:
        """Construct the BC UDP header"""
        if self._protocol is None or self._protocol.host_id is None:
            await self.connect()
            if TYPE_CHECKING:
                assert self._protocol.host_id is not None

        mess_id = self._udp_mess_id
        self._udp_mess_id += max(1, ceil(data_len / (MTU - 20)))  # messages bigger then the MTU will be split up inside the _write function

        host_id_bytes = self._protocol.host_id.to_bytes(4, byteorder="little")
        mess_id_bytes = mess_id.to_bytes(4, byteorder="little")
        mess_len_bytes = data_len.to_bytes(4, byteorder="little")

        return bytes.fromhex(MAGIC_UDP_BC) + host_id_bytes + bytes.fromhex("00000000") + mess_id_bytes + mess_len_bytes

    async def _send(self, data: bytes, cmd_id: int, full_mess_id: int, channel: int | None = None, log_mess: str = "") -> send_response_t:
        """Send a message and wait for the response"""
        return await super().send(data, cmd_id, full_mess_id, channel, log_mess)

    async def send(self, data: bytes, cmd_id: int, full_mess_id: int, channel: int | None = None, log_mess: str = "") -> send_response_t:
        """Wrap the BC message in a UDP header, send the message and wait for the response"""
        udp_header = await self._construct_udp_header(len(data))
        return await self._send(udp_header + data, cmd_id, full_mess_id, channel, log_mess)

    async def send_without_wait(self, data: bytes, cmd_id: int | None = None, timeout: int | float = TIMEOUT) -> None:
        """Wrap the BC message in a UDP header, send a message without waiting"""
        udp_header = await self._construct_udp_header(len(data))
        return await super().send_without_wait(udp_header + data, cmd_id)

    def _construct_udp_mess(self, body: str) -> tuple[bytes, int]:
        trans_id = randint(1000, 1000000)

        payload = encrypt_udp_baichuan(body, trans_id)

        mess_id_bytes = trans_id.to_bytes(4, byteorder="little")
        mess_len_bytes = len(payload).to_bytes(4, byteorder="little")
        checksum = calc_crc(payload)

        header = bytes.fromhex(MAGIC_UDP_CON) + mess_len_bytes + bytes.fromhex("01000000") + mess_id_bytes + checksum
        return header + payload, trans_id

    async def send_udp(self, body: str, retry: int = RETRY_ATTEMPTS) -> str:
        retry = retry - 1
        mess, trans_id = self._construct_udp_mess(body)

        cmd_id = -1
        log_mess = f"Baichuan host {self._host}:{self._local_port}>{self._port}: send UDP message: {body}"

        try:
            recv_payload, _, _ = await self._send(mess, cmd_id, trans_id, log_mess=log_mess)
        except (ReolinkTimeoutError, ReolinkConnectionError) as err:
            if retry <= 0:
                raise
            _LOGGER.debug("%s, trying again", err)
            return await self.send_udp(body, retry)

        recv_mess = recv_payload.decode("utf8")
        _LOGGER.debug("Baichuan host %s:%s<%s: received UDP message:\n%s", self._host, self._local_port, self._port, recv_mess)
        return recv_mess

    @property
    def udp_connected(self) -> bool:
        return self.connection_open and self._port != UDP_CONNECT_PORT and self._protocol.host_id is not None


class BaichuanUdpClientProtocol(BaichuanBaseClientProtocol, asyncio.DatagramProtocol):
    """Reolink Baichuan UDP protocol."""

    _transport: asyncio.DatagramTransport

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        host: str,
        drop_coroutine: Coroutine,
        cancel_ack_timeout: Callable[[int], None],
        push_callback: Callable[[int, bytes, int, bytes], None] | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(loop, host, push_callback, close_callback)
        self.drop_coroutine = drop_coroutine
        self.cancel_ack_timeout = cancel_ack_timeout
        self._loop = loop
        self._type: str = "UDP"
        self._udp_data: bytes = b""
        self.remote_port: int = UDP_CONNECT_PORT
        self.host_id: int | None = None
        self.client_id: int | None = None

        self._seq_data: dict[int, bytes] = {}
        self._recv_seq_id: int = -1
        self._send_seq_id: int = -1
        self._last_ack: float = 0

    def connection_lost(self, exc: Exception | None = None) -> None:
        """Connection lost callback"""
        self._seq_data.clear()
        self._recv_seq_id = -1
        self._send_seq_id = -1
        self._last_ack = 0
        super().connection_lost(exc)
        self.host_id = None
        self.client_id = None
        self.remote_port = UDP_CONNECT_PORT

    def send_ack(self) -> None:
        """Send an acknowledgement that a BC message was received"""
        if self._transport is None or self.host_id is None:
            _LOGGER.warning("Baichuan host %s: can not send acknowledgement without a transport and host_id", self._host)
            return
        host_id_bytes = self.host_id.to_bytes(4, byteorder="little")
        payload = b""
        if self._seq_data:
            for j in range(self._recv_seq_id + 1, max(self._seq_data)):
                if j in self._seq_data:
                    payload += b"\x01"
                else:
                    payload += b"\x00"

        if self._recv_seq_id < 0:
            return
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Baichuan host %s:send UDP ACK, recv_seq_id %s  %s", self._host, self._recv_seq_id, payload.hex())

        seq_id_bytes = self._recv_seq_id.to_bytes(4, byteorder="little")
        payload_len_bytes = len(payload).to_bytes(4, byteorder="little")
        udp_header = bytes.fromhex(MAGIC_UDP_ACK) + host_id_bytes + bytes.fromhex("0000000000000000") + seq_id_bytes + bytes.fromhex("00000000") + payload_len_bytes
        self._transport.sendto(udp_header + payload, (self._host, self.remote_port))

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Data received callback"""
        # parse received header
        if data[0:4].hex() in MAGICS_UDP:
            if self._udp_data:
                _LOGGER.debug("Baichuan host %s: received UDP magic header while there is still data in the buffer, clearing old data", self._host)
            self._udp_data = data
        else:
            self._udp_data = self._udp_data + data

        _, port = addr
        self.parse_udp_data(port)

    def parse_udp_data(self, port: int) -> None:
        """Select parsing logic for the UDP packet"""
        try:
            if self._udp_data[0:4].hex() == MAGIC_UDP_BC:
                self.parse_udp_bc(port)
            elif self._udp_data[0:4].hex() == MAGIC_UDP_ACK:
                self.parse_udp_ack(port)
            elif self._udp_data[0:4].hex() == MAGIC_UDP_CON:
                self.parse_udp_connection(port)
            elif len(self._udp_data) < 4 and any(bytes.fromhex(magic).startswith(self._data) for magic in MAGICS_UDP):
                # do not clear self._data, wait for the rest of the data
                _LOGGER.debug("Baichuan host %s: received start of UDP magic header but less then 4 bytes, waiting for the rest", self._host)
                return
            else:
                self._set_error(f"with invalid magic header: {self._udp_data[0:4].hex()}, dropping data", UnexpectedDataError)
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
        data_len = len(self._udp_data)
        if data_len < 20:
            # do not clear self._udp_data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received start of UDP BC header but less then 20 bytes, waiting for the rest", self._host)
            return

        header = self._udp_data[0:20]
        client_id = int.from_bytes(header[4:8], byteorder="little")
        seq_id = int.from_bytes(header[12:16], byteorder="little")
        payload_size = int.from_bytes(header[16:20], byteorder="little")
        mess_len = 20 + payload_size

        # check message length
        if data_len < mess_len:
            # do not clear self._udp_data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received %s bytes in the body, while UDP BC header specified %s bytes, waiting for the rest", self._host, data_len, mess_len)
            return

        # Extract data chunk from buffer
        data_chunk = self._udp_data[20:mess_len]
        self._udp_data = self._udp_data[mess_len::]

        try:
            # check client_id
            if client_id != self.client_id:
                _LOGGER.warning("Baichuan host %s: received client_id %s in UDP BC header while connection client_id is %s, ignoring", self._host, client_id, self.client_id)
                return

            # check seq_id
            if seq_id != self._recv_seq_id + 1:
                if seq_id <= self._recv_seq_id:
                    # this pakket was already received ignore
                    _LOGGER.debug("Baichuan host %s: received UDP BC header with seq_id %s which was already processed, ignoring", self._host, seq_id)
                    return
                # missing a pakket, store this pakket for later and send ACK to request pakket
                self._seq_data[seq_id] = header + data_chunk
                _LOGGER.debug(
                    "Baichuan host %s: received UDP BC header with seq_id %s while expecting seq_id %s, storing data untill seq_id %s is received",
                    self._host,
                    seq_id,
                    self._recv_seq_id + 1,
                    self._recv_seq_id + 1,
                )
                return
            self._recv_seq_id += 1

            # process the data
            self.bc_data_received(data_chunk)
        finally:
            try:
                # Send acknowledgement
                self.send_ack()
            finally:
                if self._seq_data:
                    if (next_pakket := self._seq_data.pop(self._recv_seq_id + 1, None)) is not None:
                        _LOGGER.debug("Baichuan host %s: next UDP seq_id %s already stored in memory, processing", self._host, self._recv_seq_id + 1)
                        # re-insert data in front and continue processing
                        self._udp_data = next_pakket + self._udp_data
                        self.parse_udp_data(port)
                    for mem_seq_id in self._seq_data:
                        if mem_seq_id <= self._recv_seq_id:
                            _LOGGER.warning("Baichuan host %s: UDP seq_id %s already processed but still in stored memory, clearing", self._host, mem_seq_id)
                            self._seq_data.pop(mem_seq_id)
                if self._udp_data:
                    _LOGGER.debug("Baichuan host %s: received %s bytes while UDP BC header specified %s bytes, parsing multiple messages", self._host, data_len, mess_len)
                    self.parse_udp_data(port)

    def parse_udp_ack(self, port: int) -> None:
        """Parse received UDP acknowledgement"""
        data_len = len(self._udp_data)
        if data_len < 28:
            # do not clear self._udp_data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received start of UDP ACK header but less then 28 bytes, waiting for the rest", self._host)
            return

        client_id = int.from_bytes(self._udp_data[4:8], byteorder="little")
        seq_id = int.from_bytes(self._udp_data[16:20], byteorder="little")
        payload_size = int.from_bytes(self._udp_data[24:28], byteorder="little")
        mess_len = 28 + payload_size

        # check message length
        if data_len < mess_len:
            # do not clear self._udp_data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received %s bytes in the body, while UDP ACK header specified %s bytes, waiting for the rest", self._host, data_len, mess_len)
            return

        # Extract payload from buffer
        payload = self._udp_data[28:mess_len]
        self._udp_data = self._udp_data[mess_len::]

        # check client_id
        if client_id != self.client_id:
            _LOGGER.debug("Baichuan host %s: received client_id %s in UDP ACK header while connection client_id is %s, ignoring", self._host, client_id, self.client_id)
            return

        if seq_id != self._send_seq_id:
            self.cancel_ack_timeout(seq_id)
            _LOGGER.debug("Baichuan host %s:received UDP ACK, send_seq_id %s  %s", self._host, seq_id, payload.hex())
        self._send_seq_id = seq_id

        try:
            now = time_now()
            if now - self._last_ack > 10.0:
                self._last_ack = now
                self.send_ack()
        finally:
            if self._udp_data:
                _LOGGER.debug("Baichuan host %s: received %s bytes while UDP ACK header specified %s bytes, parsing multiple messages", self._host, data_len, mess_len)
                self.parse_udp_data(port)

    def parse_udp_connection(self, port: int) -> None:
        """Parse received UDP connection data"""
        data_len = len(self._udp_data)
        if data_len < 20:
            # do not clear self._udp_data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received start of UDP CON header but less then 20 bytes, waiting for the rest", self._host)
            return

        rec_len_body = int.from_bytes(self._udp_data[4:8], byteorder="little")
        rec_mess_id = int.from_bytes(self._udp_data[12:16], byteorder="little")
        rec_checksum = self._udp_data[16:20]
        mess_len = 20 + rec_len_body

        # check message length
        if data_len < mess_len:
            # do not clear self._udp_data, wait for the rest of the data
            _LOGGER.debug("Baichuan host %s: received %s bytes in the body, while UDP CON header specified %s bytes, waiting for the rest", self._host, data_len, mess_len)
            return

        # extract data chunk
        payload = self._udp_data[20:mess_len]
        self._udp_data = self._udp_data[mess_len::]

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

            decrypted_payload = decrypt_udp_baichuan(payload, rec_mess_id)

            if receive_future is None or receive_future.done():
                mess = decrypted_payload.decode("utf8")
                root = XML.fromstring(mess)
                if root.tag != "P2P":
                    _LOGGER.debug("Baichuan host %s: received unknown UDP connection message with mess_id %s, dropping:\n%s", self._host, rec_mess_id, mess)
                    return
                for child in root:
                    if child.tag == "D2C_C_R":
                        _LOGGER.debug("Baichuan host %s: received UDP connection message with mess_id %s:\n%s", self._host, rec_mess_id, mess)
                    elif child.tag == "D2C_DISC":
                        _LOGGER.debug("Baichuan host %s: received UDP disconnect message with mess_id %s:\n%s", self._host, rec_mess_id, mess)
                        self._loop.create_task(self.drop_coroutine)
                    elif child.tag == "D2C_S_R" and len(self.receive_futures.get(-1, {})) == 1 and receive_future is None:
                        exp_mess_id = next(iter(self.receive_futures[-1].keys()))
                        receive_future = self.receive_futures.get(-1, {}).get(exp_mess_id)
                        _LOGGER.debug(
                            "Baichuan host %s: received UDP D2C_S_R with mess_id %s, while expecting mess_id %s, "
                            "likely the camera is entering sleep mode or there already is a second connection, "
                            "accepting message",
                            self._host,
                            rec_mess_id,
                            exp_mess_id,
                        )
                    else:
                        if mess_ids := list(self.receive_futures.get(-1, {}).keys()):
                            _LOGGER.debug(
                                "Baichuan host %s: received unknown UDP connection message with mess_id %s, while waiting on mess_id %s dropping:\n%s",
                                self._host,
                                rec_mess_id,
                                mess_ids,
                                mess,
                            )
                        else:
                            _LOGGER.debug("Baichuan host %s: received unknown UDP connection message with mess_id %s, dropping:\n%s", self._host, rec_mess_id, mess)
                if receive_future is None or receive_future.done():
                    return

            self.remote_port = port
            receive_future.set_result((decrypted_payload, 0, b""))
        finally:
            if self._udp_data:
                _LOGGER.debug("Baichuan host %s: received %s bytes while UDP CON header specified %s bytes, parsing multiple messages", self._host, data_len, mess_len)
                self.parse_udp_data(port)

    def error_received(self, exc: Exception | None):
        """Error callback, normally packets are silently dropped by UDP"""
        _LOGGER.warning("Baichuan host %s: received UDP error %s", self._host, exc)
