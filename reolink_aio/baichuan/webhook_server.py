"""HTTP(s) webhook server for push callbacks from the Baichuan protocol"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from aiohttp import web
from orjson import JSONDecodeError  # pylint: disable=no-name-in-module
from orjson import loads as json_loads  # pylint: disable=no-name-in-module

_LOGGER = logging.getLogger(__name__)


class WebhookServer:
    """Reolink webhook server to receive Baichuan push callbacks."""

    def __init__(self, host: str, port: int = 0, push_callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.port: int = port
        self.adress: str = ""
        self.local_ip: str = "127.0.0.1"
        self._host = host
        self._random_port: bool = port == 0
        self._push_callback = push_callback
        self._runner: web.AppRunner | None = None
        self._loop = asyncio.get_event_loop()

    async def handle_webhook(self, request: web.Request) -> web.Response:
        """Handle a incomming message on the webhook."""
        text = ""
        try:
            text = await request.text()
            data = json_loads(text)
        except JSONDecodeError as err:
            _LOGGER.debug("Baichuan server %s: error during decoding json data:\n%s\n%s", self.port, text, err)
            return web.Response(text="JSONDecodeError", status=400)
        except Exception as err:
            _LOGGER.debug("Baichuan server %s: error during receiving data:\n%s\n%s", self.port, text, err)
            return web.Response(text="Error", status=400)

        if self._push_callback is not None:
            try:
                self._push_callback(data)
            except Exception as err:
                _LOGGER.debug("Baichuan server %s: Error during push callback with data:\n%s\n%s", self.port, data, err)
        else:
            _LOGGER.debug("Baichuan server %s: Received:\n%s", self.port, data)

        return web.Response(text="OK", status=200)

    async def start(self) -> None:
        """Start the server and assign the port."""
        if self._runner is not None:
            _LOGGER.debug("Baichuan server %s: already running", self.port)
            return

        self.local_ip = await self._get_local_ip()

        app = web.Application()
        app.add_routes([web.route("*", "/webhook", self.handle_webhook)])
        self._runner = web.AppRunner(app)
        await self._runner.setup()

        if self._random_port:
            self.port = 0

        site = web.TCPSite(self._runner, "0.0.0.0", port=self.port)
        await site.start()

        server = site._server
        assert isinstance(server, asyncio.Server)
        for socket in server.sockets:
            _, self.port = socket.getsockname()

        self.adress = f"http://{self.local_ip}:{self.port}/webhook"
        _LOGGER.debug("Baichuan webhook server started on %s", self.adress)

    async def stop(self) -> None:
        """Stop the server and cleanup."""
        if self._runner is not None:
            await self._runner.cleanup()
            _LOGGER.debug("Baichuan webhook server stopped on port %s", self.port)
        self._runner = None

    async def _get_local_ip(self) -> str:
        """Retrieve the local ip adress of the server."""
        try:
            # create_datagram_endpoint which lets the OS populate a route to the 'remote_addr'
            # Since UDP datagram is connectionless, 'making the connection' does not generate any network traffic
            _, protocol = await self._loop.create_datagram_endpoint(lambda: LocalIPProtocol(self._loop), remote_addr=(self._host, 1))

            # Wait untill the connection_made is called and the transport is closed
            ip = await protocol.local_ip
        except Exception as err:
            _LOGGER.warning("Baichuan webhook server failed to retrieve local IP: %s", err)
            ip = "127.0.0.1"
        return ip


class LocalIPProtocol(asyncio.DatagramProtocol):
    """DatagramProtocol to retrieve the local IP, no actual network trafic will be generated."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.local_ip = loop.create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Connection callback"""
        try:
            local_ip, _ = transport.get_extra_info("sockname")
        except Exception as exc:
            self.local_ip.set_exception(exc)
        else:
            self.local_ip.set_result(local_ip)
        finally:
            transport.close()
