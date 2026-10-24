"""UDP protocol and transport for the Reolink Baichuan API"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from inspect import CORO_CREATED, getcoroutinestate
from math import ceil
from random import randint
from socket import AF_INET, IPPROTO_UDP
from time import time as time_now
from typing import TYPE_CHECKING, Coroutine
from xml.etree import ElementTree as XML

from ..const import TIMEOUT, UNKNOWN
from ..enums import ConnectionEnum
from ..exceptions import (
    ReolinkConnectionError,
    ReolinkError,
    ReolinkTimeoutSendACK,
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
    pretty_xml,
)

MAGIC_UDP_CON = "3acf872a"
MAGIC_UDP_BC = "10cf872a"
MAGIC_UDP_ACK = "20cf872a"
MAGICS_UDP = [MAGIC_UDP_BC, MAGIC_UDP_ACK, MAGIC_UDP_CON]
UDP_CONNECT_PORT = 2015
MTU = 1350  # The maximum transmission unit of the connection. Which is the largest packet size in bytes
UDP_RESEND_TIMEOUT = 1.0  # seconds, resend a packet if no ACK is received within this time
UDP_MAX_RESENDS = 3  # number of resends after a timeout before giving up
UDP_RESEND_MIN_INTERVAL = 0.1  # seconds, minimal time between resends of a packet requested by ACKs
UDP_SEND_WINDOW = 10  # maximum number of send packets waiting on an ACK, the camera drops packets if too many are send at once

_LOGGER = logging.getLogger(__name__)


@dataclass
class UdpSendPacket:
    """A UDP BC packet that was (or will be) send and waits on an ACK"""

    seq_id: int
    data: bytes
    cmd_id: int | None
    full_mess_id: int | None
    resends: int = 0
    send_time: float = 0
    timer: asyncio.TimerHandle | None = None


class BaichuanUdpConnection(BaichuanBaseConnection):
    """Reolink Baichuan UDP connection."""

    _transport: asyncio.DatagramTransport
    _protocol: BaichuanUdpClientProtocol

    def __init__(
        self,
        host: str,
        port: int = 0,
        push_callback: Callable[[int, bytes, int, bytes], None] | None = None,
        close_callback: Callable[[ConnectionEnum], None] | None = None,
        uid: str = UNKNOWN,
    ) -> None:
        super().__init__(host, UDP_CONNECT_PORT, push_callback, close_callback)
        self.con_type = ConnectionEnum.udp
        self._local_port: int = port
        self._random_local_port: bool = port == 0
        self.uid: str = uid
        self._udp_mess_id: int = 0
        self._connect_mutex = asyncio.Lock()
        self._send_packets: dict[int, UdpSendPacket] = {}  # send packets waiting on an ACK
        self._send_queue: deque[UdpSendPacket] = deque()  # packets waiting for room in the send window

    async def _create_connection(self) -> tuple[asyncio.DatagramTransport, BaichuanUdpClientProtocol]:
        if self._random_local_port:
            self._local_port = 0

        transport, protocol = await self._loop.create_datagram_endpoint(
            lambda: BaichuanUdpClientProtocol(self._loop, self._host, self.drop_connection(), self.received_ack, self._push_callback, self._close_callback),
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
            self._clear_send_buffer()
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
            self.nonce = get_value_from_xml(mess, "nc")
            pl = get_value_from_xml(mess, "pl")
            if pl is not None and "X25519" in pl:
                self.account_device = True  # login may be blocked, LoginAccountDeviceError is raised if login fails

    async def drop_connection(self) -> None:
        """Drop the connection without sending a close message"""
        if self._protocol is not None:
            self._protocol.host_id = None  # Prevent sending another close message which will block
        await self.close()

    def received_ack(self, ack_seq_id: int, payload: bytes) -> None:
        """Called when a send ACK is received, the payload is a truth table of the packets after ack_seq_id: 00 = missing, resend"""
        # all packets up to ack_seq_id are received
        for seq_id in [seq_id for seq_id in self._send_packets if seq_id <= ack_seq_id]:  # _send_packets modified in loop, need copy of list
            self._ack_packet(seq_id)

        # check truth table for re-sent
        now = time_now()
        for idx, received in enumerate(payload):
            seq_id = ack_seq_id + 1 + idx
            if (packet := self._send_packets.get(seq_id)) is None:
                continue
            if received:
                self._ack_packet(seq_id)
            elif now - packet.send_time >= UDP_RESEND_MIN_INTERVAL:
                _LOGGER.debug("Baichuan host %s: UDP ACK requested resend of seq_id %s", self._host, seq_id)
                self._send_packet(packet)

        self._send_next()

    def _ack_packet(self, seq_id: int) -> None:
        """A send packet is received by the camera"""
        packet = self._send_packets.pop(seq_id)
        if packet.timer is not None:
            packet.timer.cancel()

    def _send_packet(self, packet: UdpSendPacket) -> None:
        """(Re)send a UDP packet and start the timer to wait on the ACK"""
        if packet.timer is not None:
            packet.timer.cancel()
        if self._transport is None:
            return
        self._transport.sendto(packet.data, (self._host, self._port))
        packet.send_time = time_now()
        packet.timer = self._loop.call_later(UDP_RESEND_TIMEOUT, self._timeout_send_ack, packet.seq_id)

    def _send_next(self) -> None:
        """Send queued packets as long as there is room in the send window"""
        while self._send_queue and len(self._send_packets) < UDP_SEND_WINDOW:
            packet = self._send_queue.popleft()
            self._send_packets[packet.seq_id] = packet
            self._send_packet(packet)

    def _timeout_send_ack(self, seq_id: int) -> None:
        """Called when no ACK was received in time, resend the packet or set a exception on the waiting future"""
        if (packet := self._send_packets.get(seq_id)) is None:
            return
        if packet.resends < UDP_MAX_RESENDS:
            packet.resends += 1
            _LOGGER.debug("Baichuan host %s: no UDP ACK received for seq_id %s, resend %s", self._host, seq_id, packet.resends)
            self._send_packet(packet)
            return

        # the camera will not process later packets without this one, so drop everything that is waiting to be send
        # and let all commands of which packets are dropped fail directly instead of waiting on their response timeout
        dropped_mess = {(pkt.cmd_id, pkt.full_mess_id) for pkt in [*self._send_packets.values(), *self._send_queue]}
        self._clear_send_buffer()
        for cmd_id, full_mess_id in dropped_mess:
            if cmd_id is None or full_mess_id is None:
                continue
            if (receive_future := self.receive_futures.get(cmd_id, {}).get(full_mess_id)) is not None and not receive_future.done():
                receive_future.set_exception(ReolinkTimeoutSendACK(f"Baichuan host {self._host}: Timeout waiting on send ACK of seq_id {seq_id} cmd_id {cmd_id}"))

    def _clear_send_buffer(self) -> None:
        """Cancel all pending resends and drop the packets waiting to be send"""
        for packet in self._send_packets.values():
            if packet.timer is not None:
                packet.timer.cancel()
        self._send_packets.clear()
        self._send_queue.clear()

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

        if self._send_packets or self._send_queue:
            _LOGGER.debug("Baichuan host %s: dropping %s UDP packets waiting on an ACK or to be send", self._host, len(self._send_packets) + len(self._send_queue))
        self._clear_send_buffer()

        await super().close()
        self._port = UDP_CONNECT_PORT

    def _write(self, data: bytes, cmd_id: int | None = None, full_mess_id: int | None = None) -> None:
        """Write data over the transport."""
        if self._transport is None:
            raise ReolinkConnectionError(f"Baichuan host {self._host}: lost UDP connection while waiting to write cmd_id {cmd_id}")
        if data[0:4].hex() != MAGIC_UDP_BC:
            self._transport.sendto(data, (self._host, self._port))
            return

        # split the BC message in UDP packets of maximum the MTU size, which are send using a send window and resend if not acknowledged
        seq_id = int.from_bytes(data[12:16], byteorder="little")
        header_prefix = data[0:12]
        payload = data[20:]
        max_payload_size = MTU - 20

        for offset in range(0, max(1, len(payload)), max_payload_size):
            chunk_end = offset + max_payload_size
            chunk = payload[offset:chunk_end]
            chunk_header = header_prefix + seq_id.to_bytes(4, byteorder="little") + len(chunk).to_bytes(4, byteorder="little")
            self._send_queue.append(UdpSendPacket(seq_id, chunk_header + chunk, cmd_id, full_mess_id))
            seq_id += 1
        self._send_next()

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
        return await super().send_without_wait(udp_header + data, cmd_id, timeout)

    def _construct_udp_mess(self, body: str) -> tuple[bytes, int]:
        trans_id = randint(1000, 1000000)

        payload = encrypt_udp_baichuan(body, trans_id)

        mess_id_bytes = trans_id.to_bytes(4, byteorder="little")
        mess_len_bytes = len(payload).to_bytes(4, byteorder="little")
        checksum = calc_crc(payload)

        header = bytes.fromhex(MAGIC_UDP_CON) + mess_len_bytes + bytes.fromhex("01000000") + mess_id_bytes + checksum
        return header + payload, trans_id

    async def send_udp(self, body: str) -> str:
        mess, trans_id = self._construct_udp_mess(body)

        cmd_id = -1
        log_mess = f"Baichuan host {self._host}:{self._local_port}>{self._port}: send UDP message: {body}"

        recv_payload, _, _ = await self._send(mess, cmd_id, trans_id, log_mess=log_mess)
        recv_mess = recv_payload.decode("utf8")
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Baichuan host %s:%s<%s: received UDP message:\n%s", self._host, self._local_port, self._port, pretty_xml(recv_mess))
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
        received_ack: Callable[[int, bytes], None],
        push_callback: Callable[[int, bytes, int, bytes], None] | None = None,
        close_callback: Callable[[ConnectionEnum], None] | None = None,
    ) -> None:
        super().__init__(loop, host, push_callback, close_callback)
        self.drop_coroutine = drop_coroutine
        self.received_ack = received_ack
        self._loop = loop
        self.con_type = ConnectionEnum.udp
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
            _LOGGER.debug("Baichuan host %s:received UDP ACK, send_seq_id %s  %s", self._host, seq_id, payload.hex())
        self._send_seq_id = seq_id

        try:
            self.received_ack(seq_id, payload)
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
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug("Baichuan host %s: received unknown UDP connection message with mess_id %s, dropping:\n%s", self._host, rec_mess_id, pretty_xml(mess))
                    return
                for child in root:
                    if child.tag == "D2C_C_R":
                        if _LOGGER.isEnabledFor(logging.DEBUG):
                            _LOGGER.debug("Baichuan host %s: received UDP connection message with mess_id %s:\n%s", self._host, rec_mess_id, pretty_xml(mess))
                    elif child.tag == "D2C_DISC":
                        if _LOGGER.isEnabledFor(logging.DEBUG):
                            _LOGGER.debug("Baichuan host %s: received UDP disconnect message with mess_id %s:\n%s", self._host, rec_mess_id, pretty_xml(mess))
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
                            if _LOGGER.isEnabledFor(logging.DEBUG):
                                _LOGGER.debug(
                                    "Baichuan host %s: received unknown UDP connection message with mess_id %s, while waiting on mess_id %s dropping:\n%s",
                                    self._host,
                                    rec_mess_id,
                                    mess_ids,
                                    pretty_xml(mess),
                                )
                        else:
                            if _LOGGER.isEnabledFor(logging.DEBUG):
                                _LOGGER.debug(
                                    "Baichuan host %s: received unknown UDP connection message with mess_id %s, dropping:\n%s", self._host, rec_mess_id, pretty_xml(mess)
                                )
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
