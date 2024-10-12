""" Reolink Baichuan API """

import logging
import asyncio
from hashlib import md5
from xml.etree import ElementTree as XML
from enum import Enum
from Cryptodome.Cipher import AES

from . import xmls
from ..exceptions import (
    ApiError,
    InvalidContentTypeError,
    InvalidParameterError,
    ReolinkError,
    UnexpectedDataError,
    ReolinkConnectionError,
    ReolinkTimeoutError,
)

_LOGGER = logging.getLogger(__name__)

BC_PORT = 9000
HEADER_MAGIC = "f0debc0a"
RETRY_ATTEMPTS = 3

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
    decrypted = ""
    for idx, byte in enumerate(buf):
        key = XML_KEY[(offset + idx) % len(XML_KEY)]
        char = byte ^ key ^ (offset)
        decrypted += chr(char)
    return decrypted


def encrypt_baichuan(buf: str, offset: int) -> bytes:
    """Encrypt a message using the baichuan protocol before sending"""
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


class Baichuan:
    """Reolink Baichuan API class."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = BC_PORT,
    ) -> None:
        self._host: str = host
        self._port: int = port
        self._username: str = username
        self._password: str = password
        self._nonce: str | None = None
        self._user_hash: str | None = None
        self._password_hash: str | None = None
        self._aes_key: bytes | None = None

        # TCP connection
        self._mutex = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._logged_in: bool = False

        # states
        self._ports: dict[str, dict[str, int | bool]] = {}

    async def send(self, cmd_id: int, body: str = "", enc_type: EncType = EncType.AES, message_class: str = "1464", enc_offset: int = 0, retry: int = RETRY_ATTEMPTS) -> str:
        """Generic baichuan send method."""
        retry = retry - 1

        if not self._logged_in and cmd_id > 2:
            # not logged in and requesting a non login/logout cmd, first login
            await self.login()

        mess_len = len(body)
        payload_offset = 0

        cmd_id_bytes = (cmd_id).to_bytes(4, byteorder="little")
        mess_len_bytes = (mess_len).to_bytes(4, byteorder="little")
        enc_offset_bytes = (enc_offset).to_bytes(4, byteorder="little")
        payload_offset_bytes = (payload_offset).to_bytes(4, byteorder="little")

        if message_class == "1465":
            encrypt = "12dc"
            header = bytes.fromhex(HEADER_MAGIC) + cmd_id_bytes + mess_len_bytes + enc_offset_bytes + bytes.fromhex(encrypt + message_class)
        elif message_class == "1464":
            status_code = "0000"
            header = bytes.fromhex(HEADER_MAGIC) + cmd_id_bytes + mess_len_bytes + enc_offset_bytes + bytes.fromhex(status_code + message_class) + payload_offset_bytes
        else:
            raise InvalidParameterError(f"Baichuan host {self._host}: invalid param message_class '{message_class}'")

        enc_body_bytes = b""
        if mess_len > 0:
            if enc_type == EncType.BC:
                enc_body_bytes = encrypt_baichuan(body, enc_offset)
            elif enc_type == EncType.AES:
                enc_body_bytes = self._aes_encrypt(body)
            else:
                raise InvalidParameterError(f"Baichuan host {self._host}: invalid param enc_type '{enc_type}'")

        # send message
        async with self._mutex:
            if self._writer is None or self._reader is None or self._writer.is_closing():
                try:
                    async with asyncio.timeout(15):
                        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
                except asyncio.TimeoutError as err:
                    raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error") from err
                _LOGGER.debug("Baichuan host %s: opened connection", self._host)

            if _LOGGER.isEnabledFor(logging.DEBUG):
                if mess_len > 0:
                    _LOGGER.debug("Baichuan host %s: writing cmd_id %s, body:\n%s", self._host, cmd_id, self._hide_password(body))
                else:
                    _LOGGER.debug("Baichuan host %s: writing cmd_id %s, without body", self._host, cmd_id)

            try:
                async with asyncio.timeout(15):
                    self._writer.write(header + enc_body_bytes)
                    await self._writer.drain()

                    data = await self._reader.read(16384)
            except asyncio.TimeoutError as err:
                raise ReolinkTimeoutError(f"Baichuan host {self._host}: Timeout error: {str(err)}") from err

        # parse received header
        if data[0:4].hex() != HEADER_MAGIC:
            raise UnexpectedDataError(f"Baichuan host {self._host}: received message with invalid magic header: {data.hex()}")

        rec_cmd_id = int.from_bytes(data[4:8], byteorder="little")
        if rec_cmd_id != cmd_id:
            raise UnexpectedDataError(f"Baichuan host {self._host}: received cmd_id '{rec_cmd_id}', while sending cmd_id '{cmd_id}'")

        len_body = int.from_bytes(data[8:12], byteorder="little")
        rec_enc_offset = int.from_bytes(data[12:16], byteorder="little")
        rec_enc_type = data[16:18].hex()
        mess_class = data[18:20].hex()

        # check message class
        if mess_class == "1466":  # modern 20 byte header
            len_header = 20
        elif mess_class in ["1464", "0000"]:  # modern 24 byte header
            len_header = 24
            payload_offset = int.from_bytes(data[20:24], byteorder="little")
            if payload_offset != 0:
                raise InvalidContentTypeError("Baichuan host {self._host}: received a message with a non-zero payload offset, parsing not implemented")
        elif mess_class == "1465":  # legacy 20 byte header
            len_header = 20
            raise InvalidContentTypeError("Baichuan host {self._host}: received legacy message, parsing not implemented")
        else:
            raise InvalidContentTypeError(f"Baichuan host {self._host}: received unknown message class '{mess_class}'")

        # check body length
        enc_body = data[len_header::]
        if len(enc_body) != len_body:
            if retry <= 0:
                raise UnexpectedDataError(f"Baichuan host {self._host}: received {len(enc_body)} bytes in the body, while header specified {len_body} bytes")
            _LOGGER.error("Baichuan host %s: received %s bytes in the body, while header specified %s bytes, trying again", self._host, len(enc_body), len_body)
            return await self.send(cmd_id, body, enc_type, message_class, enc_offset, retry)

        # check status code
        if len_header == 24:
            rec_status_code = int.from_bytes(data[16:18], byteorder="little")
            if rec_status_code != 200:
                raise ApiError(f"Baichuan host {self._host}: received status code {rec_status_code}", rspCode=rec_status_code)

        # decryption
        if (len_header == 20 and rec_enc_type in ["01dd", "12dd"]) or (len_header == 24 and enc_type == EncType.BC):
            # Baichuan Encryption
            rec_body = decrypt_baichuan(enc_body, rec_enc_offset)
        elif (len_header == 20 and rec_enc_type in ["02dd", "03dd"]) or (len_header == 24 and enc_type == EncType.AES):
            # AES Encryption
            rec_body = self._aes_decrypt(enc_body)
        elif rec_enc_type == "00dd":  # Unencrypted
            rec_body = enc_body.decode("utf8")
        else:
            raise InvalidContentTypeError(f"Baichuan host {self._host}: received unknown encryption type '{rec_enc_type}', data: {data.hex()}")

        if _LOGGER.isEnabledFor(logging.DEBUG):
            if len_body > 0:
                _LOGGER.debug("Baichuan host %s: received:\n%s", self._host, self._hide_password(rec_body))
            else:
                _LOGGER.debug("Baichuan host %s: received status 200:OK without body", self._host)

        return rec_body

    def _aes_encrypt(self, body: str) -> bytes:
        """Encrypt a message using AES encryption"""
        if self._aes_key is None:
            raise InvalidParameterError("Baichuan host {self._host}: first login before using AES encryption")

        cipher = AES.new(key=self._aes_key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
        return cipher.encrypt(body.encode("utf8"))

    def _aes_decrypt(self, data: bytes) -> str:
        """Decrypt a message using AES decryption"""
        if self._aes_key is None:
            raise InvalidParameterError("Baichuan host {self._host}: first login before using AES decryption")

        cipher = AES.new(key=self._aes_key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
        return cipher.decrypt(data).decode("utf8")

    def _hide_password(self, content: str | bytes | dict | list) -> str:
        """Redact sensitive informtation from the logs"""
        redacted = str(content)
        if self._password:
            redacted = redacted.replace(self._password, "<password>")
        if self._nonce:
            redacted = redacted.replace(self._nonce, "<nonce>")
        if self._user_hash:
            redacted = redacted.replace(self._user_hash, "<user_md5_hash>")
        if self._password_hash:
            redacted = redacted.replace(self._password_hash, "<password_md5_hash>")
        return redacted

    async def _get_nonce(self) -> str:
        """Get the nonce needed for the modern login"""
        # send only a header to receive the nonce (alternatively use legacy login)
        mess = await self.send(cmd_id=1, enc_type=EncType.BC, message_class="1465")
        root = XML.fromstring(mess)
        nonce = root.find(".//nonce")
        if nonce is None:
            raise UnexpectedDataError(f"Baichuan host {self._host}: could not find nonce in response:\n{mess}")
        self._nonce = nonce.text
        if self._nonce is None:
            raise UnexpectedDataError(f"Baichuan host {self._host}: could not find nonce in response:\n{mess}")

        aes_key_str = md5_str_modern(f"{self._nonce}-{self._password}")[0:16]
        self._aes_key = aes_key_str.encode("utf8")

        return self._nonce

    async def login(self) -> None:
        """Login using the Baichuan protocol"""
        nonce = await self._get_nonce()

        # modern login
        self._user_hash = md5_str_modern(f"{self._username}{nonce}")
        self._password_hash = md5_str_modern(f"{self._password}{nonce}")
        xml = xmls.LOGIN_XML.format(userName=self._user_hash, password=self._password_hash)

        await self.send(cmd_id=1, enc_type=EncType.BC, body=xml)
        self._logged_in = True

    async def logout(self) -> None:
        """Close the TCP session and cleanup"""
        if self._writer is not None:
            try:
                xml = xmls.LOGOUT_XML.format(userName=self._username, password=self._password)
                await self.send(cmd_id=2, body=xml)
            except ReolinkError as err:
                _LOGGER.error("Baichuan host %s: failed to logout: %s", self._host, err)

            self._writer.close()
            await self._writer.wait_closed()
            _LOGGER.debug("Baichuan host %s: closed connection", self._host)

        self._logged_in = False
        self._writer = None
        self._reader = None
        self._nonce = None
        self._aes_key = None
        self._user_hash = None
        self._password_hash = None

    async def get_ports(self) -> dict[str, dict[str, int | bool]]:
        """Get the HTTP(S)/RTSP/RTMP/ONVIF port state"""
        mess = await self.send(cmd_id=37)

        self._ports = {}
        root = XML.fromstring(mess)
        for protocol in root:
            for key in protocol:
                proto_key = protocol.tag.replace("Port", "").lower()
                sub_key = key.tag.replace(proto_key, "").lower()
                if key.text is None:
                    continue
                self._ports.setdefault(proto_key, {})
                self._ports[proto_key][sub_key] = int(key.text)

        return self._ports

    async def set_port_enabled(self, port: PortType, enable: bool) -> None:
        """set the HTTP(S)/RTSP/RTMP/ONVIF port"""
        xml_body = XML.Element("body")
        main = XML.SubElement(xml_body, port.value.capitalize() + "Port", version="1.1")
        sub = XML.SubElement(main, "enable")
        sub.text = "1" if enable else "0"
        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml

        await self.send(cmd_id=36, body=xml)

    @property
    def http_port(self) -> int | None:
        return self._ports.get("http", {}).get("port")

    @property
    def https_port(self) -> int | None:
        return self._ports.get("https", {}).get("port")

    @property
    def rtmp_port(self) -> int | None:
        return self._ports.get("rtmp", {}).get("port")

    @property
    def rtsp_port(self) -> int | None:
        return self._ports.get("rtsp", {}).get("port")

    @property
    def onvif_port(self) -> int | None:
        return self._ports.get("onvif", {}).get("port")

    @property
    def http_enabled(self) -> bool | None:
        enabled = self._ports.get("http", {}).get("enable")
        if enabled is None:
            return None
        return enabled == 1

    @property
    def https_enabled(self) -> bool | None:
        enabled = self._ports.get("https", {}).get("enable")
        if enabled is None:
            return None
        return enabled == 1

    @property
    def rtmp_enabled(self) -> bool | None:
        enabled = self._ports.get("rtmp", {}).get("enable")
        if enabled is None:
            return None
        return enabled == 1

    @property
    def rtsp_enabled(self) -> bool | None:
        enabled = self._ports.get("rtsp", {}).get("enable")
        if enabled is None:
            return None
        return enabled == 1

    @property
    def onvif_enabled(self) -> bool | None:
        enabled = self._ports.get("onvif", {}).get("enable")
        if enabled is None:
            return None
        return enabled == 1
