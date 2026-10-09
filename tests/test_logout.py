"""Tests for the Baichuan logout."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from Cryptodome.Cipher import AES

from reolink_aio.baichuan.baichuan import Baichuan
from reolink_aio.baichuan.util import AES_IV
from reolink_aio.exceptions import ApiError

AES_KEY = b"0123456789ABCDEF"


class _FakeConnection:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[int, bytes]] = []
        self.receive_futures: dict = {}
        self.error = error
        self.close = AsyncMock()

    async def connect(self) -> None:
        return

    async def send(self, data: bytes, cmd_id: int, full_mess_id: int, channel=None, log_mess: str = ""):
        self.sent.append((cmd_id, data))
        if self.error is not None:
            raise self.error
        return data[:24], 24, b""


class TestLogout(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bc = Baichuan("1.2.3.4", "admin", "secret-password", http_api=SimpleNamespace(nvr_name="test", is_battery=False))  # type: ignore[arg-type]
        self.bc._aes_key = AES_KEY
        self.bc._logged_in = True
        self.bc._connect_if_needed = AsyncMock()  # type: ignore[method-assign]

    def _body(self, data: bytes) -> str:
        cipher = AES.new(key=AES_KEY, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
        return cipher.decrypt(data[24:]).decode()

    async def test_logout_does_not_send_credentials(self) -> None:
        connection = _FakeConnection()
        self.bc._connection = connection  # type: ignore[assignment]
        await self.bc.logout()

        cmd_id, data = connection.sent[0]
        self.assertEqual(cmd_id, 2)
        body = self._body(data)
        self.assertIn("<LoginUser", body)
        self.assertNotIn("secret-password", body)
        self.assertNotIn("admin", body)
        connection.close.assert_awaited_once()

    async def test_logout_rejected_is_not_retried(self) -> None:
        connection = _FakeConnection(ApiError("rejected", rspCode=400))
        self.bc._connection = connection  # type: ignore[assignment]
        with (
            patch("reolink_aio.baichuan.baichuan.asyncio.sleep", AsyncMock()) as mock_sleep,
            self.assertLogs("reolink_aio.baichuan.baichuan", level="ERROR") as logs,
        ):
            await self.bc.logout()

        self.assertEqual([cmd_id for cmd_id, _ in connection.sent], [2])
        mock_sleep.assert_not_awaited()
        self.assertIn("failed to logout", logs.output[0])
        connection.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
