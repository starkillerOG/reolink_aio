"""IMA/DVI4 ADPCM encoding for Reolink two-way audio (talk)

Reolink cameras only accept ADPCM audio for the talk feature, framed in
BcMedia packets. The block layout is the standard IMA ADPCM one: a 4 byte
header with the predictor state, followed by one nibble per sample.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable

BCMEDIA_ADPCM_MAGIC = 0x62773130
BCMEDIA_ADPCM_DATA_MAGIC = 0x0100
BCMEDIA_PAD_SIZE = 8

STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41,
    45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190,
    209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724,
    796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272,
    2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132,
    7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500,
    20350, 22385, 24623, 27086, 29794, 32767,
)
INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)


class AdpcmEncoder:
    """IMA/DVI4 ADPCM encoder, the predictor state runs across blocks."""

    def __init__(self) -> None:
        self.predictor = 0
        self.index = 0

    def _encode_sample(self, sample: int) -> int:
        step = STEP_TABLE[self.index]
        diff = sample - self.predictor
        nibble = 0
        if diff < 0:
            nibble = 8
            diff = -diff

        delta = step >> 3
        if diff >= step:
            nibble |= 4
            diff -= step
            delta += step
        if diff >= step >> 1:
            nibble |= 2
            diff -= step >> 1
            delta += step >> 1
        if diff >= step >> 2:
            nibble |= 1
            delta += step >> 2

        self.predictor += -delta if nibble & 8 else delta
        self.predictor = max(-32768, min(32767, self.predictor))
        self.index = max(0, min(len(STEP_TABLE) - 1, self.index + INDEX_TABLE[nibble]))
        return nibble

    def encode_block(self, samples: Iterable[int]) -> bytes:
        """Encode one ADPCM block: 4 byte header followed by the nibbles"""
        samples = list(samples)
        block = bytearray(struct.pack("<hBB", self.predictor, self.index, 0))
        for i in range(0, len(samples), 2):
            low = self._encode_sample(samples[i])
            high = self._encode_sample(samples[i + 1]) if i + 1 < len(samples) else 0
            block.append((high << 4) | low)
        return bytes(block)


def bcmedia_adpcm_packet(block: bytes) -> bytes:
    """Wrap an ADPCM block in a BcMedia packet, padded to 8 bytes"""
    size = len(block) + 4  # data + 2 byte magic + 2 byte block size
    packet = struct.pack(
        "<IHHHH",
        BCMEDIA_ADPCM_MAGIC,
        size,
        size,
        BCMEDIA_ADPCM_DATA_MAGIC,
        (len(block) - 4) // 2,  # block size without the header, halved
    ) + block
    return packet + b"\x00" * (-len(packet) % BCMEDIA_PAD_SIZE)
