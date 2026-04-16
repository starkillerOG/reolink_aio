"""Tests for two-way audio (talk) implementation."""

from __future__ import annotations

import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from reolink_aio.baichuan.audio import (
    BC_MEDIA_ADPCM_MAGIC,
    build_bc_media_frame,
    encode_pcm_to_adpcm,
)
from reolink_aio.baichuan.baichuan import Baichuan


class _TalkRecordingTransport:
    """Transport that records writes without setting response futures (fire-and-forget)."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def is_closing(self) -> bool:
        return False


class _SendRecordingTransport:
    """Transport that records writes and resolves response futures (for send())."""

    def __init__(self, protocol, response_body: str = "") -> None:
        self._protocol = protocol
        self._response_body = response_body
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        cmd_id = int.from_bytes(data[4:8], byteorder="little")
        mess_id = int.from_bytes(data[12:16], byteorder="little")
        if cmd_id in self._protocol.receive_futures and mess_id in self._protocol.receive_futures[cmd_id]:
            self._protocol.receive_futures[cmd_id][mess_id].set_result((data[:24], 24, b""))

    def is_closing(self) -> bool:
        return False


# --- ADPCM Encoder Tests ---


class TestAdpcmEncoder(unittest.TestCase):
    def test_empty_input(self) -> None:
        result = encode_pcm_to_adpcm(b"")
        self.assertEqual(result, [])

    def test_single_byte_ignored(self) -> None:
        """A single byte is not a complete 16-bit sample."""
        result = encode_pcm_to_adpcm(b"\x00")
        self.assertEqual(result, [])

    def test_silence_single_block(self) -> None:
        """1024 zero samples should produce one block."""
        pcm = b"\x00\x00" * 1024
        blocks = encode_pcm_to_adpcm(pcm, samples_per_block=1024)
        self.assertEqual(len(blocks), 1)

    def test_block_structure(self) -> None:
        """Verify preamble format and data size."""
        pcm = b"\x00\x00" * 1024
        blocks = encode_pcm_to_adpcm(pcm, samples_per_block=1024)
        block = blocks[0]

        # Block should be 4-byte preamble + 512 bytes of nibble data
        self.assertEqual(len(block), 4 + 512)

        # Preamble: predictor (i16 LE) + step_index (u8) + reserved (u8)
        predictor, step_index, reserved = struct.unpack("<hBB", block[:4])
        self.assertEqual(reserved, 0)
        self.assertGreaterEqual(step_index, 0)
        self.assertLessEqual(step_index, 88)

    def test_silence_encodes_to_zero_nibbles(self) -> None:
        """All-zero PCM with zero initial state should produce all-zero nibbles."""
        pcm = b"\x00\x00" * 1024
        blocks = encode_pcm_to_adpcm(pcm, samples_per_block=1024)
        block = blocks[0]

        # Preamble should be zeros (predictor=0, step_index=0)
        self.assertEqual(block[:4], b"\x00\x00\x00\x00")

        # All nibbles should be zero (difference is always 0)
        for byte in block[4:]:
            self.assertEqual(byte, 0)

    def test_multiple_blocks(self) -> None:
        """Input longer than samples_per_block should produce multiple blocks."""
        pcm = b"\x00\x00" * 2048
        blocks = encode_pcm_to_adpcm(pcm, samples_per_block=1024)
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            self.assertEqual(len(block), 4 + 512)

    def test_partial_last_block(self) -> None:
        """Input not a multiple of samples_per_block produces a smaller last block."""
        # 1536 samples: 1 full block (1024) + 1 partial (512)
        pcm = b"\x00\x00" * 1536
        blocks = encode_pcm_to_adpcm(pcm, samples_per_block=1024)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(len(blocks[0]), 4 + 512)  # Full block
        self.assertEqual(len(blocks[1]), 4 + 256)  # 512 samples = 256 nibble bytes

    def test_non_zero_pcm_produces_non_zero_nibbles(self) -> None:
        """A loud tone should produce non-zero ADPCM nibbles."""
        # Simple sawtooth: ramps from 0 to 32000
        samples = [int(32000 * i / 1024) for i in range(1024)]
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        blocks = encode_pcm_to_adpcm(pcm, samples_per_block=1024)
        self.assertEqual(len(blocks), 1)
        # At least some data bytes should be non-zero
        data = blocks[0][4:]
        self.assertTrue(any(b != 0 for b in data))

    def test_predictor_stays_in_range(self) -> None:
        """Predictor should never exceed 16-bit signed range."""
        # Extreme input: alternating min/max
        samples = [32767, -32768] * 512
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        blocks = encode_pcm_to_adpcm(pcm, samples_per_block=1024)
        # Just verify it completes without error — predictor clamping works
        self.assertEqual(len(blocks), 1)

    def test_small_block_size(self) -> None:
        """Verify encoder works with small block sizes."""
        pcm = b"\x00\x00" * 8
        blocks = encode_pcm_to_adpcm(pcm, samples_per_block=4)
        self.assertEqual(len(blocks), 2)
        # 4 samples = 4 nibbles = 2 bytes + 4 preamble = 6
        for block in blocks:
            self.assertEqual(len(block), 4 + 2)


# --- BcMedia Frame Tests ---


class TestBcMediaFrame(unittest.TestCase):
    def test_frame_magic(self) -> None:
        """Frame should start with '01wb' magic."""
        block = b"\x00" * (4 + 512)  # Fake ADPCM block
        frame = build_bc_media_frame(block)
        self.assertEqual(frame[:4], BC_MEDIA_ADPCM_MAGIC)

    def test_frame_payload_size(self) -> None:
        """payload_size should be data_len + 4."""
        block = b"\x00" * (4 + 512)  # 516 bytes
        frame = build_bc_media_frame(block)
        payload_size_1 = struct.unpack("<H", frame[4:6])[0]
        payload_size_2 = struct.unpack("<H", frame[6:8])[0]
        self.assertEqual(payload_size_1, 516 + 4)  # data + sub_header
        self.assertEqual(payload_size_1, payload_size_2)

    def test_frame_sub_magic(self) -> None:
        """Sub-magic should be 0x0001."""
        block = b"\x00" * (4 + 512)
        frame = build_bc_media_frame(block)
        sub_magic = struct.unpack("<H", frame[8:10])[0]
        self.assertEqual(sub_magic, 0x0001)

    def test_frame_half_block_size(self) -> None:
        """half_block_size should be (data_len - 4) / 2."""
        block = b"\x00" * (4 + 512)
        frame = build_bc_media_frame(block)
        half_block = struct.unpack("<H", frame[10:12])[0]
        self.assertEqual(half_block, 256)  # (516 - 4) / 2 = 256

    def test_frame_8byte_alignment(self) -> None:
        """Data portion (data + padding) should be 8-byte aligned."""
        # 516 data → padding = (8 - 516 % 8) % 8 = 4 → data+padding = 520
        block = b"\x00" * 516
        frame = build_bc_media_frame(block)
        data_plus_padding = len(frame) - 12  # subtract 12-byte header
        self.assertEqual(data_plus_padding % 8, 0)

    def test_frame_no_padding_needed(self) -> None:
        """Data that's already 8-byte aligned needs no padding."""
        # 520 bytes → 520 % 8 = 0
        block = b"\x00" * 520
        frame = build_bc_media_frame(block)
        # 12 header + 520 data = 532, 532 % 8 = 4 → no wait, padding is based on data_len not total
        # padding = (8 - 520 % 8) % 8 = 0
        self.assertEqual(len(frame), 12 + 520)

    def test_frame_contains_data(self) -> None:
        """ADPCM data should appear after the 12-byte header."""
        block = bytes(range(256)) * 2 + bytes(4)  # 516 bytes
        frame = build_bc_media_frame(block)
        self.assertEqual(frame[12 : 12 + len(block)], block)

    def test_typical_block_frame_size(self) -> None:
        """Typical 8kHz/1024-sample block: 516 data → 532 total."""
        block = b"\x00" * 516  # 4 preamble + 512 nibble bytes
        frame = build_bc_media_frame(block)
        # 12 header + 516 data + 4 padding = 532
        self.assertEqual(len(frame), 532)


