"""Base TCP/UDP protocol and transport for the Reolink Baichuan API"""

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
from .util import HEADER_MAGIC

_LOGGER = logging.getLogger(__name__)


class BaichuanBaseClientProtocol(asyncio.BaseProtocol):
    """Reolink Baichuan base protocol."""

    def __init__(self, loop, host: str, push_callback: Callable[[int, bytes, int, bytes], None] | None = None, close_callback: Callable[[], None] | None = None) -> None:
        self._host: str = host
        self._type: str = "Base"
        self._data: bytes = b""
        self._data_chunk: bytes = b""

        self.receive_futures: dict[int, dict[int, asyncio.Future]] = {}  # expected_cmd_id: rec_future
        self.close_future: asyncio.Future = loop.create_future()
        self._close_callback = close_callback
        self._push_callback = push_callback
        self.time_recv: float = 0
        self.time_connect: float = 0

        self._log_once: list[str] = []

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Connection callback"""
        self.time_connect = time_now()
        _LOGGER.debug("Baichuan host %s: opened %s connection", self._host, self._type)

    def connection_lost(self, exc: Exception | None) -> None:
        """Connection lost callback"""
        if self.receive_futures:
            if exc is None:
                expected_cmd_ids = ", ".join(map(str, self.receive_futures.keys()))
                exc = ReolinkConnectionError(f"Baichuan host {self._host}: lost {self._type} connection while waiting for cmd_id {expected_cmd_ids}")
            for val in self.receive_futures.values():
                for receive_future in val.values():
                    if receive_future.done():
                        continue
                    receive_future.set_exception(exc)
        _LOGGER.debug("Baichuan host %s: closed %s connection", self._host, self._type)
        if self._close_callback is not None:
            self._close_callback()
        self.close_future.set_result(True)

    def _set_error(self, err_mess: str, exc_class: type[Exception] = ReolinkError, cmd_id: int | None = None, mess_id: int | None = None) -> None:
        """Set a error message to the future or log the error"""
        self._data = b""
        if self.receive_futures and (cmd_id is None or cmd_id in self.receive_futures):
            exc = exc_class(f"Baichuan host {self._host}: received a message {err_mess}")
            if cmd_id is None:
                for val in self.receive_futures.values():
                    for receive_future in val.values():
                        receive_future.set_exception(exc)
            elif mess_id is None or mess_id not in self.receive_futures[cmd_id]:
                for receive_future in self.receive_futures[cmd_id].values():
                    receive_future.set_exception(exc)
            else:
                self.receive_futures[cmd_id][mess_id].set_exception(exc)
        else:
            _LOGGER.debug("Baichuan host %s: received unrequested message %s, dropping", self._host, err_mess)
