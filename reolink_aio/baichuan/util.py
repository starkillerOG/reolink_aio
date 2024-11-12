""" Reolink Baichuan constants and utility functions """

from enum import Enum
from hashlib import md5
from ..exceptions import InvalidParameterError

BC_PORT = 9000
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
