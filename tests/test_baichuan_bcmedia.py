import random
import unittest

from reolink_aio.baichuan.util import BcMediaStreamParser


def _video_chunk(magic: bytes, payload: bytes, add_header: bytes = b"") -> bytes:
    """Build a BcMedia video chunk (magic + "H264" + header + payload)."""
    return (
        magic
        + b"H264"
        + len(payload).to_bytes(4, "little")
        + len(add_header).to_bytes(4, "little")
        + (12345).to_bytes(4, "little")  # microseconds
        + (0).to_bytes(4, "little")  # unknown
        + add_header
        + payload
    )


def _audio_chunk(payload: bytes) -> bytes:
    """Build a BcMedia audio chunk (8 byte header + payload), should be skipped."""
    return b"05wb" + len(payload).to_bytes(2, "little") + b"\x00\x00" + payload


I_FRAME = b"\x00\x00\x00\x01\x67sps\x00\x00\x00\x01\x68pps\x00\x00\x00\x01\x65" + bytes(
    range(200)
)
P_FRAME = b"\x00\x00\x00\x01\x41" + bytes(range(100))
STREAM_INFO = b"1001" + bytes(28)  # stream info header, must be skipped
PADDING = b"\x00\x00\x00\x00"  # inter-chunk padding, must be skipped


class TestBcMediaStreamParser(unittest.TestCase):
    @staticmethod
    def _build_stream() -> bytes:
        parts = [
            STREAM_INFO,
            _video_chunk(b"00dc", I_FRAME, add_header=b"\xaa\xbb\xcc\xdd"),
            _audio_chunk(b"some-aac-audio-data"),
            _video_chunk(b"01dc", P_FRAME),
            _video_chunk(b"01dc", P_FRAME + b"second"),
        ]
        return PADDING.join(parts)

    def test_extracts_video_frames_and_keyframe_flags(self) -> None:
        frames = BcMediaStreamParser().push(self._build_stream())
        # audio and stream-info are skipped, only the 3 video frames remain
        self.assertEqual(
            [frame for frame, _ in frames], [I_FRAME, P_FRAME, P_FRAME + b"second"]
        )
        # "00dc" is a key-frame, "01dc" is not
        self.assertEqual([key_frame for _, key_frame in frames], [True, False, False])

    def test_identical_across_fixed_chunk_boundaries(self) -> None:
        data = self._build_stream()
        baseline = BcMediaStreamParser().push(data)
        for size in (1, 2, 3, 7, 13, 64, 1500):
            parser = BcMediaStreamParser()
            frames = []
            for idx in range(0, len(data), size):
                frames += parser.push(data[idx : idx + size])
            self.assertEqual(
                frames, baseline, f"chunk size {size} gave a different result"
            )

    def test_identical_across_random_chunk_boundaries(self) -> None:
        data = self._build_stream()
        baseline = BcMediaStreamParser().push(data)
        random.seed(42)
        parser = BcMediaStreamParser()
        frames = []
        idx = 0
        while idx < len(data):
            length = random.randint(1, 50)
            frames += parser.push(data[idx : idx + length])
            idx += length
        self.assertEqual(frames, baseline)

    def test_incomplete_frame_is_buffered_until_complete(self) -> None:
        data = self._build_stream()
        parser = BcMediaStreamParser()
        # feed everything except the last byte: the final frame must not be emitted yet
        partial = parser.push(data[:-1])
        self.assertEqual([frame for frame, _ in partial], [I_FRAME, P_FRAME])
        # the last byte completes the final frame
        rest = parser.push(data[-1:])
        self.assertEqual([frame for frame, _ in rest], [P_FRAME + b"second"])


if __name__ == "__main__":
    unittest.main()
