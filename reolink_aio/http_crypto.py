"""Encrypted HTTP(s) API session ("Login Version 1" / HTTP Digest) as used by the Reolink web UI."""

from __future__ import annotations

import binascii
import hashlib
import re
import secrets
from base64 import b64decode, b64encode
from typing import Any

from Cryptodome.Cipher import AES  # type: ignore

DIGEST_URI = "cgi-bin/api.cgi?cmd=Login"
DIGEST_METHOD = "POST"
DIGEST_CNONCE_LEN = 48  # number of hex characters, same as the web UI
# count ids 0, 1 and 2 are reserved by the web UI for preview, playback and uploads
FIRST_API_COUNT_ID = 3


def md5_hex(string: str) -> str:
    """Get the lowercase hex MD5 digest of a string"""
    return hashlib.md5(string.encode("utf8")).hexdigest()  # noqa: S324


def parse_digest_challenge(header: str) -> dict[str, str]:
    """Parse the WWW-Authenticate: Digest header into a dict (keys lowercased)"""
    return {key.lower(): value.strip('"') for key, value in re.findall(r'(\w+)\s*=\s*("[^"]*"|[^,\s]*)', header.removeprefix("Digest").strip())}


class HttpCrypto:
    """AES-CFB encryption of the HTTP(s) API after a Version 1 (digest) login."""

    def __init__(self, username: str, password: str, nonce: str, cnonce: str) -> None:
        self._key = md5_hex(f"{nonce}-{password}-{cnonce}")[0:16].upper().encode("utf8")
        self._iv = md5_hex(f"webapp-{cnonce}-{password}-{nonce}-{username}")[0:16].upper().encode("utf8")
        self._count_id: int = 0
        self._count_val: int = 0

    def _cipher(self) -> Any:
        return AES.new(key=self._key, mode=AES.MODE_CFB, iv=self._iv, segment_size=128)

    def encrypt(self, data: str | bytes) -> str:
        """Encrypt (zero padded to the AES block size) and base64 encode"""
        raw = data.encode("utf8") if isinstance(data, str) else data
        raw += b"\0" * (-len(raw) % AES.block_size)
        return b64encode(self._cipher().encrypt(raw)).decode("ascii")

    def decrypt(self, data: str) -> str | None:
        """base64 decode and decrypt, returns None if the data is not a valid encrypted message"""
        try:
            raw = b64decode(data, validate=True)
            return self._cipher().decrypt(raw).rstrip(b"\0").decode("utf8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None

    def init_counts(self, token: dict[str, Any]) -> None:
        """Initialize the request counter from the Token object in the login response"""
        total = int(token.get("countTotal", 0))
        self._count_id = FIRST_API_COUNT_ID if total > FIRST_API_COUNT_ID else 0
        self._count_val = int(token.get("checkBasic", 0))

    def encrypted_query(self, params: dict[str, Any]) -> str:
        """Build the value of the 'encrypt' URL parameter, this contains all (other) URL parameters"""
        self._count_val += 1
        query = f"countId={self._count_id}&checkNum={self._count_val}"
        for key, value in params.items():
            query += f"&{key}={value}"
        return self.encrypt(query)


def digest_response(username: str, password: str, realm: str, nonce: str, nc: str, cnonce: str, qop: str) -> str:
    """Calculate the RFC 2617 (MD5, qop=auth) response of the digest login"""
    ha1 = md5_hex(f"{username}:{realm}:{password}")
    ha2 = md5_hex(f"{DIGEST_METHOD}:{DIGEST_URI}")
    return md5_hex(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")


def new_cnonce() -> str:
    """Generate a random client nonce"""
    return secrets.token_hex(DIGEST_CNONCE_LEN // 2)
