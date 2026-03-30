from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from reolink_aio.baichuan.baichuan import Baichuan
from reolink_aio.baichuan import xmls


class _RecordingTransport:
    def __init__(self, protocol) -> None:
        self._protocol = protocol
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        cmd_id = int.from_bytes(data[4:8], byteorder="little")
        mess_id = int.from_bytes(data[12:16], byteorder="little")
        self._protocol.receive_futures[cmd_id][mess_id].set_result((data[:24], 24, b""))

    def is_closing(self) -> bool:
        return False


class TestBaichuanUtf8Length(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.baichuan = Baichuan(
            host="127.0.0.1",
            username="user",
            password="password",
            http_api=SimpleNamespace(nvr_name="test", _updating=False),
        )
        self.baichuan._logged_in = True  # noqa: SLF001
        self.baichuan._protocol = SimpleNamespace(receive_futures={})  # type: ignore[assignment]  # noqa: SLF001
        self.baichuan._transport = _RecordingTransport(self.baichuan._protocol)  # type: ignore[assignment]  # noqa: SLF001
        self.baichuan._connect_if_needed = AsyncMock()  # type: ignore[method-assign]
        self.baichuan._aes_encrypt = Mock(side_effect=lambda body: body.encode("utf-8"))  # type: ignore[method-assign]
        self.baichuan._decrypt = Mock(return_value="<ok/>")  # type: ignore[method-assign]

    async def test_send_uses_utf8_byte_lengths_for_header_fields(self) -> None:
        body_xml = "<body><name>未命名</name></body>"
        extension_xml = xmls.CHANNEL_EXTENSION_XML.format(channel=0)

        rec_body = await self.baichuan.send(
            cmd_id=53,
            body=body_xml,
            channel=0,
        )

        self.assertEqual(rec_body, "<ok/>")
        self.assertEqual(len(self.baichuan._transport.writes), 1)  # type: ignore[union-attr]
        written = self.baichuan._transport.writes[0]  # type: ignore[union-attr]
        extension_len = len(extension_xml.encode("utf-8"))
        body_len = len(body_xml.encode("utf-8"))
        self.assertEqual(int.from_bytes(written[8:12], byteorder="little"), extension_len + body_len)
        self.assertEqual(int.from_bytes(written[20:24], byteorder="little"), extension_len)
        self.baichuan._decrypt.assert_called_once()
