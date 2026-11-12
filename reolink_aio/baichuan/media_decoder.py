"""Reolink Baichuan media stream decoder"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterable, Iterator
from contextlib import aclosing
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

STREAM_READ_SIZE = 65536  # 64 KB
STREAM_MAX_CHUNK_SIZE = 262144  # 256 KB, decoding and muxing runs in the event loop, larger chunks are split so a single step does not block the event loop for too long
STREAM_MAX_AUDIO_WAIT = 2_000_000  # microseconds of video to buffer at the start of streaming, waiting for audio to be known for the MPEG-TS header
AUDIO_RESYNC = 90000  # 1 second in 90 kHz units, move the audio forward to the video when it is further behind (gap between recordings)
MP4_MAX_HEADER_SIZE = 1048576  # 1 MB, max mp4 data to buffer while waiting for the moov header to write the duration in
HLS_PLAYLIST = "playlist.m3u8"
HLS_INIT = "init.mp4"
HLS_SEGMENT = "{index}.m4s"
HLS_SEGMENT_LENGTH = 6.0  # target length of a segment in seconds


class MediaStream:
    """Decoded Baichuan media stream"""

    def __init__(self, first_i_frame_only: bool = False) -> None:
        self.first_i_frame_only = first_i_frame_only  # stop decoding after the first i-frame
        self.width: int = 0
        self.height: int = 0
        self.start_time: datetime | None = None
        self.video_codec: str | None = None
        self.audio_codec: str | None = None
        self.video_frames: list[bytes] = []
        self.frame_us: list[int] = []  # frame_microseconds
        self.i_frame_indices: list[int] = []
        self.audio_frames: list[bytes] = []
        self.audio_us: list[int] = []  # timestamp of the last video frame when the audio frame was received
        self.skipped_bytes: int = 0
        self.duplicate_frames: int = 0
        self.skipped_audio_frames: int = 0
        # decoder state
        self._pending: list[bytes] = []  # received data not yet decoded
        self._pending_len: int = 0
        self._needed: int = 8  # number of pending bytes needed to decode the next item
        self._first_i_frame_us: int | None = None  # timestamp of the first i-frame
        self._keyframe_times: deque[float] = deque()  # seconds since the first i-frame of the i-frames not yet processed by stream_hls
        # muxer state
        self._muxer: MpegTsMuxer | None = None
        self._start_frame_us: int = 0  # timestamp of the first video frame
        self._audio_duration: int = 0  # duration of an audio frame in 90 kHz units, 0 if audio is not muxed
        self._audio_pts: int = TS_PTS_OFFSET  # timestamp of the next audio frame in 90 kHz units

    def decode_chunk(self, chunk: bytes, final: bool = True) -> None:
        """Decode a chunk of Baichuan media stream, final=True (default) for the last chunk or a complete stream."""
        if self.first_i_frame_only and self.i_frame_indices:
            return
        if chunk:
            self._pending.append(chunk)
            self._pending_len += len(chunk)
        if self._pending_len < self._needed and not final:
            return  # not enough bytes, wait for more

        data = b"".join(self._pending)
        self._needed = 8
        idx = 0
        while idx + 8 <= len(data):
            (magic,) = struct.unpack_from("<4s", data, idx)
            if magic in MAGIC_STREAM_HEADER:
                # parse stream header
                if idx + 32 > len(data):
                    self._needed = 32
                    break
                self.width, self.height = struct.unpack_from("<II", data, idx + 8)
                idx += 32
            elif magic in MAGIC_VIDEO:
                # parse frame header
                if idx + 24 > len(data):
                    self._needed = 24
                    break
                codec, frame_len, add_header_len, microseconds = struct.unpack_from("<4sIII", data, idx + 4)
                start = idx + 24 + add_header_len
                end = start + frame_len
                next_idx = end + (-frame_len % 8)  # frames are padded to 8 bytes
                if next_idx > len(data):
                    self._needed = next_idx - idx
                    break
                frame = data[start:end]
                if self.video_frames and frame == self.video_frames[-1]:
                    # camera sometimes sends the same frame twice, skip the duplicate
                    self.duplicate_frames += 1
                    idx = next_idx
                    continue
                self.video_codec = codec.decode("utf8", errors="replace")
                if self.start_time is None and add_header_len >= 4 and idx + 28 <= len(data):
                    try:
                        self.start_time = datetime.fromtimestamp(struct.unpack_from("<I", data, idx + 24)[0])
                    except (OverflowError, OSError, ValueError):
                        pass
                if magic == MAGIC_VIDEO[0]:
                    self.i_frame_indices.append(len(self.video_frames))
                    if self._first_i_frame_us is None:
                        self._first_i_frame_us = microseconds
                    self._keyframe_times.append(((microseconds - self._first_i_frame_us) & 0xFFFFFFFF) / 1e6)  # the timestamp can wrap around
                self.video_frames.append(frame)
                self.frame_us.append(microseconds)
                idx = next_idx
                if self.first_i_frame_only and self.i_frame_indices:
                    break
            elif magic in MAGIC_AUDIO:
                # parse audio header
                (frame_len,) = struct.unpack_from("<H", data, idx + 4)
                start = idx + 8
                end = start + frame_len
                next_idx = end + (-frame_len % 8)  # frames are padded to 8 bytes
                if next_idx > len(data):
                    self._needed = next_idx - idx
                    break
                self.audio_codec = MAGIC_AUDIO[magic]
                if self.video_frames:
                    self.audio_frames.append(data[start:end])
                    self.audio_us.append(self.frame_us[-1])
                else:
                    # pre-record audio sent before the first video frame, drop it to keep audio in sync with video
                    self.skipped_audio_frames += 1
                idx = next_idx
            else:
                # unknown data, search next magic, if not found keep the last 3 bytes since a magic could be split over two chunks
                found = [pos for search in MAGICS if (pos := data.find(search, idx + 1)) != -1]
                next_idx = min(found, default=len(data) - 3)
                self.skipped_bytes += next_idx - idx
                idx = next_idx

        done = self.first_i_frame_only and self.i_frame_indices
        if final and not done:
            # incomplete (truncated) data at the end of the stream
            self.skipped_bytes += len(data) - idx
        rest = b"" if final or done else data[idx:]
        self._pending = [rest] if rest else []
        self._pending_len = len(rest)

        if not final:
            return
        if self.skipped_bytes:
            _LOGGER.debug("Media stream decoder skipped %s bytes of unknown data", self.skipped_bytes)
        if self.duplicate_frames:
            _LOGGER.debug("Media stream decoder skipped %s duplicate video frames", self.duplicate_frames)
        if self.skipped_audio_frames:
            _LOGGER.debug("Media stream decoder dropped %s audio frames received before the first video frame", self.skipped_audio_frames)

    async def to_jpeg(self, ffmpeg: str = "ffmpeg") -> bytes:
        """Convert the first i-frame of the stream to a JPEG image using ffmpeg"""
        if not self.i_frame_indices:
            raise UnexpectedDataError("Media stream does not contain an i-frame to convert to a JPEG image")
        frame = self.video_frames[self.i_frame_indices[0]]
        return await asyncio.shield(_run_ffmpeg(["-i", "pipe:0", "-f", "image2", "pipe:1"], frame, ffmpeg, "Reolink frame to JPEG image"))

    async def stream_mp4(self, chunks: AsyncIterable[bytes], ffmpeg: str = "ffmpeg", duration: float | None = None) -> AsyncGenerator[bytes, None]:
        """
        Decode chunks of a Baichuan media stream and remux them (without re-encoding) into a fragmented mp4 using ffmpeg, yields the mp4 data as it becomes available.
        Frames are removed from this MediaStream once passed to ffmpeg to limit memory usage.
        duration: expected duration in seconds, written in the mp4 header so players show the full timeline from the start instead of a growing one.
        """
        # buffer the start of the mp4 until the moov header is complete, write the duration in it, then pass everything through
        header: bytes | None = b"" if duration is not None and duration > 0 else None
        async with aclosing(self._stream_mp4(chunks, ffmpeg)) as mp4_stream:
            async for data in mp4_stream:
                if header is None or duration is None:
                    yield data
                    continue
                header += data
                patched = _mp4_set_duration(header, duration)
                if patched is None:
                    if len(header) < MP4_MAX_HEADER_SIZE:
                        continue  # moov not complete yet
                    _LOGGER.debug("Media stream mp4 header not found in the first %s bytes, duration not set", len(header))
                    patched = header
                yield patched
                header = None
            if header:
                yield header  # stream ended before the moov was complete

    async def stream_hls(
        self, chunks: AsyncIterable[bytes], duration: float, ffmpeg: str = "ffmpeg", segment_length: float = HLS_SEGMENT_LENGTH
    ) -> AsyncGenerator[tuple[str, bytes], None]:
        """
        Decode chunks of a Baichuan media stream and convert them (using stream_mp4) into the files of an HLS VOD playlist.
        Yields (file name, content) while the chunks are being received: first HLS_PLAYLIST and HLS_INIT, then the media segments HLS_SEGMENT in order.
        duration: expected duration in seconds, the complete playlist is written at the start so a player can seek in the full timeline.

        The mp4 has one fragment (moof + mdat) per keyframe and nothing is re-encoded, a segment consists of whole fragments.
        The segments in the playlist all have the same duration: a whole number of times the time between the first two keyframes.
        Each fragment goes in the segment of its keyframe time, so segments stay at their place in the playlist when the keyframe interval changes.
        """
        init = b""  # ftyp + moov
        moof = b""
        pending: list[tuple[float, bytes]] = []  # (keyframe time, fragment) received but not yet added to a segment
        segment: list[bytes] = []  # fragments of the segment being received
        index = 0  # index of the segment being received
        N_segments = 0  # number of segments in the playlist, 0 until the playlist.m3u8 is written
        interval = 0.0  # seconds between the first two keyframes
        segment_duration = 0.0  # always a multiple of interval

        async with aclosing(self.stream_mp4(chunks, ffmpeg, duration)) as mp4_stream, aclosing(_mp4_stream_boxes(mp4_stream)) as boxes:
            async for box_type, box in boxes:
                if box_type in (b"ftyp", b"moov"):  # only occur 1x in the header
                    init += box
                elif box_type == b"moof":  # fragment header
                    moof = box
                elif box_type == b"mdat":  # fragment data
                    if not self._keyframe_times:
                        raise UnexpectedDataError("Media stream HLS: received more mp4 fragments than keyframes")
                    # ffmpeg writes a fragment for each keyframe, in the same order
                    pending.append((self._keyframe_times.popleft(), moof + box))
                    if not N_segments:
                        # yield the playlist.m3u8 and init.mp4, only runs once
                        if len(pending) < 2:
                            continue
                        interval = pending[1][0] - pending[0][0]
                        if interval <= 0:
                            raise UnexpectedDataError(f"Media stream HLS: unexpected time of {interval} s between the first two keyframes")
                        segment_duration = max(1, round(segment_length / interval)) * interval
                        # The recording starts at the first keyframe at/after the requested start, so it can be up to one interval shorter than requested.
                        # A segment in the playlist that is never received would stall the player, a longer recording is no problem,
                        # therefore make the duration 1 interval shorter.
                        duration_list = _hls_segment_durations(duration - interval, segment_duration)
                        N_segments = len(duration_list)
                        yield HLS_PLAYLIST, _hls_playlist(duration_list)
                        yield HLS_INIT, init
                    for time, fragment in pending:
                        # Half an interval of margin: a keyframe just before the start of a segment belongs to that segment.
                        # At most one segment further: no empty segments after a gap in the recording, the next segments catch up.
                        # The last segment of the playlist gets all remaining fragments.
                        time_index = int((time + interval / 2) // segment_duration)
                        if (next_index := min(time_index, index + 1, N_segments - 1)) > index:
                            yield HLS_SEGMENT.format(index=index), b"".join(segment)
                            segment = []
                            index = next_index
                        segment.append(fragment)
                    pending.clear()

        if pending:
            # only one fragment received: a single segment
            yield HLS_PLAYLIST, _hls_playlist([duration])
            yield HLS_INIT, init
            segment = [fragment for _time, fragment in pending]
        if not segment:
            raise UnexpectedDataError("Media stream HLS: stream ended without video")
        yield HLS_SEGMENT.format(index=index), b"".join(segment)

    async def _stream_mp4(self, chunks: AsyncIterable[bytes], ffmpeg: str = "ffmpeg") -> AsyncGenerator[bytes, None]:
        """Decode the chunks and remux them into a fragmented mp4 using ffmpeg"""
        # aclosing: stop decoding and reading the chunks on every exit, also on errors before ffmpeg is started
        async with aclosing(self._decode_mpegts(chunks)) as mpegts:
            # decode until the MPEG-TS header is muxed, the video codec is then known which is needed for the ffmpeg arguments
            first: list[bytes] = []
            async for data in mpegts:
                first.append(data)
                if self._muxer is not None:
                    break

            # delay_moov: write the moov after the first packets so it includes the AAC config generated by aac_adtstoasc
            movflags = "frag_keyframe+empty_moov+delay_moov+default_base_moof"
            # Safari only plays H265 tagged as hvc1 (ffmpeg default hev1), ffmpeg refuses this tag for H264
            tag = ["-tag:v", "hvc1"] if self.video_codec == "H265" else []
            args = ["-f", "mpegts", "-i", "pipe:0", "-c", "copy", *tag, "-bsf:a", "aac_adtstoasc", "-f", "mp4", "-movflags", movflags, "pipe:1"]
            process = await _start_ffmpeg(args, ffmpeg, "media stream to mp4")
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            writer = asyncio.create_task(self._write_to_ffmpeg(first, mpegts, process.stdin))
            stderr = asyncio.create_task(process.stderr.read())
            try:
                # the writer task feeds ffmpeg while the mp4 output is read here, ffmpeg pipes block when not read (backpressure)
                while data := await process.stdout.read(STREAM_READ_SIZE):
                    yield data
                await writer  # raise errors of the chunks iterator or the decoder
                if await process.wait() != 0:
                    raise UnexpectedDataError(f"Unexpected error while converting media stream to mp4 using ffmpeg: {(await stderr).decode(errors='replace')}")
            finally:
                # also runs when the consumer stops early or is cancelled
                writer.cancel()
                stderr.cancel()
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                await asyncio.gather(writer, return_exceptions=True)  # wait until the writer stopped, before aclosing closes the mpegts generator it reads

    async def _decode_mpegts(self, chunks: AsyncIterable[bytes]) -> AsyncGenerator[bytes, None]:
        """Decode the chunks and yield them muxed as MPEG-TS"""
        async for chunk in chunks:
            # guard against very large chunks: decode and mux at most STREAM_MAX_CHUNK_SIZE per step to not block the event loop for too long
            for offset in range(0, len(chunk), STREAM_MAX_CHUNK_SIZE):
                end = offset + STREAM_MAX_CHUNK_SIZE
                self.decode_chunk(chunk[offset:end], final=False)
                yield self._to_mpegts(final=False)
        self.decode_chunk(b"")
        yield self._to_mpegts()

    async def _write_to_ffmpeg(self, first: list[bytes], mpegts: AsyncGenerator[bytes, None], stdin: asyncio.StreamWriter) -> None:
        """Write the first already decoded MPEG-TS data and then the rest of the decoded chunks to ffmpeg stdin"""
        try:
            for data in first:
                stdin.write(data)
                await stdin.drain()
            first.clear()
            async for data in mpegts:
                stdin.write(data)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return  # ffmpeg stopped, its error is raised by stream_mp4
        finally:
            stdin.close()  # end of input for ffmpeg, also on errors so ffmpeg closes

    def _to_mpegts(self, final: bool = True) -> bytes:
        """
        Mux the decoded frames into an MPEG transport stream, keeping the timestamp of each video frame.
        For streaming call after each decode_chunk(final=False), muxed frames are removed (except the last video frame for duplicate detection).
        """
        header = b""
        video_idx = 1  # the last video frame was kept after the previous call and is already muxed
        if self._muxer is None:
            # audio needs to be known for the MPEG-TS header, wait for the first audio frame or STREAM_MAX_AUDIO_WAIT of video
            video_us = (self.frame_us[-1] - self.frame_us[0]) & 0xFFFFFFFF if self.frame_us else 0  # frame_us can wrap around (& 0xFFFFFFFF)
            if not (final or self.audio_frames or video_us >= STREAM_MAX_AUDIO_WAIT):
                return b""  # wait for more data, self.video_frames are kept
            if not self.video_frames:
                raise UnexpectedDataError("Media stream has no video frames")
            audio = self.audio_codec == "aac" and bool(self.audio_frames) and len(self.audio_frames[0]) > 2
            if self.audio_frames and not audio:
                _LOGGER.debug("Media stream audio codec '%s' not supported for mp4, dropping audio", self.audio_codec)
            self._muxer = MpegTsMuxer(self.video_codec, audio)
            self._start_frame_us = self.frame_us[0]
            self._audio_duration = _aac_frame_duration(self.audio_frames[0]) if audio else 0
            header = self._muxer.header()
            video_idx = 0
        muxer = self._muxer

        # frames as (pts, encoder_fn, data, order) sorted on timestamp
        start = self._start_frame_us
        video = zip(self.frame_us[video_idx:], self.video_frames[video_idx:])  # only references, bytes are not copied
        frames = [(_video_pts(us, start), muxer.video, frame, 0) for us, frame in video]
        if self._audio_duration:
            for us, frame in zip(self.audio_us, self.audio_frames):
                # audio timestamps are counted on, after a gap between recordings move them forward to the video timestamps
                video_pts = _video_pts(us, start)
                if video_pts - self._audio_pts > AUDIO_RESYNC:
                    self._audio_pts = video_pts
                frames.append((self._audio_pts, muxer.audio, frame, 1))
                self._audio_pts += self._audio_duration
        frames.sort(key=lambda frame: (frame[0], frame[3]))  # sort on pts (timestamp) first, if equal first video then audio.

        # remove the muxed frames to limit memory usage, keep last frame for duplicate detection and no audio before first frame check
        muxed = len(self.video_frames) - 1
        del self.video_frames[:muxed], self.frame_us[:muxed]
        self.i_frame_indices[:] = [idx - muxed for idx in self.i_frame_indices if idx >= muxed]
        self.audio_frames.clear()
        self.audio_us.clear()
        return header + b"".join(encoder_fn(pts, data) for pts, encoder_fn, data, _order in frames)


async def _run_ffmpeg(args: list[str], stdin: bytes, ffmpeg: str, description: str) -> bytes:
    """Run ffmpeg feeding stdin and return stdout"""
    process = await _start_ffmpeg(args, ffmpeg, description)
    stdout, stderr = await process.communicate(stdin)
    if process.returncode != 0:
        raise UnexpectedDataError(f"Unexpected error while converting {description} using ffmpeg: {stderr.decode(errors='replace')}")
    return stdout


async def _start_ffmpeg(args: list[str], ffmpeg: str, description: str) -> asyncio.subprocess.Process:
    """Start ffmpeg with stdin, stdout and stderr pipes"""
    try:
        return await asyncio.create_subprocess_exec(
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


def _mp4_set_duration(data: bytes, duration: float) -> bytes | None:
    """
    Write the duration (seconds) in the moov header of a fragmented mp4 written by ffmpeg: mvhd, tkhd and mdhd durations and an added mvex/mehd.
    Firefox uses the mehd, Chromium the track (tkhd/mdhd) durations.
    Returns None if the moov header is not complete yet, returns data unchanged if the duration can not be written.
    """
    moov = next(((start, end) for box_type, start, end in _mp4_boxes(data, 0, len(data)) if box_type == b"moov"), None)
    if moov is None or moov[1] > len(data):
        return None
    out = bytearray(data)
    mvex, value = 0, 0  # value: duration in the mvhd timescale, the mvhd is processed before the tkhd boxes
    # boxes of an empty moov are version 0 (32 bit fields): mvhd/mdhd timescale at offset 20 and duration at 24, tkhd duration at 28
    todo = [moov]
    while todo:
        parent_start, parent_end = todo.pop(0)
        for box_type, box, box_end in _mp4_boxes(data, parent_start + 8, parent_end):
            if box_type in (b"trak", b"mdia", b"mvex"):
                todo.append((box, box_end))
                if box_type == b"mvex":
                    mvex = box
            elif box_type == b"mehd":
                return data  # ffmpeg does not write a mehd for an empty moov, leave an unexpected one unchanged
            elif box_type in (b"mvhd", b"mdhd", b"tkhd"):
                box_value = value if box_type == b"tkhd" else round(duration * struct.unpack_from(">I", data, box + 20)[0])
                if data[box + 8] != 0 or box_value > 0xFFFFFFFF:
                    return data
                struct.pack_into(">I", out, box + (28 if box_type == b"tkhd" else 24), box_value)
                if box_type == b"mvhd":
                    value = box_value
    if not mvex or not value:
        return data
    # insert a mehd (movie extends header, fragment duration) in the mvex and increase the sizes of the mvex and moov that contain it
    pos = mvex + 8
    out[pos:pos] = struct.pack(">I4sII", 16, b"mehd", 0, value)
    for box in (mvex, moov[0]):
        # correct the length specified in the mvex and moov boxes for the added 16 bytes in the mvex.
        size = struct.unpack_from(">I", out, box)[0]
        struct.pack_into(">I", out, box, size + 16)
    return bytes(out)


def _mp4_boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Parse mp4 boxes as (type, start, end), the last box can be incomplete (end > len(data)), ffmpeg does not write 64 bit box sizes in the header"""
    while start + 8 <= end:
        size, box_type = struct.unpack_from(">I4s", data, start)
        if size < 8:
            return
        yield box_type, start, start + size
        start += size


