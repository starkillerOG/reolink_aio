""" Reolink Baichuan API """

from __future__ import annotations

import logging
import asyncio
from time import time as time_now
from typing import TYPE_CHECKING
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

from .util import BC_PORT, HEADER_MAGIC, AES_IV, EncType, PortType, decrypt_baichuan, encrypt_baichuan, md5_str_modern

if TYPE_CHECKING:
    from ..api import Host

_LOGGER = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
KEEP_ALLIVE_INTERVAL = 30  # seconds


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
        self._ext_callback: dict[int | None, dict[int | None, dict[str, Callable[[], None]]]] = {}

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
                enc_body_bytes = encrypt_baichuan(extension + body, enc_offset)
            elif enc_type == EncType.AES:
                enc_body_bytes = self._aes_encrypt(extension + body)
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
            _LOGGER.warning(err)
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
            _LOGGER.error("Baichuan host %s: lost event subscription", self._host)

    def _get_value_from_xml_element(self, xml_element: XML.Element, key: str) -> str | None:
        """Get a value for a key in a xml element"""
        xml_value = xml_element.find(f".//{key}")
        if xml_value is None:
            return None
        return xml_value.text

    def _get_channel_from_xml_element(self, xml_element: XML.Element, key: str = "channel") -> int | None:
        channel_str = self._get_value_from_xml_element(xml_element, key)
        if channel_str is None:
            return None
        channel = int(channel_str)
        if self.http_api is not None and channel not in self.http_api._channels:
            return None
        return channel

    def _get_keys_from_xml(self, xml: str, keys: list[str]) -> dict[str, str]:
        """Get multiple keys from a xml and return as a dict"""
        root = XML.fromstring(xml)
        result = {}
        for key in keys:
            value = self._get_value_from_xml_element(root, key)
            if value is None:
                continue
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

        channels: set[int | None] = {None}
        cmd_ids: set[int | None] = {None, cmd_id}
        if cmd_id == 33:  # Motion/AI/Visitor event | DayNightEvent
            root = XML.fromstring(xml)
            for event_list in root:
                for event in event_list:
                    channel = self._get_channel_from_xml_element(event, "channelId")
                    if channel is None:
                        continue
                    channels.add(channel)

                    if event.tag == "AlarmEvent":
                        states = self._get_value_from_xml_element(event, "status")
                        ai_types = self._get_value_from_xml_element(event, "AItype")
                        if self._subscribed and not self._events_active:
                            self._events_active = True

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

                            ai_type_list = ai_types.split(",")
                            for ai_type in ai_type_list:
                                if ai_type == "none":
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

        elif cmd_id == 291:  # Floodlight
            root = XML.fromstring(xml)
            for event_list in root:
                for event in event_list:
                    channel = self._get_channel_from_xml_element(event)
                    if channel is None:
                        continue
                    channels.add(channel)
                    state = self._get_value_from_xml_element(event, "status")
                    if state is not None:
                        self.http_api._whiteled_settings[channel]["WhiteLed"]["state"] = int(state)
                        _LOGGER.debug("Reolink %s TCP event channel %s, Floodlight: %s", self.http_api.nvr_name, channel, state)

        elif cmd_id == 623:  # Sleep status
            pass

        # call the callbacks
        for cmd in cmd_ids:
            for ch in channels:
                for callback in self._ext_callback.get(cmd, {}).get(ch, {}).values():
                    callback()

    async def _keepalive_loop(self) -> None:
        """Loop which keeps the TCP connection allive when subscribed for events"""
        while True:
            _LOGGER.debug("Baichuan host %s: sending keepalive for event subscription", self._host)
            try:
                await self.send(cmd_id=31)
            except Exception as err:
                _LOGGER.debug("Baichuan host %s: error while sending keepalive for event subscription: %s", self._host, str(err))
            await asyncio.sleep(KEEP_ALLIVE_INTERVAL)
            while self._protocol is not None:
                sleep_t = KEEP_ALLIVE_INTERVAL - (time_now() - self._protocol.time_recv)
                if sleep_t < 0.5:
                    break
                await asyncio.sleep(sleep_t)

    async def subscribe_events(self) -> None:
        """Subscribe to baichuan push events, keeping the connection open"""
        if self._subscribed:
            _LOGGER.debug("Baichuan host %s: already subscribed to events", self._host)
            return
        self._subscribed = True
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

    @property
    def events_active(self) -> bool:
        return self._events_active

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
