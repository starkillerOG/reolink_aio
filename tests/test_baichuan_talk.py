"""Tests for two-way audio (talk), no camera needed"""

from __future__ import annotations

import math
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from reolink_aio.baichuan.adpcm import (
    INDEX_TABLE,
    STEP_TABLE,
    AdpcmEncoder,
    bcmedia_adpcm_packet,
)
from reolink_aio.baichuan.baichuan import Baichuan
from reolink_aio.exceptions import ApiError

SAMPLE_RATE = 16000
SAMPLES_PER_BLOCK = 1024

TALK_ABILITY_XML = """<?xml version="1.0" encoding="UTF-8" ?>
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

TALK_ABILITY_XML_NO_ADPCM = TALK_ABILITY_XML.replace("adpcm", "aac")


def decode_adpcm_block(block: bytes) -> list[int]:
    """Reference IMA ADPCM decoder, written from the spec, not from the encoder"""
    predictor, index = struct.unpack("<hB", block[0:3])
    samples = []
    for byte in block[4:]:
        for nibble in (byte & 0x0F, byte >> 4):  # low nibble holds the first sample
            step = STEP_TABLE[index]
            diff = step >> 3
            if nibble & 4:
                diff += step
            if nibble & 2:
                diff += step >> 1
            if nibble & 1:
                diff += step >> 2
            predictor += -diff if nibble & 8 else diff
            predictor = max(-32768, min(32767, predictor))
            index = max(0, min(len(STEP_TABLE) - 1, index + INDEX_TABLE[nibble]))
            samples.append(predictor)
    return samples


def sine(seconds: float, freq: float = 1000.0, amplitude: float = 0.8) -> list[int]:
    return [int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)) for i in range(int(SAMPLE_RATE * seconds))]


class TestAdpcm(unittest.TestCase):
    def test_encodes_to_a_signal_a_reference_decoder_recognises(self) -> None:
        original = sine(0.25)
        encoder = AdpcmEncoder()
        decoded: list[int] = []
        for start in range(0, len(original), SAMPLES_PER_BLOCK):
            decoded.extend(decode_adpcm_block(encoder.encode_block(original[start : start + SAMPLES_PER_BLOCK])))

        self.assertEqual(len(decoded), len(original))
        signal = sum(sample * sample for sample in original)
        noise = sum((decoded[i] - original[i]) ** 2 for i in range(len(original)))
        snr_db = 10 * math.log10(signal / noise)
        self.assertGreater(snr_db, 20, f"ADPCM round trip only reached {snr_db:.1f} dB")

    def test_block_holds_one_nibble_per_sample_plus_a_header(self) -> None:
        block = AdpcmEncoder().encode_block(sine(0.064))  # exactly one block
        self.assertEqual(len(block), 4 + SAMPLES_PER_BLOCK // 2)

    def test_block_header_carries_the_state_the_block_starts_from(self) -> None:
        # the predictor runs across blocks, so every header has to hold the
        # state as it was before that block, otherwise a decoder drifts off
        encoder = AdpcmEncoder()
        samples = sine(0.192)

        first = encoder.encode_block(samples[:SAMPLES_PER_BLOCK])
        self.assertEqual(struct.unpack("<hBB", first[0:4])[0:2], (0, 0), "the first block starts from silence")

        state_before = (encoder.predictor, encoder.index)
        self.assertNotEqual(state_before, (0, 0), "encoding should have moved the state")
        second = encoder.encode_block(samples[SAMPLES_PER_BLOCK : 2 * SAMPLES_PER_BLOCK])
        predictor, index, _ = struct.unpack("<hBB", second[0:4])
        self.assertEqual((predictor, index), state_before)

    def test_bcmedia_packet_layout(self) -> None:
        block = AdpcmEncoder().encode_block(sine(0.064))
        packet = bcmedia_adpcm_packet(block)

        magic, size, size_again, data_magic, block_size = struct.unpack("<IHHHH", packet[0:12])
        self.assertEqual(magic, 0x62773130)
        self.assertEqual(size, len(block) + 4)
        self.assertEqual(size_again, size)
        self.assertEqual(data_magic, 0x0100)
        self.assertEqual(block_size, (len(block) - 4) // 2)
        self.assertEqual(packet[12 : 12 + len(block)], block)
        self.assertEqual(len(packet) % 8, 0, "BcMedia packets are padded to 8 bytes")


class TestTalk(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.baichuan = Baichuan(
            host="127.0.0.1",
            username="user",
            password="password",
            http_api=SimpleNamespace(nvr_name="test", _updating=False),
        )
        self.baichuan._logged_in = True  # noqa: SLF001
        self.baichuan._connect_if_needed = AsyncMock()  # type: ignore[method-assign]
        self.baichuan._aes_encrypt = Mock(side_effect=lambda body: body)  # type: ignore[method-assign]

        self.sent: list[tuple[int, str]] = []  # (cmd_id, body) of every command
        self.audio: list[bytes] = []  # raw writes of the audio command
        self.responses: dict[int, Exception | None] = {}

        async def _send(cmd_id: int, channel=None, body: str = "", **kwargs) -> str:
            self.sent.append((cmd_id, body))
            error = self.responses.get(cmd_id)
            if error is not None:
                raise error
            return TALK_ABILITY_XML if cmd_id == 10 else ""

        self.baichuan.send = AsyncMock(side_effect=_send)  # type: ignore[method-assign]
        self.baichuan._connection = SimpleNamespace(  # type: ignore[assignment]
            send_without_wait=AsyncMock(side_effect=lambda data, cmd_id=None: self.audio.append(data))
        )

    async def test_get_talk_ability_reads_the_audio_format(self) -> None:
        ability = await self.baichuan.get_talk_ability(2)

        assert ability is not None
        self.assertEqual(ability.audio_type, "adpcm")
        self.assertEqual(ability.sample_rate, 16000)
        self.assertEqual(ability.length_per_encoder, 1024)
        self.assertEqual(ability.duplex, "FDX")
        self.assertEqual(ability.sound_track, "mono")

    async def test_get_talk_ability_without_adpcm_returns_none(self) -> None:
        self.baichuan.send = AsyncMock(return_value=TALK_ABILITY_XML_NO_ADPCM)  # type: ignore[method-assign]

        self.assertIsNone(await self.baichuan.get_talk_ability(2))

    async def test_get_talk_ability_of_a_channel_without_speaker_returns_none(self) -> None:
        self.baichuan.send = AsyncMock(side_effect=ApiError("not supported", rspCode=400))  # type: ignore[method-assign]

        self.assertIsNone(await self.baichuan.get_talk_ability(1))

    async def test_talk_starts_a_session_streams_audio_and_stops(self) -> None:
        await self.baichuan.talk(2, sine(0.128))  # two blocks

        commands = [cmd_id for cmd_id, _ in self.sent]
        self.assertEqual(commands, [10, 201, 11], "expected ability, config and stop")
        self.assertIn("<audioType>adpcm</audioType>", self.sent[1][1])
        self.assertIn("<channelId>2</channelId>", self.sent[1][1])
        self.assertEqual(len(self.audio), 2, "one command per ADPCM block")

    async def test_audio_command_uses_stream_type_zero(self) -> None:
        # mess_id (3 bytes LE) is [stream_type][msg_num lo][msg_num hi] and the
        # camera silently drops the audio unless stream_type is zero
        await self.baichuan.talk(2, sine(0.064))

        written = self.audio[0]
        self.assertEqual(int.from_bytes(written[4:8], byteorder="little"), 202)
        self.assertEqual(written[12], 2 + 1, "channel id is the channel plus one")
        self.assertEqual(written[13], 0, "stream type has to be zero for talk")

    async def test_audio_payload_is_not_encrypted(self) -> None:
        self.baichuan._aes_encrypt = Mock(side_effect=lambda body: b"\x00" * len(body))  # type: ignore[method-assign]

        await self.baichuan.talk(2, sine(0.064))

        written = self.audio[0]
        payload_offset = int.from_bytes(written[20:24], byteorder="little")
        payload = written[24 + payload_offset :]
        self.assertEqual(int.from_bytes(payload[0:4], byteorder="little"), 0x62773130, "payload must stay raw BcMedia")

    async def test_a_running_session_of_another_client_is_reported_not_taken_over(self) -> None:
        self.responses[201] = ApiError("already talking", rspCode=422)

        with self.assertRaises(ApiError) as caught:
            await self.baichuan.talk(2, sine(0.064))

        self.assertEqual(caught.exception.rspCode, 422)
        self.assertIn("another client is already talking", str(caught.exception))
        self.assertNotIn(11, [cmd_id for cmd_id, _ in self.sent], "must not stop the session of another client")
        self.assertEqual(self.audio, [], "no audio may be sent when the session was refused")

    async def test_other_errors_are_passed_through(self) -> None:
        self.responses[201] = ApiError("bad request", rspCode=400)

        with self.assertRaises(ApiError) as caught:
            await self.baichuan.talk(2, sine(0.064))

        self.assertEqual(caught.exception.rspCode, 400)

    async def test_session_is_stopped_even_when_sending_audio_fails(self) -> None:
        self.baichuan._connection.send_without_wait = AsyncMock(side_effect=OSError("connection lost"))

        with self.assertRaises(OSError):
            await self.baichuan.talk(2, sine(0.064))

        self.assertIn(11, [cmd_id for cmd_id, _ in self.sent])


if __name__ == "__main__":
    unittest.main()
