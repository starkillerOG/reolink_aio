"""Tests for retries after a timeout and the login protection."""

import unittest
from time import time as time_now
from types import SimpleNamespace
from unittest.mock import AsyncMock

from reolink_aio.baichuan import baichuan as bc_module
from reolink_aio.baichuan.baichuan import Baichuan
from reolink_aio.exceptions import ReolinkTimeoutError, ReolinkTimeoutSendACK


class _TimeoutOnceConnection:
    """Connection whose first send times out, optionally dropping the connection like UDP does."""

    def __init__(self, bc: Baichuan, drop: bool) -> None:
        self.bc = bc
        self.drop = drop
        self.sent: list[int] = []
        self.receive_futures: dict = {}

    async def send(self, data: bytes, cmd_id: int, full_mess_id: int, channel=None, log_mess: str = ""):
        self.sent.append(cmd_id)
        if len(self.sent) == 1:
            if self.drop:
                self.bc._close_callback()  # drop_connection
                raise ReolinkTimeoutSendACK("Timeout waiting on send ACK")
            raise ReolinkTimeoutError("Timeout")
        return data[:24], 24, b""


class TestRetryAfterTimeout(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bc = Baichuan("1.2.3.4", "admin", "password", http_api=SimpleNamespace(nvr_name="test", is_battery=True))  # type: ignore[arg-type]
        self.bc._aes_key = b"0123456789ABCDEF"
        self.bc._logged_in = True
        self.bc._connect_if_needed = AsyncMock()  # type: ignore[method-assign]

    def use_connection(self, drop: bool) -> _TimeoutOnceConnection:
        connection = _TimeoutOnceConnection(self.bc, drop)
        self.bc._connection = connection  # type: ignore[assignment]
        return connection

    async def test_timeout_raised_when_login_not_allowed_yet(self) -> None:
        """A dropped connection shortly after the last login raises the timeout, not a LoginError from the retry."""
        connection = self.use_connection(drop=True)
        self.bc._last_login = time_now() - 5
        with self.assertRaises(ReolinkTimeoutSendACK):
            await self.bc.send(cmd_id=76)
        self.assertEqual(connection.sent, [76])

    async def test_retry_with_new_login_when_allowed(self) -> None:
        """Longer after the last login, the retry logs in again and the command succeeds."""
        connection = self.use_connection(drop=True)
        self.bc._last_login = time_now() - bc_module.MIN_LOGIN_INTERVAL - 1

        async def login() -> None:
            self.bc._logged_in = True

        login_mock = AsyncMock(side_effect=login)
        self.bc.login = login_mock  # type: ignore[method-assign]
        await self.bc.send(cmd_id=76)
        self.assertEqual(connection.sent, [76, 76])
        login_mock.assert_awaited_once()

    async def test_retry_unchanged_without_dropped_connection(self) -> None:
        """A timeout that keeps the connection (TCP) is retried as before, regardless of the last login."""
        connection = self.use_connection(drop=False)
        self.bc._last_login = time_now() - 5
        await self.bc.send(cmd_id=76)
        self.assertEqual(connection.sent, [76, 76])


if __name__ == "__main__":
    unittest.main()
