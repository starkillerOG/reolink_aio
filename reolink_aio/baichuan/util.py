"""Reolink Baichuan constants and utility functions"""

import asyncio
import logging
from collections.abc import Callable
from enum import Enum
from hashlib import md5
from itertools import cycle
from typing import Any, TypeVar, overload
from xml.etree import ElementTree as XML

from ..exceptions import InvalidParameterError, UnexpectedDataError

DEFAULT_BC_PORT = 9000
HEADER_MAGIC = "f0debc0a"

XML_KEY = [0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF]
UDP_KEY = [
    0x1f2d3c4b, 0x5a6c7f8d, 0x38172e4b, 0x8271635a,
    0x863f1a2b, 0xa5c6f7d8, 0x8371e1b4, 0x17f2d3a5,
]  # fmt: skip
AES_IV = b"0123456789abcdef"


T = TypeVar("T")
_LOGGER = logging.getLogger(__name__)


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
    """Decrypt a received message using the baichuan TCP protocol"""
    offset = offset % 256
    decrypted = ""
    for idx, byte in enumerate(buf):
        key = XML_KEY[(offset + idx) % len(XML_KEY)]
        char = byte ^ key ^ (offset)
        decrypted += chr(char)
    return decrypted


def encrypt_baichuan(buf: str, offset: int) -> bytes:
    """Encrypt a message using the baichuan TCP protocol before sending"""
    if offset > 255:
        raise InvalidParameterError(f"Baichuan encryption offset {offset} can not be larger than 255")

    encrypt = b""
    for idx, char in enumerate(buf):
        key = XML_KEY[(offset + idx) % len(XML_KEY)]
        byte = ord(char) ^ key ^ (offset)
        encrypt += byte.to_bytes(1, "big")
    return encrypt


def decrypt_udp_baichuan(buf: bytes, offset: int) -> str:
    """Decrypt a received message using the baichuan UDP protocol"""
    # Use cycle to repeat the UDP_KEY indefinetly and XOR with the buffer
    key_bytes: list[int] = []
    for key_byte in UDP_KEY:
        shift_k_byte = (key_byte + offset) & 0xFFFFFFFF  # chop to uint32 size
        key_bytes.extend(shift_k_byte.to_bytes(4, "little"))

    return bytes(byte ^ k_byte for byte, k_byte in zip(buf, cycle(key_bytes))).decode("utf8")


def encrypt_udp_baichuan(buf: str, offset: int) -> bytes:
    """Encrypt a message using the baichuan UDP protocol before sending"""
    # Use cycle to repeat the UDP_KEY indefinetly and XOR with the buffer
    key_bytes: list[int] = []
    for key_byte in UDP_KEY:
        shift_k_byte = (key_byte + offset) & 0xFFFFFFFF  # chop to uint32 size
        key_bytes.extend(shift_k_byte.to_bytes(4, "little"))

    return bytes(byte ^ k_byte for byte, k_byte in zip(buf.encode("utf8"), cycle(key_bytes)))


def calc_crc(data: bytes) -> bytes:
    """
    CRC32 checksum for the baichuan UDP protocol
    Poly=0xEDB88320, Init=0x00, XorOut=0x00
    This is the little endian variant of the Poly=0x04C11DB7
    """
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1
    return crc.to_bytes(4, byteorder="little")


def md5_str_modern(string: str) -> str:
    """Get the MD5 hex hash of a string according to the baichuan protocol"""
    enc_str = string.encode("utf8")
    md5_bytes = md5(enc_str).digest()
    md5_hex = md5_bytes.hex()[0:31]
    md5_HEX = md5_hex.upper()
    return md5_HEX


@overload
def get_value_from_xml_element(xml_element: XML.Element, key: str) -> str | None: ...
@overload
def get_value_from_xml_element(xml_element: XML.Element, key: str, type_class: type[T]) -> T | None: ...
@overload
def get_value_from_xml_element(xml_element: XML.Element, key: str, type_class: type[T], recursive: bool) -> T | None: ...


def get_value_from_xml_element(xml_element: XML.Element, key: str, type_class=str, recursive: bool = True):
    """Get a value for a key in a xml element"""
    xml_value = xml_element.find(f".//{key}" if recursive else key)
    if xml_value is None:
        return None
    value: str | int | None = xml_value.text
    if value is None:
        return None
    try:
        if type_class == bool:
            value = int(value)
        return type_class(value)
    except ValueError as err:
        _LOGGER.debug(err)
        return None


def get_keys_from_xml(xml: str | XML.Element, keys: list[str] | dict[str, tuple[str, type]], recursive: bool = True) -> dict[str, Any]:
    """Get multiple keys from a xml and return as a dict"""
    if isinstance(xml, str):
        root = XML.fromstring(xml)
    else:
        root = xml
    result: dict[str, Any] = {}
    for key in keys:
        value: str | int | None = get_value_from_xml_element(root, key, str, recursive)
        if value is None:
            continue
        if isinstance(keys, dict):
            new_key, type_class = keys[key]
            if type_class == bool:
                value = int(value)
            result[new_key] = type_class(value)
        else:
            result[key] = value

    return result


def get_value_from_xml(xml: str, key: str) -> str | None:
    """Get the value of a single key in a xml"""
    return get_keys_from_xml(xml, [key]).get(key)


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
