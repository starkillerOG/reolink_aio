"""IMA ADPCM encoder and BcMedia frame builder for Reolink two-way audio.

The Reolink Baichuan protocol uses IMA ADPCM (DVI-4) encoding for two-way
audio (talk). Audio frames are wrapped in BcMedia headers before transmission.

Reference: neolink project (QuantumEntangledAndy/neolink) bcmedia module.
"""

from __future__ import annotations

import struct

# Standard IMA ADPCM step size table (89 entries, indexed 0-88)
_STEP_TABLE: list[int] = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
]

# Standard IMA ADPCM index adjustment table (16 entries, indexed by nibble 0-15)
_INDEX_TABLE: list[int] = [
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
]

# BcMedia ADPCM magic: ASCII "01wb" (little-endian u32 = 0x62773130)
BC_MEDIA_ADPCM_MAGIC = b"01wb"

# Sub-header magic for ADPCM frames
_BC_MEDIA_SUB_MAGIC = 0x0001


def encode_pcm_to_adpcm(
    pcm_data: bytes,
    samples_per_block: int = 1024,
) -> list[bytes]:
    """Convert 16-bit signed LE mono PCM to IMA ADPCM blocks.

    Each output block consists of a 4-byte preamble followed by nibble-packed
    ADPCM data. The preamble stores the encoder state (predictor and step
    index) at the start of the block.

    Block format::

        Offset  Size  Field
        0x00    2     predictor (i16 LE) — initial predicted sample value
        0x02    1     step_index (u8) — ADPCM step table index (0–88)
        0x03    1     reserved (u8) — always 0
        0x04    N     nibble data — packed low-nibble-first

    Args:
        pcm_data: Raw PCM audio (16-bit signed little-endian, mono).
        samples_per_block: PCM samples per ADPCM block. This corresponds to
            the ``lengthPerEncoder`` value from the camera's TalkAbility
            response. Typical values: 1024 (8 kHz) or 2048 (16 kHz).

    Returns:
        List of ADPCM blocks, each ``4 + samples_per_block // 2`` bytes.
    """
    num_samples = len(pcm_data) // 2
    if num_samples == 0:
        return []

    samples = struct.unpack(f"<{num_samples}h", pcm_data[: num_samples * 2])

    blocks: list[bytes] = []
    predictor = 0
    step_index = 0

    for block_start in range(0, num_samples, samples_per_block):
        block_samples = samples[block_start : block_start + samples_per_block]
        if not block_samples:
            break

        # Preamble: current encoder state before encoding this block
        preamble = struct.pack("<hBB", predictor, step_index, 0)

        # Encode each sample to a 4-bit nibble
        nibbles: list[int] = []
        for sample in block_samples:
            step = _STEP_TABLE[step_index]
            diff = sample - predictor

            # Determine sign bit (bit 3 of nibble)
            sign = 0
            if diff < 0:
                sign = 8
                diff = -diff

            # Quantize magnitude into bits 2, 1, 0
            code = 0
            threshold = step
            if diff >= threshold:
                code |= 4
                diff -= threshold
            threshold >>= 1
            if diff >= threshold:
                code |= 2
                diff -= threshold
            threshold >>= 1
            if diff >= threshold:
                code |= 1

            code |= sign
            nibbles.append(code)

            # Decode to update predictor (must match decoder exactly)
            decoded_diff = step >> 3
            if code & 4:
                decoded_diff += step
            if code & 2:
                decoded_diff += step >> 1
            if code & 1:
                decoded_diff += step >> 2

            if code & 8:
                predictor -= decoded_diff
            else:
                predictor += decoded_diff

            # Clamp predictor to 16-bit signed range
            if predictor > 32767:
                predictor = 32767
            elif predictor < -32768:
                predictor = -32768

            # Update step index
            step_index += _INDEX_TABLE[code]
            if step_index > 88:
                step_index = 88
            elif step_index < 0:
                step_index = 0

        # Pack nibbles into bytes (low nibble first in each byte)
        data = bytearray()
        for i in range(0, len(nibbles), 2):
            low = nibbles[i]
            high = nibbles[i + 1] if i + 1 < len(nibbles) else 0
            data.append((high << 4) | low)

        blocks.append(preamble + bytes(data))

    return blocks


def build_bc_media_frame(adpcm_block: bytes) -> bytes:
    """Wrap one ADPCM block in a BcMedia frame with 8-byte alignment padding.

    Frame layout::

        Offset  Size  Field
        0x00    4     magic — b"01wb" (0x62773130 LE)
        0x04    2     payload_size (LE u16) — len(data) + 4
        0x06    2     payload_size (LE u16) — duplicate
        0x08    2     sub_magic (LE u16) — 0x0001
        0x0A    2     half_block_size (LE u16) — (len(data) - 4) / 2
        0x0C    N     ADPCM data — preamble + encoded nibbles
        0x0C+N  P     zero padding to 8-byte alignment

    Args:
        adpcm_block: Complete ADPCM block (4-byte preamble + encoded data).

    Returns:
        BcMedia frame bytes ready for wire transmission.
    """
    data_len = len(adpcm_block)
    payload_size = data_len + 4  # sub_magic (2) + half_block_size (2)
    half_block = (data_len - 4) // 2  # ADPCM nibble bytes / 2

    header = struct.pack(
        "<4sHHHH",
        BC_MEDIA_ADPCM_MAGIC,
        payload_size,
        payload_size,
        _BC_MEDIA_SUB_MAGIC,
        half_block,
    )

    # Pad data to 8-byte boundary (per neolink serializer convention)
    padding_len = (8 - (data_len % 8)) % 8

    return header + adpcm_block + (b"\x00" * padding_len)