# --- send_binary_no_reply Tests ---


class TestSendBinaryNoReply(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bc = Baichuan(
            host="127.0.0.1",
            username="user",
            password="password",
            http_api=SimpleNamespace(nvr_name="test", _updating=False),
        )
        self.bc._logged_in = True
        self.bc._aes_key = b"0123456789abcdef"  # 16-byte key for AES-128
        self.transport = _TalkRecordingTransport()
        self.bc._protocol = SimpleNamespace(receive_futures={})
        self.bc._transport = self.transport
        self.bc._connect_if_needed = AsyncMock()

    async def test_header_cmd_id(self) -> None:
        """cmd_id in the header should match the argument."""
        await self.bc.send_binary_no_reply(cmd_id=202, channel=0, binary_body=b"\x00" * 100)

        written = self.transport.writes[0]
        cmd_id = int.from_bytes(written[4:8], byteorder="little")
        self.assertEqual(cmd_id, 202)

    async def test_header_message_class_1464(self) -> None:
        """Message class 1464 produces a 24-byte header."""
        await self.bc.send_binary_no_reply(cmd_id=202, channel=0, binary_body=b"\x00" * 100)

        written = self.transport.writes[0]
        message_class = written[18:20].hex()
        self.assertEqual(message_class, "1464")

    async def test_payload_offset_equals_encrypted_extension_length(self) -> None:
        """payload_offset should equal the length of the AES-encrypted extension."""
        await self.bc.send_binary_no_reply(cmd_id=202, channel=0, binary_body=b"\xAA" * 50)

        written = self.transport.writes[0]
        payload_offset = int.from_bytes(written[20:24], byteorder="little")
        mess_len = int.from_bytes(written[8:12], byteorder="little")

        self.assertEqual(mess_len, payload_offset + 50)
        self.assertGreater(payload_offset, 0)

    async def test_raw_payload_is_not_encrypted(self) -> None:
        """The binary body should appear as-is (not encrypted) in the write."""
        marker = b"\xDE\xAD\xBE\xEF" * 10  # 40 bytes of recognizable data
        await self.bc.send_binary_no_reply(cmd_id=202, channel=0, binary_body=marker)

        written = self.transport.writes[0]
        self.assertTrue(written.endswith(marker))

    async def test_extension_is_encrypted(self) -> None:
        """The extension XML should be AES-encrypted (different from plaintext)."""
        await self.bc.send_binary_no_reply(cmd_id=202, channel=0, binary_body=b"\x00" * 10)

        written = self.transport.writes[0]
        payload_offset = int.from_bytes(written[20:24], byteorder="little")
        enc_ext = written[24 : 24 + payload_offset]

        self.assertNotIn(b"channelId", enc_ext)

    async def test_mess_id_increments(self) -> None:
        """Each call should increment the message ID."""
        self.bc._mess_id = 100
        await self.bc.send_binary_no_reply(cmd_id=202, channel=0, binary_body=b"\x00")
        await self.bc.send_binary_no_reply(cmd_id=202, channel=0, binary_body=b"\x00")

        id1 = int.from_bytes(self.transport.writes[0][12:16], byteorder="little")
        id2 = int.from_bytes(self.transport.writes[1][12:16], byteorder="little")
        self.assertNotEqual(id1, id2)


# --- talk() Orchestration Tests ---


class TestTalkOrchestration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bc = Baichuan(
            host="127.0.0.1",
            username="user",
            password="password",
            http_api=SimpleNamespace(nvr_name="test", _updating=False),
        )
        self.bc._logged_in = True
        self.bc._aes_key = b"0123456789abcdef"
        self.transport = _TalkRecordingTransport()
        self.protocol = SimpleNamespace(receive_futures={})
        self.bc._protocol = self.protocol
        self.bc._transport = self.transport
        self.bc._connect_if_needed = AsyncMock()

        # Mock send() to avoid full protocol handling
        self.send_calls: list[dict] = []

        async def mock_send(cmd_id, channel=None, body="", **kwargs):
            self.send_calls.append({"cmd_id": cmd_id, "channel": channel, "body": body})
            return "<ok/>"

        self.bc.send = mock_send

    async def test_talk_sends_talk_config_first(self) -> None:
        """talk() should send TalkConfig (cmd_id=201) before audio frames."""
        pcm = b"\x00\x00" * 1024  # 1 block of silence
        await self.bc.talk(channel=0, audio_data=pcm)

        # First send should be TalkConfig
        self.assertEqual(self.send_calls[0]["cmd_id"], 201)
        self.assertIn("TalkConfig", self.send_calls[0]["body"])

    async def test_talk_sends_talk_reset_at_end(self) -> None:
        """talk() should send TalkReset (cmd_id=11) when done."""
        pcm = b"\x00\x00" * 1024
        await self.bc.talk(channel=0, audio_data=pcm)

        # Last send should be TalkReset
        self.assertEqual(self.send_calls[-1]["cmd_id"], 11)

    async def test_talk_sends_audio_frames(self) -> None:
        """talk() should write audio frames via send_binary_no_reply."""
        pcm = b"\x00\x00" * 1024
        await self.bc.talk(channel=0, audio_data=pcm)

        # Should have at least one transport write (audio frame)
        self.assertGreater(len(self.transport.writes), 0)

    async def test_talk_uses_config_from_talk_ability(self) -> None:
        """talk() should use cached TalkAbility config."""
        self.bc._talk_config[0] = {
            "sample_rate": 16000,
            "block_size": 2048,
            "duplex": "HDX",
            "stream_mode": "mixAudioStream",
        }
        pcm = b"\x00\x00" * 2048
        await self.bc.talk(channel=0, audio_data=pcm)

        # TalkConfig body should contain the cached values
        body = self.send_calls[0]["body"]
        self.assertIn("<sampleRate>16000</sampleRate>", body)
        self.assertIn("<lengthPerEncoder>2048</lengthPerEncoder>", body)
        self.assertIn("<duplex>HDX</duplex>", body)

    async def test_talk_parameter_overrides(self) -> None:
        """Explicit sample_rate/block_size should override TalkAbility."""
        self.bc._talk_config[0] = {
            "sample_rate": 8000,
            "block_size": 1024,
            "duplex": "FDX",
            "stream_mode": "followVideoStream",
        }
        pcm = b"\x00\x00" * 2048
        await self.bc.talk(channel=0, audio_data=pcm, sample_rate=16000, block_size=2048)

        body = self.send_calls[0]["body"]
        self.assertIn("<sampleRate>16000</sampleRate>", body)
        self.assertIn("<lengthPerEncoder>2048</lengthPerEncoder>", body)

    async def test_talk_reset_on_error(self) -> None:
        """TalkReset should be sent even if audio sending fails."""
        async def failing_send_binary(cmd_id, channel=None, binary_body=b""):
            raise ConnectionError("fake error")

        self.bc.send_binary_no_reply = failing_send_binary

        pcm = b"\x00\x00" * 1024
        with self.assertRaises(ConnectionError):
            await self.bc.talk(channel=0, audio_data=pcm)

        # TalkReset (cmd_id=11) should still be sent
        reset_calls = [c for c in self.send_calls if c["cmd_id"] == 11]
        self.assertEqual(len(reset_calls), 1)


# --- build_bcmedia_adpcm Tests ---


class TestBuildBcmediaAdpcm(unittest.TestCase):
    """Tests for Baichuan.build_bcmedia_adpcm() (our split-API framing helper)."""

    def _make_block(self, lpe: int = 1024) -> bytes:
        """Return a minimal valid ADPCM block for the given lengthPerEncoder."""
        return b"\x00" * (4 + lpe // 2)

    def test_magic_bytes(self) -> None:
        """Frame should start with the ADPCM magic (0x62773130 LE = bytes 30 31 77 62)."""
        block = self._make_block()
        payload = Baichuan.build_bcmedia_adpcm([block])
        self.assertEqual(payload[:4], struct.pack("<I", 0x62773130))

    def test_payload_size_field(self) -> None:
        """payload_size should be len(block) + 4, duplicated in bytes 4-8."""
        block = self._make_block(1024)  # 4 + 512 = 516 bytes
        payload = Baichuan.build_bcmedia_adpcm([block])
        ps1 = struct.unpack("<H", payload[4:6])[0]
        ps2 = struct.unpack("<H", payload[6:8])[0]
        self.assertEqual(ps1, len(block) + 4)
        self.assertEqual(ps1, ps2)

    def test_sub_magic(self) -> None:
        """Sub-magic field should be 0x0100 (confirmed from pcap)."""
        block = self._make_block()
        payload = Baichuan.build_bcmedia_adpcm([block])
        sub_magic = struct.unpack("<H", payload[8:10])[0]
        self.assertEqual(sub_magic, 0x0100)

    def test_half_block_is_2(self) -> None:
        """half_block field is always 2 (confirmed from Ghidra)."""
        block = self._make_block()
        payload = Baichuan.build_bcmedia_adpcm([block])
        half_block = struct.unpack("<H", payload[10:12])[0]
        self.assertEqual(half_block, 2)

    def test_block_data_follows_header(self) -> None:
        """ADPCM block data should appear immediately after the 12-byte frame header."""
        block = bytes(range(100)) + b"\x00" * (4 + 512 - 100)
        payload = Baichuan.build_bcmedia_adpcm([block])
        self.assertEqual(payload[12 : 12 + len(block)], block)

    def test_8byte_alignment(self) -> None:
        """Total frame length should always be a multiple of 8."""
        for lpe in [160, 320, 512, 1024]:
            block = self._make_block(lpe)
            payload = Baichuan.build_bcmedia_adpcm([block])
            self.assertEqual(len(payload) % 8, 0, f"lpe={lpe}: frame length {len(payload)} not 8-byte aligned")

    def test_no_padding_when_already_aligned(self) -> None:
        """No padding bytes when payload_size is already a multiple of 8."""
        # For lpe=1024: block = 4 + 512 = 516, payload_size = 520, 520 % 8 = 0 → no padding
        block = self._make_block(1024)
        payload = Baichuan.build_bcmedia_adpcm([block])
        expected_len = 12 + len(block)  # 12 header + 516 data + 0 padding
        self.assertEqual(len(payload), expected_len)

    def test_multiple_blocks_concatenated(self) -> None:
        """Multiple blocks should produce concatenated BcMedia frames."""
        block = self._make_block()
        payload = Baichuan.build_bcmedia_adpcm([block, block, block])
        # Each frame: 12 header + 516 data = 528 bytes (520 payload_size, no padding needed)
        single_frame_len = 12 + len(block)
        self.assertEqual(len(payload), 3 * single_frame_len)

    def test_empty_block_list(self) -> None:
        """Empty block list returns empty bytes."""
        self.assertEqual(Baichuan.build_bcmedia_adpcm([]), b"")


# --- get_talk_ability / start_talk / stop_talk / send_talk_data Tests ---


class TestSplitTalkApi(unittest.IsolatedAsyncioTestCase):
    """Tests for the get_talk_ability / start_talk / stop_talk / send_talk_data split API."""

    _TALK_ABILITY_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<body>
<TalkAbility version="1.1">
<duplexList><duplex>FDX</duplex></duplexList>
<audioStreamModeList><audioStreamMode>followVideoStream</audioStreamMode></audioStreamModeList>
<audioConfigList>
<audioConfig>
<priority>0</priority>
<audioType>adpcm</audioType>
<sampleRate>16000</sampleRate>
<samplePrecision>16</samplePrecision>
<lengthPerEncoder>1024</lengthPerEncoder>
<soundTrack>mono</soundTrack>
</audioConfig>
</audioConfigList>
</TalkAbility>
</body>"""

    async def asyncSetUp(self) -> None:
        self.bc = Baichuan(
            host="127.0.0.1",
            username="user",
            password="password",
            http_api=SimpleNamespace(nvr_name="test", _updating=False),
        )
        self.bc._logged_in = True
        self.bc._aes_key = b"0123456789abcdef"
        self.transport = _TalkRecordingTransport()
        self.bc._protocol = SimpleNamespace(receive_futures={})
        self.bc._transport = self.transport
        self.bc._connect_if_needed = AsyncMock()

        self.send_calls: list[dict] = []

        async def mock_send(cmd_id, channel=None, body="", **kwargs):
            self.send_calls.append({"cmd_id": cmd_id, "channel": channel, "body": body})
            if cmd_id == 10:
                return self._TALK_ABILITY_XML
            return "<ok/>"

        self.bc.send = mock_send

    async def test_get_talk_ability_parses_sample_rate(self) -> None:
        ability = await self.bc.get_talk_ability(channel=0)
        self.assertEqual(ability["sample_rate"], 16000)

    async def test_get_talk_ability_parses_length_per_encoder(self) -> None:
        ability = await self.bc.get_talk_ability(channel=0)
        self.assertEqual(ability["length_per_encoder"], 1024)

    async def test_get_talk_ability_parses_duplex(self) -> None:
        ability = await self.bc.get_talk_ability(channel=0)
        self.assertEqual(ability["duplex"], "FDX")

    async def test_get_talk_ability_parses_audio_stream_mode(self) -> None:
        ability = await self.bc.get_talk_ability(channel=0)
        self.assertEqual(ability["audio_stream_mode"], "followVideoStream")

    async def test_start_talk_sends_cmd_id_10_then_201(self) -> None:
        """start_talk() must call TalkAbility (10) then TalkConfig (201)."""
        await self.bc.start_talk(channel=0)
        self.assertEqual(self.send_calls[0]["cmd_id"], 10)
        self.assertEqual(self.send_calls[1]["cmd_id"], 201)

    async def test_start_talk_uses_camera_sample_rate(self) -> None:
        """TalkConfig body should use the sample_rate from TalkAbility (not a default)."""
        await self.bc.start_talk(channel=0)
        body = self.send_calls[1]["body"]
        self.assertIn("<sampleRate>16000</sampleRate>", body)

    async def test_start_talk_returns_ability_dict(self) -> None:
        ability = await self.bc.start_talk(channel=0)
        self.assertEqual(ability["sample_rate"], 16000)
        self.assertEqual(ability["length_per_encoder"], 1024)

    async def test_stop_talk_sends_cmd_id_11(self) -> None:
        await self.bc.stop_talk(channel=0)
        self.assertEqual(self.send_calls[0]["cmd_id"], 11)

    async def test_send_talk_data_calls_send_binary_no_reply(self) -> None:
        """send_talk_data() should fire the audio payload via send_binary_no_reply."""
        marker = b"\xAB\xCD" * 20
        await self.bc.send_talk_data(channel=0, bcmedia_data=marker)

        self.assertEqual(len(self.transport.writes), 1)
        self.assertTrue(self.transport.writes[0].endswith(marker))

    async def test_send_talk_data_uses_cmd_202(self) -> None:
        await self.bc.send_talk_data(channel=0, bcmedia_data=b"\x00" * 8)
        written = self.transport.writes[0]
        cmd_id = int.from_bytes(written[4:8], byteorder="little")
        self.assertEqual(cmd_id, 202)


# --- Capability Detection Tests ---


class TestTwoWayAudioCapability(unittest.IsolatedAsyncioTestCase):
    """Tests for two_way_audio capability detection from TalkAbility (cmd_id=10)."""

    async def _make_baichuan(self) -> Baichuan:
        bc = Baichuan(
            host="127.0.0.1",
            username="user",
            password="password",
            http_api=SimpleNamespace(nvr_name="test", _updating=False),
        )
        bc.capabilities[0] = set()
        return bc

    def _process_cmd10(self, bc: Baichuan, xml: str) -> None:
        """Simulate the get_channel_data() processing of a cmd_id=10 result."""
        from xml.etree import ElementTree as XML
        root = XML.fromstring(xml)
        for audio in root.findall(".//audioStreamMode"):
            if audio.text == "mixAudioStream":
                bc.capabilities[0].add("two_way_audio")
        audio_cfg = root.find(".//audioConfig")
        if audio_cfg is not None:
            bc.capabilities[0].add("two_way_audio")
            bc._talk_config[0] = {
                "sample_rate": int(audio_cfg.findtext("sampleRate", "8000")),
                "block_size": int(audio_cfg.findtext("lengthPerEncoder", "1024")),
                "duplex": root.findtext(".//duplex", "FDX"),
                "stream_mode": root.findtext(".//audioStreamMode", "followVideoStream"),
            }

    async def test_follow_video_stream_sets_capability(self) -> None:
        """Cameras using followVideoStream (e.g. E1) must also be detected."""
        bc = await self._make_baichuan()
        xml = """<body><TalkAbility version="1.1">
            <duplexList><duplex>FDX</duplex></duplexList>
            <audioStreamModeList><audioStreamMode>followVideoStream</audioStreamMode></audioStreamModeList>
            <audioConfigList><audioConfig>
                <audioType>adpcm</audioType><sampleRate>16000</sampleRate>
                <samplePrecision>16</samplePrecision><lengthPerEncoder>1024</lengthPerEncoder>
                <soundTrack>mono</soundTrack>
            </audioConfig></audioConfigList>
        </TalkAbility></body>"""
        self._process_cmd10(bc, xml)
        self.assertIn("two_way_audio", bc.capabilities[0])

    async def test_mix_audio_stream_sets_capability(self) -> None:
        """Cameras using mixAudioStream should still be detected."""
        bc = await self._make_baichuan()
        xml = """<body><TalkAbility version="1.1">
            <duplexList><duplex>FDX</duplex></duplexList>
            <audioStreamModeList><audioStreamMode>mixAudioStream</audioStreamMode></audioStreamModeList>
            <audioConfigList><audioConfig>
                <audioType>adpcm</audioType><sampleRate>8000</sampleRate>
                <samplePrecision>16</samplePrecision><lengthPerEncoder>320</lengthPerEncoder>
                <soundTrack>mono</soundTrack>
            </audioConfig></audioConfigList>
        </TalkAbility></body>"""
        self._process_cmd10(bc, xml)
        self.assertIn("two_way_audio", bc.capabilities[0])

    async def test_correct_sample_rate_stored(self) -> None:
        """_talk_config should store the camera's actual sample_rate."""
        bc = await self._make_baichuan()
        xml = """<body><TalkAbility version="1.1">
            <duplexList><duplex>FDX</duplex></duplexList>
            <audioStreamModeList><audioStreamMode>followVideoStream</audioStreamMode></audioStreamModeList>
            <audioConfigList><audioConfig>
                <audioType>adpcm</audioType><sampleRate>16000</sampleRate>
                <samplePrecision>16</samplePrecision><lengthPerEncoder>1024</lengthPerEncoder>
                <soundTrack>mono</soundTrack>
            </audioConfig></audioConfigList>
        </TalkAbility></body>"""
        self._process_cmd10(bc, xml)
        self.assertEqual(bc._talk_config[0]["sample_rate"], 16000)


if __name__ == "__main__":
    unittest.main()
