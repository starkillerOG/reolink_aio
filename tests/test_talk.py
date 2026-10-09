"""Tests for two-way audio (talk) over Baichuan."""

from __future__ import annotations

import asyncio
import math
import random
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree as XML

from Cryptodome.Cipher import AES

from reolink_aio.baichuan import baichuan as bc_module
from reolink_aio.baichuan.audio import AdpcmEncoder, bcmedia_adpcm_frame
from reolink_aio.baichuan.baichuan import Baichuan
from reolink_aio.baichuan.util import AES_IV, HEADER_MAGIC
from reolink_aio.exceptions import ApiError, ReolinkError, ReolinkTimeoutError

# TalkAbility (cmd_id 10) as returned by a Reolink Video Doorbell PoE
TALK_ABILITY_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<body>
<TalkAbility version="1.1">
<duplexList><duplex>FDX</duplex></duplexList>
<audioStreamModeList>
<audioStreamMode>followVideoStream</audioStreamMode>
<audioStreamMode>mixAudioStream</audioStreamMode>
</audioStreamModeList>
<audioConfigList><audioConfig>
<priority>0</priority><audioType>adpcm</audioType><sampleRate>16000</sampleRate>
<samplePrecision>16</samplePrecision><lengthPerEncoder>1024</lengthPerEncoder><soundTrack>mono</soundTrack>
</audioConfig></audioConfigList>
</TalkAbility>
</body>
"""

AES_KEY = b"0123456789ABCDEF"

_STEPS = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411,
    1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
]  # fmt: skip
_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]


def decode_block(block: bytes) -> list[int]:
    """Reference IMA ADPCM decoder for one DVI-4 block."""
    predictor, index, _ = struct.unpack("<hBB", block[:4])
    out = [predictor]
    for byte in block[4:]:
        for code in (byte & 0x0F, byte >> 4):
            step = _STEPS[index]
            diff = step >> 3
            if code & 4:
                diff += step
            if code & 2:
                diff += step >> 1
            if code & 1:
                diff += step >> 2
            predictor = max(-32768, min(32767, predictor - diff if code & 8 else predictor + diff))
            index = max(0, min(88, index + _INDEX[code & 7]))
            out.append(predictor)
    return out


def sweep(n_samples: int, rate: int = 16000) -> bytes:
    """Speech-like test signal: a 200-3000 Hz sweep with a slow amplitude envelope."""
    samples = []
    phase = 0.0
    for i in range(n_samples):
        t = i / rate
        phase += 2 * math.pi * (200 + 2800 * (i / n_samples)) / rate
        samples.append(int(12000 * (0.6 + 0.4 * math.sin(2 * math.pi * 3 * t)) * math.sin(phase)))
    return struct.pack(f"<{n_samples}h", *samples)


class TestAdpcmEncoder(unittest.TestCase):
    def test_block_size(self) -> None:
        encoder = AdpcmEncoder(1024)
        self.assertEqual(encoder.samples_per_block, 1025)
        blocks = encoder.encode(b"\x00\x00" * 1025 * 3)
        self.assertEqual([len(block) for block in blocks], [516, 516, 516])

    def test_round_trip_quality(self) -> None:
        pcm = sweep(1025 * 16)
        blocks = AdpcmEncoder(1024).encode(pcm)
        decoded = [sample for block in blocks for sample in decode_block(block)]
        original = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        signal = sum(x * x for x in original)
        noise = sum((x - y) ** 2 for x, y in zip(original, decoded))
        self.assertGreater(10 * math.log10(signal / noise), 18)

    def test_streaming_matches_one_shot(self) -> None:
        pcm = sweep(1025 * 10 + 300)
        expected = AdpcmEncoder(1024).encode(pcm)
        encoder = AdpcmEncoder(1024)
        blocks: list[bytes] = []
        rnd = random.Random(1)
        pos = 0
        while pos < len(pcm):
            size = rnd.randint(1, 3000)  # includes odd sizes that split a sample
            blocks += encoder.encode(pcm[pos : pos + size])
            pos += size
        self.assertEqual(blocks, expected)

    def test_step_index_carries_over_between_blocks(self) -> None:
        blocks = AdpcmEncoder(1024).encode(sweep(1025 * 2))
        self.assertEqual(blocks[0][2], 0)
        self.assertGreater(blocks[1][2], 0)

    def test_flush_pads_last_block(self) -> None:
        encoder = AdpcmEncoder(1024)
        self.assertEqual(encoder.encode(b"\x10\x00" * 100), [])
        blocks = encoder.flush()
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]), 516)
        self.assertEqual(encoder.flush(), [])

    def test_invalid_length_per_encoder(self) -> None:
        with self.assertRaises(ValueError):
            AdpcmEncoder(1023)
        with self.assertRaises(ValueError):
            AdpcmEncoder(0)


class TestBcMediaFrame(unittest.TestCase):
    def test_frame_matches_reolink_client(self) -> None:
        block = AdpcmEncoder(1024).encode(b"\x00\x00" * 1025)[0]
        frame = bcmedia_adpcm_frame(block)
        # header as sent by the Reolink client: "01wb", 520, 520, 0x0100, 2
        self.assertEqual(frame[:12], bytes.fromhex("303177620802080200010200"))
        self.assertEqual(frame[12:], block)
        self.assertEqual(len(frame), 528)

    def test_frame_padding(self) -> None:
        block = AdpcmEncoder(1000).encode(b"\x00\x00" * 1001)[0]
        frame = bcmedia_adpcm_frame(block)
        self.assertEqual(len(frame) % 8, 0)
        self.assertEqual(struct.unpack("<H", frame[4:6])[0], len(block) + 4)


class _FakeConnection:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    async def send_without_wait(self, data: bytes, cmd_id: int | None = None, timeout: float = 15) -> None:
        self.writes.append(data)


class TalkTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bc = Baichuan("1.2.3.4", "admin", "password", http_api=SimpleNamespace(nvr_name="test", _updating=False))  # type: ignore[arg-type]
        self.bc._aes_key = AES_KEY
        self.bc._logged_in = True
        self.connection = _FakeConnection()
        self.bc._connection = self.connection  # type: ignore[assignment]
        self.bc._connect_if_needed = AsyncMock()  # type: ignore[method-assign]
        self.sent: list[tuple[int, str]] = []
        self.bc.send = AsyncMock(side_effect=self._send)  # type: ignore[method-assign]

        # fake clock so pacing does not slow down the tests
        self.clock = 1000.0
        self.sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay: float, *_args) -> None:
            self.sleeps.append(delay)
            self.clock += delay
            await real_sleep(0)

        self.enterContext(patch.object(bc_module, "monotonic", lambda: self.clock))
        self.enterContext(patch.object(bc_module.asyncio, "sleep", fake_sleep))

    async def _send(self, cmd_id: int, channel: int | None = None, body: str = "", **_kwargs) -> str:
        self.sent.append((cmd_id, body))
        if cmd_id == 10:
            return TALK_ABILITY_XML
        return ""

    def ack_all(self) -> None:
        session = self.bc._talk_sessions[0]
        header = bytes.fromhex(HEADER_MAGIC) + (202).to_bytes(4, "little") + bytes(4) + session.full_mess_id.to_bytes(4, "little") + bytes.fromhex("c8000000") + bytes(4)
        for _ in range(session.blocks_sent - session.blocks_acked):
            self.bc._push_callback(202, header, 24, b"")


class TestTalkSession(TalkTestCase):
    async def test_parse_talk_ability(self) -> None:
        ability = self.bc._parse_talk_ability(XML.fromstring(TALK_ABILITY_XML))
        self.assertEqual(ability, {"duplex": "FDX", "audio_stream_mode": "followVideoStream", "sample_rate": 16000, "length_per_encoder": 1024})
        self.assertIsNone(self.bc._parse_talk_ability(XML.fromstring("<body><TalkAbility/></body>")))
        self.assertIsNone(self.bc._parse_talk_ability(XML.fromstring(TALK_ABILITY_XML.replace("adpcm", "aac"))))

    async def test_start_talk_sends_talk_config(self) -> None:
        ability = await self.bc.start_talk(0)
        self.assertEqual(ability["sample_rate"], 16000)
        self.assertEqual(self.bc.talk_sample_rate(0), 16000)
        self.assertEqual([cmd for cmd, _ in self.sent], [10, 201])
        config = XML.fromstring(self.sent[1][1]).find("TalkConfig")
        assert config is not None
        self.assertEqual(config.findtext("channelId"), "0")
        self.assertEqual(config.findtext("duplex"), "FDX")
        self.assertEqual(config.findtext("audioStreamMode"), "followVideoStream")
        self.assertEqual(config.findtext("audioConfig/audioType"), "adpcm")
        self.assertEqual(config.findtext("audioConfig/sampleRate"), "16000")
        self.assertEqual(config.findtext("audioConfig/lengthPerEncoder"), "1024")

    async def test_start_talk_resets_busy_session(self) -> None:
        calls = 0

        async def busy_once(cmd_id: int, channel: int | None = None, body: str = "", **_kwargs) -> str:
            nonlocal calls
            self.sent.append((cmd_id, body))
            if cmd_id == 201 and calls == 0:
                calls += 1
                raise ApiError("busy", rspCode=422)
            return TALK_ABILITY_XML if cmd_id == 10 else ""

        self.bc.send = AsyncMock(side_effect=busy_once)  # type: ignore[method-assign]
        await self.bc.start_talk(0)
        self.assertEqual([cmd for cmd, _ in self.sent], [10, 201, 11, 201])
        self.assertEqual(self.sent[1][1], self.sent[3][1])

    async def test_start_talk_in_use_by_other_client(self) -> None:
        async def busy(cmd_id: int, channel: int | None = None, body: str = "", **_kwargs) -> str:
            self.sent.append((cmd_id, body))
            if cmd_id == 201:
                raise ApiError("busy", rspCode=422)
            return TALK_ABILITY_XML if cmd_id == 10 else ""

        self.bc.send = AsyncMock(side_effect=busy)  # type: ignore[method-assign]
        with self.assertRaisesRegex(ReolinkError, "in use by another client"):
            await self.bc.start_talk(0)
        self.assertEqual([cmd for cmd, _ in self.sent], [10, 201, 11, 201])
        self.assertNotIn(0, self.bc._talk_sessions)

    async def test_start_talk_other_error_raises(self) -> None:
        self.bc.send = AsyncMock(side_effect=ApiError("denied", rspCode=401))  # type: ignore[method-assign]
        with self.assertRaises(ApiError):
            await self.bc.start_talk(0)
        self.assertNotIn(0, self.bc._talk_sessions)

    async def test_start_talk_twice_raises(self) -> None:
        await self.bc.start_talk(0)
        with self.assertRaises(ReolinkError):
            await self.bc.start_talk(0)

    async def test_send_without_session_raises(self) -> None:
        with self.assertRaises(ReolinkError):
            await self.bc.send_talk_audio(0, b"\x00\x00" * 2000)

    async def test_audio_frames_on_the_wire(self) -> None:
        await self.bc.start_talk(0)
        session = self.bc._talk_sessions[0]
        pcm = sweep(1025 * 3)
        await self.bc.send_talk_audio(0, pcm)
        self.assertEqual(len(self.connection.writes), 3)

        expected_frames = [bcmedia_adpcm_frame(block) for block in AdpcmEncoder(1024).encode(pcm)]
        for data, frame in zip(self.connection.writes, expected_frames):
            self.assertEqual(data[0:4].hex(), HEADER_MAGIC)
            self.assertEqual(int.from_bytes(data[4:8], "little"), 202)
            self.assertEqual(int.from_bytes(data[12:16], "little"), session.full_mess_id)
            self.assertEqual(data[12], 1)  # channel 0 + 1
            self.assertEqual(data[16:20].hex(), "00001464")
            ext_len = int.from_bytes(data[20:24], "little")
            self.assertEqual(int.from_bytes(data[8:12], "little"), ext_len + len(frame))
            ext = AES.new(key=AES_KEY, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128).decrypt(data[24 : 24 + ext_len]).decode()
            self.assertIn("<binaryData>1</binaryData>", ext)
            self.assertIn("<channelId>0</channelId>", ext)
            self.assertEqual(data[24 + ext_len :], frame)  # audio payload is sent unencrypted

    async def test_pacing_stays_close_to_real_time(self) -> None:
        await self.bc.start_talk(0)
        session = self.bc._talk_sessions[0]
        send_times: list[float] = []

        async def record(data: bytes, cmd_id: int | None = None, timeout: float = 15) -> None:
            send_times.append(self.clock)

        self.connection.send_without_wait = record  # type: ignore[method-assign]
        for _ in range(40):
            await self.bc.send_talk_audio(0, sweep(1025))
            session.blocks_acked = session.blocks_sent
        start = send_times[0]
        for i, sent_at in enumerate(send_times):
            ahead = start + i * session.block_time - sent_at
            self.assertLessEqual(ahead, bc_module.TALK_LEAD + 1e-6)
            self.assertGreaterEqual(ahead, 0)
        self.assertAlmostEqual(ahead, bc_module.TALK_LEAD, places=6)

    async def test_pacing_restarts_after_underrun(self) -> None:
        await self.bc.start_talk(0)
        session = self.bc._talk_sessions[0]
        await self.bc.send_talk_audio(0, sweep(1025 * 2))
        self.clock += 10  # producer stalls, the camera plays out everything
        sent_before = len(self.connection.writes)
        sleeps_before = len(self.sleeps)
        await self.bc.send_talk_audio(0, sweep(1025 * 3))
        self.assertEqual(len(self.connection.writes), sent_before + 3)
        # no burst of catch-up, but no waiting on the stale clock either
        self.assertTrue(all(delay <= session.block_time for delay in self.sleeps[sleeps_before:]))

    async def test_acks_release_flow_control(self) -> None:
        await self.bc.start_talk(0)
        session = self.bc._talk_sessions[0]
        task = asyncio.create_task(self.bc.send_talk_audio(0, b"\x00\x00" * 1025 * (bc_module.TALK_MAX_IN_FLIGHT + 2)))
        for _ in range(50):
            await asyncio.sleep(0)
        self.assertEqual(session.blocks_sent, bc_module.TALK_MAX_IN_FLIGHT)
        self.assertFalse(task.done())
        self.ack_all()
        await asyncio.wait_for(task, 1)
        self.assertEqual(session.blocks_sent, bc_module.TALK_MAX_IN_FLIGHT + 2)

    async def test_missing_acks_time_out(self) -> None:
        await self.bc.start_talk(0)
        with patch.object(bc_module, "TALK_ACK_TIMEOUT", 0.05):
            with self.assertRaises(ReolinkTimeoutError):
                await self.bc.send_talk_audio(0, b"\x00\x00" * 1025 * (bc_module.TALK_MAX_IN_FLIGHT + 1))

    async def test_ack_for_unknown_session_is_ignored(self) -> None:
        header = bytes.fromhex(HEADER_MAGIC) + (202).to_bytes(4, "little") + bytes(4) + (12345).to_bytes(4, "little") + bytes.fromhex("c8000000") + bytes(4)
        self.bc._push_callback(202, header, 24, b"")

    async def test_stop_talk_flushes_and_waits_for_playout(self) -> None:
        await self.bc.start_talk(0)
        session = self.bc._talk_sessions[0]
        await self.bc.send_talk_audio(0, sweep(1025 * 2 + 500))
        self.assertEqual(len(self.connection.writes), 2)
        await self.bc.stop_talk(0)
        self.assertEqual(len(self.connection.writes), 3)  # remaining 500 samples padded into a last block
        assert session.start is not None
        self.assertGreaterEqual(self.clock, session.start + 3 * session.block_time + bc_module.TALK_PLAYOUT_MARGIN - 1e-6)
        self.assertEqual(self.sent[-1][0], 11)
        self.assertNotIn(0, self.bc._talk_sessions)

    async def test_stop_talk_without_wait(self) -> None:
        await self.bc.start_talk(0)
        await self.bc.send_talk_audio(0, sweep(500))
        clock = self.clock
        await self.bc.stop_talk(0, wait=False)
        self.assertEqual(self.connection.writes, [])
        self.assertEqual(self.clock, clock)
        self.assertEqual(self.sent[-1][0], 11)
        self.assertNotIn(0, self.bc._talk_sessions)

    async def test_talk_plays_clip(self) -> None:
        await self.bc.talk(0, sweep(1025 * 4))
        self.assertEqual([cmd for cmd, _ in self.sent], [10, 201, 11])
        self.assertEqual(len(self.connection.writes), 4)
        self.assertNotIn(0, self.bc._talk_sessions)

    async def test_battery_connection_kept_open_while_talking(self) -> None:
        await self.bc.start_talk(0)
        self.bc._connection = SimpleNamespace(time_send=0, receive_futures={})  # type: ignore[assignment]
        self.bc.logout = AsyncMock()  # type: ignore[method-assign]
        real_sleep = asyncio.sleep
        sleeps: list[float] = []

        async def end_talk_after_first_sleep(delay: float, *_args) -> None:
            sleeps.append(delay)
            self.bc._talk_sessions.clear()  # talk ends, the next round may close the idle connection
            await real_sleep(0)

        with patch.object(bc_module.asyncio, "sleep", end_talk_after_first_sleep):
            await self.bc._battery_close_loop()
        self.assertEqual(sleeps, [bc_module.BATTERY_CLOSE_TIME])  # not closed while talking
        self.bc.logout.assert_awaited_once()

    async def test_talk_resets_session_on_error(self) -> None:
        async def broken(data: bytes, cmd_id: int | None = None, timeout: float = 15) -> None:
            raise ReolinkError("connection lost")

        self.connection.send_without_wait = broken  # type: ignore[method-assign]
        with self.assertRaises(ReolinkError):
            await self.bc.talk(0, sweep(1025 * 4))
        self.assertEqual(self.sent[-1][0], 11)
        self.assertNotIn(0, self.bc._talk_sessions)



class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self, size: int = -1) -> bytes:
        size = len(self.data) if size < 0 else size
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class _FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._returncode = returncode
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self._returncode

    def kill(self) -> None:
        self.returncode = -9


class TestPlayAudioFile(TalkTestCase):
    async def test_streams_decoded_audio(self) -> None:
        pcm = sweep(1025 * 5 + 100)
        exec_mock = AsyncMock(return_value=_FakeProcess(pcm))
        with patch.object(bc_module.asyncio, "create_subprocess_exec", exec_mock):
            await self.bc.play_audio_file(0, "http://ha.local/api/tts_proxy/abc.mp3", ffmpeg="/usr/bin/ffmpeg")
        command = exec_mock.call_args.args
        self.assertEqual(command[0], "/usr/bin/ffmpeg")
        self.assertIn("http://ha.local/api/tts_proxy/abc.mp3", command)
        self.assertEqual(command[command.index("-ar") + 1], "16000")
        self.assertEqual([cmd for cmd, _ in self.sent], [10, 201, 11])
        self.assertEqual(len(self.connection.writes), 6)  # 5 full blocks + the padded remainder
        self.assertNotIn(0, self.bc._talk_sessions)

    async def test_decode_error_does_not_start_talk(self) -> None:
        exec_mock = AsyncMock(return_value=_FakeProcess(b"", returncode=1, stderr=b"No such file"))
        with patch.object(bc_module.asyncio, "create_subprocess_exec", exec_mock):
            with self.assertRaisesRegex(ReolinkError, "No such file"):
                await self.bc.play_audio_file(0, "/missing.mp3")
        self.assertNotIn(201, [cmd for cmd, _ in self.sent])
        self.assertEqual(self.connection.writes, [])


if __name__ == "__main__":
    unittest.main()
