"""IMA ADPCM encoding and BcMedia framing for Reolink two-way audio (talk).

The camera speaker takes IMA/DVI-4 ADPCM audio over Baichuan cmd_id 202, with
every ADPCM block wrapped in a BcMedia "01wb" frame. The framing below matches
what the Reolink client sends.
"""

from __future__ import annotations

import asyncio
import struct

BCMEDIA_ADPCM_MAGIC = b"01wb"
BCMEDIA_ADPCM_SUB_MAGIC = 0x0100
BCMEDIA_ADPCM_HALF_BLOCK = 2  # the Reolink client always sends 2 in this field

# Standard IMA ADPCM tables
_STEP_TABLE: tuple[int, ...] = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
)  # fmt: skip
_INDEX_TABLE: tuple[int, ...] = (-1, -1, -1, -1, 2, 4, 6, 8)


class AdpcmEncoder:
    """Streaming IMA ADPCM encoder producing DVI-4 blocks.

    Each block is a 4 byte header (first sample as i16 LE, step index, 0)
    followed by `length_per_encoder` 4-bit codes, so a block holds
    `length_per_encoder + 1` samples (1025 samples = 64 ms at 16 kHz for the
    usual 1024). The step index carries over between blocks like one
    continuous stream. PCM that does not fill a whole block is kept for the
    next call to `encode`, or padded with silence by `flush`.
    """

    def __init__(self, length_per_encoder: int = 1024) -> None:
        if length_per_encoder <= 0 or length_per_encoder % 2:
            raise ValueError(f"length_per_encoder must be a positive even number, got {length_per_encoder}")
        self.samples_per_block = length_per_encoder + 1
        self._index = 0
        self._pending = b""

    def encode(self, pcm: bytes) -> list[bytes]:
        """Encode 16 bit signed little endian mono PCM into whole ADPCM blocks."""
        data = self._pending + pcm
        block_len = self.samples_per_block * 2
        n_blocks = len(data) // block_len
        self._pending = data[n_blocks * block_len :]
        return [self._encode_block(data[i * block_len : (i + 1) * block_len]) for i in range(n_blocks)]

    def flush(self) -> list[bytes]:
        """Encode the remaining PCM, padded with silence, into a last block."""
        pending = self._pending[: len(self._pending) // 2 * 2]
        self._pending = b""
        if not pending:
            return []
        return [self._encode_block(pending + b"\x00" * (self.samples_per_block * 2 - len(pending)))]

    def _encode_block(self, pcm_block: bytes) -> bytes:
        samples = struct.unpack(f"<{self.samples_per_block}h", pcm_block)
        predictor = samples[0]
        index = self._index
        block = bytearray(struct.pack("<hBB", predictor, index, 0))
        low: int | None = None
        for sample in samples[1:]:
            step = _STEP_TABLE[index]
            diff = sample - predictor
            code = 0
            if diff < 0:
                code = 8
                diff = -diff
            delta = step >> 3
            if diff >= step:
                code |= 4
                diff -= step
                delta += step
            if diff >= step >> 1:
                code |= 2
                diff -= step >> 1
                delta += step >> 1
            if diff >= step >> 2:
                code |= 1
                delta += step >> 2
            predictor = max(-32768, min(32767, predictor - delta if code & 8 else predictor + delta))
            index = max(0, min(88, index + _INDEX_TABLE[code & 7]))
            if low is None:
                low = code
            else:
                block.append(low | (code << 4))
                low = None
        self._index = index
        return bytes(block)


def bcmedia_adpcm_frame(block: bytes) -> bytes:
    """Wrap one ADPCM block in a BcMedia "01wb" audio frame.

    Layout: magic "01wb", u16 LE payload size (block + 4) twice, u16 LE 0x0100,
    u16 LE half block field, the ADPCM block, zero padding to 8 bytes.
    """
    size = len(block) + 4
    frame = BCMEDIA_ADPCM_MAGIC + struct.pack("<HHHH", size, size, BCMEDIA_ADPCM_SUB_MAGIC, BCMEDIA_ADPCM_HALF_BLOCK) + block
    return frame + b"\x00" * (-len(frame) % 8)


class TalkSession:
    """State of an active two-way audio (talk) session on one channel."""

    def __init__(self, full_mess_id: int, sample_rate: int, length_per_encoder: int) -> None:
        self.full_mess_id = full_mess_id
        self.encoder = AdpcmEncoder(length_per_encoder)
        self.block_time = self.encoder.samples_per_block / sample_rate
        self.lock = asyncio.Lock()
        self.ack_event = asyncio.Event()
        self.start: float | None = None  # time at which the first block (re)started playing
        self.blocks_sent = 0
        self.blocks_acked = 0

    def acknowledge(self) -> None:
        """The camera acknowledged a talk audio frame."""
        self.blocks_acked += 1
        self.ack_event.set()
