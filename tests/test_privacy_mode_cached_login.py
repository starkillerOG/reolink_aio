"""Login of a host restored from cached host data while it may be in privacy mode."""

from __future__ import annotations

import asyncio
import time
import unittest

from reolink_aio.api import Host
from reolink_aio.exceptions import (
    LoginPrivacyModeError,
    ReolinkConnectionError,
    ReolinkTimeoutError,
)


class _Reached(Exception):
    """Raised by a stub to show how far a code path got."""


class TestPrivacyModeCachedLogin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Port and HTTPS are known, as when restored from a stored configuration
        self.host = Host("192.0.2.10", "admin", "password", port=443, use_https=True)
        self.calls = {"privacy": 0, "http": 0}
        self.privacy = True
        self.privacy_error: Exception | None = None

        async def get_privacy_mode(channel: int = 0) -> None:
            self.calls["privacy"] += 1
            if self.privacy_error is not None:
                raise self.privacy_error
            self.host.baichuan._privacy_mode[channel] = self.privacy  # noqa: SLF001
            self.host.baichuan.last_privacy_check = time.time()

        async def send(*_args, **_kwargs):
            # The HTTPS API of a device in privacy mode does not answer
            self.calls["http"] += 1
            raise ReolinkTimeoutError("Host 192.0.2.10:443: Timeout error: ")

        self.host.baichuan.get_privacy_mode = get_privacy_mode  # type: ignore[method-assign]
        self.host.send = send  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        await self.host._aiohttp_session.close()  # noqa: SLF001

    async def test_cached_host_in_privacy_mode_raises_privacy_error(self) -> None:
        self.host.set_raw_host_data('{"host": []}')
        with self.assertRaises(LoginPrivacyModeError):
            await asyncio.wait_for(self.host.login(), 5)
        self.assertEqual(self.calls, {"privacy": 1, "http": 0})

    async def test_get_host_data_falls_back_to_cache(self) -> None:
        self.host.set_raw_host_data('{"host": []}')

        async def send(body, *_args, **_kwargs):
            if body[0]["cmd"] != "Login":
                await self.host.login()  # the real send() logs in first
            self.calls["http"] += 1
            raise ReolinkTimeoutError("Host 192.0.2.10:443: Timeout error: ")

        async def baichuan_get_host_data() -> None:
            raise _Reached

        self.host.send = send  # type: ignore[method-assign]
        self.host.baichuan.get_host_data = baichuan_get_host_data  # type: ignore[method-assign]
        with self.assertRaises(_Reached):
            await asyncio.wait_for(self.host.get_host_data(), 5)
        self.assertEqual(self.calls, {"privacy": 1, "http": 0})

    async def test_cached_host_privacy_off_logs_in_over_http(self) -> None:
        self.privacy = False
        self.host.set_raw_host_data('{"host": []}')
        with self.assertRaises(ReolinkTimeoutError):
            await asyncio.wait_for(self.host.login(), 5)
        self.assertEqual(self.calls, {"privacy": 1, "http": 1})

    async def test_no_cached_host_data_does_not_query_privacy(self) -> None:
        with self.assertRaises(ReolinkTimeoutError):
            await asyncio.wait_for(self.host.login(), 5)
        self.assertEqual(self.calls, {"privacy": 0, "http": 1})

    async def test_privacy_query_error_falls_back_to_http_login(self) -> None:
        self.privacy_error = ReolinkConnectionError("Baichuan unreachable")
        self.host.set_raw_host_data('{"host": []}')
        with self.assertRaises(ReolinkTimeoutError):
            await asyncio.wait_for(self.host.login(), 5)
        self.assertEqual(self.calls, {"privacy": 1, "http": 1})

    async def test_privacy_queried_only_once(self) -> None:
        self.privacy_error = ReolinkConnectionError("Baichuan unreachable")
        self.host.set_raw_host_data('{"host": []}')
        for _ in range(2):
            with self.assertRaises(ReolinkTimeoutError):
                await asyncio.wait_for(self.host.login(), 5)
        self.assertEqual(self.calls["privacy"], 1)


if __name__ == "__main__":
    unittest.main()
