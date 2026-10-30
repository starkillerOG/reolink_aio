"""Reolink Baichuan media stream decoder"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime

from ..exceptions import InvalidParameterError, UnexpectedDataError

_LOGGER = logging.getLogger(__name__)

MAGIC_STREAM_HEADER = (b"1001", b"1002")
MAGIC_VIDEO = (b"00dc", b"01dc")  # i-frame, p-frame
MAGIC_AUDIO = {b"05wb": "aac", b"01wb": "adpcm"}
MAGICS = (*MAGIC_STREAM_HEADER, *MAGIC_VIDEO, *MAGIC_AUDIO)

# MPEG-TS constants
TS_PAYLOAD = 184  # 188 byte MPEG-TS packet minus 4 byte header
TS_PTS_OFFSET = 90000  # start timestamps at 1 second (only MPEG-TS), this allows audio which starts slightly earlier
PID_PMT, PID_VIDEO, PID_AUDIO = 0x1000, 0x100, 0x101
PES_START_CODE = b"\x00\x00\x01"
AAC_SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350, 16000, 16000, 16000)


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
            raise UnexpectedDataError("Media stream does not contain an i-frame to convert to a JPEG image")
        frame = self.video_frames[self.i_frame_indices[0]]
        return await asyncio.shield(_run_ffmpeg(["-i", "pipe:0", "-f", "image2", "pipe:1"], frame, ffmpeg, "Reolink frame to JPEG image"))

    async def to_mp4(self, ffmpeg: str = "ffmpeg") -> bytes:
        """Remux the stream (without re-encoding) into a fragmented mp4 using ffmpeg, returns the mp4 file content"""
        args = ["-f", "mpegts", "-i", "pipe:0", "-c", "copy", "-bsf:a", "aac_adtstoasc", "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1"]
        mpegts = await asyncio.get_running_loop().run_in_executor(None, self.to_mpegts)
        return await asyncio.shield(_run_ffmpeg(args, mpegts, ffmpeg, "media stream to mp4"))

    def to_mpegts(self) -> bytes:
        """Mux the stream into an MPEG transport stream, keeping the timestamp of each video frame"""
        if not self.video_frames:
            raise UnexpectedDataError("Media stream has no video frames")
        audio = self.audio_codec == "aac" and len(self.audio_frames[0]) > 2
        if self.audio_frames and not audio:
            _LOGGER.debug("Media stream audio codec '%s' not supported for mp4, dropping audio", self.audio_codec)
        muxer = MpegTsMuxer(self.video_codec, audio)

        # frames as (pts, encoder_fn, data, order) sorted on timestamp
        start = self.frame_microseconds[0]
        frames = [(_video_pts(us, start), muxer.video, frame, 0) for us, frame in zip(self.frame_microseconds, self.video_frames)]  # only references, bytes are not copied
        if audio:
            duration = _aac_frame_duration(self.audio_frames[0])
            frames.extend((TS_PTS_OFFSET + idx * duration, muxer.audio, frame, 1) for idx, frame in enumerate(self.audio_frames))
        frames.sort(key=lambda frame: (frame[0], frame[3]))  # sort on pts (timestamp) first, if equal first video then audio.
        return muxer.header() + b"".join(encoder_fn(pts, data) for pts, encoder_fn, data, _order in frames)


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


async def _run_ffmpeg(args: list[str], stdin: bytes, ffmpeg: str, description: str) -> bytes:
    """Run ffmpeg feeding stdin and return stdout"""
    try:
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
    except FileNotFoundError as err:
        raise InvalidParameterError(f"ffmpeg not found at '{ffmpeg}', ffmpeg needs to be installed for converting {description}") from err
    stdout, stderr = await process.communicate(stdin)
    if process.returncode != 0:
        raise UnexpectedDataError(f"Unexpected error while converting {description} using ffmpeg: {stderr.decode(errors='replace')}")
    return stdout


class MpegTsMuxer:
    """Incremental MPEG-TS muxer for one video and optionally one AAC audio stream, frames should be written in timestamp order"""

    def __init__(self, video_codec: str | None, audio: bool) -> None:
        stream_type = {"H264": 0x1B, "H265": 0x24}.get(video_codec or "")
        if stream_type is None:
            raise UnexpectedDataError(f"Unsupported video codec '{video_codec}' for MPEG-TS")
        self._streams = struct.pack(">BHH", stream_type, 0xE000 | PID_VIDEO, 0xF000)
        if audio:
            self._streams += struct.pack(">BHH", 0x0F, 0xE000 | PID_AUDIO, 0xF000)
        self._counters = {PID_VIDEO: 0, PID_AUDIO: 0}

    def header(self) -> bytes:
        """PAT and PMT tables, these need to be at the start of the stream"""
        # PAT (Program Association Table) on PID 0: program 1 is described by the PMT on PID_PMT
        pat = _ts_psi(0, 0x00, struct.pack(">HBBBHH", 1, 0xC1, 0, 0, 1, 0xE000 | PID_PMT))
        # PMT (Program Map Table): program 1 consists of the streams (codec + PID) in self._streams, PCR reference on the video PID
        pmt = _ts_psi(PID_PMT, 0x02, struct.pack(">HBBBHH", 1, 0xC1, 0, 0, 0xE000 | PID_VIDEO, 0xF000) + self._streams)
        return pat + pmt

    def video(self, pts: int, frame: bytes) -> bytes:
        """MPEG-TS packets of a video frame, pts in 90 kHz units"""
        return self._pes(PID_VIDEO, 0xE0, 0, pts, frame)

    def audio(self, pts: int, frame: bytes) -> bytes:
        """MPEG-TS packets of an AAC audio frame including ADTS header, pts in 90 kHz units"""
        return self._pes(PID_AUDIO, 0xC0, len(frame) + 8, pts, frame)

    def _pes(self, pid: int, stream_id: int, pes_len: int, pts: int, data: bytes) -> bytes:
        """Wrap data in a PES (Packetized Elementary Stream) packet and split it into MPEG-TS packets, stuffing goes in the first packet so all others are full"""
        payload = PES_START_CODE + struct.pack(">BHBBB", stream_id, pes_len, 0x80, 0x80, 5) + _ts_timestamp(pts) + data
        counter = self._counters[pid]
        # construct padding and first packet
        first_len = len(payload) % TS_PAYLOAD or TS_PAYLOAD
        gap = TS_PAYLOAD - first_len
        adaptation = b"" if gap == 0 else b"\x00" if gap == 1 else bytes([gap - 1, 0]) + b"\xff" * (gap - 2)
        packets = [struct.pack(">BHB", 0x47, 0x4000 | pid, (0x30 if adaptation else 0x10) | counter) + adaptation + payload[:first_len]]
        # split the rest of the payload in packets
        headers = [struct.pack(">BHB", 0x47, pid, 0x10 | idx) for idx in range(16)]
        starts = range(first_len, len(payload), TS_PAYLOAD)
        ends = range(first_len + TS_PAYLOAD, len(payload) + 1, TS_PAYLOAD)
        packets.extend(headers[(counter + idx) & 0x0F] + payload[start:end] for idx, (start, end) in enumerate(zip(starts, ends), 1))
        # update the counter for the next time and return
        self._counters[pid] = (counter + 1 + len(starts)) & 0x0F
        return b"".join(packets)


def _video_pts(microseconds: int, start: int) -> int:
    """MPEG-TS timestamp (90 kHz) of a video frame relative to the first frame, handles the 32 bit microsecond counter wrapping"""
    return TS_PTS_OFFSET + ((microseconds - start) & 0xFFFFFFFF) * 9 // 100


def _aac_frame_duration(frame: bytes) -> int:
    """Duration of an AAC frame in 90 kHz units, each frame holds 1024 samples, sample rate index from the ADTS header"""
    return 1024 * 90000 // AAC_SAMPLE_RATES[(frame[2] >> 2) & 0x0F]


def _crc32_mpeg(data: bytes) -> int:
    """CRC32 used in MPEG-TS tables, Poly=0x04C11DB7, Init=0xFFFFFFFF, not reflected"""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = (crc << 1) ^ 0x104C11DB7 if crc & 0x80000000 else crc << 1
    return crc


def _ts_psi(pid: int, table_id: int, body: bytes) -> bytes:
    """MPEG-TS packet holding a single PAT or PMT table"""
    section = struct.pack(">BH", table_id, 0xB000 | (len(body) + 4)) + body
    section += struct.pack(">I", _crc32_mpeg(section))
    return struct.pack(">BHBB", 0x47, 0x4000 | pid, 0x10, 0) + section + b"\xff" * (183 - len(section))


def _ts_timestamp(pts: int) -> bytes:
    """33 bit PES presentation timestamp"""
    return bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, ((pts >> 14) & 0xFE) | 1, (pts >> 7) & 0xFF, ((pts << 1) & 0xFE) | 1])
