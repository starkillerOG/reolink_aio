"""Reolink Baichuan media stream decoder"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime

from ..exceptions import UnexpectedDataError

_LOGGER = logging.getLogger(__name__)

MAGIC_STREAM_HEADER = (b"1001", b"1002")
MAGIC_VIDEO = (b"00dc", b"01dc")  # i-frame, p-frame
MAGIC_AUDIO = {b"05wb": "aac", b"01wb": "adpcm"}
MAGICS = (*MAGIC_STREAM_HEADER, *MAGIC_VIDEO, *MAGIC_AUDIO)


@dataclass
class MediaStream:
    """Decoded Baichuan media stream"""

    width: int = 0
    height: int = 0
    start_time: datetime | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    video_frames: list[bytes] = field(default_factory=list)
    frame_microseconds: list[int] = field(default_factory=list)
    i_frame_indices: list[int] = field(default_factory=list)
    audio_frames: list[bytes] = field(default_factory=list)
    skipped_bytes: int = 0

    async def to_jpeg(self, ffmpeg: str = "ffmpeg") -> bytes:
        """Convert the first i-frame of the stream to a JPEG image using ffmpeg"""
        if not self.i_frame_indices:
            raise UnexpectedDataError("Media stream does not contain a i-frame to convert to a JPEG image")
        frame = self.video_frames[self.i_frame_indices[0]]
        return await asyncio.shield(_run_ffmpeg(["-i", "pipe:0", "-f", "image2", "pipe:1"], frame, ffmpeg, "Reolink frame to JPEG image"))


async def _run_ffmpeg(args: list[str], stdin: bytes, ffmpeg: str, description: str) -> bytes:
    """Run ffmpeg feeding stdin and return stdout"""
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(stdin)
    if process.returncode != 0:
        raise UnexpectedDataError(f"Unexpected error while converting {description} using ffmpeg: {stderr.decode(errors='replace')}")
    return stdout


def decode_media_stream(payload: bytes, first_i_frame_only: bool = False) -> MediaStream:
    """Decode a Baichuan media stream into its video and audio frames"""
    stream = MediaStream()
    idx = 0
    while idx + 8 <= len(payload):
        (magic,) = struct.unpack_from("<4s", payload, idx)
        if magic in MAGIC_STREAM_HEADER:
            # parse stream header
            if idx + 16 > len(payload):
                break
            stream.width, stream.height = struct.unpack_from("<II", payload, idx + 8)
            idx += 32
        elif magic in MAGIC_VIDEO:
            # parse frame header
            if idx + 24 > len(payload):
                break
            codec, frame_len, add_header_len, microseconds = struct.unpack_from("<4sIII", payload, idx + 4)
            stream.video_codec = codec.decode("utf8", errors="replace")
            stream.frame_microseconds.append(microseconds)
            if stream.start_time is None and add_header_len >= 4 and idx + 28 <= len(payload):
                try:
                    stream.start_time = datetime.fromtimestamp(struct.unpack_from("<I", payload, idx + 24)[0])
                except (OverflowError, OSError, ValueError):
                    pass
            if magic == MAGIC_VIDEO[0]:
                stream.i_frame_indices.append(len(stream.video_frames))
            # extract frame, frames are padded to 8 bytes
            idx += 24 + add_header_len
            end = idx + frame_len
            stream.video_frames.append(payload[idx:end])
            idx = end + (-frame_len % 8)
            if first_i_frame_only and stream.i_frame_indices:
                break
        elif magic in MAGIC_AUDIO:
            # parse audio header
            stream.audio_codec = MAGIC_AUDIO[magic]
            (frame_len,) = struct.unpack_from("<H", payload, idx + 4)
            idx += 8
            end = idx + frame_len
            stream.audio_frames.append(payload[idx:end])
            idx = end + (-frame_len % 8)
        else:
            # unknown data, search next magic
            found = [pos for search in MAGICS if (pos := payload.find(search, idx + 1)) != -1]
            next_idx = min(found, default=len(payload))
            stream.skipped_bytes += next_idx - idx
            idx = next_idx

    if idx > len(payload):
        _LOGGER.debug("Media stream truncated, last frame is %s bytes short", idx - len(payload))
    if stream.skipped_bytes:
        _LOGGER.debug("Media stream decoder skipped %s bytes of unknown data", stream.skipped_bytes)
    return stream
