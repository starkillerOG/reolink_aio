""" Reolink Baichuan API """

from __future__ import annotations

import logging
import asyncio
from inspect import getmembers
from time import time as time_now
from typing import TYPE_CHECKING, Any
from collections.abc import Callable
from xml.etree import ElementTree as XML
from Cryptodome.Cipher import AES

from . import xmls
from .tcp_protocol import BaichuanTcpClientProtocol
from ..exceptions import (
    InvalidContentTypeError,
    InvalidParameterError,
    ReolinkError,
    UnexpectedDataError,
    ReolinkConnectionError,
    ReolinkTimeoutError,
)
from ..enums import BatteryEnum

from .util import BC_PORT, HEADER_MAGIC, AES_IV, EncType, PortType, decrypt_baichuan, encrypt_baichuan, md5_str_modern, http_cmd

if TYPE_CHECKING:
    from ..api import Host

_LOGGER = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
KEEP_ALLIVE_INTERVAL = 30  # seconds
MIN_KEEP_ALLIVE_INTERVAL = 9  # seconds


class Baichuan:
    """Reolink Baichuan API class."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = BC_PORT,
        http_api: Host | None = None,
    ) -> None:
        self.http_api = http_api

        self._host: str = host
        self._port: int = port
        self._username: str = username
        self._password: str = password
        self._nonce: str | None = None
        self._user_hash: str | None = None
        self._password_hash: str | None = None
        self._aes_key: bytes | None = None
        self._log_once: list[str] = []

        # TCP connection
        self._mutex = asyncio.Lock()
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
        for _name, func in getmembers(self, lambda o: hasattr(o, "http_cmd")):
            self.cmd_funcs[func.http_cmd] = func

        # states
        self._ports: dict[str, dict[str, int | bool]] = {}
        self._dev_info: dict[int | None, dict[str, str]] = {}
        self._day_night_state: str | None = None
        self._ptz_position: dict[int, dict[str, str]] = {}

    async def send(
        self,
        cmd_id: int,
        channel: int | None = None,
        body: str = "",
        extension: str = "",
        enc_type: EncType = EncType.AES,
        message_class: str = "1464",
        enc_offset: int = 0,
        retry: int = RETRY_ATTEMPTS,
    ) -> str:
        """Generic baichuan send method."""
        retry = retry - 1

        if not self._logged_in and cmd_id > 2:
            # not logged in and requesting a non login/logout cmd, first login
            await self.login()

        if channel is not None:
            if extension:
                raise InvalidParameterError(f"Baichuan host {self._host}: cannot specify both channel and extension")
            extension = xmls.CHANNEL_EXTENSION_XML.format(channel=channel)

        mess_len = len(extension) + len(body)
        payload_offset = len(extension)

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
                enc_body_bytes = encrypt_baichuan(extension, enc_offset) + encrypt_baichuan(body, enc_offset)
            elif enc_type == EncType.AES:
                enc_body_bytes = self._aes_encrypt(extension) + self._aes_encrypt(body)
            else:
                raise InvalidParameterError(f"Baichuan host {self._host}: invalid param enc_type '{enc_type}'")

        # send message
        if self._transport is None or self._protocol is None or self._transport.is_closing():
            try:
                async with asyncio.timeout(15):
                    async with self._mutex:
                        self._transport, self._protocol = await self._loop.create_connection(
                            lambda: BaichuanTcpClientProtocol(self._loop, self._host, self._push_callback, self._close_callback), self._host, self._port
                        )
            except asyncio.TimeoutError as err:
                raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error") from err
            except (ConnectionResetError, OSError) as err:
                raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error: {str(err)}") from err

        if _LOGGER.isEnabledFor(logging.DEBUG):
            if mess_len > 0:
                _LOGGER.debug("Baichuan host %s: writing cmd_id %s, body:\n%s", self._host, cmd_id, self._hide_password(extension + body))
            else:
                _LOGGER.debug("Baichuan host %s: writing cmd_id %s, without body", self._host, cmd_id)

        if cmd_id in self._protocol.receive_futures:
            raise ReolinkError(f"Baichuan host {self._host}: receive future is already set for cmd_id {cmd_id}, cannot receive multiple requests simultaneously")

        self._protocol.receive_futures[cmd_id] = self._loop.create_future()

        try:
            async with asyncio.timeout(15):
                async with self._mutex:
                    self._transport.write(header + enc_body_bytes)
                data, len_header = await self._protocol.receive_futures[cmd_id]
        except asyncio.TimeoutError as err:
            raise ReolinkTimeoutError(f"Baichuan host {self._host}: Timeout error") from err
        except (ConnectionResetError, OSError) as err:
            if retry <= 0 or cmd_id == 2:
                raise ReolinkConnectionError(f"Baichuan host {self._host}: Connection error during read/write: {str(err)}") from err
            _LOGGER.debug("Baichuan host %s: Connection error during read/write: %s, trying again", self._host, str(err))
            return await self.send(cmd_id, channel, body, extension, enc_type, message_class, enc_offset, retry)
        finally:
            if cmd_id in self._protocol.receive_futures:
                if not self._protocol.receive_futures[cmd_id].done():
                    self._protocol.receive_futures[cmd_id].cancel()
                self._protocol.receive_futures.pop(cmd_id, None)

        # decryption
        rec_body = self._decrypt(data, len_header, cmd_id, enc_type)

        if _LOGGER.isEnabledFor(logging.DEBUG):
            if len(rec_body) > 0:
                _LOGGER.debug("Baichuan host %s: received:\n%s", self._host, self._hide_password(rec_body))
            else:
                _LOGGER.debug("Baichuan host %s: received status 200:OK without body", self._host)

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
        rec_enc_offset = int.from_bytes(data[12:16], byteorder="little")
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
            if enc_type != EncType.BC:
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
        self._events_active = False
        if self._subscribed:
            if self.http_api is not None and self.http_api._updating:
                _LOGGER.debug("Baichuan host %s: lost event subscription during firmware reboot", self._host)
                return
            now = time_now()
            if self._protocol is not None:
                time_since_recv = now - self._protocol.time_recv
            else:
                time_since_recv = 0
            if now - self._time_reestablish > 150:  # limit the amount of reconnects to prevent fast loops
                self._time_reestablish = now
                self._loop.create_task(self._reestablish_connection(time_since_recv))
                return

            self._time_connection_lost = now
            _LOGGER.error("Baichuan host %s: lost event subscription after %.2f s", self._host, time_since_recv)

    async def _reestablish_connection(self, time_since_recv: float) -> None:
        """Try to reestablish the connection after a connection is closed"""
        time_start = time_now()
        try:
            await self.send(cmd_id=31)  # Subscribe to events
        except Exception as err:
            _LOGGER.error("Baichuan host %s: lost event subscription after %.2f s and failed to reestablished connection", self._host, time_since_recv)
            _LOGGER.debug("Baichuan host %s: failed to reestablished connection: %s", self._host, str(err))
            self._time_connection_lost = time_start
        else:
            _LOGGER.debug("Baichuan host %s: lost event subscription after %.2f s, but reestablished connection immediately", self._host, time_since_recv)
            if time_now() - time_start < 5:
                origianal_keepalive = self._keepalive_interval
                self._keepalive_interval = max(MIN_KEEP_ALLIVE_INTERVAL, min(time_since_recv - 1, self._keepalive_interval - 1))
                _LOGGER.debug("Baichuan host %s: reducing keepalive interval from %.2f to %.2f s", self._host, origianal_keepalive, self._keepalive_interval)

    def _get_value_from_xml_element(self, xml_element: XML.Element, key: str) -> str | None:
        """Get a value for a key in a xml element"""
        xml_value = xml_element.find(f".//{key}")
        if xml_value is None:
            return None
        return xml_value.text

    def _get_channel_from_xml_element(self, xml_element: XML.Element, key: str = "channelId") -> int | None:
        channel_str = self._get_value_from_xml_element(xml_element, key)
        if channel_str is None:
            return None
        channel = int(channel_str)
        if self.http_api is not None and channel not in self.http_api._channels:
            return None
        return channel

    def _get_keys_from_xml(self, xml: str | XML.Element, keys: list[str] | dict[str, tuple[str, type]]) -> dict[str, Any]:
        """Get multiple keys from a xml and return as a dict"""
        if isinstance(xml, str):
            root = XML.fromstring(xml)
        else:
            root = xml
        result = {}
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

    def _parse_xml(self, cmd_id: int, xml: str) -> None:
        """parce received xml"""
        if self.http_api is None:
            return

        root = XML.fromstring(xml)

        state: Any
        channels: set[int | None] = {None}
        cmd_ids: set[int | None] = {None, cmd_id}
        if cmd_id == 33:  # Motion/AI/Visitor event | DayNightEvent
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
                    elif event.tag == "DayNightEvent":
                        state = self._get_value_from_xml_element(event, "mode")
                        if state is not None:
                            self._day_night_state = state
                            _LOGGER.debug("Reolink %s TCP event channel %s, day night state: %s", self.http_api.nvr_name, channel, state)
                    else:
                        if f"TCP_event_tag_{event.tag}" not in self._log_once:
                            self._log_once.append(f"TCP_event_tag_{event.tag}")
                            _LOGGER.warning("Reolink %s TCP event cmd_id %s, channel %s, received unknown event tag %s", self.http_api.nvr_name, cmd_id, channel, event.tag)

        if cmd_id == 145:  # ChannelInfoList: Sleep status
            for event in root.findall(".//ChannelInfo"):
                channel = self._get_channel_from_xml_element(event)
                if channel is None:
                    continue
                channels.add(channel)
                state = self._get_value_from_xml_element(event, "loginState") == "standby"
                if state != self.http_api._sleep.get(channel):
                    _LOGGER.debug("Reolink %s TCP event channel %s, sleeping: %s", self.http_api.nvr_name, channel, state)
                self.http_api._sleep[channel] = state

        if cmd_id == 252:  # BatteryInfo
            for event in root.findall(".//BatteryInfo"):
                channel = self._get_channel_from_xml_element(event)
                if channel is None:
                    continue
                channels.add(channel)
                data = self._get_keys_from_xml(
                    root,
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

        elif cmd_id == 291:  # Floodlight
            for event_list in root:
                for event in event_list:
                    channel = self._get_channel_from_xml_element(event, "channel")
                    if channel is None:
                        continue
                    channels.add(channel)
                    state = self._get_value_from_xml_element(event, "status")
                    if state is not None and channel in self.http_api._whiteled_settings:
                        self.http_api._whiteled_settings[channel]["WhiteLed"]["state"] = int(state)
                        _LOGGER.debug("Reolink %s TCP event channel %s, Floodlight: %s", self.http_api.nvr_name, channel, state)

        # call the callbacks
        for cmd in cmd_ids:
            for ch in channels:
                for callback in self._ext_callback.get(cmd, {}).get(ch, {}).values():
                    callback()

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
                _LOGGER.debug("Baichuan host %s: sending keepalive for event subscription", self._host)
                try:
                    if self._events_active:
                        await self.send(cmd_id=93)  # LinkType is used as keepalive
                    else:
                        await self.send(cmd_id=31)  # Subscribe to events
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
        try:
            await self.send(cmd_id=31)
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
        nonce = await self._get_nonce()

        # modern login
        self._user_hash = md5_str_modern(f"{self._username}{nonce}")
        self._password_hash = md5_str_modern(f"{self._password}{nonce}")
        xml = xmls.LOGIN_XML.format(userName=self._user_hash, password=self._password_hash)

        await self.send(cmd_id=1, enc_type=EncType.BC, body=xml)
        self._logged_in = True

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

    async def get_info(self, channel: int | None = None) -> dict[str, str]:
        """Get the device info of the host or a channel"""
        if channel is None:
            mess = await self.send(cmd_id=80)
        else:
            mess = await self.send(cmd_id=318, channel=channel)
        self._dev_info[channel] = self._get_keys_from_xml(mess, ["type", "hardwareVersion", "firmwareVersion", "itemNo"])
        return self._dev_info[channel]

    async def get_channel_uids(self) -> None:
        """Get a channel list containing the UIDs"""
        # the NVR sends a message with cmd_id 145 when connecting, but it seems to not allow requesting that id.
        await self.send(cmd_id=145)

    async def get_wifi_signal(self) -> None:
        """Get the wifi signal of the host"""
        await self.send(cmd_id=115)

    async def get_ptz_position(self, channel: int) -> None:
        """Get the wifi signal of the host"""
        mess = await self.send(cmd_id=433, channel=channel)
        self._ptz_position[channel] = self._get_keys_from_xml(mess, ["pPos", "tPos"])

    @http_cmd("GetDingDongList")
    async def GetDingDongList(self, channel: int, **_kwargs) -> None:
        """Get the DingDongList info"""
        if self.http_api is None:
            return

        mess = await self.send(cmd_id=484, channel=channel)
        root = XML.fromstring(mess)

        chime_list = []
        for chime in root.findall(".//dingdongDeviceInfo"):
            data = self._get_keys_from_xml(chime, {"id": ("deviceId", int), "name": ("deviceName", str), "netstate": ("netState", int), "netState": ("netState", int)})
            chime_list.append(data)
        json_data = [{"cmd": "GetDingDongList", "code": 0, "value": {"DingDongList": {"pairedlist": chime_list}}}]
        self.http_api.map_channel_json_response(json_data, channel)

    @http_cmd("DingDongOpt")
    async def get_DingDongOpt(self, channel: int = -1, chime_id: int = -1, **kwargs) -> None:
        """Get the DingDongOpt info"""
        if self.http_api is None:
            return
        if ch := kwargs.get("DingDong", {}).get("channel"):
            channel = ch
        if ring_id := kwargs.get("DingDong", {}).get("id"):
            chime_id = ring_id
        option = kwargs.get("DingDong", {}).get("option", 2)

        xml = ""
        if option == 1:
            xml = xmls.DingDongOpt_1_XML.format(chime_id=chime_id)
        if option == 2:
            xml = xmls.DingDongOpt_2_XML.format(chime_id=chime_id)
        if option == 3:
            name = kwargs.get("DingDong", {}).get("name", "Reolink Chime")
            vol = kwargs.get("DingDong", {}).get("volLevel", 4)
            led = kwargs.get("DingDong", {}).get("ledState", 1)
            xml = xmls.DingDongOpt_3_XML.format(chime_id=chime_id, vol=vol, led=led, name=name)
        if option == 4:
            tone_id = kwargs.get("DingDong", {}).get("musicId", 0)
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
        if self.http_api is None:
            return

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
        channel = kwargs.get("DingDongCfg", {}).get("channel", -1)
        chime_id = kwargs.get("DingDongCfg", {}).get("ringId", -1)
        event_type = list(kwargs.get("DingDongCfg", {}).get("type", {"event": {}}).keys())[0]
        state = kwargs.get("DingDongCfg", {}).get("type", {}).get(event_type, {}).get("switch", 0)
        tone_id = kwargs.get("DingDongCfg", {}).get("type", {}).get(event_type, {}).get("musicId", -1)

        xml = xmls.SetDingDongCfg_XML.format(chime_id=chime_id, event_type=event_type, state=state, tone_id=tone_id)
        await self.send(cmd_id=487, channel=channel, body=xml)

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
        channel = kwargs.get("Rec", {}).get("schedule", {}).get("channel")
        enable = kwargs.get("Rec", {}).get("scheduleEnable")
        if channel is None or enable is None:
            raise InvalidParameterError(f"Baichuan host {self._host}: SetRecV20 invalid input params")

        xml = xmls.SetRecEnable.format(channel=channel, enable=enable)
        await self.send(cmd_id=82, channel=channel, body=xml)

    @property
    def events_active(self) -> bool:
        return self._events_active and time_now() - self._time_connection_lost > 120

    @property
    def day_night_state(self) -> str | None:
        """known values: day, night, led_day"""
        return self._day_night_state

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

    def model(self, channel: int | None = None) -> str | None:
        return self._dev_info.get(channel, {}).get("type")

    def hardware_version(self, channel: int | None = None) -> str:
        return self._dev_info.get(channel, {}).get("hardwareVersion", "Unknown")

    def item_number(self, channel: int | None = None) -> str | None:
        return self._dev_info.get(channel, {}).get("itemNo")

    def sw_version(self, channel: int | None = None) -> str | None:
        return self._dev_info.get(channel, {}).get("firmwareVersion")

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
