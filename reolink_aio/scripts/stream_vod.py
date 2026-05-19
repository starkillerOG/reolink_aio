"""Stream a Reolink VOD recording as H.264 Annex-B to stdout.

Intended as a ``go2rtc`` ``exec:`` source for baichuan_only cameras that do not
support HTTP download or RTSP VOD.  The script connects to the camera via the
Baichuan TCP protocol, retrieves the requested recording, and writes raw H.264
Annex-B frames to stdout **at the real-time rate implied by the BcMedia
microsecond timestamps**.  Because go2rtc assigns RTP timestamps from its own
wall clock, rate-limiting the output is enough to achieve correct playback speed
without any ``setpts`` manipulation.

Usage::

    reolink-stream-vod HOST USER PASS CHANNEL FILE_NAME START_TIME [STREAM_TYPE]

    HOST        Camera hostname or IP address
    USER        Username
    PASS        Password
    CHANNEL     Channel index (integer, e.g. 0)
    FILE_NAME   Recording filename returned by get_recordings_for_day()
    START_TIME  Recording start time as ISO 8601 string (e.g. "2026-02-22T22:41:42")
    STREAM_TYPE Optional: "mainStream" (default) or "subStream"

Example go2rtc configuration (go2rtc.yaml)::

    streams:
      reolink_ch0_0120260222224142:
        - exec:reolink-stream-vod 192.168.1.10 admin secret 0 0120260222224142 2026-02-22T22:41:42

The stream name can then be used as an HLS/WebRTC source in Home Assistant or any
other go2rtc consumer.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime

from ..api import Host

_LOGGER = logging.getLogger(__name__)

# u32 microsecond timestamp wraps at 2^32 µs ≈ 71.6 minutes.
_U32_MASK = 0xFFFFFFFF


async def _stream(
    host_addr: str,
    user: str,
    password: str,
    channel: int,
    file_name: str,
    start_time: datetime,
    stream_type: str,
) -> None:
    """Connect to the camera and write H.264 frames to stdout at real-time rate."""
    out = sys.stdout.buffer

    host = Host(host_addr, user, password)
    try:
        # Skip get_host_data()/get_states() — the Baichuan TCP layer authenticates
        # itself lazily on the first command, so no HTTP round-trips are needed.
        first_us: int | None = None
        wall_start: float | None = None

        async for us, h264 in host.stream_recording_bc(channel, file_name, start_time, stream_type):
            if first_us is None:
                # Anchor real-time clock to the first BcMedia timestamp.
                first_us = us
                wall_start = time.monotonic()
            else:
                # Elapsed recording time (µs), with u32 wraparound handled.
                elapsed_bc_us = (us - first_us) & _U32_MASK
                target = wall_start + elapsed_bc_us / 1_000_000
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

            try:
                out.write(h264)
                out.flush()
            except BrokenPipeError:
                # Consumer (go2rtc) closed the pipe — stop streaming.
                _LOGGER.debug("stdout pipe closed, stopping stream")
                break

    finally:
        await host.logout()


def main() -> None:
    """Entry point for the ``reolink-stream-vod`` console script."""
    args = sys.argv[1:]
    if len(args) not in (6, 7):
        print(
            f"Usage: {sys.argv[0]} HOST USER PASS CHANNEL FILE_NAME START_TIME [STREAM_TYPE]",
            file=sys.stderr,
        )
        sys.exit(1)

    host_addr, user, password, channel_str, file_name, start_time_str = args[:6]
    stream_type = args[6] if len(args) == 7 else "mainStream"

    try:
        channel = int(channel_str)
    except ValueError:
        print(f"CHANNEL must be an integer, got: {channel_str!r}", file=sys.stderr)
        sys.exit(1)

    try:
        start_time = datetime.fromisoformat(start_time_str)
    except ValueError as exc:
        print(f"START_TIME must be an ISO 8601 datetime string: {exc}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    try:
        asyncio.run(_stream(host_addr, user, password, channel, file_name, start_time, stream_type))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
