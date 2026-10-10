"""The battery idle close must not close the connection during a live stream."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reolink_aio.baichuan import baichuan as bc_module
from reolink_aio.baichuan.baichuan import Baichuan, _PreviewStream


class TestBatteryCloseWhileStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_connection_kept_open_while_streaming(self) -> None:
        bc = Baichuan("1.2.3.4", "admin", "password", http_api=SimpleNamespace(nvr_name="test", is_battery=True))  # type: ignore[arg-type]
        bc._logged_in = True
        bc._connection = SimpleNamespace(time_send=0, receive_futures={})  # type: ignore[assignment]
        bc._video_streams[1] = _PreviewStream()
        logout = AsyncMock()
        bc.logout = logout  # type: ignore[method-assign]
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def stream_ends_after_first_sleep(delay: float, *_args) -> None:
            sleeps.append(delay)
            bc._video_streams.clear()  # the stream ends, the next round may close the idle connection
            await real_sleep(0)

        with patch("reolink_aio.baichuan.baichuan.asyncio.sleep", stream_ends_after_first_sleep):
            await bc._battery_close_loop()
        self.assertEqual(sleeps, [bc_module.BATTERY_CLOSE_TIME])  # not closed while streaming
        logout.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
