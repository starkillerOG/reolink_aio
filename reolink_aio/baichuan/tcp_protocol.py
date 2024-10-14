""" TCP protocol and transport for the Reolink Baichuan API """

import logging
import asyncio

from ..exceptions import (
    UnexpectedDataError,
    ReolinkConnectionError,
)
from .util import HEADER_MAGIC

_LOGGER = logging.getLogger(__name__)


class BaichuanTcpClientProtocol(asyncio.Protocol):
    """Reolink Baichuan TCP protocol."""

    def __init__(self, loop, host: str):
        self._host: str = host

        self.expected_cmd_id: int | None = None
        self.receive_future: asyncio.Future | None = None
        self.close_future: asyncio.Future = loop.create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Connection callback"""
        _LOGGER.debug("Baichuan host %s: opened connection", self._host)

    def data_received(self, data: bytes) -> None:
        """Data received callback"""
        # parse received header
        if data[0:4].hex() != HEADER_MAGIC:
            if self.receive_future is not None:
                exc = UnexpectedDataError(f"Baichuan host {self._host}: received message with invalid magic header: {data[0:4].hex()}")
                self.receive_future.set_exception(exc)
            else:
                _LOGGER.debug("Baichuan host %s: received unrequested message with invalid magic header: %s, ignoring", self._host, data[0:4].hex())
            return

        rec_cmd_id = int.from_bytes(data[4:8], byteorder="little")
        if self.receive_future is None or self.expected_cmd_id is None:
            _LOGGER.debug("Baichuan host %s: received unrequested message with cmd_id %s, ignoring", self._host, rec_cmd_id)
            return

        if rec_cmd_id != self.expected_cmd_id:
            _LOGGER.debug(
                "Baichuan host %s: received unrequested message with cmd_id %s, while waiting on cmd_id %s, ignoring and waiting for next data",
                self._host,
                rec_cmd_id,
                self.expected_cmd_id,
            )
            return

        self.receive_future.set_result(data)

    def connection_lost(self, exc: Exception | None) -> None:
        """Connection lost callback"""
        if self.receive_future is not None:
            if exc is None:
                exc = ReolinkConnectionError(f"Baichuan host {self._host}: lost connection while waiting for cmd_id {self.expected_cmd_id}")
            self.receive_future.set_exception(exc)
        _LOGGER.debug("Baichuan host %s: closed connection", self._host)
        self.close_future.set_result(True)
