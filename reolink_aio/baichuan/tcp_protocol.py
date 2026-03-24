"""TCP protocol and transport for the Reolink Baichuan API"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .base_protocol import BaichuanBaseClientProtocol, BaichuanBaseConnection


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
        self.bc_data_received(data)