async def _mp4_stream_boxes(mp4_stream: AsyncIterable[bytes]) -> AsyncGenerator[tuple[bytes, bytes], None]:
    """Split an mp4 stream into its top level boxes, yields (box type, complete box)"""
    buffer = bytearray()
    async for data in mp4_stream:
        buffer += data
        while len(buffer) >= 8:
            size, box_type = struct.unpack_from(">I4s", buffer)
            if size < 8:  # ffmpeg does not write 64 bit box sizes (size 1) for fragments of a few seconds
                raise UnexpectedDataError(f"Media stream HLS: unsupported mp4 box size {size}")
            if len(buffer) < size:
                break  # box not complete yet
            yield box_type, bytes(buffer[:size])
            del buffer[:size]


def _hls_segment_durations(duration: float, segment_duration: float) -> list[float]:
    """List of segment durations of the playlist: all segment_duration, the last one gets the rest (0.5 - 1.5 times segment_duration)"""
    N_segments = max(1, round(duration / segment_duration))
    last = max(duration - (N_segments - 1) * segment_duration, 0.001)
    return [segment_duration] * (N_segments - 1) + [last]


def _hls_playlist(extinf: list[float]) -> bytes:
    """HLS VOD playlist (playlist.m3u8) with the duration of each segment in seconds"""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{math.ceil(max(extinf))}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        f'#EXT-X-MAP:URI="{HLS_INIT}"',
    ]
    for index, seconds in enumerate(extinf):
        lines += [f"#EXTINF:{seconds:.3f},", HLS_SEGMENT.format(index=index)]
    lines.append("#EXT-X-ENDLIST")
    return ("\n".join(lines) + "\n").encode()


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
