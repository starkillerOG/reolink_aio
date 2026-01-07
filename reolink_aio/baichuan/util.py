"""Reolink Baichuan constants and utility functions"""

import asyncio
from collections.abc import Callable
from enum import Enum
from hashlib import md5

from ..exceptions import InvalidParameterError, UnexpectedDataError

DEFAULT_BC_PORT = 9000
HEADER_MAGIC = "f0debc0a"

XML_KEY = [0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF]
AES_IV = b"0123456789abcdef"


class EncType(Enum):
    """encoding types with messege class"""

    BC = "bc"
    AES = "aes"


class PortType(Enum):
    """communication port"""

    http = "http"
    https = "https"
    rtmp = "rtmp"
    rtsp = "rtsp"
    onvif = "onvif"


def decrypt_baichuan(buf: bytes, offset: int) -> str:
    """Decrypt a received message using the baichuan protocol"""
    offset = offset % 256
    decrypted = ""
    for idx, byte in enumerate(buf):
        key = XML_KEY[(offset + idx) % len(XML_KEY)]
        char = byte ^ key ^ (offset)
        decrypted += chr(char)
    return decrypted


def encrypt_baichuan(buf: str, offset: int) -> bytes:
    """Encrypt a message using the baichuan protocol before sending"""
    if offset > 255:
        raise InvalidParameterError(f"Baichuan encryption offset {offset} can not be larger than 255")

    encrypt = b""
    for idx, char in enumerate(buf):
        key = XML_KEY[(offset + idx) % len(XML_KEY)]
        byte = ord(char) ^ key ^ (offset)
        encrypt += byte.to_bytes(1, "big")
    return encrypt


def md5_str_modern(string: str) -> str:
    """Get the MD5 hex hash of a string according to the baichuan protocol"""
    enc_str = string.encode("utf8")
    md5_bytes = md5(enc_str).digest()
    md5_hex = md5_bytes.hex()[0:31]
    md5_HEX = md5_hex.upper()
    return md5_HEX


async def _i_frame_to_jpeg_shielded(frame: bytes, ffmpeg: str) -> bytes:
    """Convert a i-frame from a Reolink stream to a JPEG image"""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "image2", "pipe:1"]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # feed in the frame
    assert process.stdin
    try:
        process.stdin.write(frame)
        await process.stdin.drain()
    finally:
        if process.stdin:
            process.stdin.close()

    # read out the image
    assert process.stdout
    try:
        image = await process.stdout.read()
    finally:
        # Wait for process termination and check for errors.
        retcode = await process.wait()
        if retcode != 0:
            assert process.stderr
            stderr_data = await process.stderr.read()
            raise UnexpectedDataError(f"Unexpected error while converting Reolink frame to JPEG image using ffmpeg: {stderr_data.decode()}")

    return image


async def i_frame_to_jpeg(frame: bytes, ffmpeg: str = "ffmpeg") -> bytes:
    """Convert a i-frame from a Reolink stream to a JPEG image"""
    return await asyncio.shield(_i_frame_to_jpeg_shielded(frame, ffmpeg))


# Decorators
def http_cmd(cmd: str | list) -> Callable:
    def decorator_http_cmd(func):
        if isinstance(cmd, list):
            func.http_cmds = cmd
        else:
            func.http_cmds = [cmd]
        return func

    return decorator_http_cmd
