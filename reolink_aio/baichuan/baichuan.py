"""Reolink Baichuan API"""

from __future__ import annotations

import logging
import asyncio
from inspect import getmembers
from time import time as time_now
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar, overload, Coroutine
from collections.abc import Callable
from xml.etree import ElementTree as XML
from Cryptodome.Cipher import AES

from . import xmls
from .tcp_protocol import BaichuanTcpClientProtocol
from ..const import NONE_WAKING_COMMANDS, UNKNOWN
from ..typings import cmd_list_type, VOD_trigger, VOD_file
from ..software_version import SoftwareVersion
from ..exceptions import (
    ApiError,
    InvalidContentTypeError,
    InvalidParameterError,
    ReolinkError,
    UnexpectedDataError,
    ReolinkConnectionError,
    ReolinkTimeoutError,
    CredentialsInvalidError,
)
from ..enums import BatteryEnum, DayNightEnum, HardwiredChimeTypeEnum, SpotlightModeEnum
from ..utils import reolink_time_to_datetime, to_reolink_time_id, datetime_to_reolink_time

from .util import DEFAULT_BC_PORT, HEADER_MAGIC, AES_IV, EncType, PortType, decrypt_baichuan, encrypt_baichuan, md5_str_modern, http_cmd

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
        self._log_once: list[str] = []
        self.last_privacy_check: float = 0

        # TCP connection
        self._mutex = asyncio.Lock()
        self._login_mutex = asyncio.Lock()
        self._loop = asyncio.get_event_loop()
        self._transport: asyncio.Transport | None = None
        self._protocol: BaichuanTcpClientProtocol | None = None
        self._logged_in: bool = False

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

        # channel states
        self._dev_info: dict[int | None, dict[str, str]] = {}
        self._network_info: dict[int | None, dict[str, str]] = {}
        self._wifi_connection: dict[int, bool] = {}
        self._ptz_position: dict[int, dict[str, str]] = {}
        self._privacy_mode: dict[int, bool] = {}
        self._ai_detect: dict[int, dict[str, dict[int, dict[str, Any]]]] = {}
        self._hardwired_chime_settings: dict[int, dict[str, str | int]] = {}
        self._ir_brightness: dict[int, int] = {}
        self._cry_sensitivity: dict[int, int] = {}
        self._pre_record_state: dict[int, dict] = {}

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
        mess_id: int | None = None,
        retry: int = RETRY_ATTEMPTS,
    ) -> str:
        """Generic baichuan send method."""
        retry = retry - 1

        if not self._logged_in and cmd_id > 2:
            # not logged in and requesting a non login/logout cmd, first login
            await self.login()

        # mess_id: 0/251 = push, 1-100 = channel, 250 = host
        if mess_id is None:
            if channel is None:
                mess_id = 250
            else:
                mess_id = channel + 1

        ext = extension  # do not overwrite the original arguments for retries
        if channel is not None:
            if extension:
                raise InvalidParameterError(f"Baichuan host {self._host}: cannot specify both channel and extension")
            ext = xmls.CHANNEL_EXTENSION_XML.format(channel=channel)

        mess_len = len(ext) + len(body)
        payload_offset = len(ext)

        cmd_id_bytes = (cmd_id).to_bytes(4, byteorder="little")
        mess_len_bytes = (mess_len).to_bytes(4, byteorder="little")
        mess_id_bytes = (mess_id).to_bytes(4, byteorder="little")
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
                enc_body_bytes = encrypt_baichuan(ext, mess_id) + encrypt_baichuan(body, mess_id)  # enc_offset = mess_id
            elif enc_type == EncType.AES:
                enc_body_bytes = self._aes_encrypt(ext) + self._aes_encrypt(body)
            else:
                raise InvalidParameterError(f"Baichuan host {self._host}: invalid param enc_type '{enc_type}'")

        # send message
        await self._connect_if_needed()
        if TYPE_CHECKING:
            assert self._protocol is not None
            assert self._transport is not None

        # check for simultaneous cmd_ids with same mess_id
        if (receive_future := self._protocol.receive_futures.get(cmd_id, {}).get(mess_id)) is not None:
            try:
                async with asyncio.timeout(TIMEOUT):
                    try:
                        await receive_future
                    except Exception:
                        pass
                    while self._protocol.receive_futures.get(cmd_id, {}).get(mess_id) is not None:
                        await asyncio.sleep(0.010)
            except asyncio.TimeoutError as err:
                raise ReolinkError(
                    f"Baichuan host {self._host}: receive future is already set for cmd_id {cmd_id} "
                    "and timeout waiting for it to finish, cannot receive multiple requests simultaneously"
                ) from err

        self._protocol.receive_futures.setdefault(cmd_id, {})[mess_id] = self._loop.create_future()

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
                data, len_header = await self._protocol.receive_futures[cmd_id][mess_id]
        except ApiError as err:
            if retry <= 0 or err.rspCode != 400:
                raise err
            _LOGGER.debug("%s, trying again", str(err))
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
        finally:
            if self._protocol is not None and (receive_future := self._protocol.receive_futures.get(cmd_id, {}).get(mess_id)) is not None:
                if not receive_future.done():
                    receive_future.cancel()
                self._protocol.receive_futures[cmd_id].pop(mess_id, None)
                if not self._protocol.receive_futures[cmd_id]:
                    self._protocol.receive_futures.pop(cmd_id, None)

        if retrying:
            # needed because the receive_future first needs to be cleared.
            return await self.send(cmd_id, channel, body, extension, enc_type, message_class, mess_id, retry)

        # decryption
        rec_body = self._decrypt(data, len_header, cmd_id, enc_type)

        if _LOGGER.isEnabledFor(logging.DEBUG):
            ch_str = f" ch {channel}" if channel is not None else ""
            if len(rec_body) > 0:
                _LOGGER.debug("Baichuan host %s: received cmd_id %s%s:\n%s", self._host, cmd_id, ch_str, self._hide_password(rec_body))
            else:
                _LOGGER.debug("Baichuan host %s: received cmd_id %s%s status 200:OK without body", self._host, cmd_id, ch_str)

        return rec_body

    def _aes_encrypt(self, body: str) -> bytes:
        """Encrypt a message using AES encryption"""
        if not body:
            return b""
        if self._aes_key is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: first login before using AES encryption")

        cipher = AES.new(key=self._aes_key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
        return cipher.encrypt(body.encode("utf8"))

    def _aes_decrypt(self, data: bytes, header: bytes = b"") -> str:
        """Decrypt a message using AES decryption"""
        if self._aes_key is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: first login before using AES decryption, header: {header.hex()}")

        cipher = AES.new(key=self._aes_key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
        return cipher.decrypt(data).decode("utf8")

    def _decrypt(self, data: bytes, len_header: int, cmd_id: int, enc_type: EncType = EncType.AES) -> str:
        """Figure out the encryption method and decrypt the message"""
        rec_enc_offset = int.from_bytes(data[12:16], byteorder="little")  # mess_id = enc_offset
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

    def _push_callback(self, cmd_id: int, data: bytes, len_header: int) -> None:
        """Callback to parse a received message that was pushed"""
        # decryption
        try:
            rec_body = self._decrypt(data, len_header, cmd_id)
        except ReolinkError as err:
            _LOGGER.debug(err)
            return

        if len(rec_body) == 0:
            _LOGGER.debug("Baichuan host %s: received push cmd_id %s withouth body", self._host, cmd_id)
            return

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("Baichuan host %s: received push cmd_id %s:\n%s", self._host, cmd_id, self._hide_password(rec_body))

        self._parse_xml(cmd_id, rec_body)

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
            await self.send(cmd_id=31, mess_id=251)  # Subscribe to events
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

    def _get_value_from_xml_element(self, xml_element: XML.Element, key: str, type_class=str):
        """Get a value for a key in a xml element"""
        xml_value = xml_element.find(f".//{key}")
        if xml_value is None:
            return None
        try:
            return type_class(xml_value.text)
        except ValueError as err:
            _LOGGER.debug(err)
            return None

    def _get_channel_from_xml_element(self, xml_element: XML.Element, key: str = "channelId") -> int | None:
        channel = self._get_value_from_xml_element(xml_element, key, int)
        if channel not in self.http_api._channels:
            return None
        return channel

    def _get_keys_from_xml(self, xml: str | XML.Element, keys: list[str] | dict[str, tuple[str, type]]) -> dict[str, Any]:
        """Get multiple keys from a xml and return as a dict"""
        if isinstance(xml, str):
            root = XML.fromstring(xml)
        else:
            root = xml
        result: dict[str, Any] = {}
        for key in keys:
            value = self._get_value_from_xml_element(root, key)
            if value is None:
                continue
            if isinstance(keys, dict):
                (new_key, type_class) = keys[key]
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
        self._parse_xml(cmd_id, rec_body)

    def _parse_xml(self, cmd_id: int, xml: str) -> None:
        """parce received xml"""
        root = XML.fromstring(xml)

        state: Any
        channels: set[int | None] = {None}
        cmd_ids: set[int | None] = {None, cmd_id}
        if cmd_id == 26:
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

            if (DayNight := root.find(".//DayNight")) is not None:
                value = self._get_value_from_xml_element(DayNight, "mode")
                if value is not None:
                    value = value.replace("And", "&")
                    value = value[0].upper() + value[1:]
                    self.http_api._isp_settings.setdefault(channel, {}).setdefault("Isp", {})["dayNight"] = DayNightEnum(value).value

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
                        if self._subscribed and not self._events_active:
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
                                    self._log_once.append(f"TCP_event_unknown_{ai_type}")
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
                                        self._ai_detect[channel][smart_type][location]["state"] = detected
                                        if sub_list is None and detected:
                                            _LOGGER.debug("Reolink %s TCP event channel %s, %s location %s detected", self.http_api.nvr_name, channel, smart_type, location)
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
                            self._log_once.append(f"TCP_event_tag_{event.tag}")
                            _LOGGER.warning("Reolink %s TCP event cmd_id %s, channel %s, received unknown event tag %s", self.http_api.nvr_name, cmd_id, channel, event.tag)

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
            values = self._get_keys_from_xml(root, {"brightness_cur": ("bright", int), "alarmMode": ("mode", int)})
            if values.get("mode") == 4 and (self.api_version("ledCtrl", channel) >> 8) & 1:  # schedule_plus, 9th bit (256), shift 8
                # Floodlight: the schedule_plus has the same number 4 as autoadaptive, so switch it around
                values["mode"] = 3
            self.http_api._whiteled_settings.setdefault(channel, {}).setdefault("WhiteLed", {}).update(values)

        elif cmd_id == 291:  # Floodlight
            for event_list in root:
                for event in event_list:
                    channel = self._get_channel_from_xml_element(event, "channel")
                    if channel is None:
                        continue
                    channels.add(channel)
                    state = self._get_value_from_xml_element(event, "status", int)
                    if state is not None:
                        self.http_api._whiteled_settings.setdefault(channel, {}).setdefault("WhiteLed", {})["state"] = state
                        _LOGGER.debug("Reolink %s TCP event channel %s, Floodlight: %s", self.http_api.nvr_name, channel, state)

        elif cmd_id == 464:  # network link type wire/wifi
            self._get_value_from_xml_element(root, "net_type")

        elif cmd_id == 527:  # crossline detection
            self._parse_smart_ai_settings(root, channels, "crossline")
        elif cmd_id == 529:  # intrusion detection
            self._parse_smart_ai_settings(root, channels, "intrusion")
        elif cmd_id == 531:  # linger detection
            self._parse_smart_ai_settings(root, channels, "loitering")
        elif cmd_id == 549:  # forgotten item
            self._parse_smart_ai_settings(root, channels, "legacy")
        elif cmd_id == 551:  # taken item
            self._parse_smart_ai_settings(root, channels, "loss")

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
                if state is not None and channel in self.http_api._whiteled_settings:
                    self.http_api._manual_record_settings[channel]["Rec"]["enable"] = state
                    _LOGGER.debug("Reolink %s TCP event channel %s, Manual record: %s", self.http_api.nvr_name, channel, state)

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
                state = self._get_value_from_xml_element(item, "sleep")
                if state is not None:
                    self._privacy_mode[channel] = state == "1"
                    _LOGGER.debug("Reolink %s TCP event channel %s, Privacy mode: %s", self.http_api.nvr_name, channel, self._privacy_mode[channel])

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
                        await self.send(cmd_id=31, mess_id=251)  # Subscribe to events
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
            await self.send(cmd_id=31, mess_id=251)
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
                dev_type = dev_type_ob.text
                dev_type_info = dev_type_info_ob.text
                if not self.http_api._is_nvr:
                    self.http_api._is_nvr = dev_type in ["nvr", "wifi_nvr", "homehub"] or dev_type_info in ["NVR", "WIFI_NVR", "HOMEHUB"]
                if not self.http_api._is_hub:
                    self.http_api._is_hub = dev_type == "homehub" or dev_type_info == "HOMEHUB"

            data = self._get_keys_from_xml(dev_info, {"sleep": ("sleep", str), "channelNum": ("channelNum", int)})
            # privacy mode
            if "sleep" in data:
                self._privacy_mode[0] = data["sleep"] == "1"
            # channels
            if "channelNum" in data and self.http_api._num_channels == 0 and not self.http_api._is_nvr:
                self.http_api._channels.clear()
                self.http_api._num_channels = data["channelNum"]
                if self.http_api._num_channels > 0:
                    for ch in range(self.http_api._num_channels):
                        self.http_api._channels.append(ch)

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
        if self.api_version("reboot") > 0:
            self.capabilities[None].add("reboot")
        host_coroutines: list[tuple[Any, Coroutine]] = []
        host_coroutines.append(("network_info", self.get_network_info()))
        if self.api_version("sceneModeCfg") > 0:
            host_coroutines.append((603, self.send(cmd_id=603)))

        if host_coroutines:
            results = await asyncio.gather(*[cor[1] for cor in host_coroutines], return_exceptions=True)
            for i, result in enumerate(results):
                (cmd_id, _) = host_coroutines[i]
                if isinstance(result, ReolinkError):
                    _LOGGER.debug("%s, during getting of host capabilities cmd_id %s", result, cmd_id)
                    continue
                if isinstance(result, BaseException):
                    raise result

                if cmd_id == 603:  # sceneListID
                    self.capabilities[None].add("scenes")
                    self._scenes[-1] = "off"
                    self._parse_xml(cmd_id, result)

        # Stream capabilities
        for channel in self.http_api._stream_channels:
            self.capabilities.setdefault(channel, set())

            if self.api_version("rtsp") > 0:
                self.capabilities[channel].add("stream")

        # Channel capabilities
        coroutines: list[tuple[Any, int, Coroutine]] = []
        for channel in self.http_api._channels:
            self.capabilities.setdefault(channel, set())

            if self.http_api.api_version("talk", channel) > 0:
                coroutines.append((10, channel, self.send(cmd_id=10, channel=channel)))

            if (self.http_api.is_nvr or self.privacy_mode() is not None) and self.api_version("remoteAbility", channel) > 0:
                coroutines.append(("privacy_mode", channel, self.get_privacy_mode(channel)))  # capability added in get_privacy_mode

            if self.api_version("smartAI", channel) > 0:
                coroutines.append((527, channel, self.send(cmd_id=527, channel=channel)))
                coroutines.append((529, channel, self.send(cmd_id=529, channel=channel)))
                coroutines.append((531, channel, self.send(cmd_id=531, channel=channel)))
                coroutines.append((549, channel, self.send(cmd_id=549, channel=channel)))
                coroutines.append((551, channel, self.send(cmd_id=551, channel=channel)))

            if (self.api_version("newIspCfg", channel) >> 16) & 1:  # 17th bit (65536), shift 16
                coroutines.append(("day_night_state", channel, self.get_day_night_state(channel)))

            if self.http_api.is_doorbell(channel) and self.http_api.supported(channel, "battery"):
                self.capabilities[channel].add("hardwired_chime")
                # cmd_id 483 makes the chime rattle a bit, just assume its supported
                # coroutines.append((483, channel, self.get_ding_dong_ctrl(channel)))
            if (self.api_version("ledCtrl", channel) >> 0) & 1:  # 1th bit (1), shift 0
                self.capabilities[channel].add("status_led")  # internal use only
                self.capabilities[channel].add("power_led")
            if (self.api_version("ledCtrl", channel) >> 1) & 1 and (self.api_version("ledCtrl", channel) >> 2) & 1:  # 2nd bit (2), shift 1, 3nd bit (4), shift 2
                self.capabilities[channel].add("floodLight")
            if (self.api_version("ledCtrl", channel) >> 12) & 1:  # 13 th bit (4096) shift 12
                self.capabilities[channel].add("ir_brightness")

            if (self.api_version("recordCfg", channel) >> 7) & 1:  # 8 th bit (128) shift 7
                self.capabilities[channel].add("pre_record")

            coroutines.append(("cry", channel, self.get_cry_detection(channel)))
            coroutines.append(("network_info", channel, self.get_network_info(channel)))
            # Fallback for missing information
            if self.http_api.camera_hardware_version(channel) == UNKNOWN:
                coroutines.append(("ch_info", channel, self.get_info(channel)))

        for scene_id in self._scenes:
            if scene_id < 0:
                continue
            coroutines.append(("scene", scene_id, self.get_scene_info(scene_id)))

        if coroutines:
            results = await asyncio.gather(*[cor[2] for cor in coroutines], return_exceptions=True)
            for i, result in enumerate(results):
                (cmd_id, channel, _) = coroutines[i]
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
                elif cmd_id == "day_night_state" and self.day_night_state is not None:
                    self.capabilities[channel].add("day_night_state")
                elif cmd_id == "cry" and result:
                    self.capabilities[channel].add("ai_cry")

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

    async def get_states(self, cmd_list: cmd_list_type = None, wake: dict[int, bool] | None = None) -> None:
        """Update the state information of polling data"""
        if cmd_list is None:
            cmd_list = {}
        if wake is None:
            wake = dict.fromkeys(self.http_api._channels, True)

        any_battery = any(self.http_api.supported(ch, "battery") for ch in self.http_api._channels)
        all_wake = all(wake.values())

        def inc_host_cmd(cmd: str) -> bool:
            return (cmd in cmd_list or not cmd_list) and (all_wake or not any_battery or cmd in NONE_WAKING_COMMANDS)

        def inc_cmd(cmd: str, channel: int) -> bool:
            return (channel in cmd_list.get(cmd, []) or not cmd_list or len(cmd_list.get(cmd, [])) == 1) and (
                wake[channel] or cmd in NONE_WAKING_COMMANDS or not self.http_api.supported(channel, "battery")
            )

        coroutines: list[Coroutine] = []
        if self.supported(None, "scenes") and inc_host_cmd("GetScene"):
            coroutines.append(self.get_scene())
        if self.http_api.supported(None, "wifi") and self.http_api.wifi_connection() and inc_host_cmd("115"):
            coroutines.append(self.get_wifi_signal())

        for channel in self.http_api._channels:
            if self.http_api.supported(channel, "wifi") and inc_cmd("115", channel):
                coroutines.append(self.get_wifi_signal(channel))

            if self.supported(channel, "day_night_state") and inc_cmd("296", channel):
                coroutines.append(self.get_day_night_state(channel))

            if self.supported(channel, "hardwired_chime") and channel in cmd_list.get("483", []) and channel not in self._hardwired_chime_settings:
                # only get the state if not known yet, cmd_id 483 can make the hardwired chime rattle a bit
                coroutines.append(self.get_ding_dong_ctrl(channel))

            if self.supported(channel, "ir_brightness") and inc_cmd("208", channel):
                coroutines.append(self.get_status_led(channel))

            if self.supported(channel, "ai_cry") and inc_cmd("299", channel):
                coroutines.append(self.get_cry_detection(channel))

            if self.supported(channel, "pre_record") and inc_cmd("594", channel):
                coroutines.append(self.get_pre_recording(channel))

        if coroutines:
            results = await asyncio.gather(*coroutines, return_exceptions=True)
            for result in results:
                if isinstance(result, ReolinkError):
                    _LOGGER.debug(result)
                    continue
                if isinstance(result, BaseException):
                    raise result

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

        if (bc_port := self._ports.get("server", {}).get("port")) is not None and bc_port != self.port:
            _LOGGER.warning("Baichuan host %s: baichuan port changed from %s to %s", self._host, self.port, bc_port)
            self.port = bc_port

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

    @http_cmd(["GetDevInfo", "GetChnTypeInfo"])
    async def get_info(self, channel: int | None = None) -> dict[str, str]:
        """Get the device info of the host or a channel"""
        if channel is None:
            mess = await self.send(cmd_id=80)
        else:
            mess = await self.send(cmd_id=318, channel=channel)
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
            if (self.http_api.api_version("supportWiFi", channel) > 0 or self.http_api._is_hub):
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

    async def get_ptz_position(self, channel: int) -> None:
        """Get the wifi signal of the host"""
        mess = await self.send(cmd_id=433, channel=channel)
        self._ptz_position[channel] = self._get_keys_from_xml(mess, ["pPos", "tPos"])

    async def get_cry_detection(self, channel: int) -> bool:
        """Check if cry detection is supported and get the sensitivity level"""
        mess = await self.send(cmd_id=299, channel=channel)
        data = self._get_keys_from_xml(mess, ["cryDetectAbility", "cryDetectLevel"])
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

    async def get_day_night_state(self, channel: int) -> None:
        """Get the day night state"""
        mess = await self.send(cmd_id=296, channel=channel)
        data = self._get_keys_from_xml(mess, ["stat"])
        self._day_night_state[channel] = data["stat"]

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
        sleep = self._get_value_from_xml(mess, "sleep")
        if sleep is None:
            return None
        value = sleep == "1"
        self._privacy_mode[channel] = value
        self.last_privacy_check = time_now()

        self.capabilities.setdefault(channel, set()).add("privacy_mode")
        if not self.http_api.is_nvr:
            self.capabilities.setdefault(None, set()).add("privacy_mode")

        return value

    async def set_privacy_mode(self, channel: int = 0, enable: bool = False) -> None:
        """Set the privacy mode"""
        enable_str = "1" if enable else "0"
        xml = xmls.SetPrivacyMode.format(enable=enable_str)
        await self.send(cmd_id=575, channel=channel, body=xml)
        # get the new privacy mode status
        await self.get_privacy_mode(channel)

    async def reboot(self) -> None:
        """Reboot the host device"""
        await self.send(cmd_id=23)

    @http_cmd("GetWhiteLed")
    async def get_floodlight(self, channel: int, **_kwargs) -> None:
        """Get the floodlight state"""
        mess = await self.send(cmd_id=289, channel=channel)
        self._parse_xml(289, mess)

    @http_cmd("SetWhiteLed")
    async def set_floodlight(self, channel: int = 0, state: bool | None = None, brightness: int | None = None, mode: int | None = None, **kwargs) -> None:
        """Control the floodlight"""
        if data := kwargs.get("WhiteLed"):
            channel = data["channel"]
            state = data.get("state", state)
            brightness = data.get("bright", brightness)
            mode = data.get("mode", mode)

        if state is None and brightness is None and mode is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: invalid param for SetWhiteLed")

        if mode == SpotlightModeEnum.schedule.value and (self.api_version("ledCtrl", channel) >> 8) & 1:  # schedule_plus, 9th bit (256), shift 8
            # Floodlight: the schedule_plus has the same number 4 as autoadaptive, so switch it around
            mode = 4

        if state is not None:
            xml = xmls.SetWhiteLed.format(channel=channel, state=state)
            await self.send(cmd_id=288, channel=channel, body=xml)
        if brightness is not None or mode is not None:
            mess = await self.send(cmd_id=289, channel=channel)
            xml_body = XML.fromstring(mess)
            xml_element = xml_body.find(".//FloodlightTask")
            if xml_element is None:
                raise UnexpectedDataError(f"Baichuan host {self._host}: set_floodlight: could not find FloodlightTask")
            if brightness is not None:
                xml_brightness = xml_element.find("brightness_cur")
                if xml_brightness is not None:
                    xml_brightness.text = str(brightness)
            if mode is not None:
                xml_mode = xml_element.find("alarmMode")
                if xml_mode is not None:
                    xml_mode.text = str(mode)
                xml_enable = xml_element.find("enable")
                if xml_enable is not None:
                    xml_enable.text = str(mode)
            xml = XML.tostring(xml_body, encoding="unicode")
            xml = xmls.XML_HEADER + xml
            await self.send(cmd_id=290, channel=channel, body=xml)

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

    @http_cmd("GetDingDongList")
    async def GetDingDongList(self, channel: int, retry: int = 3, **_kwargs) -> None:
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
            data = self._get_keys_from_xml(chime, {"id": ("deviceId", int), "name": ("deviceName", str), "netstate": ("netState", int), "netState": ("netState", int)})
            chime_list.append(data)
        json_data = [{"cmd": "GetDingDongList", "code": 0, "value": {"DingDongList": {"pairedlist": chime_list}}}]
        self.http_api.map_channel_json_response(json_data, channel)

    @http_cmd("DingDongOpt")
    async def get_DingDongOpt(self, channel: int = -1, chime_id: int = -1, **kwargs) -> None:
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
            root = XML.fromstring(mess)
            data = self._get_keys_from_xml(root, {"name": ("name", str), "volLevel": ("volLevel", int), "ledState": ("ledState", int)})
            json_data = [{"cmd": "DingDongOpt", "code": 0, "value": {"DingDong": data}}]
            self.http_api.map_channel_json_response(json_data, channel, chime_id)

    @http_cmd("GetDingDongCfg")
    async def GetDingDongCfg(self, channel: int, **_kwargs) -> None:
        """Get the GetDingDongCfg info"""
        mess = await self.send(cmd_id=486, channel=channel)
        root = XML.fromstring(mess)

        chime_list = []
        for chime in root.findall(".//deviceCfg"):
            data: dict[str, Any] = self._get_keys_from_xml(chime, {"id": ("ringId", int)})
            data["type"] = {}
            for ringtone in chime.findall(".//alarminCfg"):
                tone_type = self._get_value_from_xml_element(ringtone, "type")
                data["type"][tone_type] = self._get_keys_from_xml(ringtone, {"valid": ("switch", int), "musicId": ("musicId", int)})
            chime_list.append(data)
        json_data = [{"cmd": "GetDingDongCfg", "code": 0, "value": {"DingDongCfg": {"pairedlist": chime_list}}}]
        self.http_api.map_channel_json_response(json_data, channel)

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

    @http_cmd("SetRecV20")
    async def SetRecV20(self, **kwargs) -> None:
        """Get the GetDingDongCfg info"""
        rec = kwargs.get("Rec", {})
        channel = rec.get("schedule", {}).get("channel")
        enable = rec.get("scheduleEnable")
        if channel is None or enable is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: SetRecV20 invalid input params")

        xml = xmls.SetRecEnable.format(channel=channel, enable=enable)
        await self.send(cmd_id=82, channel=channel, body=xml)

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

    async def get_scene_info(self, scene_id: int) -> None:
        """Get the name of a scene"""
        xml = xmls.GetSceneInfo.format(scene_id=scene_id)
        mess = await self.send(cmd_id=604, body=xml)
        root = XML.fromstring(mess)

        data = self._get_keys_from_xml(root, {"id": ("scene_id", int), "valid": ("valid", int), "name": ("name", str)})
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

    def pan_position(self, channel: int) -> int | None:
        pos = self._ptz_position.get(channel, {}).get("pPos")
        if pos is None:
            return None
        return int(pos)

    def tilt_position(self, channel: int) -> int | None:
        pos = self._ptz_position.get(channel, {}).get("tPos")
        if pos is None:
            return None
        return int(pos)

    def hardwired_chime_type(self, channel: int) -> str | None:
        return str(self._hardwired_chime_settings.get(channel, {}).get("type"))

    def hardwired_chime_enabled(self, channel: int) -> bool:
        if channel not in self._hardwired_chime_settings:
            return False

        return self._hardwired_chime_settings[channel]["enable"] == 1
