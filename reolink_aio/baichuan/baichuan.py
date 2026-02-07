"""Reolink Baichuan API"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from inspect import getmembers
from time import time as time_now
from typing import TYPE_CHECKING, Any, Coroutine, Literal, TypeVar, overload
from xml.etree import ElementTree as XML

from Cryptodome.Cipher import AES

from ..const import (
    AI_DETECT_CONVERSION,
    MAX_COLOR_TEMP,
    MIN_COLOR_TEMP,
    NONE_WAKING_COMMANDS,
    UNKNOWN,
    YOLO_CONVERSION,
    YOLO_DETECT_TYPES,
    YOLO_DETECTS,
)
from ..enums import (
    BatteryEnum,
    DayNightEnum,
    EncodingEnum,
    ExposureEnum,
    HardwiredChimeTypeEnum,
    SpotlightEventModeEnum,
    SpotlightModeEnum,
)
from ..exceptions import (
    ApiError,
    CredentialsInvalidError,
    InvalidContentTypeError,
    InvalidParameterError,
    NotSupportedError,
    ReolinkConnectionError,
    ReolinkError,
    ReolinkTimeoutError,
    UnexpectedDataError,
)
from ..software_version import SoftwareVersion
from ..typings import VOD_file, VOD_trigger, cmd_list_type
from ..utils import (
    datetime_to_reolink_time,
    reolink_time_to_datetime,
    to_reolink_time_id,
)
from . import xmls
from .tcp_protocol import BaichuanTcpClientProtocol
from .util import (
    AES_IV,
    DEFAULT_BC_PORT,
    HEADER_MAGIC,
    EncType,
    PortType,
    decrypt_baichuan,
    encrypt_baichuan,
    http_cmd,
    i_frame_to_jpeg,
    md5_str_modern,
)

if TYPE_CHECKING:
    from ..api import Host

_LOGGER = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
KEEP_ALLIVE_INTERVAL = 30  # seconds
MIN_KEEP_ALLIVE_INTERVAL = 9  # seconds
TIMEOUT = 30  # seconds

AI_DETECTS = {"people", "vehicle", "dog_cat", "state"}
SMART_AI = {
    "crossline": (527, 528),
    "intrusion": (529, 530),
    "loitering": (531, 532),
    "legacy": (549, 550),
    "loss": (551, 552),
}

TIME_STR_TO_INT_SEC = {
    "15 Seconds": 15,
    "30 Seconds": 30,
    "1 Minute": 60,
    "2 Minutes": 120,
    "5 Minutes": 300,
    "10 Minutes": 600,
    "30 Minutes": 1800,
    "45 Minutes": 2700,
    "60 Minutes": 3600,
}
TIME_INT_SEC_TO_STR = {v: k for k, v in TIME_STR_TO_INT_SEC.items()}

T = TypeVar("T")


class Baichuan:
    """Reolink Baichuan API class."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        http_api: Host,
        port: int = DEFAULT_BC_PORT,
    ) -> None:
        self.http_api = http_api

        self._host: str = host
        self.port: int = port
        self._username: str = username
        self._password: str = password
        self._nonce: str | None = None
        self._user_hash: str | None = None
        self._password_hash: str | None = None
        self._aes_key: bytes | None = None
        self._log_once: set[str] = set()
        self._log_error: bool = True
        self.last_privacy_check: float = 0
        self.last_privacy_on: float = 0

        # TCP connection
        self._mutex = asyncio.Lock()
        self._login_mutex = asyncio.Lock()
        self._loop = asyncio.get_event_loop()
        self._transport: asyncio.Transport | None = None
        self._protocol: BaichuanTcpClientProtocol | None = None
        self._logged_in: bool = False
        self._mess_id = 0

        # Event subscription
        self._subscribed: bool = False
        self._events_active: bool = False
        self._keepalive_task: asyncio.Task | None = None
        self._keepalive_interval: float = KEEP_ALLIVE_INTERVAL
        self._time_keepalive_loop: float = 0
        self._time_reestablish: float = 0
        self._time_keepalive_increase: float = 0
        self._time_connection_lost: float = 0
        self._ext_callback: dict[int | None, dict[int | None, dict[str, Callable[[], None]]]] = {}

        # http_cmd functions, set by the http_cmd decorator
        self.cmd_funcs: dict[str, Callable] = {}
        for _name, func in getmembers(self, lambda o: hasattr(o, "http_cmds")):
            for cmd in func.http_cmds:
                self.cmd_funcs[cmd] = func

        # supported
        self.capabilities: dict[int | None, set[str]] = {}
        self._abilities: dict[int | None, XML.Element] = {}

        # host states
        self._ports: dict[str, dict[str, int | bool]] = {}
        self._scenes: dict[int, str] = {}
        self._active_scene: int = -1
        self._day_night_state: dict[int, str] = {}
        self._dev_type: str = ""

        # channel states
        self._dev_info: dict[int | None, dict[str, str]] = {}
        self._network_info: dict[int | None, dict[str, str]] = {}
        self._wifi_connection: dict[int, bool] = {}
        self._ptz_running: dict[int, bool] = {}
        self._ptz_position: dict[int, dict[str, str]] = {}
        self._privacy_mode: dict[int, bool] = {}
        self._ai_detect: dict[int, dict[str, dict[int, dict[str, Any]]]] = {}
        self._hardwired_chime_settings: dict[int, dict[str, str | int]] = {}
        self._ir_brightness: dict[int, int] = {}
        self._cry_sensitivity: dict[int, int] = {}
        self._pre_record_state: dict[int, dict] = {}
        self._siren_state: dict[int, bool] = {}
        self._siren_play_time: dict[int | None, float] = {}
        self._noise_reduction: dict[int, int] = {}
        self._ai_yolo_600: dict[int, dict[str, bool]] = {}
        self._ai_yolo_696: dict[int, dict[str, bool]] = {}
        self._ai_yolo_sub_type: dict[int, dict[str, str | None]] = {}
        self._rule_ids: set[int] = set()
        self._rules: dict[int, dict[int, dict[str, Any]]] = {}
        self._io_inputs: dict[int | None, list[int]] = {}
        self._io_outputs: dict[int | None, list[int]] = {}
        self._io_input: dict[int | None, dict[int, bool]] = {}

        # futures
        self._payload_future: dict[int | None, dict[int, asyncio.Future]] = {}
        self._payload_future_data: dict[int | None, bytes] = {}

    async def _connect_if_needed(self):
        """Initialize the protocol and make the connection if needed."""
        if self._transport is not None and self._protocol is not None and not self._transport.is_closing():
            return  # connection is open

        if self._protocol is not None and self._protocol.receive_futures:
            # Ensure all previous receive futures get the change to throw their exceptions
            _LOGGER.debug("Baichuan host %s: waiting for previous receive futures to finish before opening a new connection", self._host)
            try:
                async with asyncio.timeout(TIMEOUT + 5):
                    while self._protocol.receive_futures:
                        await asyncio.sleep(0)
            except asyncio.TimeoutError:
                _LOGGER.warning("Baichuan host %s: Previous receive futures did not finish before opening a new connection", self._host)

        try:
            async with asyncio.timeout(TIMEOUT):
                async with self._mutex:
                    if self._transport is not None and self._protocol is not None and not self._transport.is_closing():
                        return  # connection already opened in the meantime

                    self._transport, self._protocol = await self._loop.create_connection(
                        lambda: BaichuanTcpClientProtocol(self._loop, self._host, self._push_callback, self._close_callback), self._host, self.port
                    )
        except asyncio.TimeoutError as err:
            raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error") from err
        except (ConnectionResetError, OSError) as err:
            raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error: {str(err)}") from err

    async def send(
        self,
        cmd_id: int,
        channel: int | None = None,
        body: str = "",
        extension: str = "",
        enc_type: EncType = EncType.AES,
        message_class: str = "1464",
        ch_id: int | None = None,
        mess_id: int | None = None,
        retry: int = RETRY_ATTEMPTS,
    ) -> str:
        """Generic baichuan send method."""
        retry = retry - 1

        if not self._logged_in and cmd_id > 2:
            # not logged in and requesting a non login/logout cmd, first login
            await self.login()

        # ch_id: 0/251 = push, 1-100 = channel, 250 = host
        if ch_id is None:
            if channel is None:
                ch_id = 250
            else:
                ch_id = channel + 1

        ext = extension  # do not overwrite the original arguments for retries
        if channel is not None:
            if extension:
                raise InvalidParameterError(f"Baichuan host {self._host}: cannot specify both channel and extension")
            ext = xmls.CHANNEL_EXTENSION_XML.format(channel=channel)

        mess_len = len(ext) + len(body)
        payload_offset = len(ext)
        if mess_id is None:
            self._mess_id = (self._mess_id + 1) % 16777216
        else:
            self._mess_id = mess_id

        cmd_id_bytes = (cmd_id).to_bytes(4, byteorder="little")
        mess_len_bytes = (mess_len).to_bytes(4, byteorder="little")
        mess_id_bytes = (ch_id).to_bytes(1, byteorder="little") + (self._mess_id).to_bytes(3, byteorder="little")
        full_mess_id = int.from_bytes(mess_id_bytes, byteorder="little")
        payload_offset_bytes = (payload_offset).to_bytes(4, byteorder="little")

        if message_class == "1465":
            encrypt = "12dc"
            header = bytes.fromhex(HEADER_MAGIC) + cmd_id_bytes + mess_len_bytes + mess_id_bytes + bytes.fromhex(encrypt + message_class)
        elif message_class == "1464":
            status_code = "0000"
            header = bytes.fromhex(HEADER_MAGIC) + cmd_id_bytes + mess_len_bytes + mess_id_bytes + bytes.fromhex(status_code + message_class) + payload_offset_bytes
        else:
            raise InvalidParameterError(f"Baichuan host {self._host}: invalid param message_class '{message_class}'")

        enc_body_bytes = b""
        if mess_len > 0:
            if enc_type == EncType.BC:
                enc_body_bytes = encrypt_baichuan(ext, ch_id) + encrypt_baichuan(body, ch_id)  # enc_offset = ch_id
            elif enc_type == EncType.AES:
                enc_body_bytes = self._aes_encrypt(ext) + self._aes_encrypt(body)
            else:
                raise InvalidParameterError(f"Baichuan host {self._host}: invalid param enc_type '{enc_type}'")

        # send message
        await self._connect_if_needed()
        if TYPE_CHECKING:
            assert self._protocol is not None
            assert self._transport is not None

        # check for simultaneous cmd_ids with same full_mess_id
        if (receive_future := self._protocol.receive_futures.get(cmd_id, {}).get(full_mess_id)) is not None:
            try:
                async with asyncio.timeout(TIMEOUT):
                    try:
                        await receive_future
                    except Exception:
                        pass
                    while self._protocol.receive_futures.get(cmd_id, {}).get(full_mess_id) is not None:
                        await asyncio.sleep(0.010)
            except asyncio.TimeoutError as err:
                raise ReolinkError(
                    f"Baichuan host {self._host}: receive future is already set for cmd_id {cmd_id} "
                    "and timeout waiting for it to finish, cannot receive multiple requests simultaneously"
                ) from err

        self._protocol.receive_futures.setdefault(cmd_id, {})[full_mess_id] = self._loop.create_future()

        if _LOGGER.isEnabledFor(logging.DEBUG):
            if mess_len > 0:
                _LOGGER.debug("Baichuan host %s: writing cmd_id %s, body:\n%s", self._host, cmd_id, self._hide_password(ext + body))
            else:
                _LOGGER.debug("Baichuan host %s: writing cmd_id %s, without body", self._host, cmd_id)

        retrying = False
        try:
            async with asyncio.timeout(TIMEOUT):
                async with self._mutex:
                    self._transport.write(header + enc_body_bytes)
                data, len_header, payload = await self._protocol.receive_futures[cmd_id][full_mess_id]
        except ApiError as err:
            if retry <= 0 or err.rspCode != 400:
                raise err
            _LOGGER.debug("%s, trying again in 1.5 s", str(err))
            await asyncio.sleep(1.5)  # give the battery cam time to wake
            retrying = True
        except asyncio.TimeoutError as err:
            ch_str = f", ch {channel}" if channel is not None else ""
            err_str = f"Baichuan host {self._host}: Timeout error for cmd_id {cmd_id}{ch_str}"
            if retry <= 0 or cmd_id == 2:
                raise ReolinkTimeoutError(err_str) from err
            _LOGGER.debug("%s, trying again", err_str)
            retrying = True
        except (ConnectionResetError, OSError) as err:
            ch_str = f", ch {channel}" if channel is not None else ""
            err_str = f"Baichuan host {self._host}: Connection error during read/write of cmd_id {cmd_id}{ch_str}: {str(err)}"
            if retry <= 0 or cmd_id == 2:
                raise ReolinkConnectionError(err_str) from err
            _LOGGER.debug("%s, trying again", err_str)
            retrying = True
        except asyncio.CancelledError:
            _LOGGER.debug("Baichuan host %s: cmd_id %s mess_id %s got cancelled", self._host, cmd_id, full_mess_id)
            raise
        finally:
            if self._protocol is not None and (receive_future := self._protocol.receive_futures.get(cmd_id, {}).get(full_mess_id)) is not None:
                if not receive_future.done():
                    receive_future.cancel()
                self._protocol.receive_futures[cmd_id].pop(full_mess_id, None)
                if not self._protocol.receive_futures[cmd_id]:
                    self._protocol.receive_futures.pop(cmd_id, None)

        if retrying:
            # needed because the receive_future first needs to be cleared.
            return await self.send(cmd_id, channel, body, extension, enc_type, message_class, ch_id, mess_id, retry)

        # check full message id
        rec_mess_id = int.from_bytes(data[12:16], byteorder="little")
        if full_mess_id != rec_mess_id:
            err_str = f"Baichuan host {self._host}: message id error for cmd_id {cmd_id}, send mess_id {full_mess_id} received mess_id {rec_mess_id}"
            if retry <= 0:
                raise UnexpectedDataError(err_str)
            _LOGGER.debug("%s, trying again", str(err_str))
            return await self.send(cmd_id, channel, body, extension, enc_type, message_class, ch_id, mess_id, retry)

        # decryption
        rec_body = self._decrypt(data, len_header, cmd_id, enc_type)

        if _LOGGER.isEnabledFor(logging.DEBUG):
            ch_str = f" ch {channel}" if channel is not None else ""
            payload_str = f" with payload length {len(payload)}" if len(payload) > 0 else ""
            if len(rec_body) > 0:
                _LOGGER.debug("Baichuan host %s: received cmd_id %s%s%s:\n%s", self._host, cmd_id, ch_str, payload_str, self._hide_password(rec_body))
            else:
                _LOGGER.debug("Baichuan host %s: received cmd_id %s%s status 200:OK without body%s", self._host, cmd_id, ch_str, payload_str)

        return rec_body

    async def send_payload(
        self,
        cmd_id: int,
        channel: int | None = None,
        body: str = "",
        extension: str = "",
    ) -> tuple[str, bytes]:
        """Generic send method which expects a binary payload as return."""
        mess_id = (self._mess_id + 1) % 16777216
        self._mess_id = mess_id

        # ch_id: 0/251 = push, 1-100 = channel, 250 = host
        if channel is None:
            ch_id = 250
        else:
            ch_id = channel + 1

        full_mess_id = (mess_id << 8) + ch_id

        # check for simultaneous payload requests of the same channel
        try:
            async with asyncio.timeout(TIMEOUT):
                while payload_future_dict := self._payload_future.get(ch_id, {}):
                    try:
                        for payload_future in payload_future_dict.values():
                            await payload_future
                    except Exception:
                        pass
                    if self._payload_future.get(ch_id, {}):
                        await asyncio.sleep(0.010)
        except asyncio.TimeoutError as err:
            raise ReolinkError(
                f"Baichuan host {self._host}: payload future is already set and timeout waiting for it to finish, cannot receive multiple payloads simultaneously"
            ) from err

        self._payload_future.setdefault(ch_id, {})[full_mess_id] = self._loop.create_future()
        self._payload_future_data[full_mess_id] = b""

        try:
            rec_body = await self.send(cmd_id=cmd_id, channel=channel, body=body, extension=extension, mess_id=mess_id)

            async with asyncio.timeout(TIMEOUT):
                payload = await self._payload_future[ch_id][full_mess_id]
        except asyncio.TimeoutError as err:
            raise ReolinkTimeoutError(f"Baichuan host {self._host}: Timeout error waiting on payload for cmd_id {cmd_id} channel {channel}") from err
        except asyncio.CancelledError:
            _LOGGER.debug("Baichuan host %s: cmd_id %s channel %s mess_id %s got cancelled", self._host, cmd_id, channel, full_mess_id)
            raise
        finally:
            self._payload_future_data.pop(full_mess_id, None)
            if (pay_future := self._payload_future[ch_id].get(full_mess_id)) is not None:
                if not pay_future.done():
                    pay_future.cancel()
            self._payload_future[ch_id].pop(full_mess_id, None)

        return (rec_body, payload)

    def _aes_encrypt(self, body: str) -> bytes:
        """Encrypt a message using AES encryption"""
        if not body:
            return b""
        if self._aes_key is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: first login before using AES encryption")

        cipher = AES.new(key=self._aes_key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
        return cipher.encrypt(body.encode("utf8"))

    @overload
    def _aes_decrypt(self, data: bytes, header: bytes) -> str: ...
    @overload
    def _aes_decrypt(self, data: bytes, header: bytes, decode: Literal[True]) -> str: ...
    @overload
    def _aes_decrypt(self, data: bytes, header: bytes, decode: Literal[False]) -> bytes: ...

    def _aes_decrypt(self, data: bytes, header: bytes, decode: bool = True) -> str | bytes:
        """Decrypt a message using AES decryption"""
        if self._aes_key is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: first login before using AES decryption, header: {header.hex()}")

        cipher = AES.new(key=self._aes_key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
        decrypted = cipher.decrypt(data)
        if not decode:
            return decrypted
        return decrypted.decode("utf8")

    def _decrypt(self, data: bytes, len_header: int, cmd_id: int, enc_type: EncType = EncType.AES) -> str:
        """Figure out the encryption method and decrypt the message"""
        rec_enc_offset = int.from_bytes(data[12:13], byteorder="little")  # enc_offset = ch_id
        rec_enc_type = data[16:18].hex()
        enc_body = data[len_header::]
        header = data[0:len_header]

        rec_body = ""
        if len(enc_body) == 0:
            return rec_body

        # decryption
        if (len_header == 20 and rec_enc_type in ["01dd", "12dd"]) or enc_type == EncType.BC:
            # Baichuan Encryption
            rec_body = decrypt_baichuan(enc_body, rec_enc_offset)
            enc_type = EncType.BC
        elif (len_header == 20 and rec_enc_type in ["02dd", "03dd"]) or (len_header == 24 and enc_type == EncType.AES):
            # AES Encryption
            try:
                rec_body = self._aes_decrypt(enc_body, header)
            except UnicodeDecodeError as err:
                _LOGGER.debug("Baichuan host %s: AES decryption failed for cmd_id %s with UnicodeDecodeError: %s, trying Baichuan decryption", self._host, cmd_id, err)
        elif rec_enc_type == "00dd":  # Unencrypted
            rec_body = enc_body.decode("utf8")
        else:
            raise InvalidContentTypeError(f"Baichuan host {self._host}: received unknown encryption type '{rec_enc_type}', data: {data.hex()}")

        # check if decryption suceeded
        if not rec_body.startswith("<?xml"):
            if rec_enc_type == "00dd":
                rec_body = enc_body.decode("utf8")
            if not rec_body.startswith("<?xml") and enc_type != EncType.BC:
                rec_body = decrypt_baichuan(enc_body, rec_enc_offset)
            if not rec_body.startswith("<?xml"):
                raise UnexpectedDataError(
                    f"Baichuan host {self._host}: unable to decrypt message with cmd_id {cmd_id}, "
                    f"header '{header.hex()}', decrypted data startswith '{rec_body[0:5]}', "
                    f"encrypted data startswith '{enc_body[0:5].hex()}' instead of '<?xml'"
                )

        return rec_body

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

    def _push_callback(self, cmd_id: int, data: bytes, len_header: int, payload: bytes) -> None:
        """Callback to parse a received message that was pushed"""
        payload_len = len(payload)
        mess_id: int = int.from_bytes(data[12:16], byteorder="little")

        # decryption
        try:
            rec_body = self._decrypt(data, len_header, cmd_id)
            if payload_len != 0:
                encryptLen = self._get_value_from_xml_element(XML.fromstring(rec_body.lower()), "encryptlen", int)
                if encryptLen is not None:
                    payload = self._aes_decrypt(payload[0:encryptLen], b"", decode=False) + payload[encryptLen::]
        except ReolinkError as err:
            _LOGGER.debug(err)
            return

        if len(rec_body) == 0:
            if payload_len == 0:
                _LOGGER.debug("Baichuan host %s: received push cmd_id %s without body", self._host, cmd_id)
                if (payload_future_data := self._payload_future_data.get(mess_id)) is not None:
                    ch_id = mess_id % 256
                    payload_future = self._payload_future.get(ch_id, {}).get(mess_id)
                    if payload_future is None:
                        _LOGGER.debug(
                            "Reolink %s baichaun push cmd_id %s ch_id %s mess_id %s received with payload without payload_future",
                            self.http_api.nvr_name,
                            cmd_id,
                            ch_id,
                            mess_id,
                        )
                        return
                    payload_future.set_result(payload_future_data)
                    self._payload_future_data[mess_id] = b""
            else:
                _LOGGER.debug("Baichuan host %s: received push cmd_id %s without body but with %s bytes payload", self._host, cmd_id, payload_len)
            return

        if _LOGGER.isEnabledFor(logging.DEBUG):
            payload_str = ""
            if payload_len != 0:
                payload_str = f" with payload length {payload_len}"
            _LOGGER.debug("Baichuan host %s: received push cmd_id %s%s:\n%s", self._host, cmd_id, payload_str, self._hide_password(rec_body))

        self._parse_xml(cmd_id, rec_body, payload, mess_id)

    def _close_callback(self) -> None:
        """Callback for when the connection is closed"""
        self._logged_in = False
        events_active = self._events_active
        self._events_active = False
        if self._subscribed:
            now = time_now()
            if not events_active:  # Their was no proper connection, or close_callback is beeing called multiple times
                self._time_connection_lost = now
                _LOGGER.debug("Baichuan host %s: disconnected while event subscription was not active", self._host)
                return
            if self.http_api._updating:
                _LOGGER.debug("Baichuan host %s: lost event subscription during firmware reboot", self._host)
                return
            if self._protocol is not None:
                time_since_recv = now - self._protocol.time_recv
            else:
                time_since_recv = 0
            if now - self._time_reestablish > 60:  # limit the amount of reconnects to prevent fast loops
                self._time_reestablish = now
                self._loop.create_task(self._reestablish_connection(time_since_recv))
                return

            self._time_connection_lost = now
            _LOGGER.error("Baichuan host %s: lost event subscription after %.2f s, last reestablish %.2f s ago", self._host, time_since_recv, now - self._time_reestablish)

    async def _reestablish_connection(self, time_since_recv: float) -> None:
        """Try to reestablish the connection after a connection is closed"""
        time_start = time_now()
        try:
            await self.send(cmd_id=31, ch_id=251)  # Subscribe to events
        except Exception as err:
            _LOGGER.error("Baichuan host %s: lost event subscription after %.2f s and failed to reestablished connection", self._host, time_since_recv)
            _LOGGER.debug("Baichuan host %s: failed to reestablished connection: %s", self._host, str(err))
            self._time_connection_lost = time_start
        else:
            _LOGGER.debug("Baichuan host %s: lost event subscription after %.2f s, but reestablished connection immediately", self._host, time_since_recv)
            if time_now() - time_start < 5:
                origianal_keepalive = self._keepalive_interval
                self._keepalive_interval = max(MIN_KEEP_ALLIVE_INTERVAL, min(time_since_recv - 2, self._keepalive_interval - 1))
                _LOGGER.debug("Baichuan host %s: reducing keepalive interval from %.2f to %.2f s", self._host, origianal_keepalive, self._keepalive_interval)

    @overload
    def _get_value_from_xml_element(self, xml_element: XML.Element, key: str) -> str | None: ...
    @overload
    def _get_value_from_xml_element(self, xml_element: XML.Element, key: str, type_class: type[T]) -> T | None: ...
    @overload
    def _get_value_from_xml_element(self, xml_element: XML.Element, key: str, type_class: type[T], recursive: bool) -> T | None: ...

    def _get_value_from_xml_element(self, xml_element: XML.Element, key: str, type_class=str, recursive: bool = True):
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

    def _get_channel_from_xml_element(self, xml_element: XML.Element, key: str = "channelId") -> int | None:
        channel = self._get_value_from_xml_element(xml_element, key, int)
        if channel not in self.http_api._channels:
            return None
        return channel

    def _get_keys_from_xml(self, xml: str | XML.Element, keys: list[str] | dict[str, tuple[str, type]], recursive: bool = True) -> dict[str, Any]:
        """Get multiple keys from a xml and return as a dict"""
        if isinstance(xml, str):
            root = XML.fromstring(xml)
        else:
            root = xml
        result: dict[str, Any] = {}
        for key in keys:
            value: str | int | None = self._get_value_from_xml_element(root, key, str, recursive)
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

    def _get_value_from_xml(self, xml: str, key: str) -> str | None:
        """Get the value of a single key in a xml"""
        return self._get_keys_from_xml(xml, [key]).get(key)

    async def _get_nonce(self) -> str:
        """Get the nonce needed for the modern login"""
        # send only a header to receive the nonce (alternatively use legacy login)
        mess = await self.send(cmd_id=1, enc_type=EncType.BC, message_class="1465")
        self._nonce = self._get_value_from_xml(mess, "nonce")
        if self._nonce is None:
            raise UnexpectedDataError(f"Baichuan host {self._host}: could not find nonce in response:\n{mess}")

        aes_key_str = md5_str_modern(f"{self._nonce}-{self._password}")[0:16]
        self._aes_key = aes_key_str.encode("utf8")

        return self._nonce

    async def _send_and_parse(self, cmd_id: int, channel: int | None = None) -> None:
        """Send the command and parse the response"""
        rec_body = await self.send(cmd_id=cmd_id, channel=channel)
        mess_id = channel + 1 if channel is not None else None
        self._parse_xml(cmd_id, rec_body, mess_id=mess_id)

    def _parse_xml(self, cmd_id: int, xml: str, payload: bytes = b"", mess_id: int | None = None) -> None:
        """parce received xml"""
        root = XML.fromstring(xml)

        state: Any
        channels: set[int | None] = {None}
        cmd_ids: set[int | None] = {None, cmd_id}
        if cmd_id in {26, 78}:
            channel = self._get_channel_from_xml_element(root)
            if channel is None:
                return
            channels.add(channel)

            if (VideoInput := root.find(".//VideoInput")) is not None:
                data = self._get_keys_from_xml(
                    VideoInput,
                    {
                        "bright": ("bright", int),
                        "contrast": ("contrast", int),
                        "saturation": ("saturation", int),
                        "hue": ("hue", str),
                        "sharpen": ("sharpen", int),
                    },
                )
                self.http_api._image_settings.setdefault(channel, {}).setdefault("Image", {}).update(data)

            isp = self.http_api._isp_settings.setdefault(channel, {})
            if (DayNight := root.find(".//DayNight")) is not None:
                value = self._get_value_from_xml_element(DayNight, "mode")
                if value is not None:
                    value = value.replace("And", "&")
                    value = value[0].upper() + value[1:]
                    isp["dayNight"] = DayNightEnum(value).value

            data = self._get_keys_from_xml(root, {"hdrSwitch": ("hdr", int), "binning_mode": ("binningMode", int)})
            data["channel"] = channel
            exposure = self._get_value_from_xml_element(root, "InputAdvanceCfg/Exposure/mode", str, recursive=False)
            for val in ExposureEnum:
                if val.value.lower() == exposure:  # ensure the proper upper-case
                    data["exposure"] = val.value
            isp.update(data)

        elif cmd_id == 33:  # Motion/AI/Visitor event | DayNightEvent
            for event_list in root:
                for event in event_list:
                    channel = self._get_channel_from_xml_element(event)
                    if channel is None:
                        continue
                    channels.add(channel)

                    if event.tag == "AlarmEvent":
                        states = self._get_value_from_xml_element(event, "status")
                        ai_types = self._get_value_from_xml_element(event, "AItype")
                        if not self._events_active and self._subscribed:
                            self._events_active = True

                        motion_state = False
                        if states is not None:
                            motion_state = "MD" in states
                            visitor_state = "visitor" in states
                            if motion_state != self.http_api._motion_detection_states.get(channel, motion_state):
                                _LOGGER.debug("Reolink %s TCP event channel %s, motion: %s", self.http_api.nvr_name, channel, motion_state)
                            if visitor_state != self.http_api._visitor_states.get(channel, visitor_state):
                                _LOGGER.debug("Reolink %s TCP event channel %s, visitor: %s", self.http_api.nvr_name, channel, visitor_state)
                            self.http_api._motion_detection_states[channel] = motion_state
                            self.http_api._visitor_states[channel] = visitor_state

                        if ai_types is not None:
                            for ai_type_key in self.http_api._ai_detection_states.get(channel, {}):
                                ai_state = ai_type_key in ai_types
                                if ai_state != self.http_api._ai_detection_states[channel][ai_type_key]:
                                    _LOGGER.debug("Reolink %s TCP event channel %s, %s: %s", self.http_api.nvr_name, channel, ai_type_key, ai_state)
                                self.http_api._ai_detection_states[channel][ai_type_key] = ai_state

                            if not motion_state:
                                self.http_api._motion_detection_states[channel] = "other" in ai_types

                            ai_type_list = ai_types.split(",")
                            for ai_type in ai_type_list:
                                if ai_type in ("none", "other"):
                                    continue
                                if ai_type not in self.http_api._ai_detection_states.get(channel, {}) and f"TCP_event_unknown_{ai_type}" not in self._log_once:
                                    self._log_once.add(f"TCP_event_unknown_{ai_type}")
                                    _LOGGER.warning("Reolink %s TCP event channel %s, received unknown event %s", self.http_api.nvr_name, channel, ai_type)

                        # reset all smart AI events to False
                        for smart_type_dict in self._ai_detect.get(channel, {}).values():
                            for smart_ai_dict in smart_type_dict.values():
                                ai_type_set = AI_DETECTS.intersection(smart_ai_dict)
                                for ai_type in ai_type_set:
                                    smart_ai_dict[ai_type] = False
                        # set all detected smart AI events to True
                        smart_list = event.find("smartAiTypeList")
                        if smart_list is not None:
                            for smart_ai in smart_list.findall(".//smartAiType"):
                                smart_type = self._get_value_from_xml_element(smart_ai, "type")
                                if smart_type is None:
                                    continue
                                sub_list = smart_ai.findall("subList")
                                index_bit_ob = smart_ai.find("index")
                                if index_bit_ob is not None and index_bit_ob.text is not None:
                                    # The index is based on bits, bit 0 = loc 0, bit 1 = loc 1, index 7 = loc 1, 2 and 3.
                                    index_bit = int(index_bit_ob.text)
                                    loop_bit = 1
                                    while index_bit >= loop_bit:
                                        location = loop_bit.bit_length() - 1
                                        detected = index_bit & loop_bit > 0
                                        smart_ai_dict = self._ai_detect[channel][smart_type][location]
                                        smart_ai_dict["state"] = detected
                                        if not sub_list and detected:
                                            _LOGGER.debug("Reolink %s TCP event channel %s, %s location %s detected", self.http_api.nvr_name, channel, smart_type, location)
                                            for ai_type in AI_DETECTS.intersection(smart_ai_dict):
                                                smart_ai_dict[ai_type] = True
                                        loop_bit <<= 1
                                for sub in sub_list:
                                    location_ob = sub.find("index")
                                    ai_type_ob = sub.find("type")
                                    if location_ob is None or ai_type_ob is None or location_ob.text is None or ai_type_ob.text is None:
                                        continue
                                    location = int(location_ob.text)
                                    ai_type = ai_type_ob.text
                                    self._ai_detect[channel][smart_type][location][ai_type] = True
                                    _LOGGER.debug(
                                        "Reolink %s TCP event channel %s, %s location %s detected %s", self.http_api.nvr_name, channel, smart_type, location, ai_type
                                    )

                    elif event.tag == "DayNightEvent":
                        state = self._get_value_from_xml_element(event, "mode")
                        if state is not None:
                            self._day_night_state[channel] = state
                            _LOGGER.debug("Reolink %s TCP event channel %s, day night state: %s", self.http_api.nvr_name, channel, state)
                    else:
                        if f"TCP_event_tag_{event.tag}" not in self._log_once:
                            self._log_once.add(f"TCP_event_tag_{event.tag}")
                            _LOGGER.warning("Reolink %s TCP event cmd_id %s, channel %s, received unknown event tag %s", self.http_api.nvr_name, cmd_id, channel, event.tag)

        elif cmd_id in {109, 298}:  # 109=Snapshot, 298=CoverPreview
            if mess_id is None:
                _LOGGER.warning("Reolink %s baichaun push cmd_id %s received with payload without mess_id", self.http_api.nvr_name, cmd_id)
                return
            ch_id = mess_id % 256
            payload_future = self._payload_future.get(ch_id, {}).get(mess_id)
            payload_future_data = self._payload_future_data.get(mess_id)
            if payload_future is None or payload_future_data is None:
                _LOGGER.debug(
                    "Reolink %s baichaun push cmd_id %s ch_id %s mess_id %s received with payload without payload_future", self.http_api.nvr_name, cmd_id, ch_id, mess_id
                )
                return
            if len(payload) <= 0:
                payload_future.set_result(payload_future_data)
                self._payload_future_data[mess_id] = b""
                return
            self._payload_future_data[mess_id] = payload_future_data + payload
            return

        elif cmd_id == 145:  # ChannelInfoList: Sleep status
            for event in root.findall(".//ChannelInfo"):
                channel = self._get_channel_from_xml_element(event)
                if channel is None:
                    continue
                channels.add(channel)
                state = self._get_value_from_xml_element(event, "loginState") == "standby"
                if state != self.http_api._sleep.get(channel):
                    _LOGGER.debug("Reolink %s TCP event channel %s, sleeping: %s", self.http_api.nvr_name, channel, state)
                self.http_api._sleep[channel] = state

        elif cmd_id == 252:  # BatteryInfo
            for event in root.findall(".//BatteryInfo"):
                channel = self._get_channel_from_xml_element(event)
                if channel is None:
                    continue
                channels.add(channel)
                data = self._get_keys_from_xml(
                    event,
                    {
                        "adapterStatus": ("adapterStatus", str),
                        "batteryPercent": ("batteryPercent", int),
                        "batteryVersion": ("batteryVersion", int),
                        "chargeStatus": ("chargeStatus", str),
                        "current": ("current", int),
                        "lowPower": ("lowPowerFlag", int),
                        "temperature": ("temperature", int),
                        "voltage": ("voltage", int),
                    },
                )
                if data["chargeStatus"] == "none":
                    data["chargeStatus"] = "discharging"
                try:
                    data["chargeStatus"] = BatteryEnum[data["chargeStatus"].lower()].value
                except KeyError:
                    _LOGGER.warning("BatteryInfo cmd_id 252 push contained unknown chargeStatus: %s, assuming discharging", data["chargeStatus"])
                    data["chargeStatus"] = BatteryEnum.discharging.value
                self.http_api._battery.setdefault(channel, {}).update(data)
                _LOGGER.debug("Reolink %s TCP event channel %s, BatteryInfo", self.http_api.nvr_name, channel)

        elif cmd_id in [289, 438]:  # Floodlight
            channel = self._get_channel_from_xml_element(root, "channel")
            if channel is None:
                return
            channels.add(channel)
            values = self._get_keys_from_xml(
                root,
                {
                    "brightness_cur": ("bright", int),
                    "alarmMode": ("mode", int),
                    "newColorTemperature": ("ColorTemp", int),
                    "alarmLightEnabledSL": ("event_mode_enabled", int),
                    "alarmLightModeSL": ("event_mode", str),
                    "brightnessAlarmSL": ("event_brightness", int),
                    "duration": ("event_on_time", int),
                    "flickerDurationSL": ("event_flash_time", int),
                },
            )
            if values.get("mode") == 4 and (self.api_version("ledCtrl", channel) >> 8) & 1:  # schedule_plus, 9th bit (256), shift 8
                # Floodlight: the schedule_plus has the same number 4 as autoadaptive, so switch it around
                values["mode"] = 3
            self.http_api._whiteled_settings.setdefault(channel, {}).update(values)

        elif cmd_id == 291:  # Floodlight
            for event_list in root:
                for event in event_list:
                    channel = self._get_channel_from_xml_element(event, "channel")
                    if channel is None:
                        continue
                    channels.add(channel)
                    state = self._get_value_from_xml_element(event, "status", int)
                    if state is not None:
                        self.http_api._whiteled_settings.setdefault(channel, {})["state"] = state
                        _LOGGER.debug("Reolink %s TCP event channel %s, Floodlight: %s", self.http_api.nvr_name, channel, state)

        elif cmd_id == 294:  # ZoomFocus
            channel = self._get_channel_from_xml_element(root)
            if channel is None:
                return
            channels.add(channel)
            for zoom in root.findall(".//zoom"):
                data = self._get_keys_from_xml(zoom, {"curPos": ("pos", int), "minPos": ("min", int), "maxPos": ("max", int)})
                self.http_api._zoom_focus_settings.setdefault(channel, {}).setdefault("zoom", {}).update(data)
            for focus in root.findall(".//focus"):
                data = self._get_keys_from_xml(focus, {"curPos": ("pos", int), "minPos": ("min", int), "maxPos": ("max", int)})
                self.http_api._zoom_focus_settings.setdefault(channel, {}).setdefault("focus", {}).update(data)

        elif cmd_id == 296:  # DayNight
            channel = self._get_channel_from_xml_element(root)
            if channel is None:
                return
            channels.add(channel)
            data = self._get_keys_from_xml(root, {"stat": ("stat", str), "cur": ("dayNightThreshold", int)})
            self._day_night_state[channel] = data["stat"]
            if (threshold := data.get("dayNightThreshold")) is not None:
                self.http_api._isp_settings.setdefault(channel, {})["dayNightThreshold"] = threshold

        elif cmd_id == 433:  # PTZ position
            if mess_id is None:
                return
            channel = mess_id % 256 - 1
            channels.add(channel)
            ptz_position = self._get_keys_from_xml(root, {"pPos": ("Ppos", int), "tPos": ("Tpos", int)})
            self.http_api._ptz_position.setdefault(channel, {}).update(ptz_position)

        elif cmd_id == 464:  # network link type wire/wifi
            self._get_value_from_xml_element(root, "net_type")
            if (signal := self._get_value_from_xml_element(root, "signal")) is not None:
                self.http_api._wifi_signal[None] = int(signal)

        elif cmd_id == 527:  # crossline detection
            self._parse_smart_ai_settings(root, channels, "crossline")
        elif cmd_id == 529:  # intrusion detection
            self._parse_smart_ai_settings(root, channels, "intrusion")
        elif cmd_id == 531:  # linger detection
            self._parse_smart_ai_settings(root, channels, "loitering")
        elif cmd_id == 542:  # PTZ moving
            channel = self._get_channel_from_xml_element(root)
            if channel is None:
                return
            channels.add(channel)
            state = self._get_value_from_xml_element(root, "ptzRunning", int)
            if state is not None:
                self._ptz_running[channel] = state == 1
                self._loop.create_task(self._send_and_parse(433, channel))  # fetch PTZ pos
                self._loop.create_task(self._send_and_parse(294, channel))  # fetch ZoomFocus

        elif cmd_id == 549:  # forgotten item
            self._parse_smart_ai_settings(root, channels, "legacy")
        elif cmd_id == 551:  # taken item
            self._parse_smart_ai_settings(root, channels, "loss")

        elif cmd_id == 547:  # siren status
            for item in root.findall(".//SirenStatus"):
                channel = self._get_channel_from_xml_element(item, "channel")
                state = self._get_value_from_xml_element(item, "status", int)
                if channel is None or state is None:
                    continue
                channels.add(channel)
                self._siren_state[channel] = state == 1
                _LOGGER.debug("Reolink %s TCP event channel %s, Siren status: %s", self.http_api.nvr_name, channel, state == 1)

        elif cmd_id == 580:  # modify Cfg
            channel = self._get_channel_from_xml_element(root)
            cmd_id_modified = self._get_value_from_xml_element(root, "cmdId", int)
            if cmd_id_modified not in {26, 527, 529, 531, 549, 551}:
                return
            self._loop.create_task(self._send_and_parse(cmd_id_modified, channel))
            return

        elif cmd_id == 588:  # manual record
            for item in root.findall(".//manualRec"):
                channel = self._get_channel_from_xml_element(item, "channel")
                if channel is None:
                    continue
                channels.add(channel)
                state = self._get_value_from_xml_element(item, "stat", int)
                if state is not None:
                    self.http_api._manual_record_settings.setdefault(channel, {})["enable"] = state
                    _LOGGER.debug("Reolink %s TCP event channel %s, Manual record: %s", self.http_api.nvr_name, channel, state)

        elif cmd_id == 600:  # AI YOLO world basic detection
            for chan, item_dict in self._ai_yolo_600.items():
                channels.add(chan)
                for key in item_dict:
                    item_dict[key] = False

            for event_list in root:
                for event in event_list:
                    channel = self._get_channel_from_xml_element(event, "channel")
                    if channel is None:
                        continue
                    channels.add(channel)

                    for event_type in event.findall("YoloWorldType"):
                        yolo_type = self._get_value_from_xml_element(event_type, "type")
                        if yolo_type is None:
                            continue
                        if not self._events_active and self._subscribed:
                            self._events_active = True
                        yolo_type = YOLO_CONVERSION.get(yolo_type, yolo_type)
                        if not self.http_api.supported(channel, f"ai_{yolo_type}") or yolo_type not in YOLO_DETECTS:
                            if f"TCP_yolo_event_unknown_{yolo_type}" not in self._log_once:
                                self._log_once.add(f"TCP_yolo_event_unknown_{yolo_type}")
                                _LOGGER.warning("Reolink %s TCP event channel %s, received unknown yolo AI event %s", self.http_api.nvr_name, channel, yolo_type)
                            continue

                        _LOGGER.debug("Reolink %s TCP yolo event channel %s, %s: True", self.http_api.nvr_name, channel, yolo_type)
                        self._ai_yolo_600.setdefault(channel, {})[yolo_type] = True

        elif cmd_id == 677:  # IO input
            for event_list in root.findall(".//statusList"):
                channel = self._get_channel_from_xml_element(event_list, "channel")
                if channel is None:
                    continue
                channels.add(channel)
                for event in event_list.findall(".//ioItem"):
                    index = self._get_value_from_xml_element(event, "index", int)
                    state = self._get_value_from_xml_element(event, "result", bool)
                    if index is None or state is None:
                        continue
                    self._io_input.setdefault(channel, {})[index] = state
                    _LOGGER.debug("Reolink %s TCP IO input event channel %s, index %s: %s", self.http_api.nvr_name, channel, index, state)

        elif cmd_id == 696:  # AI YOLO world detailed detection
            for event_list in root:
                for event in event_list:
                    channel = self._get_channel_from_xml_element(event, "channel")
                    if channel is None:
                        continue
                    channels.add(channel)

                    yolo_dict = self._ai_yolo_696.setdefault(channel, {})
                    sub_type_dict = self._ai_yolo_sub_type.setdefault(channel, {})
                    for key in yolo_dict:
                        yolo_dict[key] = False
                    for key in sub_type_dict:
                        sub_type_dict[key] = None

                    for event_type in event.findall("YoloWorldType"):
                        yolo_type = self._get_value_from_xml_element(event_type, "type")
                        if yolo_type is None:
                            continue
                        if not self._events_active and self._subscribed:
                            self._events_active = True
                        yolo_type = YOLO_CONVERSION.get(yolo_type, yolo_type)
                        if not self.http_api.supported(channel, f"ai_{yolo_type}") or yolo_type not in YOLO_DETECTS:
                            if f"TCP_yolo_event_unknown_{yolo_type}" not in self._log_once:
                                self._log_once.add(f"TCP_yolo_event_unknown_{yolo_type}")
                                _LOGGER.warning("Reolink %s TCP event channel %s, received unknown yolo AI event '%s'", self.http_api.nvr_name, channel, yolo_type)
                            continue

                        _LOGGER.debug("Reolink %s TCP yolo event channel %s, %s: True", self.http_api.nvr_name, channel, yolo_type)
                        yolo_dict[yolo_type] = True

                        sub_type = None
                        for type_item in event_type.findall("subTypeList"):
                            new_sub_type = self._get_value_from_xml_element(type_item, "subType")
                            if new_sub_type is None:
                                continue
                            new_sub_type = new_sub_type.replace(" ", "_")
                            if new_sub_type not in YOLO_DETECT_TYPES.get(yolo_type, []):
                                if f"TCP_yolo_type_unknown_{new_sub_type}" not in self._log_once:
                                    self._log_once.add(f"TCP_yolo_type_unknown_{new_sub_type}")
                                    _LOGGER.warning(
                                        "Reolink %s TCP event channel %s, received unknown yolo AI event sub type '%s' of type '%s'",
                                        self.http_api.nvr_name,
                                        channel,
                                        new_sub_type,
                                        yolo_type,
                                    )
                                continue
                            if sub_type is None:
                                sub_type = new_sub_type
                            else:
                                sub_type = f"{sub_type}, {new_sub_type}"
                        sub_type_dict[yolo_type] = sub_type

        elif cmd_id == 603:  # sceneListID
            for scene_id in root.findall(".//id"):
                if scene_id.text is not None:
                    self._scenes[int(scene_id.text)] = UNKNOWN

        elif cmd_id == 623:  # Privacy mode
            items = root.findall(".//status")
            if not items:
                items.append(root)
            for item in items:
                channel = self._get_channel_from_xml_element(item)
                if channel is None:
                    channel = 0
                channels.add(channel)
                state = self._get_value_from_xml_element(item, "sleep", bool)
                if state is not None:
                    self._privacy_mode[channel] = state
                    _LOGGER.debug("Reolink %s TCP event channel %s, Privacy mode: %s", self.http_api.nvr_name, channel, state)

        # call the callbacks
        for cmd in cmd_ids:
            for ch in channels:
                for callback in self._ext_callback.get(cmd, {}).get(ch, {}).values():
                    callback()

    def _parse_smart_ai_settings(self, root: XML.Element, channels: set[int | None], smart_type: str) -> None:
        """Parse smart ai settings response"""
        channel = self._get_channel_from_xml_element(root)
        if channel is None:
            return
        channels.add(channel)

        original_index_dict = {}
        for loc in self.smart_location_list(channel, smart_type):
            idx = self.smart_ai_index(channel, smart_type, loc)
            original_index_dict[f"{loc}_{idx}"] = self.smart_ai_type_list(channel, smart_type, loc)

        index_dict = {}
        for item in root.findall(f".//{smart_type}DetectItem"):
            location = self._get_value_from_xml_element(item, "location", int)
            if location is None:
                continue
            smart_ai = self._ai_detect.setdefault(channel, {}).setdefault(smart_type, {}).setdefault(location, {})
            smart_ai["name"] = self._get_value_from_xml_element(item, "name", str)
            smart_ai["sensitivity"] = self._get_value_from_xml_element(item, "sesensitivity", int)
            if (delay := self._get_value_from_xml_element(item, "stayTime", int)) is not None:
                smart_ai["delay"] = delay
            if (delay := self._get_value_from_xml_element(item, "timeThresh", int)) is not None:
                smart_ai["delay"] = delay
            smart_ai["index"] = self._get_value_from_xml_element(item, "index", int)
            smart_ai.setdefault("state", False)
            if (ai_types := self._get_value_from_xml_element(item, "aiType", str)) is not None:
                ai_type_list = ai_types.split(",")
                for ai_type in ai_type_list:
                    smart_ai.setdefault(ai_type, False)

            index_dict[f"{location}_{smart_ai['index']}"] = self.smart_ai_type_list(channel, smart_type, location)

        if not self.http_api._startup and index_dict != original_index_dict:
            _LOGGER.info(
                "New Reolink %s smart detection zone discovered for %s",
                smart_type,
                self.http_api.camera_name(channel),
            )
            self.http_api._new_devices = True

    async def _keepalive_loop(self) -> None:
        """Loop which keeps the TCP connection allive when subscribed for events"""
        now: float = 0
        while True:
            try:
                while self._protocol is not None:
                    now = time_now()
                    self._time_keepalive_loop = now
                    sleep_t = min(self._keepalive_interval - (now - self._protocol.time_recv), self._keepalive_interval)
                    if sleep_t < 0.5:
                        break
                    await asyncio.sleep(sleep_t)

                self._time_keepalive_loop = time_now()
                _LOGGER.debug("Baichuan host %s: sending keepalive for event subscription", self._host)
                try:
                    if self._events_active:
                        await self.send(cmd_id=93)  # LinkType is used as keepalive
                    else:
                        await self.send(cmd_id=31, ch_id=251)  # Subscribe to events
                except Exception as err:
                    _LOGGER.debug("Baichuan host %s: error while sending keepalive for event subscription: %s", self._host, str(err))

                if (
                    self._keepalive_interval < KEEP_ALLIVE_INTERVAL
                    and self._protocol is not None
                    and now - self._protocol.time_connect > 3600
                    and now - self._time_keepalive_increase > 3600
                ):
                    self._time_keepalive_increase = now
                    origianal_keepalive = self._keepalive_interval
                    self._keepalive_interval = min(KEEP_ALLIVE_INTERVAL, self._keepalive_interval + 1)
                    _LOGGER.debug("Baichuan host %s: increasing keepalive interval from %.2f to %.2f s", self._host, origianal_keepalive, self._keepalive_interval)

                await asyncio.sleep(self._keepalive_interval)
            except Exception as err:
                _LOGGER.exception("Baichuan host %s: error during keepalive loop: %s", self._host, str(err))

    async def subscribe_events(self) -> None:
        """Subscribe to baichuan push events, keeping the connection open"""
        if self._subscribed:
            _LOGGER.debug("Baichuan host %s: already subscribed to events", self._host)
            return
        self._subscribed = True
        self._time_keepalive_loop = time_now()
        try:
            await self.send(cmd_id=31, ch_id=251)
        except Exception as err:
            _LOGGER.debug("Baichuan host %s: error while subscribing: %s", self._host, str(err))
        if self._keepalive_task is None:
            self._keepalive_task = self._loop.create_task(self._keepalive_loop())

    async def unsubscribe_events(self) -> None:
        """Unsubscribe from the baichuan push events"""
        self._subscribed = False
        self._events_active = False
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        await self.logout()

    async def check_subscribe_events(self) -> None:
        """Subscribe to baichuan push events, keeping the connection open"""
        if not self._subscribed:
            await self.subscribe_events()
            return

        if time_now() - self._time_keepalive_loop > 5 * KEEP_ALLIVE_INTERVAL:
            # keepalive loop seems to have stopped running, restart
            _LOGGER.error("Baichuan host %s: keepalive loop seems to have stopped running, restarting", self._host)
            self._events_active = False
            if self._keepalive_task is not None:
                self._keepalive_task.cancel()
            self._keepalive_task = self._loop.create_task(self._keepalive_loop())

    async def login(self) -> None:
        """Login using the Baichuan protocol"""
        async with self._login_mutex:
            if self._logged_in:
                return

            nonce = await self._get_nonce()

            # modern login
            self._user_hash = md5_str_modern(f"{self._username}{nonce}")
            self._password_hash = md5_str_modern(f"{self._password}{nonce}")
            xml = xmls.LOGIN_XML.format(userName=self._user_hash, password=self._password_hash)

            try:
                mess = await self.send(cmd_id=1, enc_type=EncType.BC, body=xml)
            except ApiError as err:
                if err.rspCode == 401:
                    raise CredentialsInvalidError(f"Baichuan host {self._host}: Invalid credentials during login") from err
                raise
            self._logged_in = True

        # parse response
        root = XML.fromstring(mess)
        if (dev_info := root.find(".//DeviceInfo")) is not None:
            # is_nvr / is_hub
            dev_type_ob = dev_info.find("type")
            dev_type_info_ob = dev_info.find("typeInfo")
            if dev_type_ob is not None and dev_type_info_ob is not None:
                if dev_type_ob.text is not None:
                    self._dev_type = dev_type_ob.text
                dev_type_info = dev_type_info_ob.text
                if not self.http_api._is_nvr:
                    self.http_api._is_nvr = self._dev_type in ["nvr", "wifi_nvr", "homehub"] or dev_type_info in ["NVR", "WIFI_NVR", "HOMEHUB"]
                if not self.http_api._is_hub:
                    self.http_api._is_hub = self._dev_type == "homehub" or dev_type_info == "HOMEHUB"

            data = self._get_keys_from_xml(dev_info, {"sleep": ("sleep", bool), "channelNum": ("channelNum", int)})
            # privacy mode
            if "sleep" in data:
                self._privacy_mode[0] = data["sleep"]
            # channels
            if "channelNum" in data and self.http_api._num_channels == 0 and not self.http_api._is_nvr:
                self.http_api._channels.clear()
                self.http_api._num_channels = data["channelNum"]
                if self.http_api._num_channels > 0:
                    for ch in range(self.http_api._num_channels):
                        self.http_api._channels.append(ch)

        self.http_api._enc_range = {}
        for info in root.findall(".//StreamInfo"):
            channelBits = self._get_value_from_xml_element(info, "channelBits", int)
            if channelBits is None:
                continue
            for ch in range(0, channelBits.bit_length(), 1):
                if not (channelBits >> ch) & 1:
                    continue
                enc_range = self.http_api._enc_range.setdefault(ch, [])
                enc_data: dict[str, Any] = {"chnBit": channelBits}
                for encoding in info.findall(".//encodeTable"):
                    stream_type = self._get_value_from_xml_element(encoding, "type", str)
                    if stream_type is None:
                        continue
                    enc_data[stream_type] = self._get_keys_from_xml(encoding, {"width": ("width", int), "height": ("height", int)})
                    framerateTable = self._get_value_from_xml_element(encoding, "framerateTable", str)
                    if framerateTable is not None:
                        enc_data[stream_type]["frameRate"] = [int(val) for val in framerateTable.split(",")]
                    bitrateTable = self._get_value_from_xml_element(encoding, "bitrateTable", str)
                    if bitrateTable is not None:
                        enc_data[stream_type]["bitRate"] = [int(val) for val in bitrateTable.split(",")]
                enc_range.append(enc_data)

    async def logout(self) -> None:
        """Close the TCP session and cleanup"""
        if self._subscribed:
            # first call unsubscribe_events
            _LOGGER.debug("Baichuan host %s: logout called while still subscribed, keeping connection", self._host)
            return

        if self._logged_in and self._transport is not None and self._protocol is not None:
            try:
                xml = xmls.LOGOUT_XML.format(userName=self._username, password=self._password)
                await self.send(cmd_id=2, body=xml)
            except ReolinkError as err:
                _LOGGER.error("Baichuan host %s: failed to logout: %s", self._host, err)

            try:
                self._transport.close()
                await self._protocol.close_future
            except ConnectionResetError as err:
                _LOGGER.debug("Baichuan host %s: connection already reset when trying to close: %s", self._host, err)

        self._logged_in = False
        self._events_active = False
        self._transport = None
        self._protocol = None
        self._nonce = None
        self._aes_key = None
        self._user_hash = None
        self._password_hash = None

    def register_callback(self, callback_id: str, callback: Callable[[], None], cmd_id: int | None = None, channel: int | None = None) -> None:
        """Register a callback which is called when a push event is received"""
        self._ext_callback.setdefault(cmd_id, {})
        self._ext_callback[cmd_id].setdefault(channel, {})
        if callback_id in self._ext_callback[cmd_id][channel]:
            _LOGGER.warning("Baichuan host %s: callback id '%s', cmd_id %s, ch %s already registered, overwriting", self._host, callback_id, cmd_id, channel)
        self._ext_callback[cmd_id][channel][callback_id] = callback

    def unregister_callback(self, callback_id: str) -> None:
        """Unregister a callback"""
        for cmd_id in list(self._ext_callback):
            for channel in list(self._ext_callback[cmd_id]):
                self._ext_callback[cmd_id][channel].pop(callback_id, None)
                if not self._ext_callback[cmd_id][channel]:
                    self._ext_callback[cmd_id].pop(channel)
            if not self._ext_callback[cmd_id]:
                self._ext_callback.pop(cmd_id)

    async def get_host_data(self) -> None:
        """Fetch the host settings/capabilities."""
        # Get Baichaun capabilities
        try:
            mess = await self.send(cmd_id=199)
        except ReolinkError as err:
            _LOGGER.debug("Baichuan host %s: Could not obtain abilities (cmd_id 199): %s", self._host, str(err).replace(f"Baichuan host {self._host}: ", ""))
        else:
            root = XML.fromstring(mess)
            for support in root:
                for item in support.findall("item"):
                    # channel item
                    channel = self._get_channel_from_xml_element(item, "chnID")
                    self._abilities[channel] = item
                    support.remove(item)
                self._abilities[None] = support

            # check if HTTP(s) API is supported
            if self.api_version("netPort", no_key_return=55) <= 1:
                self.http_api.baichuan_only = True

        # Host capabilities
        self.capabilities.setdefault(None, set())
        for channel in self.http_api._channels:
            self.capabilities.setdefault(channel, set())
        if self.api_version("reboot") > 0:
            self.capabilities[None].add("reboot")
        if (io_inputs := self.api_version("IOInputPortNum")) > 0:
            channel = None if self.http_api._is_nvr else 0
            self._io_inputs[channel] = list(range(0, io_inputs))
        if (io_outputs := self.api_version("IOOutputPortNum")) > 0:
            channel = None if self.http_api._is_nvr else 0
            self._io_outputs[channel] = list(range(0, io_outputs))
        host_coroutines: list[tuple[Any, Coroutine]] = []
        host_coroutines.append(("network_info", self.get_network_info()))
        host_coroutines.append(("ability_info", self._get_ability_info()))
        if self.api_version("sceneModeCfg") > 0:
            host_coroutines.append((603, self.send(cmd_id=603)))
        if self.api_version("wifi") > 0:
            self.capabilities[None].add("wifi")
        if self.api_version("rtsp") > 0 and self.http_api._rtsp_port is not None:
            self.capabilities[None].add("RTSP")
        if self.api_version("onvif") > 0 and self.http_api._onvif_port is not None:
            self.capabilities[None].add("ONVIF")
        if self.http_api.is_hub and self.api_version("doorbellVersion") > 0:
            host_coroutines.append(("dingdonglist", self.GetDingDongList()))

        if host_coroutines:
            try:
                async with asyncio.timeout(3 * TIMEOUT):
                    results = await asyncio.gather(*[cor[1] for cor in host_coroutines], return_exceptions=True)
            except asyncio.TimeoutError:
                _LOGGER.warning("Baichuan host %s: Timeout of 3*%s sec getting host capabilities", self._host, TIMEOUT)
                results = []
            for i, result in enumerate(results):
                cmd_id, _ = host_coroutines[i]
                if isinstance(result, ReolinkError):
                    _LOGGER.debug("%s, during getting of host capabilities cmd_id %s", result, cmd_id)
                    continue
                if isinstance(result, BaseException):
                    raise result

                if cmd_id == 603:  # sceneListID
                    self.capabilities[None].add("scenes")
                    self._scenes[-1] = "off"
                    self._parse_xml(cmd_id, result)
                elif cmd_id == "dingdonglist":
                    if self.http_api._GetDingDong_present.get(None):
                        self.capabilities[None].add("chime")

        for channel in self.http_api._channels:
            ptz_ver = self.api_version("ptzType", channel)
            if ptz_ver != 0:
                self.capabilities[channel].add("ptz")
                if ptz_ver in [2, 3, 5]:
                    self.capabilities[channel].add("tilt")
                if ptz_ver in [2, 3, 5, 7]:
                    self.capabilities[channel].add("pan_tilt")
                    self.capabilities[channel].add("pan")
                    if self.api_version("ptzPreset", channel) > 0:
                        self.capabilities[channel].add("ptz_preset_basic")

                    ptz_ctr = self.api_version("ptzControl", channel)
                    if not (ptz_ctr >> 1) & 1:  # 2th bit (2), shift 1
                        self.capabilities[channel].add("ptz_diagonal")
                    if (ptz_ctr >> 2) & 1:  # 3th bit (4), shift 2
                        self.capabilities[channel].add("ptz_guard")
                    if (ptz_ctr >> 3) & 1:  # 4th bit (8), shift 3
                        self.capabilities[channel].add("ptz_callibrate")
                    if (ptz_ctr >> 6) & 1:  # 7th bit (64), shift 6
                        self.capabilities[channel].add("ptz_speed")

    async def get_channel_data(self) -> None:
        """Fetch the channel settings/capabilities."""
        # Stream capabilities
        RtspVersion = self.api_version("rtsp")
        for channel in self.http_api._stream_channels:
            self.capabilities.setdefault(channel, set())

            if RtspVersion > 0:
                self.capabilities[channel].add("stream")
            if RtspVersion > 0 or self.api_version("encCtrl", channel) > 0:
                self.capabilities[channel].add("snapshot")

        # Channel capabilities
        coroutines: list[tuple[Any, int, Coroutine]] = []
        for channel in self.http_api._channels:
            if self.http_api.is_nvr and self.http_api.wifi_connection(channel) and (self.http_api.api_version("supportWiFi", channel) > 0 or self.http_api._is_hub):
                coroutines.append(("wifi", channel, self.get_wifi_signal(channel)))

            if self.http_api.api_version("talk", channel) > 0:
                coroutines.append((10, channel, self.send(cmd_id=10, channel=channel)))

            if (self.http_api.is_nvr or self.privacy_mode() is not None) and self.api_version("remoteAbility", channel) > 0:
                coroutines.append(("privacy_mode", channel, self.get_privacy_mode(channel)))  # capability added in get_privacy_mode

            if self.http_api.api_version("supportIfttt", channel) > 0 or self.api_version("linkages", channel) > 0:
                coroutines.append(("rules", channel, self.get_rule_ids(channel)))

            SmartaiVersion = self.api_version("smartAI", channel)
            if (SmartaiVersion >> 1) & 1:  # 2th bit (2), shift 1
                coroutines.append((527, channel, self.send(cmd_id=527, channel=channel)))  # crossline
            if (SmartaiVersion >> 2) & 1:  # 3th bit (4), shift 2
                coroutines.append((529, channel, self.send(cmd_id=529, channel=channel)))  # intrusion
            if (SmartaiVersion >> 3) & 1:  # 4th bit (8), shift 3
                coroutines.append((531, channel, self.send(cmd_id=531, channel=channel)))  # loitering/linger
            if (SmartaiVersion >> 4) & 1:  # 5th bit (16), shift 4
                coroutines.append((549, channel, self.send(cmd_id=549, channel=channel)))  # legacy/forgotten item
            if (SmartaiVersion >> 5) & 1:  # 6th bit (32), shift 5
                coroutines.append((551, channel, self.send(cmd_id=551, channel=channel)))  # loss/taken item

            newIspCfg = self.api_version("newIspCfg", channel)
            if (newIspCfg >> 0) & 1 and self.http_api.daynight_state(channel) is not None:  # 1th bit (1), shift 0
                self.capabilities[channel].add("dayNight")
                if (newIspCfg >> 13) & 1 or (newIspCfg >> 16) & 1:  # 17th bit (65536), shift 16
                    coroutines.append(("day_night_state", channel, self.get_day_night_state(channel)))
            if (newIspCfg >> 2) & 1:  # 3th bit (4), shift 2
                self.capabilities[channel].add("exposure")
            if (newIspCfg >> 8) & 1 or (newIspCfg >> 14) & 1:  # 9th bit (256), shift 8
                self.capabilities[channel].add("isp_bright")  # 8 = brightness, 14 = brightness&shadows
            if (newIspCfg >> 9) & 1:
                self.capabilities[channel].add("isp_contrast")
            if (newIspCfg >> 10) & 1:
                self.capabilities[channel].add("isp_satruation")
            if (newIspCfg >> 11) & 1:
                self.capabilities[channel].add("isp_hue")
            if (newIspCfg >> 12) & 1:
                self.capabilities[channel].add("isp_sharpen")

            if self.api_version("motion", channel, no_key_return=1) > 0:
                self.capabilities[channel].add("motion_detection")
                self.http_api._motion_detection_states.setdefault(channel, False)

            aiVersion = self.api_version("aitype", channel)
            if (aiVersion >> 1) & 1:  # 2th bit (2), shift 1
                self.http_api._ai_detection_support.setdefault(channel, {})["people"] = True
                self.http_api._ai_detection_states.setdefault(channel, {}).setdefault("people", False)
            if (aiVersion >> 2) & 1:  # 3th bit (4), shift 2
                self.http_api._ai_detection_support.setdefault(channel, {})["vehicle"] = True
                self.http_api._ai_detection_states.setdefault(channel, {}).setdefault("vehicle", False)
            if (aiVersion >> 3) & 1:  # 4th bit (8), shift 3
                self.http_api._ai_detection_support.setdefault(channel, {})["face"] = True
                self.http_api._ai_detection_states.setdefault(channel, {}).setdefault("face", False)
            if (aiVersion >> 4) & 1:  # 5th bit (16), shift 4
                self.http_api._ai_detection_support.setdefault(channel, {})["dog_cat"] = True
                self.http_api._ai_detection_states.setdefault(channel, {}).setdefault("dog_cat", False)
            if (aiVersion >> 6) & 1:  # 7th bit (64), shift 6
                self.capabilities[channel].add("motion_detection")  # other detection (PIR)
                self.http_api._motion_detection_states.setdefault(channel, False)
            if (aiVersion >> 8) & 1:  # 9th bit (256), shift 8
                self.capabilities[channel].add("ai_delay")
            if (aiVersion >> 9) & 1:  # 10th bit (512), shift 9
                self.capabilities[channel].add("ai_sensitivity")
            if (aiVersion >> 17) & 1:  # 18th bit (131072), shift 17
                self.http_api._ai_detection_support.setdefault(channel, {})["package"] = True
                self.http_api._ai_detection_states.setdefault(channel, {}).setdefault("package", False)
            if (aiVersion >> 22) & 1:  # 23th bit (4194304), shift 22
                coroutines.append(("cry", channel, self.get_cry_detection(channel)))
            if (aiVersion >> 23) & 1:  # 24th bit (8388608), shift 23 Yolo World
                self.http_api._ai_detection_support.setdefault(channel, {})["package"] = True
                self.http_api._ai_detection_states.setdefault(channel, {}).setdefault("package", False)
                self.capabilities[channel].add("ai_non-motor vehicle")
                self.capabilities[channel].add("ai_yolo")
                if (self.api_version("aiAnimalType", channel) >> 1) & 1:  # 2th bit (2), shift 1
                    self.capabilities[channel].add("ai_yolo_type")

            if self.http_api.api_version("doorbellVersion", channel) > 0:
                self.http_api._is_doorbell[channel] = True
                self.http_api._visitor_states.setdefault(channel, False)
            if self.http_api.is_doorbell(channel) and self.http_api.supported(channel, "battery"):
                self.capabilities[channel].add("hardwired_chime")
                # cmd_id 483 makes the chime rattle a bit, just assume its supported
                # coroutines.append((483, channel, self.get_ding_dong_ctrl(channel)))

            if self.supported(channel, "pan_tilt"):
                coroutines.append(("ptz_position", channel, self.get_ptz_position(channel)))

            ledVersion = self.api_version("ledCtrl", channel)
            if (ledVersion >> 0) & 1:  # 1th bit (1), shift 0
                self.capabilities[channel].add("status_led")  # internal use only
                self.capabilities[channel].add("power_led")
            if (ledVersion >> 1) & 1 and (ledVersion >> 2) & 1:  # 2nd bit (2), shift 1, 3nd bit (4), shift 2
                self.capabilities[channel].add("floodLight")
            if (ledVersion >> 12) & 1:  # 13 th bit (4096) shift 12
                self.capabilities[channel].add("ir_brightness")
            if (ledVersion >> 17) & 1:  # 18 th bit (131072) shift 17
                self.capabilities[channel].add("color_temp")
            if (ledVersion >> 19) & 1:  # 20 th bit (524288) shift 19
                self.capabilities[channel].add("floodlight_event")

            if (self.api_version("recordCfg", channel) >> 7) & 1:  # 8 th bit (128) shift 7
                self.capabilities[channel].add("pre_record")

            if self.http_api.is_nvr and self.api_version("reboot", channel) > 0:
                self.capabilities[channel].add("reboot")

            audioVersion = self.api_version("audioVersion", channel)
            if (audioVersion >> 2) & 1:  # 3 th bit (4) shift 2
                self.capabilities[channel].add("siren_play")
                if self.http_api.api_version("supportIfttt", channel) <= 0 and self.api_version("linkages", channel) <= 0:
                    self.capabilities[channel].add("siren")
            if (audioVersion >> 4) & 1 or (audioVersion >> 9) & 1:  # 5 & 10 th bit (16 & 512) shift 4 & 9
                coroutines.append(("GetAudioCfg", channel, self.GetAudioCfg(channel)))
            if self.http_api.api_version("supportAIDenoise", channel) > 0:
                coroutines.append(("GetAudioNoise", channel, self.GetAudioNoise(channel)))

            if self.http_api.supported(channel, "PIR"):
                # check for pir interval compatability
                coroutines.append(("GetPirInfo", channel, self.GetPirInfo(channel)))
            if self._dev_type == "light":
                self.capabilities[channel].add("PIR")  # probably the rfVersion flag

            coroutines.append(("network_info", channel, self.get_network_info(channel)))
            # Fallback for missing information
            if self.http_api.camera_hardware_version(channel) == UNKNOWN:
                coroutines.append(("ch_info", channel, self.get_info(channel)))

        for scene_id in self._scenes:
            if scene_id < 0:
                continue
            coroutines.append(("scene", scene_id, self.get_scene_info(scene_id)))

        if coroutines:
            try:
                async with asyncio.timeout(3 * TIMEOUT):
                    results = await asyncio.gather(*[cor[2] for cor in coroutines], return_exceptions=True)
            except asyncio.TimeoutError:
                _LOGGER.warning("Baichuan host %s: Timeout of 3*%s sec getting channel capabilities", self._host, TIMEOUT)
                results = []
            for i, result in enumerate(results):
                cmd_id, channel, _ = coroutines[i]
                if isinstance(result, ReolinkError):
                    _LOGGER.debug("%s, during getting of channel capabilities cmd_id %s", result, cmd_id)
                    continue
                if isinstance(result, BaseException):
                    raise result

                if cmd_id == 10:  # two way audio
                    root = XML.fromstring(result)
                    for audio in root.findall(".//audioStreamMode"):
                        if audio.text == "mixAudioStream":
                            self.capabilities[channel].add("two_way_audio")
                if cmd_id == 483:  # hardwired chime
                    self.capabilities[channel].add("hardwired_chime")
                if cmd_id == 527:  # crossline detection
                    self.capabilities[channel].add("ai_crossline")
                    self._parse_xml(cmd_id, result)
                elif cmd_id == 529:  # intrusion detection
                    self.capabilities[channel].add("ai_intrusion")
                    self._parse_xml(cmd_id, result)
                elif cmd_id == 531:  # linger detection
                    self.capabilities[channel].add("ai_linger")
                    self._parse_xml(cmd_id, result)
                elif cmd_id == 549:  # forgotten item
                    self.capabilities[channel].add("ai_forgotten_item")
                    self._parse_xml(cmd_id, result)
                elif cmd_id == 551:  # taken item
                    self.capabilities[channel].add("ai_taken_item")
                    self._parse_xml(cmd_id, result)
                elif cmd_id == "wifi":
                    self.capabilities[channel].add("wifi")
                elif cmd_id == "rules":
                    self.capabilities[channel].add("rules")
                elif cmd_id == "day_night_state":
                    newIspCfg = self.api_version("newIspCfg", channel)
                    if ((newIspCfg >> 13) & 1 or (newIspCfg >> 16) & 1) and self.http_api.daynight_threshold(channel) is not None:
                        self.capabilities[channel].add("dayNightThreshold")
                    if self.day_night_state(channel) is not None:
                        self.capabilities[channel].add("day_night_state")
                elif cmd_id == "cry" and result:
                    self.capabilities[channel].add("ai_cry")
                elif cmd_id == "ptz_position":
                    if self.http_api.ptz_pan_position(channel) is not None:
                        self.capabilities[channel].add("ptz_position")
                        self.capabilities[channel].add("ptz_pan_position")
                    if self.http_api.ptz_tilt_position(channel) is not None:
                        self.capabilities[channel].add("ptz_position")
                        self.capabilities[channel].add("ptz_tilt_position")
                elif cmd_id == "GetAudioCfg":
                    self.capabilities[channel].add("volume")
                    if self.api_version("doorbellVersion", channel) > 0 and "visitorLoudspeaker" in self.http_api._audio_settings.get(channel, {}):
                        self.capabilities[channel].add("doorbell_button_sound")
                    if self.http_api.volume_speak(channel) is not None:
                        self.capabilities[channel].add("volume_speak")
                    if self.http_api.volume_doorbell(channel) is not None:
                        self.capabilities[channel].add("volume_doorbell")
                elif cmd_id == "GetAudioNoise":
                    self.capabilities[channel].add("noise_reduction")
                elif cmd_id == "GetPirInfo":
                    if self.http_api._pir.get(channel, {}).get("interval_max", 0) > 0:
                        self.capabilities[channel].add("PIR_interval")

    def supported(self, channel: int | None, capability: str) -> bool:
        """Return if a capability is supported by a camera channel."""
        if channel not in self.capabilities:
            return False

        return capability in self.capabilities[channel]

    def api_version(self, capability: str, channel: int | None = None, no_key_return: int = 0) -> int:
        """Return the api version of a capability, 0=not supported, >0 is supported"""
        if channel not in self._abilities:
            return no_key_return

        value = self._get_value_from_xml_element(self._abilities[channel], capability, int)
        if value is None:
            return no_key_return

        return value

    @property
    def abilities(self) -> dict[int | str, Any]:
        """Return the abilities as a dictionary"""
        abilities_dict: dict[int | str, dict[str, int | str]] = {}
        for key, xml in self._abilities.items():
            pretty_key: str | int = key if key is not None else "Host"
            abilities_dict[pretty_key] = {}
            for feature in xml:
                if feature.text is not None:
                    value: int | str
                    try:
                        value = int(feature.text)
                    except ValueError:
                        value = feature.text
                    abilities_dict[pretty_key][feature.tag] = value
        return abilities_dict

    def _analyze_ability_info(self, ability_info: XML.Element, token: str, capability: dict[str, str]) -> None:
        """Analyze a ability info segment."""
        if (info := ability_info.find(token)) is not None:
            for info_ch in info.findall("subModule"):
                channel = self._get_channel_from_xml_element(info_ch)
                if channel is None:
                    raise UnexpectedDataError(f"Baichuan host {self._host}: get_ability_info got unexpected data")
                ability = self._get_value_from_xml_element(info_ch, "abilityValue")
                if ability is None:
                    raise UnexpectedDataError(f"Baichuan host {self._host}: get_ability_info got unexpected data")
                for key, value in capability.items():
                    if key in ability:
                        self.capabilities[channel].add(value)

    async def _get_ability_info(self) -> None:
        """Get ability info as part of get_host_data."""
        xml = xmls.AbilityInfo.format(username=self._username)
        try:
            mess = await self.send(cmd_id=151, extension=xml)
            root = XML.fromstring(mess)
            if (ability_info := root.find("AbilityInfo")) is None:
                raise UnexpectedDataError(f"Baichuan host {self._host}: get_ability_info got unexpected data")
            self._analyze_ability_info(ability_info, "image", {"ledState_rw": "ir_lights"})
        except ReolinkError:
            for channel in self.http_api._channels:
                self.capabilities[channel].add("ir_lights")
            raise

        self._analyze_ability_info(ability_info, "video", {"shelter_rw": "privacy_mask_basic"})

    async def get_states(self, cmd_list: cmd_list_type = None, wake: dict[int, bool] | None = None) -> None:
        """Update the state information of polling data"""
        if cmd_list is None:
            cmd_list = {}
        if wake is None:
            wake = dict.fromkeys(self.http_api._channels, True)

        any_battery = any(self.http_api.supported(ch, "battery") for ch in self.http_api._channels)
        all_wake = all(wake.values())

        def inc_host_cmd(cmd: str, no_wake_check=False) -> bool:
            return (cmd in cmd_list or not cmd_list) and (no_wake_check or (all_wake or not any_battery or cmd in NONE_WAKING_COMMANDS))

        def inc_cmd(cmd: str, channel: int) -> bool:
            return (channel in cmd_list.get(cmd, []) or not cmd_list or len(cmd_list.get(cmd, [])) == 1) and (
                wake[channel] or cmd in NONE_WAKING_COMMANDS or not self.http_api.supported(channel, "battery")
            )

        def inc_ch_wake_cmd(cmd: str, channel: int | None = None):
            if channel is None:
                return inc_host_cmd(cmd, no_wake_check=True)
            return inc_cmd(cmd, channel)

        coroutines: list[Coroutine] = []
        # host
        if self.supported(None, "scenes") and inc_host_cmd("GetScene"):
            coroutines.append(self.get_scene())
        if self.http_api.supported(None, "wifi") and self.http_api.wifi_connection() and inc_host_cmd("115"):
            coroutines.append(self.get_wifi_signal())
        if self.supported(None, "chime"):  # always include to discover new chimes and update "online" status, not waking
            coroutines.append(self.GetDingDongList())
        if self.supported(None, "chime") and inc_host_cmd("GetDingDongCfg", no_wake_check=True):
            # None waking for Hub connected
            coroutines.append(self.GetDingDongCfg())

        # channels
        for channel in self.http_api._channels:
            if not self.http_api.camera_online(channel):
                continue

            if self.http_api.supported(channel, "wifi") and inc_cmd("115", channel):
                coroutines.append(self.get_wifi_signal(channel))

            if self.supported(channel, "rules"):
                coroutines.append(self.get_rule_ids(channel))
                if inc_cmd("rules", channel):
                    for rule_id in self.rule_ids(channel):
                        coroutines.append(self.get_rule(rule_id, channel))

            if self.supported(channel, "day_night_state") and inc_cmd("296", channel):
                coroutines.append(self.get_day_night_state(channel))

            if self.http_api.supported(channel, "chime") and inc_cmd("GetDingDongCfg", channel):
                coroutines.append(self.GetDingDongCfg(channel))

            if self.supported(channel, "hardwired_chime") and channel in cmd_list.get("483", []) and channel not in self._hardwired_chime_settings:
                # only get the state if not known yet, cmd_id 483 can make the hardwired chime rattle a bit
                coroutines.append(self.get_ding_dong_ctrl(channel))

            if self.supported(channel, "ir_brightness") and inc_cmd("208", channel):
                coroutines.append(self.get_status_led(channel))

            if self.supported(channel, "color_temp") and inc_cmd("GetWhiteLed", channel):
                coroutines.append(self.get_floodlight(channel))

            if self.supported(channel, "ai_cry") and inc_cmd("299", channel):
                coroutines.append(self.get_cry_detection(channel))

            if self.supported(channel, "ai_yolo") and self.http_api.supported(channel, "ai_sensitivity") and inc_cmd("GetAiAlarm", channel):
                coroutines.append(self.get_yolo_settings(channel))

            if self.http_api.supported(channel, "PIR") and inc_cmd("GetPirInfo", channel):
                coroutines.append(self.GetPirInfo(channel))

            if self.supported(channel, "ptz_position") and inc_cmd("GetPtzCurPos", channel):
                coroutines.append(self.get_ptz_position(channel))

            if self.supported(channel, "pre_record") and inc_cmd("594", channel):
                coroutines.append(self.get_pre_recording(channel))

            if self.supported(channel, "volume") and inc_cmd("GetAudioCfg", channel):
                coroutines.append(self.GetAudioCfg(channel))

            if self.supported(channel, "noise_reduction") and inc_cmd("439", channel):
                coroutines.append(self.GetAudioNoise(channel))

        # chimes
        for chime_id, chime in self.http_api._chime_list.items():
            if not chime.online:
                continue

            if inc_ch_wake_cmd("609", chime.channel):
                coroutines.append(self.get_ding_dong_silent(channel=chime.channel, chime_id=chime_id))

            if inc_host_cmd("DingDongOpt", no_wake_check=True) and chime.channel is not None:
                # None waking for Hub connected
                coroutines.append(self.get_DingDongOpt(chime_id=chime_id))

        if coroutines:
            try:
                async with asyncio.timeout(3 * TIMEOUT):
                    results = await asyncio.gather(*coroutines, return_exceptions=True)
            except asyncio.TimeoutError:
                if self._log_error:
                    _LOGGER.warning("Baichuan host %s: Timeout of 3*%s sec getting states", self._host, TIMEOUT)
                    self._log_error = False
                return
            self._log_error = True
            for result in results:
                if isinstance(result, ReolinkError):
                    _LOGGER.debug(result)
                    continue
                if isinstance(result, BaseException):
                    raise result

    @http_cmd("GetNetPort")
    async def get_ports(self, **_kwargs) -> dict[str, dict[str, int | bool]]:
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

        if (bc_port := self._ports.get("server", {}).get("port")) is not None and bc_port != self.port:
            _LOGGER.warning("Baichuan host %s: baichuan port changed from %s to %s", self._host, self.port, bc_port)
            self.port = bc_port

        if self.rtsp_port is not None:
            self.http_api._rtsp_port = self.rtsp_port
        if self.rtmp_port is not None:
            self.http_api._rtmp_port = self.rtmp_port
        if self.onvif_port is not None:
            self.http_api._onvif_port = self.onvif_port
            self.http_api._subscribe_url = f"http://{self.http_api._host}:{self.onvif_port}/onvif/event_service"
        if self.rtsp_enabled is not None:
            self.http_api._rtsp_enabled = self.rtsp_enabled
        if self.rtmp_enabled is not None:
            self.http_api._rtmp_enabled = self.rtmp_enabled
        if self.onvif_enabled is not None:
            self.http_api._onvif_enabled = self.onvif_enabled

        return self._ports

    @http_cmd("SetNetPort")
    async def set_port_enabled(self, port: PortType | None = None, enable: bool | None = None, **kwargs) -> None:
        """set the HTTP(S)/RTSP/RTMP/ONVIF port"""
        if port is None or enable is None:
            net = kwargs.get("NetPort", {})
            if (val := net.get("onvifEnable")) is not None:
                await self.set_port_enabled(PortType.onvif, val == 1)
            if (val := net.get("rtmpEnable")) is not None:
                await self.set_port_enabled(PortType.rtmp, val == 1)
            if (val := net.get("rtspEnable")) is not None:
                await self.set_port_enabled(PortType.rtsp, val == 1)
            return

        xml_body = XML.Element("body")
        main = XML.SubElement(xml_body, port.value.capitalize() + "Port", version="1.1")
        sub = XML.SubElement(main, "enable")
        sub.text = "1" if enable else "0"
        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml

        await self.send(cmd_id=36, body=xml)

    @http_cmd(["GetDevInfo", "GetChnTypeInfo"])
    async def get_info(self, channel: int | None = None) -> dict[str, str]:
        """Get the device info of the host or a channel"""
        if channel is None:
            mess = await self.send(cmd_id=80)
        else:
            try:
                mess = await self.send(cmd_id=318, channel=channel)
            except ReolinkError:
                if not self.http_api._GetChnTypeInfo_present:
                    raise
                if self.http_api.is_nvr and self.http_api._channel_online_check.get(channel, True):
                    _LOGGER.warning("Baichuan host %s: GetChnTypeInfo failed, camera %s, channel %s is offline", self._host, self.http_api.camera_name(channel), channel)
                self.http_api._channel_online_check[channel] = False
                raise
            self.http_api._GetChnTypeInfo_present = True
            self.http_api._channel_online_check[channel] = True
        self._dev_info[channel] = self._get_keys_from_xml(mess, ["type", "hardwareVersion", "firmwareVersion", "itemNo", "serialNumber", "name"])
        dev_info = self._dev_info[channel]

        # see login for is_nvr/is_hub
        if "name" in dev_info:
            self.http_api._name[channel] = dev_info["name"]
        if "type" in dev_info:
            self.http_api._model[channel] = dev_info["type"]
        if "hardwareVersion" in dev_info:
            self.http_api._hw_version[channel] = dev_info["hardwareVersion"]
        if "itemNo" in dev_info:
            self.http_api._item_number[channel] = dev_info["itemNo"]
        if "serialNumber" in dev_info:
            self.http_api._serial[channel] = dev_info["serialNumber"]
        if dev_info.get("firmwareVersion") not in ["", None]:
            self.http_api._sw_version[channel] = dev_info["firmwareVersion"]
            try:
                self.http_api._sw_version_object[channel] = SoftwareVersion(self.http_api._sw_version[channel])
            except UnexpectedDataError as err:
                _LOGGER.debug("Reolink %s: %s", self.http_api.camera_name(channel), err)

        return self._dev_info[channel]

    @http_cmd("GetEnc")
    async def GetEnc(self, channel: int) -> None:
        """Get the encoding info of a channel"""
        root = await self.send(cmd_id=56, channel=channel)
        mess = XML.fromstring(root)
        audio = 1
        if (main := mess.find(".//mainStream")) is not None:
            data = self._get_keys_from_xml(
                main,
                {
                    "audio": ("audio", int),
                    "width": ("width", int),
                    "height": ("height", int),
                    "videoEncType": ("vType_int", int),
                    "frame": ("frameRate", int),
                    "bitRate": ("bitRate", int),
                },
            )
            data["vType"] = list(EncodingEnum)[data.get("vType_int", 0)].value
            self.http_api._enc_settings.setdefault(channel, {}).setdefault("mainStream", {}).update(data)
            audio = audio and data.get("audio", 0)
        if (sub := mess.find(".//subStream")) is not None:
            data = self._get_keys_from_xml(
                sub,
                {
                    "audio": ("audio", int),
                    "width": ("width", int),
                    "height": ("height", int),
                    "videoEncType": ("vType_int", int),
                    "frame": ("frameRate", int),
                    "bitRate": ("bitRate", int),
                },
            )
            data["vType"] = list(EncodingEnum)[data.get("vType_int", 0)].value
            self.http_api._enc_settings.setdefault(channel, {}).setdefault("subStream", {}).update(data)
            audio = audio and data.get("audio", 0)
        self.http_api._enc_settings.setdefault(channel, {})["audio"] = audio
        self.http_api._enc_settings[channel]["channel"] = channel

    @http_cmd("SetEnc")
    async def SetEnc(self, channel: int | None = None, stream: str | None = None, encoding: str | None = None, **kwargs) -> None:
        """Set the encoding info of a stream"""
        param = kwargs.get("Enc", {})
        param_sub = param.get("subStream", {})
        param_main = param.get("mainStream", {})
        audio = param.get("audio")
        sub_bitrate = param_sub.get("bitRate")
        sub_frameRate = param_sub.get("frameRate")
        main_bitrate = param_main.get("bitRate")
        main_frameRate = param_main.get("frameRate")
        if channel is None:
            channel = param.get("channel")
        if channel is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: SetEnc channel not defined")

        mess = await self.send(cmd_id=56, channel=channel)
        xml_body = XML.fromstring(mess)

        if stream is not None and encoding is not None:
            xml_stream = xml_body.find(f".//{stream}Stream")
            if xml_stream is None:
                raise InvalidParameterError(f"Baichuan host {self._host}: SetEnc could not find stream {stream} in XML")
            if (xml_encoding := xml_stream.find(".//videoEncType")) is not None:
                encoding_int = 1 if encoding == "h265" else 0
                xml_encoding.text = str(encoding_int)

        if audio is not None:
            for xml_audio in xml_body.findall(".//audio"):
                xml_audio.text = str(audio)
        xml_sub = xml_body.find(".//subStream")
        if xml_sub is not None:
            if sub_bitrate is not None and (xml_sub_bitrate := xml_sub.find(".//bitRate")) is not None:
                xml_sub_bitrate.text = str(sub_bitrate)
            if sub_frameRate is not None and (xml_sub_framerate := xml_sub.find(".//frame")) is not None:
                xml_sub_framerate.text = str(sub_frameRate)
        xml_main = xml_body.find(".//mainStream")
        if xml_main is not None:
            if main_bitrate is not None and (xml_main_bitrate := xml_main.find(".//bitRate")) is not None:
                xml_main_bitrate.text = str(main_bitrate)
            if main_frameRate is not None and (xml_main_framerate := xml_main.find(".//frame")) is not None:
                xml_main_framerate.text = str(main_frameRate)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=57, channel=channel, body=xml)

    @http_cmd(["GetImage", "GetIsp"])
    async def GetImage(self, channel: int, **_kwargs) -> None:
        """Get the image settings"""
        await self._send_and_parse(26, channel)
        if self.supported(channel, "day_night_state"):
            try:
                await self._send_and_parse(296, channel)
            except ReolinkError as err:
                _LOGGER.debug(err)

    @http_cmd("SetImage")
    async def SetImage(self, **kwargs) -> None:
        """Set the image settings"""
        param = kwargs["Image"]
        channel = param.get("channel")
        mess = await self.send(cmd_id=26, channel=channel)
        xml_body = XML.fromstring(mess)

        if (bright := param.get("bright")) is not None and (xml_bright := xml_body.find("VideoInput/bright")) is not None:
            xml_bright.text = str(bright)
        if (contrast := param.get("contrast")) is not None and (xml_contrast := xml_body.find("VideoInput/contrast")) is not None:
            xml_contrast.text = str(contrast)
        if (saturation := param.get("saturation")) is not None and (xml_saturation := xml_body.find("VideoInput/saturation")) is not None:
            xml_saturation.text = str(saturation)
        if (hue := param.get("hue")) is not None and (xml_hue := xml_body.find("VideoInput/hue")) is not None:
            xml_hue.text = str(hue)
        if (sharpen := param.get("sharpen")) is not None and (xml_sharpen := xml_body.find("VideoInput/sharpen")) is not None:
            xml_sharpen.text = str(sharpen)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=25, channel=channel, body=xml)

    @http_cmd("SetIsp")
    async def SetIsp(self, **kwargs) -> None:
        """Set the ISP settings"""
        param = kwargs["Isp"]
        channel = param.get("channel")
        mess = await self.send(cmd_id=26, channel=channel)
        xml_body = XML.fromstring(mess)

        val: int | str | None = None
        if (val := param.get("dayNight")) is not None and (xml_val := xml_body.find("InputAdvanceCfg/DayNight/mode")) is not None:
            val = str(val).replace("&", "And")
            val = val[0].lower() + val[1:]
            xml_val.text = val
        if (val := param.get("binningMode")) is not None and (xml_val := xml_body.find(".//binning_mode")) is not None:
            xml_val.text = str(val)
        if (val := param.get("hdr")) is not None and (xml_val := xml_body.find(".//hdrSwitch")) is not None:
            xml_val.text = str(val)
        if (val := param.get("exposure")) is not None and (xml_val := xml_body.find("InputAdvanceCfg/Exposure/mode")) is not None:
            xml_val.text = val.lower()

        if val is not None:
            xml = XML.tostring(xml_body, encoding="unicode")
            xml = xmls.XML_HEADER + xml
            await self.send(cmd_id=25, channel=channel, body=xml)

        if (val := param.get("dayNightThreshold")) is not None:
            mess = await self.send(cmd_id=296, channel=channel)
            xml_body = XML.fromstring(mess)

            if (xml_val := xml_body.find(".//cur")) is not None:
                xml_val.text = str(val)

            xml = XML.tostring(xml_body, encoding="unicode")
            xml = xmls.XML_HEADER + xml
            await self.send(cmd_id=297, channel=channel, body=xml)

    async def snapshot(self, channel: int, iLogicChannel: int = 0, snapType: str = "sub", **_kwargs) -> bytes:
        """Get a JPEG snapshot image"""
        xml = xmls.Snap.format(channel=channel, logicChannel=iLogicChannel, stream=snapType)
        mess, image = await self.send_payload(cmd_id=109, channel=channel, body=xml)

        image_size = self._get_value_from_xml_element(XML.fromstring(mess), "pictureSize", int)
        if image_size is None:
            raise UnexpectedDataError(f"Baichuan host {self._host}: Did not receive image size for snapshot channel {channel}")

        if image_size != len(image):
            raise UnexpectedDataError(f"Baichuan host {self._host}: received snapshot image size {len(image)} does not match expected size {image_size} for channel {channel}")

        return image

    async def snapshot_past(self, channel: int, time: datetime, snapType: str = "sub", ffmpeg: str = "ffmpeg") -> bytes:
        """Get a JPEG image from a past recording (thumbnail)"""
        end = time + timedelta(seconds=10)
        xml = xmls.CoverPreview.format(
            channel=channel,
            stream=snapType,
            start_year=time.year,
            start_month=time.month,
            start_day=time.day,
            start_hour=time.hour,
            start_minute=time.minute,
            start_second=time.second,
            end_year=end.year,
            end_month=end.month,
            end_day=end.day,
            end_hour=end.hour,
            end_minute=end.minute,
            end_second=end.second,
        )
        _mess, payload = await self.send_payload(cmd_id=298, body=xml)

        # parse stream header
        stream_header = payload[0:32]
        magic = stream_header[0:4]
        # width = int.from_bytes(stream_header[8:12], byteorder="little")
        # height = int.from_bytes(stream_header[12:16], byteorder="little")
        # frame_rate = stream_header[17]
        # start_year = 1900 + stream_header[18]
        if magic != b"1001":
            raise UnexpectedDataError(f"Baichuan host {self._host}: snapshot_past payload did not start with stream header magic b'1001' but with {magic!r}")

        # search magic
        try:
            # search magic
            start = payload[32::].index(b"00dc")
        except ValueError as err:
            raise UnexpectedDataError(f"Baichuan host {self._host}: snapshot_past frame magic b'00dc' not found, first bytes: {payload[32:62]!r}") from err
        idx = 32 + start

        # parse frame header
        idx_start = idx + 12
        idx_end = idx_start + 4
        header_len = 24 + int.from_bytes(payload[idx_start:idx_end], byteorder="little")
        idx_end = idx + header_len
        frame_header = payload[idx:idx_end]
        idx += header_len
        # magic = frame_header[0:4]
        # encoding = frame_header[4:8].decode("utf8")
        frame_len = int.from_bytes(frame_header[8:12], byteorder="little")
        # frame_time = int.from_bytes(frame_header[24:28], byteorder="little")
        # frame_microsecond = int.from_bytes(frame_header[16:20], byteorder="little")
        # formatted_time = datetime.fromtimestamp(frame_time).strftime("%Y-%m-%d %H:%M:%S")

        # extract frame
        idx_end = idx + frame_len
        frame = payload[idx:idx_end]
        idx += frame_len

        image = await i_frame_to_jpeg(frame, ffmpeg)

        return image

    @http_cmd("GetP2p")
    async def get_uid(self) -> None:
        """Get the UID of the host"""
        root = await self.send(cmd_id=114)
        mess = XML.fromstring(root)
        value = self._get_value_from_xml_element(mess, "uid")
        if value is not None:
            self.http_api._uid[None] = value

    async def get_channel_uids(self) -> None:
        """Get a channel list containing the UIDs"""
        # the NVR sends a message with cmd_id 145 when connecting, but it seems to not allow requesting that id.
        await self.send(cmd_id=145)

    @http_cmd("GetLocalLink")
    async def get_network_info(self, channel: int | None = None, **_kwargs) -> dict[str, str]:
        """Get the network info including MAC of the host or a channel"""
        if channel is None:
            mess = await self.send(cmd_id=76)
            mess_link = await self.send(cmd_id=93)
        else:
            mess = await self.send(cmd_id=76, channel=channel)
            if self.http_api.api_version("supportWiFi", channel) > 0 or self.http_api._is_hub:
                try:
                    await self.get_wifi_signal(channel)
                except ReolinkError as err:
                    _LOGGER.debug(err)
        self._network_info[channel] = self._get_keys_from_xml(mess, ["ip", "mac"])

        if channel is None:
            link_dict = self._get_keys_from_xml(mess_link, ["type"])
            if (link := link_dict.get("type")) is not None:
                self.http_api._local_link.setdefault("LocalLink", {})["activeLink"] = link
            if (mac := self.mac_address()) is not None:
                self.http_api._mac_address = mac
        else:
            signal = self.http_api.wifi_signal(channel)
            if signal is not None:
                if signal == 100:
                    self._wifi_connection[channel] = False
                else:
                    self._wifi_connection[channel] = True

        return self._network_info[channel]

    @http_cmd("GetUser")
    async def GetUser(self) -> None:
        """Get the user list"""
        xml = xmls.UserList.format(username=self._username)
        root = await self.send(cmd_id=58, extension=xml)
        mess = XML.fromstring(root)
        self.http_api._users = []
        for user in mess.findall(".//User"):
            values = self._get_keys_from_xml(user, ["userName", "userLevel"])

            if values.get("userLevel") == "1":
                values["level"] = "admin"
            else:
                values["level"] = "guest"
            self.http_api._users.append(values)

    async def get_wifi_ssid(self, channel: int) -> None:
        """Get the wifi ssid and link type"""
        mess = await self.send(cmd_id=116, channel=channel, body=xmls.WifiSSID)
        root = XML.fromstring(mess)
        self._get_value_from_xml_element(root, "ssid", str)

    async def get_wifi_signal(self, channel: int | None = None) -> None:
        """Get the wifi signal of the host"""
        mess = await self.send(cmd_id=115, channel=channel)
        root = XML.fromstring(mess)
        value = self._get_value_from_xml_element(root, "signal")
        if value is not None:
            self.http_api._wifi_signal[channel] = int(value)

    @http_cmd("GetPtzCurPos")
    async def get_ptz_position(self, channel: int) -> None:
        """Get the current PTZ position"""
        await self._send_and_parse(433, channel)

    @http_cmd("GetZoomFocus")
    async def get_zoom_focus(self, channel: int) -> None:
        """Get the current PTZ position"""
        await self._send_and_parse(294, channel)

    @http_cmd("GetPtzPreset")
    async def get_ptz_preset(self, channel: int) -> None:
        """Get the PTZ preset list"""
        mess = await self.send(cmd_id=190, channel=channel)
        root = XML.fromstring(mess)
        for preset in root.findall(".//preset"):
            data = self._get_keys_from_xml(preset, {"id": ("id", int), "name": ("name", str)})
            self.http_api._ptz_presets.setdefault(channel, {})[data["name"]] = data["id"]

    @http_cmd("GetPtzGuard")
    async def get_ptz_guard(self, channel: int) -> None:
        """Get the PTZ Guard settings"""
        mess = await self.send(cmd_id=332, channel=channel)
        data = self._get_keys_from_xml(mess, {"timeout": ("timeout", int), "benable": ("benable", int), "bvalid": ("bexistPos", int)})
        self.http_api._ptz_guard_settings.setdefault(channel, {}).update(data)

    @http_cmd("SetPtzGuard")
    async def set_ptz_guard(self, **kwargs) -> None:
        """Set the PTZ Guard settings"""
        param = kwargs["PtzGuard"]
        channel = param["channel"]
        if param.get("cmdStr") == "toPos":
            cmd_str = "toGrd"
        else:
            cmd_str = "setGrd"

        await self.get_ptz_guard(channel)
        val = self.http_api._ptz_guard_settings.get(channel, {})

        enable = param.get("benable", val.get("benable", 1))
        timeout = param.get("timeout", val.get("timeout", 60))
        set_pos = param.get("bSaveCurrentPos", 0)

        xml = xmls.PtzGuard.format(channel=channel, enable=enable, cmd_str=cmd_str, timeout=timeout, set_pos=set_pos)
        await self.send(cmd_id=331, channel=channel, body=xml)

    @http_cmd("PtzCheck")
    async def ptz_callibrate(self, channel: int) -> None:
        await self.send(cmd_id=341, channel=channel)

    @http_cmd("PtzCtrl")
    async def set_ptz_command(self, channel: int, op: str, speed: int | None = None, **kwargs) -> None:
        xml_base = xmls.PtzControl.format(channel=channel, command=op)
        xml_body = XML.fromstring(xml_base[1:])

        if speed is not None and (xml_speed := xml_body.find(".//PtzControl")) is not None:
            XML.SubElement(xml_speed, "speed").text = str(speed)
        if (id_val := kwargs.get("id")) is not None:
            if op == "ToPos":  # preset
                xml = xmls.PtzPreset.format(channel=channel, preset_id=id_val)
                await self.send(cmd_id=19, channel=channel, body=xml)
                return
            raise NotSupportedError("PTZ patrol not yet supported")

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=18, channel=channel, body=xml)

    @http_cmd(["GetAlarm", "GetMdAlarm"])
    async def GetMdAlarm(self, channel: int | None = None, **kwargs) -> None:
        """Get the motion sensitivity"""
        channel = kwargs.get("Alarm", {}).get("channel", channel)
        mess = await self.send(cmd_id=46, channel=channel)
        root = XML.fromstring(mess)

        info = root.find(".//sensInfoNew")
        if info is None:
            raise UnexpectedDataError(f"Baichuan host {self._host}: GetMdAlarm fallback channel {channel} got unexpected data")
        sens = self._get_value_from_xml_element(info, "sensitivityDefault", int)
        if sens is None:
            raise UnexpectedDataError(f"Baichuan host {self._host}: GetMdAlarm fallback channel {channel} got unexpected data")
        if self.http_api.baichuan_only:
            self.http_api._api_version["GetMdAlarm"] = 1
        self.http_api._md_alarm_settings.setdefault(channel, {})["useNewSens"] = 1
        self.http_api._md_alarm_settings[channel].setdefault("newSens", {})["sensDef"] = sens

    @http_cmd(["SetAlarm", "SetMdAlarm"])
    async def SetMdAlarm(self, **kwargs) -> None:
        """Set the motion sensitivity"""
        channel = kwargs.get("MdAlarm", {}).get("channel")
        if channel is None:
            channel = kwargs.get("Alarm", {}).get("channel")
        sensitivity = kwargs.get("MdAlarm", {}).get("newSens", {}).get("sensDef")
        if sensitivity is None:
            sensitivity = kwargs.get("Alarm", {}).get("sens", [{}])[0].get("sensitivity")
        if channel is None or sensitivity is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: invalid param for SetMdAlarm channel {channel}")

        mess = await self.send(cmd_id=46, channel=channel)
        xml_body = XML.fromstring(mess)

        info = xml_body.find(".//sensInfoNew")
        if info is None:
            raise UnexpectedDataError(f"Baichuan host {self._host}: SetMdAlarm fallback channel {channel} got unexpected data")
        xml_sensitivity = info.find(".//sensitivityDefault")
        if xml_sensitivity is None:
            raise UnexpectedDataError(f"Baichuan host {self._host}: SetMdAlarm fallback channel {channel} got unexpected data")
        xml_sensitivity.text = str(sensitivity)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=47, channel=channel, body=xml)

    @http_cmd("GetAiAlarm")
    async def GetAiAlarm(self, channel: int, ai_type: str) -> None:
        """Get the AI detection sensitivity/delay"""
        xml = xmls.GetAiAlarm.format(channel=channel, ai_type=ai_type)
        mess = await self.send(cmd_id=342, channel=channel, body=xml)
        data = self._get_keys_from_xml(mess, {"sensitivity": ("sensitivity", int), "stayTime": ("stay_time", int)})
        self.http_api._ai_alarm_settings.setdefault(channel, {}).setdefault(ai_type, {}).update(data)

    @http_cmd("SetAiAlarm")
    async def SetAiAlarm(self, **kwargs) -> None:
        """Set the AI detection sensitivity/delay"""
        param = kwargs.get("AiAlarm", {})
        channel = param.get("channel")
        ai_type = param.get("ai_type")
        sensitivity = param.get("sensitivity")
        stay_time = param.get("stay_time")
        if channel is None or ai_type is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: invalid param for SetAiAlarm channel {channel}")

        xml = xmls.GetAiAlarm.format(channel=channel, ai_type=ai_type)
        mess = await self.send(cmd_id=342, channel=channel, body=xml)
        xml_body = XML.fromstring(mess)

        if sensitivity is not None and (xml_sensitivity := xml_body.find(".//sensitivity")) is not None:
            xml_sensitivity.text = str(sensitivity)
        if stay_time is not None and (xml_stay_time := xml_body.find(".//stayTime")) is not None:
            xml_stay_time.text = str(stay_time)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=343, channel=channel, body=xml)

    async def get_cry_detection(self, channel: int) -> bool:
        """Check if cry detection is supported and get the sensitivity level"""
        mess = await self.send(cmd_id=299, channel=channel)
        data = self._get_keys_from_xml(mess, {"cryDetectAbility": ("cryDetectAbility", str), "cryDetectLevel": ("cryDetectLevel", int)})
        if (cry_sensitivity := data.get("cryDetectLevel")) is not None:
            self._cry_sensitivity[channel] = cry_sensitivity
        return data.get("cryDetectAbility") == "1"  # supported or not

    async def set_cry_detection(self, channel: int, sensitivity: int) -> None:
        mess = await self.send(cmd_id=299, channel=channel)
        xml_body = XML.fromstring(mess)

        if (xml_cry_sensitivity := xml_body.find(".//cryDetectLevel")) is not None:
            xml_cry_sensitivity.text = str(sensitivity)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=300, channel=channel, body=xml)
        await self.get_cry_detection(channel)

    async def get_yolo_settings(self, channel: int) -> None:
        """Get the yoloworld AI settings"""
        mess = await self.send(cmd_id=628, channel=channel)
        root = XML.fromstring(mess)
        self.http_api._ai_alarm_settings.setdefault(channel, {})
        for conf in root.findall(".//YoloWorldCfg"):
            data = self._get_keys_from_xml(conf, {"type": ("type", str), "sensitivity": ("sensitivity", int), "stayTime": ("stay_time", int)})
            if (ai_type := data.get("type")) is not None:
                data.pop("type")
                ai_type = YOLO_CONVERSION.get(ai_type, ai_type)
                self.http_api._ai_alarm_settings[channel][ai_type] = data

    async def set_yolo_settings(self, channel: int, ai_type: str, sensitivity: int | None = None, delay: int | None = None) -> None:
        mess = await self.send(cmd_id=628, channel=channel)
        xml_body = XML.fromstring(mess)

        for conf in xml_body.findall(".//YoloWorldCfg"):
            conf_ai_type = self._get_value_from_xml_element(conf, "type")
            if conf_ai_type is None:
                continue
            conf_ai_type = YOLO_CONVERSION.get(conf_ai_type, conf_ai_type)
            if conf_ai_type == ai_type:
                if sensitivity is not None and (xml_sensitivity := conf.find(".//sensitivity")) is not None:
                    xml_sensitivity.text = str(sensitivity)
                if delay is not None and (xml_delay := conf.find(".//stayTime")) is not None:
                    xml_delay.text = str(delay)
                break

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=629, channel=channel, body=xml)
        await self.get_yolo_settings(channel)

    async def get_day_night_state(self, channel: int) -> None:
        """Get the day night state"""
        await self._send_and_parse(296, channel)

    async def get_pre_recording(self, channel: int) -> None:
        """Get the pre recording settings"""
        mess = await self.send(cmd_id=594, channel=channel)
        self._pre_record_state[channel] = self._get_keys_from_xml(
            mess,
            {"enable": ("enabled", int), "value": ("batteryStop", int), "preTime": ("preTime", int), "fps": ("fps", int), "usePlanList": ("schedule", int)},
        )

    async def set_pre_recording(self, channel: int, enabled: bool | None = None, time: int | None = None, fps: int | None = None, battery_stop: int | None = None) -> None:
        """Set the pre recording settings"""
        await self.get_pre_recording(channel)
        data = self._pre_record_state[channel]

        enable_str = data["enabled"]
        if enabled is not None:
            enable_str = "1" if enabled else "0"
        if time is None:
            time = data["preTime"]
        if fps is None:
            fps = data["fps"]
        if battery_stop is None:
            battery_stop = data["batteryStop"]

        xml = xmls.PreRecord.format(
            enable=enable_str,
            batteryStop=battery_stop,
            preTime=time,
            fps=fps,
            schedule=data["schedule"],
        )
        await self.send(cmd_id=595, channel=channel, body=xml)
        await self.get_pre_recording(channel)

    async def get_privacy_mode(self, channel: int = 0) -> bool | None:
        """Get the privacy mode state"""
        mess = await self.send(cmd_id=574, channel=channel)
        sleep = self._get_value_from_xml_element(XML.fromstring(mess), "sleep", bool)
        if sleep is None:
            return None
        self._privacy_mode[channel] = sleep
        now = time_now()
        self.last_privacy_check = now
        if sleep:
            self.last_privacy_on = now

        self.capabilities.setdefault(channel, set()).add("privacy_mode")
        if not self.http_api.is_nvr:
            self.capabilities.setdefault(None, set()).add("privacy_mode")

        return sleep

    async def set_privacy_mode(self, channel: int = 0, enable: bool = False) -> None:
        """Set the privacy mode"""
        enable_str = "1" if enable else "0"
        xml = xmls.SetPrivacyMode.format(enable=enable_str)
        await self.send(cmd_id=575, channel=channel, body=xml)
        # get the new privacy mode status
        await self.get_privacy_mode(channel)

    @http_cmd("GetMask")
    async def GetMask(self, channel: int, **_kwargs) -> None:
        """Get the privacy mask"""
        mess = await self.send(cmd_id=52, channel=channel)
        data = self._get_keys_from_xml(mess, {"Shelter/enable": ("enable", int)}, recursive=False)
        root = XML.fromstring(mess)
        if root.find("Shelter/shelterList/Shelter") is not None:
            data["area"] = True
        self.http_api._privacy_mask.setdefault(channel, {}).update(data)

    @http_cmd("SetMask")
    async def SetMask(self, **kwargs) -> None:
        """Get the privacy mask"""
        param = kwargs["Mask"]
        channel = param["channel"]
        mess = await self.send(cmd_id=52, channel=channel)
        xml_body = XML.fromstring(mess)

        if (enable := param.get("enable")) is not None and (xml_enable := xml_body.find("Shelter/enable")) is not None:
            xml_enable.text = str(enable)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=53, channel=channel, body=xml)

    async def reboot(self, channel: int | None = None) -> None:
        """Reboot the host device"""
        if not self.http_api.supported(channel, "reboot"):
            raise NotSupportedError(f"Baichuan host {self._host}: Reboot not supported by channel {channel}")

        await self.send(cmd_id=23, channel=channel)

    @http_cmd("GetWhiteLed")
    async def get_floodlight(self, channel: int, **_kwargs) -> None:
        """Get the floodlight state"""
        await self._send_and_parse(289, channel)

    @http_cmd("SetWhiteLed")
    async def set_floodlight(
        self,
        channel: int = 0,
        state: bool | None = None,
        brightness: int | None = None,
        mode: int | None = None,
        color_temp: int | None = None,
        event_mode: str | None = None,
        event_brightness: int | None = None,
        event_on_time: int | None = None,
        event_flash_time: int | None = None,
        **kwargs,
    ) -> None:
        """Control the floodlight"""
        if data := kwargs.get("WhiteLed"):
            channel = data["channel"]
            state = data.get("state", state)
            brightness = data.get("bright", brightness)
            mode = data.get("mode", mode)

        if (
            state is None
            and brightness is None
            and mode is None
            and color_temp is None
            and event_mode is None
            and event_brightness is None
            and event_on_time is None
            and event_flash_time is None
        ):
            raise InvalidParameterError(f"Baichuan host {self._host}: invalid param for SetWhiteLed")

        if mode == SpotlightModeEnum.schedule.value and (self.api_version("ledCtrl", channel) >> 8) & 1:  # schedule_plus, 9th bit (256), shift 8
            # Floodlight: the schedule_plus has the same number 4 as autoadaptive, so switch it around
            mode = 4

        if state is not None:
            xml = xmls.SetWhiteLed.format(channel=channel, state=state)
            await self.send(cmd_id=288, channel=channel, body=xml)
        if (
            brightness is not None
            or mode is not None
            or color_temp is not None
            or event_mode is not None
            or event_brightness is not None
            or event_on_time is not None
            or event_flash_time is not None
        ):
            get_state = False
            mess = await self.send(cmd_id=289, channel=channel)
            xml_body = XML.fromstring(mess)
            xml_element = xml_body.find(".//FloodlightTask")
            if xml_element is None:
                raise UnexpectedDataError(f"Baichuan host {self._host}: set_floodlight: could not find FloodlightTask")
            if brightness is not None and (xml_brightness := xml_element.find("brightness_cur")) is not None:
                xml_brightness.text = str(brightness)
            if color_temp is not None and (xml_color_temp := xml_element.find("newColorTemperature")) is not None:
                get_state = True
                if color_temp < MIN_COLOR_TEMP or color_temp > MAX_COLOR_TEMP:
                    raise InvalidParameterError(f"Baichuan host {self._host}: set_floodlight: color_temp {color_temp} not in range {MIN_COLOR_TEMP}...{MAX_COLOR_TEMP}")
                xml_color_temp.text = str(round(100 * (MAX_COLOR_TEMP - color_temp) / (MAX_COLOR_TEMP - MIN_COLOR_TEMP)))
            if mode is not None:
                xml_mode = xml_element.find("alarmMode")
                if xml_mode is not None:
                    xml_mode.text = str(mode)
                xml_enable = xml_element.find("enable")
                if xml_enable is not None:
                    xml_enable.text = str(mode)
            if (
                event_mode is not None
                and (xml_event_mode := xml_element.find("alarmLightModeSL")) is not None
                and (xml_event_enable := xml_element.find("alarmLightEnabledSL")) is not None
            ):
                get_state = True
                if event_mode == SpotlightEventModeEnum.off.value:
                    xml_event_enable.text = "0"
                else:
                    xml_event_enable.text = "1"
                    xml_event_mode.text = event_mode
            if event_brightness is not None and (xml_event_brightness := xml_element.find("brightnessAlarmSL")) is not None:
                get_state = True
                xml_event_brightness.text = str(event_brightness)
            if event_on_time is not None and (xml_on_time := xml_element.find("duration")) is not None:
                get_state = True
                xml_on_time.text = str(event_on_time)
            if event_flash_time is not None and (xml_flash_time := xml_element.find("flickerDurationSL")) is not None:
                get_state = True
                xml_flash_time.text = str(event_flash_time)
            xml = XML.tostring(xml_body, encoding="unicode")
            xml = xmls.XML_HEADER + xml
            await self.send(cmd_id=290, channel=channel, body=xml)

            if get_state:
                await self.get_floodlight(channel)

    @http_cmd(["GetPowerLed", "GetIrLights"])
    async def get_status_led(self, channel: int, **_kwargs) -> None:
        """Get the status led and IR light status"""
        mess = await self.send(cmd_id=208, channel=channel)
        data = self._get_keys_from_xml(
            mess, {"IRLedBrightness": ("ir_brightness", int), "state": ("ir_state", str), "lightState": ("state", str), "doorbellLightState": ("eDoorbellLightState", str)}
        )

        if (val := data.get("state")) is not None:
            if val == "open":
                data["state"] = "On"
            else:
                data["state"] = "Off"

        if (ir_brightness := data.get("ir_brightness")) is not None:
            self._ir_brightness[channel] = ir_brightness

        self.http_api._status_led_settings.setdefault(channel, {}).setdefault("PowerLed", {}).update(data)
        if "ir_state" in data:
            self.http_api._ir_settings.setdefault(channel, {}).setdefault("IrLights", {})["state"] = data["ir_state"].capitalize()

    @http_cmd(["SetPowerLed", "SetIrLights"])
    async def set_status_led(self, channel: int | None = None, ir_brightness: int | None = None, **kwargs) -> None:
        """Get the status led and IR light status"""
        power_led = kwargs.get("PowerLed", {})
        ir_lights = kwargs.get("IrLights", {})
        if channel is None:
            channel = power_led.get("channel", ir_lights.get("channel"))
        if channel is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: invalid param for set_status_led")
        status_led = power_led.get("state")
        doorbell_led = power_led.get("eDoorbellLightState")
        ir_led = ir_lights.get("state")

        mess = await self.send(cmd_id=208, channel=channel)
        xml_body = XML.fromstring(mess)

        if status_led is not None and (xml_status_led := xml_body.find(".//lightState")) is not None:
            xml_status_led.text = "open" if status_led == "On" else "close"
        if doorbell_led is not None and (xml_doorbell_led := xml_body.find(".//doorbellLightState")) is not None:
            doorbell_led = doorbell_led[0].lower() + doorbell_led[1:]
            if doorbell_led == "on":
                doorbell_led = "open"
            if doorbell_led == "off":
                doorbell_led = "close"
            xml_doorbell_led.text = doorbell_led
        if ir_led is not None and (xml_ir_led := xml_body.find(".//state")) is not None:
            if ir_led == "Off":
                ir_led = "close"
            xml_ir_led.text = ir_led.lower()
        if ir_brightness is not None and (xml_ir_bright := xml_body.find(".//IRLedBrightness")) is not None:
            xml_ir_bright.text = str(ir_brightness)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=209, channel=channel, body=xml)

        if ir_brightness is not None:
            # Request the new value
            await self.get_status_led(channel)

    @http_cmd("AudioAlarmPlay")
    async def AudioAlarmPlay(self, channel: int | None = None, alarm_mode: str = "times", **kwargs) -> None:
        """Sound the siren"""
        enable = kwargs.get("manual_switch", 1)
        times = kwargs.get("times", 1)
        now = time_now()

        if channel is not None and alarm_mode == "times":
            xml = xmls.SirenTimes.format(channel=channel, times=times)
        elif channel is not None:  # "manul"
            xml = xmls.SirenManual.format(channel=channel, enable=enable)
        elif alarm_mode == "times":
            xml = xmls.SirenHubTimes.format(times=times)
        else:  # "manul"
            xml = xmls.SirenHubManual.format(enable=enable)

        if alarm_mode == "times":
            self._siren_play_time[channel] = now + times * 5
        elif enable == 1:
            self._siren_play_time[channel] = now
        elif enable == 0 and now < self._siren_play_time.get(channel, 0):
            _LOGGER.debug("Baichaun host %s: circumventing reolink firmware limitation stopping siren_play by issuing a manual play first", self._host)
            if channel is None:
                xml_man = xmls.SirenHubManual.format(enable=1)
            else:
                xml_man = xmls.SirenManual.format(channel=channel, enable=1)
            try:
                await self.send(cmd_id=263, channel=channel, body=xml_man)
            except ReolinkError as err:
                _LOGGER.debug("Baichaun host %s: AudioAlarmPlay failed to play manual, before trying to stop a siren times play: %s", self._host, err)

        try:
            await self.send(cmd_id=263, channel=channel, body=xml)
        except ReolinkError:
            if alarm_mode != "manul" or enable != 1:
                raise
            _LOGGER.debug("Baichaun host %s: AudioAlarmPlay failed to play manual, using times 2 instead", self._host)
            if channel is None:
                xml = xmls.SirenHubTimes.format(times=2)
            else:
                xml = xmls.SirenTimes.format(channel=channel, times=2)
            self._siren_play_time[channel] = now + 2 * 5
            await self.send(cmd_id=263, channel=channel, body=xml)

    @http_cmd("GetAudioCfg")
    async def GetAudioCfg(self, channel: int, **_kwargs) -> None:
        """Get the audio settings"""
        mess = await self.send(cmd_id=264, channel=channel)
        data = self._get_keys_from_xml(
            mess,
            {
                "volume": ("volume", int),
                "talkAndReplyVolume": ("talkAndReplyVolume", int),
                "visitorVolume": ("visitorVolume", int),
                "visitorLoudspeaker": ("visitorLoudspeaker", int),
            },
        )

        self.http_api._audio_settings.setdefault(channel, {}).update(data)

    @http_cmd("SetAudioCfg")
    async def SetAudioCfg(self, **kwargs) -> None:
        """Set the audio settings"""
        param = kwargs["AudioCfg"]
        channel = param["channel"]

        mess = await self.send(cmd_id=264, channel=channel)
        xml_body = XML.fromstring(mess)

        if (volume := param.get("volume")) is not None and (xml_volume := xml_body.find(".//volume")) is not None:
            xml_volume.text = str(volume)
        if (visitorLoudspeaker := param.get("visitorLoudspeaker")) is not None and (xml_visitorLoudspeaker := xml_body.find(".//visitorLoudspeaker")) is not None:
            xml_visitorLoudspeaker.text = str(visitorLoudspeaker)
        if (talkAndReplyVolume := param.get("talkAndReplyVolume")) is not None and (xml_talkAndReplyVolume := xml_body.find(".//talkAndReplyVolume")) is not None:
            xml_talkAndReplyVolume.text = str(talkAndReplyVolume)
        if (visitorVolume := param.get("visitorVolume")) is not None and (xml_visitorVolume := xml_body.find(".//visitorVolume")) is not None:
            xml_visitorVolume.text = str(visitorVolume)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=265, channel=channel, body=xml)

    async def GetAudioNoise(self, channel: int) -> None:
        """Get the audio noise reduction settings"""
        mess = await self.send(cmd_id=439, channel=channel)
        data = self._get_keys_from_xml(
            mess,
            {
                "enable": ("enable", bool),
                "level": ("level", int),
            },
        )

        if data.get("enable"):
            self._noise_reduction[channel] = data.get("level", 0)
        else:
            self._noise_reduction[channel] = 0

    async def SetAudioNoise(self, channel: int, level: int) -> None:
        """Set the audio noise reduction settings"""
        mess = await self.send(cmd_id=439, channel=channel)
        xml_body = XML.fromstring(mess)

        if (xml_enable := xml_body.find(".//enable")) is not None:
            if level <= 0:
                xml_enable.text = "0"
            else:
                xml_enable.text = "1"
        if level > 0 and (xml_level := xml_body.find(".//level")) is not None:
            xml_level.text = str(level)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=440, channel=channel, body=xml)
        await self.GetAudioNoise(channel)

    @http_cmd("GetDingDongList")
    async def GetDingDongList(self, channel: int | None = None, retry: int = 3, **_kwargs) -> None:
        """Get the DingDongList info"""
        retry = retry - 1

        mess = await self.send(cmd_id=484, channel=channel)
        root = XML.fromstring(mess)

        # check channel
        rec_channel = self._get_channel_from_xml_element(root, "channel")
        if rec_channel is not None and rec_channel != channel:
            # got a push command not belonging to the intended channel, retry
            if retry < 0:
                raise UnexpectedDataError(f"Baichuan host {self._host}: GetDingDongList received channel {rec_channel} while requesting channel {channel}")
            _LOGGER.debug("Baichuan host %s: GetDingDongList received channel %s from a push while requesting channel %s, retrying...", self._host, rec_channel, channel)
            return self.GetDingDongList(channel=channel, retry=retry)

        chime_list = []
        for chime in root.findall(".//dingdongDeviceInfo"):
            data = self._get_keys_from_xml(
                chime, {"id": ("deviceId", int), "name": ("deviceName", str), "netstate": ("netState", int), "netState": ("netState", int), "deviceType": ("deviceType", str)}
            )
            if data.get("deviceType", "chine") == "BatteryDB":  # Reolink made a spelling mistake: chine instead of chime
                # Skip the battery doorbell
                continue
            chime_list.append(data)
        json_data = {"cmd": "GetDingDongList", "code": 0, "value": {"DingDongList": {"pairedlist": chime_list}}}
        self.http_api.map_chime_json_response(json_data, channel)

    @http_cmd("DingDongOpt")
    async def get_DingDongOpt(self, channel: int | None = None, chime_id: int = -1, **kwargs) -> None:
        """Get the DingDongOpt info"""
        dingdong = kwargs.get("DingDong", {})
        if (ch := dingdong.get("channel")) is not None:
            channel = ch
        if (ring_id := dingdong.get("id")) is not None:
            chime_id = ring_id
        option = dingdong.get("option", 2)

        xml = ""
        if option == 1:
            xml = xmls.DingDongOpt_1_XML.format(chime_id=chime_id)
        if option == 2:
            xml = xmls.DingDongOpt_2_XML.format(chime_id=chime_id)
        if option == 3:
            name = dingdong.get("name", "Reolink Chime")
            vol = dingdong.get("volLevel", 4)
            led = dingdong.get("ledState", 1)
            xml = xmls.DingDongOpt_3_XML.format(chime_id=chime_id, vol=vol, led=led, name=name)
        if option == 4:
            tone_id = dingdong.get("musicId", 0)
            xml = xmls.DingDongOpt_4_XML.format(chime_id=chime_id, tone_id=tone_id)

        mess = await self.send(cmd_id=485, channel=channel, body=xml)

        if option == 2:
            data = self._get_keys_from_xml(mess, {"name": ("name", str), "volLevel": ("volLevel", int), "ledState": ("ledState", int)})
            json_data = {"cmd": "DingDongOpt", "code": 0, "value": {"DingDong": data}}
            self.http_api.map_chime_json_response(json_data, channel, chime_id)

    @http_cmd("GetDingDongCfg")
    async def GetDingDongCfg(self, channel: int | None = None, **_kwargs) -> None:
        """Get the GetDingDongCfg info"""
        mess = await self.send(cmd_id=486, channel=channel)
        root = XML.fromstring(mess)

        chime_list = []
        for chime in root.findall(".//deviceCfg"):
            data: dict[str, Any] = self._get_keys_from_xml(chime, {"id": ("ringId", int), "version": ("version", str)})
            if channel is None and data.get("ringId") not in self.http_api._chime_list:
                # do not process Battery Doorbell, use GetDingDongList to get the list of chimes
                continue
            data["type"] = {}
            for ringtone in chime.findall(".//alarminCfg"):
                tone_type = self._get_value_from_xml_element(ringtone, "type")
                if tone_type is None:
                    continue
                data["type"][tone_type.lower()] = self._get_keys_from_xml(ringtone, {"valid": ("switch", int), "musicId": ("musicId", int)})
            chime_list.append(data)
        json_data = {"cmd": "GetDingDongCfg", "code": 0, "value": {"DingDongCfg": {"pairedlist": chime_list}}}
        self.http_api.map_chime_json_response(json_data, channel)

    @http_cmd("SetDingDongCfg")
    async def SetDingDongCfg(self, **kwargs) -> None:
        """Get the GetDingDongCfg info"""
        dingdong_cfg = kwargs.get("DingDongCfg", {})
        channel = dingdong_cfg.get("channel", -1)
        chime_id = dingdong_cfg.get("ringId", -1)
        event_type_dict = dingdong_cfg.get("type", {"event": {}})
        event_type = list(event_type_dict.keys())[0]
        state = event_type_dict.get(event_type, {}).get("switch", 0)
        tone_id = event_type_dict.get(event_type, {}).get("musicId", -1)

        xml = xmls.SetDingDongCfg_XML.format(chime_id=chime_id, event_type=event_type, state=state, tone_id=tone_id)
        await self.send(cmd_id=487, channel=channel, body=xml)

    async def get_ding_dong_silent(self, chime_id: int, channel: int | None = None) -> None:
        """Get the Silent mode state for a Reolink chime"""
        if chime_id not in self.http_api._chime_list:
            raise InvalidParameterError(
                f"Baichuan host {self._host}: get_ding_dong_silent chime_id {chime_id} not in connected chimes {list(self.http_api._chime_list.values())}"
            )

        xml = xmls.DingDongSilent.format(chime_id=chime_id)
        mess = await self.send(cmd_id=609, channel=channel, body=xml)

        data: dict[str, Any] = self._get_keys_from_xml(mess, {"id": ("ringId", int), "time": ("silent", int)})
        if data.get("ringId") != chime_id:
            raise UnexpectedDataError(f"Baichuan host {self._host}: get_ding_dong_silent requested chime_id {chime_id} but received {data.get('ringId')}")

        chime = self.http_api._chime_list[chime_id]
        chime.silent_time = data.get("silent", 0)

    async def set_ding_dong_silent(self, chime_id: int, time: int, channel: int | None = None) -> None:
        """Get the Silent mode state for a Reolink chime"""
        if chime_id not in self.http_api._chime_list:
            raise InvalidParameterError(
                f"Baichuan host {self._host}: set_ding_dong_silent chime_id {chime_id} not in connected chimes {list(self.http_api._chime_list.values())}"
            )

        xml = xmls.SetDingDongSilent.format(chime_id=chime_id, time=time)
        await self.send(cmd_id=610, channel=channel, body=xml)

        await self.get_ding_dong_silent(chime_id, channel)

    async def get_ding_dong_ctrl(self, channel: int) -> None:
        """Get the DingDongCtrl info, this can make the hardwired chime rattle a bit"""
        xml = xmls.GetDingDongCtrl_XML
        mess = await self.send(cmd_id=483, channel=channel, body=xml)
        self._parse_hardwired_chime(mess, channel)

    async def set_ding_dong_ctrl(self, channel: int, chime_type: str | None = None, enable: bool | None = None) -> None:
        """Set the DingDongCtrl info"""
        await self.get_ding_dong_ctrl(channel)

        enabled = int(enable) if enable is not None else int(self.hardwired_chime_enabled(channel))
        chime_type = chime_type if chime_type is not None else self.hardwired_chime_type(channel)
        time = self._hardwired_chime_settings.get(channel, {}).get("time", 0)

        hardwired_chime_type_list = [val.value for val in HardwiredChimeTypeEnum]
        if chime_type not in hardwired_chime_type_list:
            raise InvalidParameterError(f"Baichuan host {self._host}: set_ding_dong_ctrl type {chime_type} not in {hardwired_chime_type_list}")

        xml = xmls.SetDingDongCtrl_XML.format(chime_type=chime_type, enabled=enabled, time=time)
        mess = await self.send(cmd_id=483, channel=channel, body=xml)
        self._parse_hardwired_chime(mess, channel)

    def _parse_hardwired_chime(self, mess: str, channel: int) -> None:
        """Parse hardwired chime response"""
        self._hardwired_chime_settings[channel] = self._get_keys_from_xml(mess, {"type": ("type", str), "bopen": ("enable", int), "time": ("time", int)})

    @http_cmd("QuickReplyPlay")
    async def QuickReplyPlay(self, **kwargs) -> None:
        """Get the GetDingDongCfg info"""
        channel = kwargs.get("channel", -1)
        file_id = kwargs.get("id", -1)

        xml = xmls.QuickReplyPlay_XML.format(channel=channel, file_id=file_id)
        await self.send(cmd_id=349, channel=channel, body=xml)

    @http_cmd(["GetRecV20", "GetRec"])
    async def GetRec(self, channel: int, **_kwargs) -> None:
        """Get the recording info"""
        mess = await self.send(cmd_id=81, channel=channel)
        root = XML.fromstring(mess)
        enable = self._get_value_from_xml_element(root, "enable", int)

        rec_set = self.http_api._recording_settings.setdefault(channel, {})
        rec_set.setdefault("schedule", {})["enable"] = enable
        rec_set["schedule"]["channel"] = channel
        if self.http_api.api_version("GetRec") >= 1:
            rec_set["scheduleEnable"] = enable

        mess = await self.send(cmd_id=54, channel=channel)
        data = self._get_keys_from_xml(mess, {"recordDelayTime": ("postRec_int", int), "packageTime": ("packTime_int", int)})
        data["postRec"] = TIME_INT_SEC_TO_STR.get(data.get("postRec_int", 0))
        data["packTime"] = TIME_INT_SEC_TO_STR.get(data.get("packTime_int", 0) * 60)
        rec_set.update(data)

    @http_cmd(["SetRecV20", "SetRec"])
    async def SetRecV20(self, **kwargs) -> None:
        """Set the recoding info"""
        rec = kwargs.get("Rec", {})
        channel = rec.get("schedule", {}).get("channel")
        enable = rec.get("scheduleEnable")
        if enable is None:
            enable = rec.get("schedule", {}).get("enable")
        if enable is None:
            enable = rec.get("enable")
        packTime = rec.get("packTime")
        postRec = rec.get("postRec")
        if channel is None or (enable is None and packTime is None and postRec is None):
            raise InvalidParameterError(f"Baichuan host {self._host}: SetRecV20 invalid input params")

        if enable is not None:
            xml = xmls.SetRecEnable.format(channel=channel, enable=enable)
            await self.send(cmd_id=82, channel=channel, body=xml)
        if packTime is not None or postRec is not None:
            mess = await self.send(cmd_id=54, channel=channel)
            xml_body = XML.fromstring(mess)
            if (packTime_int := TIME_STR_TO_INT_SEC.get(packTime)) is not None and (xml_packTime := xml_body.find(".//packageTime")) is not None:
                xml_packTime.text = str(round(packTime_int / 60))
            if (postRec_int := TIME_STR_TO_INT_SEC.get(postRec)) is not None and (xml_postRec := xml_body.find(".//recordDelayTime")) is not None:
                xml_postRec.text = str(postRec_int)
            xml = XML.tostring(xml_body, encoding="unicode")
            xml = xmls.XML_HEADER + xml
            await self.send(cmd_id=55, channel=channel, body=xml)

    @http_cmd("SetNetPort")
    async def SetNetPort(self, **kwargs) -> None:
        """Backup for enabeling ONVIF/RTSP/RTMP"""
        net_port = kwargs.get("NetPort", {})
        enable_onvif = net_port.get("onvifEnable")
        enable_rtmp = net_port.get("rtmpEnable")
        enable_rtsp = net_port.get("rtspEnable")

        if enable_onvif is not None:
            await self.set_port_enabled(PortType.onvif, enable_onvif == 1)
        if enable_rtmp is not None:
            await self.set_port_enabled(PortType.rtmp, enable_rtmp == 1)
        if enable_rtsp is not None:
            await self.set_port_enabled(PortType.rtsp, enable_rtsp == 1)

    @http_cmd("GetPirInfo")
    async def GetPirInfo(self, channel: int, **_kwargs) -> None:
        """Get the Pir settings"""
        mess = await self.send(cmd_id=212, channel=channel)
        data = self._get_keys_from_xml(
            mess,
            {
                "enable": ("enable", int),
                "sensiValue": ("sensitive", int),
                "reduceFalseAlarm": ("reduceAlarm", int),
                "interval": ("interval", int),
                "intervalSecMax": ("interval_max", int),
            },
        )
        self.http_api._pir.setdefault(channel, {}).update(data)

    @http_cmd("SetPirInfo")
    async def SetPirInfo(self, **kwargs) -> None:
        """Set the Pir settings"""
        param = kwargs["pirInfo"]
        channel = param["channel"]

        mess = await self.send(cmd_id=212, channel=channel)
        xml_body = XML.fromstring(mess)

        get_state = False
        if (enable := param.get("enable")) is not None and (xml_enable := xml_body.find(".//enable")) is not None:
            xml_enable.text = str(enable)
        if (sens := param.get("sensitive")) is not None and (xml_sens := xml_body.find(".//sensiValue")) is not None:
            xml_sens.text = str(sens)
        if (reduce_alarm := param.get("reduceAlarm")) is not None and (xml_reduce_alarm := xml_body.find(".//reduceFalseAlarm")) is not None:
            xml_reduce_alarm.text = str(reduce_alarm)
        if (interval := param.get("interval")) is not None and (xml_inter := xml_body.find(".//interval")) is not None:
            xml_inter.text = str(interval)
            get_state = True

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=213, channel=channel, body=xml)
        if get_state:
            await self.GetPirInfo(channel)

    @http_cmd(["GetEmail", "GetEmailV20"])
    async def GetEmail(self, channel: int, **_kwargs) -> None:
        """Get the email settings"""
        mess = await self.send(cmd_id=217, channel=channel)
        data = self._get_keys_from_xml(mess, {"enable": ("enable", int)})
        data["scheduleEnable"] = data["enable"]
        data["schedule"] = {"enable": data["enable"]}
        self.http_api._email_settings.setdefault(channel, {}).setdefault("Email", {}).update(data)

    @http_cmd(["SetEmail", "SetEmailV20"])
    async def SetEmail(self, **kwargs) -> None:
        """Get the email settings"""
        param = kwargs["Email"]
        channel = param.get("schedule", {}).get("channel")

        mess = await self.send(cmd_id=217, channel=channel)
        xml_body = XML.fromstring(mess)

        if (enable := param.get("enable")) is not None and (xml_enable := xml_body.find(".//enable")) is not None:
            xml_enable.text = str(enable)
        if (enable := param.get("schedule", {}).get("enable")) is not None and (xml_enable := xml_body.find(".//enable")) is not None:
            xml_enable.text = str(enable)
        if (enable := param.get("scheduleEnable")) is not None and (xml_enable := xml_body.find(".//enable")) is not None:
            xml_enable.text = str(enable)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=216, channel=channel, body=xml)

    @http_cmd(["GetPush", "GetPushV20"])
    async def GetPush(self, channel: int, **_kwargs) -> None:
        """Get the push settings"""
        mess = await self.send(cmd_id=219, channel=channel)
        data = self._get_keys_from_xml(mess, {"enable": ("enable", int)})
        data["scheduleEnable"] = data["enable"]
        data["schedule"] = {"enable": data["enable"]}
        self.http_api._push_settings.setdefault(channel, {}).setdefault("Push", {}).update(data)

    @http_cmd(["SetPush", "SetPushV20"])
    async def SetPush(self, **kwargs) -> None:
        """Set the push settings"""
        param = kwargs["Push"]
        channel = param.get("schedule", {}).get("channel")

        mess = await self.send(cmd_id=219, channel=channel)
        xml_body = XML.fromstring(mess)

        if (enable := param.get("enable")) is not None and (xml_enable := xml_body.find(".//enable")) is not None:
            xml_enable.text = str(enable)
        if (enable := param.get("schedule", {}).get("enable")) is not None and (xml_enable := xml_body.find(".//enable")) is not None:
            xml_enable.text = str(enable)
        if (enable := param.get("scheduleEnable")) is not None and (xml_enable := xml_body.find(".//enable")) is not None:
            xml_enable.text = str(enable)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=218, channel=channel, body=xml)

    @http_cmd(["GetAudioAlarm", "GetAudioAlarmV20"])
    async def GetAudioAlarm(self, channel: int, **_kwargs) -> None:
        """Get the siren on event settings"""
        mess = await self.send(cmd_id=232, channel=channel)
        data = self._get_keys_from_xml(mess, {"enable": ("enable", int)})
        data["enable"] = data["enable"]
        data["schedule"] = {"enable": data["enable"]}
        self.http_api._audio_alarm_settings.setdefault(channel, {}).update(data)

    @http_cmd(["SetAudioAlarm", "SetAudioAlarmV20"])
    async def SetAudioAlarm(self, **kwargs) -> None:
        """Set the siren on event settings"""
        param = kwargs["Audio"]
        channel = param.get("schedule", {}).get("channel")
        enable = param.get("enable")
        if enable is None:
            enable = param.get("schedule", {}).get("enable")
        if channel is None or enable is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: SetAudioAlarm invalid input params")

        mess = await self.send(cmd_id=232, channel=channel)
        xml_body = XML.fromstring(mess)

        if (xml_enable := xml_body.find(".//enable")) is not None:
            xml_enable.text = str(enable)

        xml = XML.tostring(xml_body, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=231, channel=channel, body=xml)

    @http_cmd("GetAutoFocus")
    async def GetAutoFocus(self, **kwargs) -> None:
        """Get the auto focus settings"""
        channel = kwargs.get("channel")
        if channel is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: GetAutoFocus invalid input params")

        mess = await self.send(cmd_id=224, channel=channel)
        data = self._get_keys_from_xml(mess, {"disable": ("disable", int)})

        self.http_api._auto_focus_settings.setdefault(channel, {}).update(data)

    @http_cmd("SetAutoFocus")
    async def SetAutoFocus(self, **kwargs) -> None:
        """Set the auto focus settings"""
        param = kwargs.get("AutoFocus", {})
        channel = param.get("channel")
        disable = param.get("disable")
        if channel is None or disable is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: SetAutoFocus invalid input params")

        xml = xmls.SetAutoFocus.format(channel=channel, disable=disable)
        await self.send(cmd_id=225, channel=channel, body=xml)

    async def get_scene_info(self, scene_id: int) -> None:
        """Get the name of a scene"""
        xml = xmls.GetSceneInfo.format(scene_id=scene_id)
        mess = await self.send(cmd_id=604, body=xml)

        data = self._get_keys_from_xml(mess, {"id": ("scene_id", int), "valid": ("valid", int), "name": ("name", str)})
        if data.get("scene_id") != scene_id:
            raise UnexpectedDataError(f"Baichuan host {self._host}: get_scene_info requested scene_id {scene_id} but received {data.get('scene_id')}")
        if data.get("valid") != 1:
            raise UnexpectedDataError(f"Baichuan host {self._host}: get_scene_info got invalid scene for scene_id {scene_id}")

        self._scenes[scene_id] = data["name"]

    async def get_scene(self) -> int:
        """Get the currently active scene"""
        mess = await self.send(cmd_id=601)
        root = XML.fromstring(mess)

        if self._get_value_from_xml_element(root, "enable", int) != 1:
            self._active_scene = -1
            return -1  # scene mode off

        cur_scene = self._get_value_from_xml_element(root, "curSceneId", int)
        if cur_scene is None:
            self._active_scene = -1
        else:
            self._active_scene = cur_scene
        return self._active_scene

    async def set_scene(self, scene_id: int | None = None, scene_name: str | None = None) -> None:
        """Set the active scene"""
        if scene_name is not None:
            ids = [key for key, val in self._scenes.items() if val == scene_name]
            if ids:
                scene_id = ids[0]
        if scene_id not in self._scenes:
            raise InvalidParameterError(f"Baichuan host {self._host}: set_scene scene_id {scene_id} not in {list(self._scenes.keys())}")

        if scene_id < 0:
            xml = xmls.DisableScene
        else:
            xml = xmls.SetScene.format(scene_id=scene_id)

        await self.send(cmd_id=602, body=xml)
        await self.get_scene()

    async def set_smart_ai(self, channel: int, smart_type: str, location: int, sensitivity: int | None = None, delay: int | None = None) -> None:
        """Change smart AI settings"""
        mess = await self.send(cmd_id=SMART_AI[smart_type][0], channel=channel)
        root = XML.fromstring(mess)

        location_found = False
        main_key = f"{smart_type[0].upper() + smart_type[1:]}Detect"
        main = root.find(main_key)
        if main is None:
            raise UnexpectedDataError(f"Baichuan host {self._host}: set_smart_ai cannot find {main_key} in {smart_type} smart AI")
        if (main_ob := main.find("op")) is not None:
            main_ob.text = "modify"  # add/delete/modify
        for item in main.findall(f"{smart_type}DetectItem"):
            loc = self._get_value_from_xml_element(item, "location", int)
            if loc != location:
                main.remove(item)
                continue
            location_found = True
            if sensitivity is not None and (sens_ob := item.find("sesensitivity")) is not None:
                sens_ob.text = str(sensitivity)
            if delay is not None and (delay_ob := item.find("stayTime")) is not None:
                delay_ob.text = str(delay)
            if delay is not None and (delay_ob := item.find("timeThresh")) is not None:
                delay_ob.text = str(delay)

        if not location_found:
            raise InvalidParameterError(f"Baichuan host {self._host}: cannot find location {location} in {smart_type} smart AI")

        xml = XML.tostring(root, encoding="unicode")
        xml = xmls.XML_HEADER + xml

        await self.send(cmd_id=SMART_AI[smart_type][1], channel=channel, body=xml)

    def _parse_rule(self, mess: str) -> None:
        root = XML.fromstring(mess)
        for IFTTT_list in root:
            for rule in IFTTT_list:
                data = self._get_keys_from_xml(rule, {"channel": ("channel", int), "id": ("id", int), "enable": ("enable", bool), "name": ("name", str)})
                channel = data.pop("channel", None)
                rule_id = data.pop("id", None)
                if channel is None or rule_id is None:
                    continue
                ch_rule = self._rules.setdefault(channel, {})
                if not self.http_api._startup and rule_id not in ch_rule:
                    _LOGGER.info(
                        "New Reolink survaillance rule '%s' discovered for %s",
                        data.get("name", ""),
                        self.http_api.camera_name(channel),
                    )
                    self.http_api._new_devices = True
                ch_rule[rule_id] = data

    async def get_rule_ids(self, channel: int) -> None:
        """Get the list of survaillance rule ids"""
        mess = await self.send(cmd_id=685, channel=channel)
        root = XML.fromstring(mess)

        for rule_item in root.findall(".//id"):
            if rule_item.text is None:
                continue
            rule_id = int(rule_item.text)
            if rule_id not in self._rule_ids:
                self._rule_ids.add(rule_id)
                # get the corresponding channel of this unknown rule
                await self.get_rule(rule_id, channel)
                if not self.http_api._startup:
                    _LOGGER.info(
                        "New Reolink survaillance rule id '%s' discovered for %s",
                        rule_id,
                        self.http_api.nvr_name,
                    )
                    self.http_api._new_devices = True

    async def get_rule(self, rule_id: int, channel: int) -> None:
        """Get the state of a survaillance rule"""
        xml = xmls.GetRule.format(rule_id=rule_id)
        mess = await self.send(cmd_id=668, channel=channel, body=xml)
        root = XML.fromstring(mess)

        for IFTTT_list in root:
            for rule in IFTTT_list:
                data = self._get_keys_from_xml(rule, {"id": ("id", int), "enable": ("enable", bool), "name": ("name", str)})
                rule_id = data.pop("id", None)
                if rule_id is None:
                    continue
                self._rules.setdefault(channel, {})[rule_id] = data

    async def set_rule_enabled(self, channel: int, rule_id: int, enabled: bool) -> None:
        """Enable/disable a survaillance rule"""
        xml = xmls.GetRule.format(rule_id=rule_id)
        mess = await self.send(cmd_id=668, channel=channel, body=xml)
        root = XML.fromstring(mess)

        enabled_str = "1" if enabled else "0"

        if (xml_list := root.find("IFTTTList")) is not None:
            xml_list.tag = "IFTTTUpdate"
            if (xml_rule := xml_list.find("linkage")) is not None:
                if (xml_enable := xml_rule.find("enable")) is not None:
                    xml_enable.text = enabled_str

        xml = XML.tostring(root, encoding="unicode")
        xml = xmls.XML_HEADER + xml
        await self.send(cmd_id=667, channel=channel, body=xml)
        await self.get_rule(rule_id, channel)

    def rule_ids(self, channel: int) -> list[int]:
        """Return the list of survaillance rule ids"""
        return list(self._rules.get(channel, {}))

    def rule_name(self, channel: int, rule_id: int) -> str:
        """Return the survaillance rule name"""
        return self._rules.get(channel, {}).get(rule_id, {}).get("name", UNKNOWN)

    def rule_enabled(self, channel: int, rule_id: int) -> bool:
        """Return if the survaillance rule is enabled or not"""
        return self._rules.get(channel, {}).get(rule_id, {}).get("enable", False)

    def _xml_time_to_datetime(self, xml_time: XML.Element | None) -> datetime | None:
        if xml_time is None:
            return None
        year = self._get_value_from_xml_element(xml_time, "year", int)
        month = self._get_value_from_xml_element(xml_time, "month", int)
        day = self._get_value_from_xml_element(xml_time, "day", int)
        hour = self._get_value_from_xml_element(xml_time, "hour", int)
        minute = self._get_value_from_xml_element(xml_time, "minute", int)
        second = self._get_value_from_xml_element(xml_time, "second", int)
        if year is None or month is None or day is None or hour is None or minute is None or second is None:
            return None
        return datetime(year=year, month=month, day=day, hour=hour, minute=minute, second=second)

    async def search_vod_type(
        self, channel: int, start: datetime, end: datetime, stream: str | None = None, split_time: timedelta | None = None
    ) -> tuple[dict[str, VOD_trigger], dict[VOD_trigger, list[VOD_file]]]:
        vod_type_dict: dict[str, VOD_trigger] = {}
        vod_dict: dict[VOD_trigger, list[VOD_file]] = {}
        for trig in VOD_trigger:
            vod_dict[trig] = []
        uid = self.http_api.camera_uid(channel)
        uid = uid.split("_")[0]
        if uid == UNKNOWN:
            raise InvalidParameterError(f"Baichuan host {self._host}: search_vod_type: cannot get UID for channel {channel}")

        if stream == "sub":
            stream_type = 1
        elif stream in {"autotrack_sub", "telephoto_sub"}:
            stream_type = 3
        elif stream in {"autotrack_main", "telephoto_main"}:
            stream_type = 2
        else:
            stream_type = 0

        finished: int | None = 0
        request_i = 0
        while finished == 0:
            request_i += 1
            if request_i > 50:
                _LOGGER.warning("Baichuan host %s: search_vod_type took more then 50 itterations, quitting", self._host)
                break
            xml = xmls.FindRecVideoOpen.format(
                channel=channel,
                uid=uid,
                stream_type=stream_type,
                start_year=start.year,
                start_month=start.month,
                start_day=start.day,
                start_hour=start.hour,
                start_minute=start.minute,
                start_second=start.second,
                end_year=end.year,
                end_month=end.month,
                end_day=end.day,
                end_hour=end.hour,
                end_minute=end.minute,
                end_second=end.second,
            )
            mess = await self.send(cmd_id=272, channel=channel, body=xml)
            fileHandle = self._get_value_from_xml(mess, "fileHandle")

            xml = xmls.FindRecVideo.format(channel=channel, fileHandle=fileHandle)
            mess = await self.send(cmd_id=273, channel=channel, body=xml)
            root = XML.fromstring(mess)
            main = root.find("alarmVideoInfo")
            if main is None:
                await self.send(cmd_id=274, channel=channel, body=xml)
                break
            finished = self._get_value_from_xml_element(main, "bFinished", int)
            vod_list = main.find("alarmVideoList")
            if vod_list is None:
                await self.send(cmd_id=274, channel=channel, body=xml)
                break

            time_event: datetime | None = None
            for item in vod_list.findall(".//alarmVideo"):
                file_name = self._get_value_from_xml_element(item, "fileName")
                trigger = self._get_value_from_xml_element(item, "alarmType")
                if file_name is None or trigger is None:
                    continue
                start_time_file = file_name[2:]
                time_event = self._xml_time_to_datetime(item.find("startTime"))
                end_time_event = self._xml_time_to_datetime(item.find("endTime"))
                time_file = reolink_time_to_datetime(start_time_file)
                if time_event is None or end_time_event is None:
                    continue
                data = {
                    "type": stream,
                    "StartTime": datetime_to_reolink_time(time_event),
                    "EndTime": datetime_to_reolink_time(end_time_event),
                    "PlaybackTime": datetime_to_reolink_time(time_file),
                    "name": start_time_file,
                    "size": 1,
                }
                vod_file = VOD_file(data)
                vod_file.bc_triggers = VOD_trigger.NONE

                if split_time:
                    if time_event is not None:
                        start_time_file = to_reolink_time_id(time_file + int((time_event - time_file) / split_time) * split_time)

                vod_type_dict.setdefault(start_time_file, VOD_trigger.NONE)
                if "md" in trigger or "pir" in trigger or "other" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.MOTION
                    vod_file.bc_triggers |= VOD_trigger.MOTION
                    vod_dict[VOD_trigger.MOTION].append(vod_file)
                if "io" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.IO
                    vod_file.bc_triggers |= VOD_trigger.IO
                    vod_dict[VOD_trigger.IO].append(vod_file)
                if "people" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.PERSON
                    vod_file.bc_triggers |= VOD_trigger.PERSON
                    vod_dict[VOD_trigger.PERSON].append(vod_file)
                if "face" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.FACE
                    vod_file.bc_triggers |= VOD_trigger.FACE
                    vod_dict[VOD_trigger.FACE].append(vod_file)
                if "vehicle" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.VEHICLE
                    vod_file.bc_triggers |= VOD_trigger.VEHICLE
                    vod_dict[VOD_trigger.VEHICLE].append(vod_file)
                if "dog_cat" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.ANIMAL
                    vod_file.bc_triggers |= VOD_trigger.ANIMAL
                    vod_dict[VOD_trigger.ANIMAL].append(vod_file)
                if "visitor" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.DOORBELL
                    vod_file.bc_triggers |= VOD_trigger.DOORBELL
                    vod_dict[VOD_trigger.DOORBELL].append(vod_file)
                if "package" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.PACKAGE
                    vod_file.bc_triggers |= VOD_trigger.PACKAGE
                    vod_dict[VOD_trigger.PACKAGE].append(vod_file)
                if "cry" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.CRYING
                    vod_file.bc_triggers |= VOD_trigger.CRYING
                    vod_dict[VOD_trigger.CRYING].append(vod_file)
                if "crossline" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.CROSSLINE
                    vod_file.bc_triggers |= VOD_trigger.CROSSLINE
                    vod_dict[VOD_trigger.CROSSLINE].append(vod_file)
                if "intrusion" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.INTRUSION
                    vod_file.bc_triggers |= VOD_trigger.INTRUSION
                    vod_dict[VOD_trigger.INTRUSION].append(vod_file)
                if "loitering" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.LINGER
                    vod_file.bc_triggers |= VOD_trigger.LINGER
                    vod_dict[VOD_trigger.LINGER].append(vod_file)
                if "legacy" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.FORGOTTEN_ITEM
                    vod_file.bc_triggers |= VOD_trigger.FORGOTTEN_ITEM
                    vod_dict[VOD_trigger.FORGOTTEN_ITEM].append(vod_file)
                if "loss" in trigger:
                    vod_type_dict[start_time_file] |= VOD_trigger.TAKEN_ITEM
                    vod_file.bc_triggers |= VOD_trigger.TAKEN_ITEM
                    vod_dict[VOD_trigger.TAKEN_ITEM].append(vod_file)

            if finished == 0:
                if time_event is None:
                    await self.send(cmd_id=274, channel=channel, body=xml)
                    break
                start = time_event

            await self.send(cmd_id=274, channel=channel, body=xml)

        # xml = xmls.FileInfoListOpen.format(
        #    channel=channel,
        #    uid=uid,
        #    stream_type=stream_type,
        #    start_year=start.year,
        #    start_month=start.month,
        #    start_day=start.day,
        #    start_hour=start.hour,
        #    start_minute=start.minute,
        #    start_second=start.second,
        #    end_year=end.year,
        #    end_month=end.month,
        #    end_day=end.day,
        #    end_hour=end.hour,
        #    end_minute=end.minute,
        #    end_second=end.second,
        # )
        # mess = await self.send(cmd_id=14, body=xml)
        # handle = self._get_value_from_xml(mess, "handle")

        # xml_file_info = xmls.FileInfoList.format(channel=channel, handle=handle, uid=uid)
        # await self.send(cmd_id=15, body=xml_file_info)
        # await self.send(cmd_id=16, body=xml_file_info)

        return vod_type_dict, vod_dict

    @property
    def events_active(self) -> bool:
        return self._events_active and time_now() - self._time_connection_lost > 120

    @property
    def session_active(self) -> bool:
        return self._logged_in or (self._protocol is not None and time_now() - self._protocol.time_recv < 60)

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

    @property
    def active_scene_id(self) -> int:
        return self._active_scene

    @property
    def scene_names(self) -> list[str]:
        return list(self._scenes.values())

    @property
    def active_scene(self) -> str:
        return self._scenes.get(self._active_scene, UNKNOWN)

    def model(self, channel: int | None = None) -> str | None:
        return self._dev_info.get(channel, {}).get("type")

    def hardware_version(self, channel: int | None = None) -> str:
        return self._dev_info.get(channel, {}).get("hardwareVersion", UNKNOWN)

    def item_number(self, channel: int | None = None) -> str | None:
        return self._dev_info.get(channel, {}).get("itemNo")

    def mac_address(self, channel: int | None = None) -> str | None:
        mac = self._network_info.get(channel, {}).get("mac")
        if channel is not None and mac == self.mac_address():
            # MAC of channel equals MAC of host, host could not retrieve MAC of channel
            return None
        return mac

    def ip_address(self, channel: int | None = None) -> str:
        ip = self._network_info.get(channel, {}).get("ip", UNKNOWN)
        if channel is not None and ip == self.ip_address():
            # IP of channel equals IP of host, host could not retrieve IP of channel
            return UNKNOWN
        return ip

    def wifi_connection(self, channel: int) -> bool:
        return self._wifi_connection.get(channel, True)

    def sw_version(self, channel: int | None = None) -> str | None:
        return self._dev_info.get(channel, {}).get("firmwareVersion")

    def privacy_mode(self, channel: int | None = None) -> bool | None:
        if channel is None:
            if self.http_api and self.http_api.is_nvr:
                return None
            channel = 0
        return self._privacy_mode.get(channel)

    def pre_record_enabled(self, channel: int) -> bool:
        return self._pre_record_state.get(channel, {}).get("enabled") == 1

    def pre_record_time(self, channel: int) -> int | None:
        return self._pre_record_state.get(channel, {}).get("preTime")

    def pre_record_fps(self, channel: int) -> int | None:
        return self._pre_record_state.get(channel, {}).get("fps")

    def pre_record_battery_stop(self, channel: int) -> int | None:
        return self._pre_record_state.get(channel, {}).get("batteryStop")

    def day_night_state(self, channel: int) -> str | None:
        """known values: day, night, led_day"""
        return self._day_night_state.get(channel)

    def ir_brightness(self, channel: int) -> int | None:
        return self._ir_brightness.get(channel)

    def cry_sensitivity(self, channel: int) -> int | None:
        return self._cry_sensitivity.get(channel)

    def ptz_running(self, channel: int) -> bool | None:
        return self._ptz_running.get(channel)

    def smart_type_list(self, channel: int) -> list[str]:
        return list(self._ai_detect.get(channel, {}).keys())

    def smart_location_list(self, channel: int, smart_type: str) -> list[int]:
        return list(self._ai_detect.get(channel, {}).get(smart_type, {}).keys())

    def smart_ai_type_list(self, channel: int, smart_type: str, location: int) -> list[str]:
        smart_ai = self._ai_detect.get(channel, {}).get(smart_type, {}).get(location, {})
        return list(AI_DETECTS.intersection(smart_ai))

    def smart_ai_name(self, channel: int, smart_type: str, location: int) -> str:
        return self._ai_detect.get(channel, {}).get(smart_type, {}).get(location, {}).get("name", UNKNOWN)

    def smart_ai_index(self, channel: int, smart_type: str, location: int) -> int:
        return self._ai_detect.get(channel, {}).get(smart_type, {}).get(location, {}).get("index", 0)

    def smart_ai_sensitivity(self, channel: int, smart_type: str, location: int) -> int:
        return self._ai_detect.get(channel, {}).get(smart_type, {}).get(location, {}).get("sensitivity", 0)

    def smart_ai_delay(self, channel: int, smart_type: str, location: int) -> int:
        return self._ai_detect.get(channel, {}).get(smart_type, {}).get(location, {}).get("delay", 0)

    def smart_ai_state(self, channel: int, smart_type: str, location: int, ai_type: str = "state") -> bool:
        return self._ai_detect.get(channel, {}).get(smart_type, {}).get(location, {}).get(ai_type, False)

    def ai_detect_type(self, channel: int, object_type: str) -> str | None:
        val = self._ai_yolo_sub_type.get(channel, {}).get(object_type)
        if val is None:
            key = AI_DETECT_CONVERSION.get(object_type, object_type)
            val = self._ai_yolo_sub_type.get(channel, {}).get(key)
        return val

    def io_inputs(self, channel: int | None) -> list[int]:
        return self._io_inputs.get(channel, [])

    def io_outputs(self, channel: int | None) -> list[int]:
        return self._io_outputs.get(channel, [])

    def io_input_state(self, channel: int | None, index: int) -> bool | None:
        return self._io_input.get(channel, {}).get(index)

    def hardwired_chime_type(self, channel: int) -> str | None:
        return str(self._hardwired_chime_settings.get(channel, {}).get("type"))

    def hardwired_chime_enabled(self, channel: int) -> bool:
        if channel not in self._hardwired_chime_settings:
            return False

        return self._hardwired_chime_settings[channel]["enable"] == 1

    def siren_state(self, channel: int) -> bool | None:
        return self._siren_state.get(channel)

    def audio_noise_reduction(self, channel: int) -> int | None:
        return self._noise_reduction.get(channel)
