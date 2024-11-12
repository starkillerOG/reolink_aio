""" Reolink NVR/camera network API """

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import ssl
import traceback
import uuid
import re
from datetime import datetime, timedelta, tzinfo
from time import time as time_now
from os.path import basename
from typing import Any, Literal, Optional, overload
from urllib import parse
from xml.etree import ElementTree as XML
from statistics import mean

from orjson import JSONDecodeError, loads as json_loads  # pylint: disable=no-name-in-module
from aiortsp.rtsp.connection import RTSPConnection  # type: ignore
from aiortsp.rtsp.errors import RTSPError  # type: ignore
import aiohttp

from . import templates, typings
from .baichuan import Baichuan, PortType
from .enums import (
    BatteryEnum,
    DayNightEnum,
    StatusLedEnum,
    SpotlightModeEnum,
    PtzEnum,
    GuardEnum,
    TrackMethodEnum,
    SubType,
    VodRequestType,
    HDREnum,
    ChimeToneEnum,
    HubToneEnum,
)
from .exceptions import (
    ApiError,
    CredentialsInvalidError,
    InvalidContentTypeError,
    InvalidParameterError,
    LoginError,
    LoginFirmwareError,
    NoDataError,
    NotSupportedError,
    ReolinkError,
    SubscriptionError,
    UnexpectedDataError,
    ReolinkConnectionError,
    ReolinkTimeoutError,
)
from .software_version import SoftwareVersion, NewSoftwareVersion, MINIMUM_FIRMWARE
from .utils import datetime_to_reolink_time, reolink_time_to_datetime, strip_model_str

MANUFACTURER = "Reolink"
DEFAULT_STREAM = "sub"
DEFAULT_PROTOCOL = "rtmp"
DEFAULT_TIMEOUT = 30
RETRY_ATTEMPTS = 3
MAX_CHUNK_ITEMS = 35
DEFAULT_RTMP_AUTH_METHOD = "PASSWORD"
SUBSCRIPTION_TERMINATION_TIME = 15  # minutes
LONG_POLL_TIMEOUT = 5  # minutes

MOTION_DETECTION_TYPE = "motion"
FACE_DETECTION_TYPE = "face"
PERSON_DETECTION_TYPE = "person"
VEHICLE_DETECTION_TYPE = "vehicle"
PET_DETECTION_TYPE = "pet"
VISITOR_DETECTION_TYPE = "visitor"
PACKAGE_DETECTION_TYPE = "package"

_LOGGER = logging.getLogger(__name__)
_LOGGER_DATA = logging.getLogger(__name__ + ".data")
_LOGGER_RTSP = logging.getLogger(__name__ + ".aiortsp")
_LOGGER_RTSP.setLevel(logging.WARNING)

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.set_ciphers("DEFAULT")
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

# with 2 streaming channels
DUAL_LENS_DUAL_MOTION_MODELS: set[str] = {
    "Reolink Duo PoE",
    "Reolink Duo WiFi",
}
DUAL_LENS_SINGLE_MOTION_MODELS: set[str] = {
    "Reolink TrackMix PoE",
    "Reolink TrackMix WiFi",
    "RLC-81MA",
}
DUAL_LENS_MODELS: set[str] = DUAL_LENS_DUAL_MOTION_MODELS | DUAL_LENS_SINGLE_MOTION_MODELS

WAKING_COMMANDS = [
    "GetWhiteLed",
    "GetZoomFocus",
    "GetAudioCfg",
    "GetPtzGuard",
    "GetAutoReply",
    "GetPtzTraceSection",
    "GetAiCfg",
    "GetAiAlarm",
    "GetPtzCurPos",
    "GetAudioAlarm",
]

# not all chars in a password can be used in the URLS of for instance the FLV stream
ALLOWED_SPECIAL_CHARS = r"@$*~_-+=!?.,:;'()[]"
ALLOWED_CHARS = set(r"abcdefghijklmnopqrstuvwxyz" "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "0123456789" + ALLOWED_SPECIAL_CHARS)
FORBIDEN_CHARS = set(r"""#&% ^`"\/|{}<>""")


##########################################################################################################################################################
# API class
##########################################################################################################################################################
class Host:
    """Reolink network API class."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: Optional[int] = None,
        use_https: Optional[bool] = None,
        protocol: str = DEFAULT_PROTOCOL,
        stream: str = DEFAULT_STREAM,
        timeout: int = DEFAULT_TIMEOUT,
        rtmp_auth_method: str = DEFAULT_RTMP_AUTH_METHOD,
        aiohttp_get_session_callback=None,
    ):
        self._send_mutex = asyncio.Lock()
        self._login_mutex = asyncio.Lock()
        self._long_poll_mutex = asyncio.Lock()

        ##############################################################################
        # Host
        self._url: str = ""
        self._use_https: Optional[bool] = use_https
        self._host: str = host
        self._port: Optional[int] = port
        self._rtsp_port: Optional[int] = None
        self._rtmp_port: Optional[int] = None
        self._onvif_port: Optional[int] = None
        self._rtsp_enabled: Optional[bool] = None
        self._rtmp_enabled: Optional[bool] = None
        self._onvif_enabled: Optional[bool] = None
        self._mac_address: Optional[str] = None

        self.refresh_base_url()

        ##############################################################################
        # Login session
        self._username: str = username
        self._password: str = password[:31]
        self._enc_password = parse.quote(self._password, safe="")
        self._token: Optional[str] = None
        self._lease_time: Optional[datetime] = None
        # Connection session
        self._timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=timeout)
        self._aiohttp_session_internall: bool = True
        if aiohttp_get_session_callback is not None:
            self._get_aiohttp_session = aiohttp_get_session_callback
            self._aiohttp_session_internall = False
        else:
            self._get_aiohttp_session = lambda: aiohttp.ClientSession(timeout=self._timeout, connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT))
        self._aiohttp_session: aiohttp.ClientSession = self._get_aiohttp_session()

        ##############################################################################
        # Baichuan protocol (port 9000)
        self.baichuan = Baichuan(host=host, username=username, password=password, http_api=self)

        ##############################################################################
        # NVR (host-level) attributes
        self._is_nvr: bool = False
        self._is_hub: bool = False
        self._nvr_name: str = ""
        self._nvr_serial: Optional[str] = None
        self._nvr_uid: Optional[str] = None
        self._nvr_model: Optional[str] = None
        self._nvr_item_number: Optional[str] = None
        self._nvr_num_channels: int = 0
        self._nvr_hw_version: Optional[str] = None
        self._nvr_sw_version: Optional[str] = None
        self._nvr_sw_version_object: Optional[SoftwareVersion] = None

        ##############################################################################
        # Combined attributes
        self._sw_hardware_id: dict[int | None, int] = {}
        self._sw_model_id: dict[int | None, int] = {}
        self._last_sw_id_check: float = 0
        self._latest_sw_model_version: dict[str, NewSoftwareVersion] = {}
        self._latest_sw_version: dict[int | None, NewSoftwareVersion | str | Literal[False]] = {}
        self._startup: bool = True
        self._new_devices: bool = False

        ##############################################################################
        # Channels of cameras, used in this NVR ([0] for a directly connected camera)
        self._GetChannelStatus_present: bool = False
        self._GetChannelStatus_has_name: bool = False
        self._channels: list[int] = []
        self._stream_channels: list[int] = []
        self._channel_names: dict[int, str] = {}
        self._channel_uids: dict[int, str] = {}
        self._channel_models: dict[int, str] = {}
        self._channel_online: dict[int, bool] = {}
        self._channel_hw_version: dict[int, str] = {}
        self._channel_sw_versions: dict[int, str] = {}
        self._channel_sw_version_objects: dict[int, SoftwareVersion] = {}
        self._is_doorbell: dict[int, bool] = {}
        self._GetDingDong_present: dict[int, bool] = {}

        ##############################################################################
        # API-versions and capabilities
        self._api_version: dict[str, int] = {}
        self._abilities: dict[str, Any] = {}  # raw response from NVR/camera
        self._capabilities: dict[int | str, set[str]] = {"Host": set()}  # processed by construct_capabilities

        ##############################################################################
        # Video-stream formats
        self._stream: str = stream
        self._protocol: str = protocol
        self._rtmp_auth_method: str = rtmp_auth_method
        self._rtsp_mainStream: dict[int, str] = {}
        self._rtsp_subStream: dict[int, str] = {}
        self._rtsp_verified: dict[int, dict[str, str]] = {}

        ##############################################################################
        # Presets
        self._ptz_presets: dict[int, dict] = {}
        self._ptz_patrols: dict[int, dict] = {}

        ##############################################################################
        # Saved info response-blocks
        self._hdd_info: list[dict] = []
        self._local_link: Optional[dict] = None
        self._wifi_signal: Optional[int] = None
        self._performance: dict = {}
        self._state_light: dict = {}
        self._users: Optional[dict] = None

        ##############################################################################
        # Saved settings response-blocks
        # Host-level
        self._time_settings: Optional[dict] = None
        self._host_time_difference: float = 0
        self._ntp_settings: Optional[dict] = None
        self._netport_settings: Optional[dict] = None
        self._push_config: dict = {}
        # Chime-level
        self._chime_list: dict[int, Chime] = {}
        # Camera-level
        self._zoom_focus_settings: dict[int, dict] = {}
        self._zoom_focus_range: dict[int, dict] = {}
        self._auto_focus_settings: dict[int, dict] = {}
        self._isp_settings: dict[int, dict] = {}
        self._image_settings: dict[int, dict] = {}
        self._ftp_settings: dict[int, dict] = {}
        self._osd_settings: dict[int, dict] = {}
        self._push_settings: dict[int, dict] = {}
        self._webhook_settings: dict[int, dict] = {}
        self._enc_settings: dict[int, dict] = {}
        self._ptz_presets_settings: dict[int, dict] = {}
        self._ptz_patrol_settings: dict[int, dict] = {}
        self._ptz_guard_settings: dict[int, dict] = {}
        self._ptz_position: dict[int, dict] = {}
        self._email_settings: dict[int, dict] = {}
        self._ir_settings: dict[int, dict] = {}
        self._status_led_settings: dict[int, dict] = {}
        self._whiteled_settings: dict[int, dict] = {}
        self._sleep: dict[int, bool] = {}
        self._battery: dict[int, dict] = {}
        self._pir: dict[int, dict] = {}
        self._recording_settings: dict[int, dict] = {}
        self._manual_record_settings: dict[int, dict] = {}
        self._md_alarm_settings: dict[int, dict] = {}
        self._ai_alarm_settings: dict[int, dict] = {}
        self._audio_settings: dict[int, dict] = {}
        self._hub_audio_settings: dict[int, dict] = {}
        self._audio_alarm_settings: dict[int, dict] = {}
        self._buzzer_settings: dict[int, dict] = {}
        self._auto_track_settings: dict[int, dict] = {}
        self._auto_track_range: dict[int, dict] = {}
        self._auto_track_limits: dict[int, dict] = {}
        self._audio_file_list: dict[int, dict] = {}
        self._auto_reply_settings: dict[int, dict] = {}

        ##############################################################################
        # States
        self._motion_detection_states: dict[int, bool] = {}
        self._ai_detection_support: dict[int, dict[str, bool]] = {}

        ##############################################################################
        # Camera-level states
        self._ai_detection_states: dict[int, dict[str, bool]] = {}
        self._visitor_states: dict[int, bool] = {}

        ##############################################################################
        # SUBSCRIPTION managing
        self._subscribe_url: Optional[str] = None

        self._subscription_manager_url: dict[str, str] = {}
        self._subscription_termination_time: dict[str, datetime] = {}
        self._subscription_time_difference: dict[str, float] = {}
        self._onvif_only_motion = {SubType.push: True, SubType.long_poll: True}
        self._log_once: list[str] = []

    ##############################################################################
    # Properties
    @property
    def host(self) -> str:
        return self._host

    @property
    def username(self) -> str:
        return self._username

    @property
    def use_https(self) -> Optional[bool]:
        return self._use_https

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def onvif_port(self) -> Optional[int]:
        return self._onvif_port

    @property
    def rtmp_port(self) -> Optional[int]:
        return self._rtmp_port

    @property
    def rtsp_port(self) -> Optional[int]:
        return self._rtsp_port

    @property
    def onvif_enabled(self) -> Optional[bool]:
        return self._onvif_enabled

    @property
    def rtmp_enabled(self) -> Optional[bool]:
        return self._rtmp_enabled

    @property
    def rtsp_enabled(self) -> Optional[bool]:
        return self._rtsp_enabled

    @property
    def mac_address(self) -> str:
        if self._mac_address is None:
            raise NoDataError("Mac address not yet retrieved")
        return self._mac_address

    @property
    def serial(self) -> Optional[str]:
        return self._nvr_serial

    @property
    def uid(self) -> str:
        if self._nvr_uid is None:
            return "Unknown"
        return self._nvr_uid

    @property
    def wifi_connection(self) -> bool:
        """LAN or Wifi"""
        if self._local_link is None:
            return False

        return self._local_link["LocalLink"]["activeLink"] != "LAN"

    @property
    def wifi_signal(self) -> Optional[int]:
        """wifi_signal 0-4"""
        return self._wifi_signal

    @property
    def cpu_usage(self) -> Optional[int]:
        """CPU usage in %"""
        return self._performance.get("cpuUsed")

    @property
    def state_light(self) -> bool:
        """State light enabled"""
        return self._state_light.get("enable", False)

    @property
    def alarm_volume(self) -> int:
        """Hub/NVR volume for alarm sounds 0-100"""
        for data in self._hub_audio_settings.values():
            return data["AudioCfg"]["alarmVolume"]
        return 100

    @property
    def message_volume(self) -> int:
        """Hub/NVR volume for alarm sounds 0-100"""
        for data in self._hub_audio_settings.values():
            return data["AudioCfg"]["cuesVolume"]
        return 100

    @property
    def is_nvr(self) -> bool:
        return self._is_nvr

    @property
    def is_hub(self) -> bool:
        return self._is_hub

    @property
    def nvr_name(self) -> str:
        if not self._is_nvr and self._nvr_name == "":
            if len(self._channels) > 0 and self._channels[0] in self._channel_names:
                return self._channel_names[self._channels[0]]

            return "Unknown"
        return self._nvr_name

    @property
    def sw_version(self) -> str:
        if self._nvr_sw_version is None:
            return "Unknown"
        return self._nvr_sw_version

    @property
    def sw_version_object(self) -> SoftwareVersion:
        if self._nvr_sw_version_object is None:
            return SoftwareVersion(None)

        return self._nvr_sw_version_object

    @property
    def sw_version_required(self) -> SoftwareVersion:
        """Return the minimum required firmware version for proper operation of this library"""
        if self._nvr_model is None or self._nvr_hw_version is None:
            return SoftwareVersion(None)

        return SoftwareVersion(MINIMUM_FIRMWARE.get(self.model, {}).get(self.hardware_version))

    @property
    def sw_version_update_required(self) -> bool:
        """Check if a firmware version update is required for proper operation of this library"""
        if self._nvr_sw_version_object is None:
            return False

        return not self._nvr_sw_version_object >= self.sw_version_required  # pylint: disable=unneeded-not

    @property
    def model(self) -> str:
        if self._nvr_model is None:
            return "Unknown"
        return self._nvr_model

    @property
    def item_number(self) -> str | None:
        return self._nvr_item_number

    @property
    def hardware_version(self) -> str:
        if self._nvr_hw_version is None:
            return "Unknown"
        return self._nvr_hw_version

    @property
    def manufacturer(self) -> str:
        return MANUFACTURER

    @property
    def num_channels(self) -> int:
        """Return the total number of channels in the NVR (should be 1 for a standalone camera, maybe 2 for DUO cameras)."""
        return self._nvr_num_channels

    @property
    def num_cameras(self) -> int:
        """Return the number of channels IN USE in that NVR (should be 1 for a standalone camera, maybe 2 for DUO cameras)."""
        return len(self._channels)

    @property
    def channels(self) -> list[int]:
        """Return the list of indices of channels in use."""
        return self._channels

    @property
    def stream_channels(self) -> list[int]:
        """Return the list of indices of stream channels available."""
        return self._stream_channels

    @property
    def new_devices(self) -> bool:
        """Return if new devices were discovered after initial initialization."""
        return self._new_devices

    @property
    def hdd_info(self) -> list[dict]:
        return self._hdd_info

    @property
    def hdd_list(self) -> list[int]:
        return list(range(0, len(self._hdd_info)))

    @property
    def chime_list(self) -> list[Chime]:
        return list(self._chime_list.values())

    @property
    def stream(self) -> str:
        return self._stream

    @property
    def protocol(self) -> str:
        return self._protocol

    @property
    def session_active(self) -> bool:
        if self._token is not None and self._lease_time is not None and self._lease_time > (datetime.now() + timedelta(seconds=5)):
            return True
        return False

    @property
    def timeout(self) -> float:
        if self._timeout.total is None:
            return DEFAULT_TIMEOUT
        return self._timeout.total

    @property
    def user_level(self) -> str:
        """Check if the user has admin authorisation."""
        if self._users is None or len(self._users) < 1:
            return "unknown"

        for user in self._users:
            if user["userName"] == self._username:
                return user["level"]

        return "unknown"

    @property
    def is_admin(self) -> bool:
        """
        Check if the user has admin authorisation.
        Only admin users can change camera settings, not everything will work if account is not admin
        """
        return self.user_level == "admin"

    def valid_password(self) -> bool:
        """check if the password contains incompatible characters"""
        password_set = set(self._password)
        if len(password_set - ALLOWED_CHARS.union(FORBIDEN_CHARS)) != 0:
            # password contains chars not in ALLOWED_CHARS or FORBIDEN_CHARS
            unknown_chars = password_set - ALLOWED_CHARS.union(FORBIDEN_CHARS)
            _LOGGER.warning(
                "Reolink password contains untested special character: %s, this could cause problems, please make an issue at https://github.com/starkillerOG/reolink_aio",
                ", ".join(unknown_chars),
            )
            return False

        if len(password_set - ALLOWED_CHARS) != 0:
            _LOGGER.warning(
                "Reolink password contains incompatible special character, please change the password to only contain characters: a-z, A-Z, 0-9 or %s", ALLOWED_SPECIAL_CHARS
            )
            return False

        return True

    def chime(self, dev_id) -> Chime | None:
        return self._chime_list.get(dev_id)

    def hdd_storage(self, index) -> float:
        """Return the amount of storage used in %."""
        if index >= len(self._hdd_info):
            return 0

        return round(100 * (1 - self._hdd_info[index].get("size", 1) / self._hdd_info[index].get("capacity", 1)), 2)

    def hdd_type(self, index) -> str:
        """Return the storage type, 'SD', 'HDD' or 'unknown'."""
        if index >= len(self._hdd_info):
            return "unknown"

        hdd_type = self._hdd_info[index].get("storageType", 2)
        if hdd_type == 1:
            return "HDD"
        if hdd_type == 2:
            return "SD"

        return "unknown"

    def hdd_available(self, index) -> bool:
        if index >= len(self._hdd_info):
            return False

        return self._hdd_info[index].get("format") == 1 and self._hdd_info[index].get("mount") == 1

    def timezone(self) -> Optional[tzinfo]:
        """Get the timezone of the device

        Returns None if there is no current time information
        """
        if self._time_settings is None:
            return None
        return typings.Reolink_timezone(self._time_settings)

    def time(self) -> Optional[datetime]:
        """Get the approximate "current" time of the device using existing data.

        Returns None if there is no current time information.

        When None is returned async_get_time can be used to request the current camera time
        """
        if self._time_settings is None:
            return None
        # the _host_time_difference is basically the diff in "localtime" between the system and the device
        # so we will add that then set the tzinfo to our timezone rule so the resulting time
        # can be converted to other timezones correctly
        return (datetime.now() + timedelta(seconds=self._host_time_difference)).replace(tzinfo=self.timezone())

    async def async_get_time(self) -> datetime:
        """Get the current time of the device

        The preferred method is to check get_time first, and if it returns none; call this, to save
        an async round trip to the device.
        """
        await self.get_state("GetTime")
        if self._time_settings is None:
            raise NotSupportedError(f"get_time: failed to retrieve current time settings from {self._host}:{self._port}")
        return reolink_time_to_datetime(self._time_settings["Time"]).replace(tzinfo=self.timezone())

    def _hide_password(self, content: str | bytes | dict | list) -> str:
        redacted = str(content)
        if self._password:
            redacted = redacted.replace(self._password, "<password>")
        if self._enc_password:
            redacted = redacted.replace(self._enc_password, "<password>")
        if self._token:
            redacted = redacted.replace(self._token, "<token>")
        if self._nvr_uid:
            redacted = redacted.replace(self._nvr_uid, "<uid>")
        return redacted

    ##############################################################################
    # Channel-level getters/setters

    def camera_name(self, channel: int | None) -> str:
        if channel is None:
            return self.nvr_name

        if not self.is_nvr and channel not in self._channel_names and channel in self._stream_channels and channel != 0:
            return self.camera_name(0)  # Dual lens cameras
        if channel not in self._channel_names:
            if not self.is_nvr:
                return self.nvr_name
            return "Unknown"
        return self._channel_names[channel]

    def camera_uid(self, channel: int | None) -> str:
        if channel is None:
            return self.uid
        if not self.is_nvr and channel not in self._channel_uids and channel in self._stream_channels and channel != 0 and self.camera_uid(0) != "Unknown":
            return f"{self.camera_uid(0)}_{channel}"  # Dual lens cameras
        if channel not in self._channel_uids:
            return "Unknown"
        return self._channel_uids[channel]

    def channel_for_uid(self, uid: str) -> int:
        """Returns the channel belonging to a UID"""
        channel = -1
        for ch, ch_uid in self._channel_uids.items():
            if ch_uid.startswith(uid):
                channel = ch
                break
        return channel

    def camera_online(self, channel: int) -> bool:
        if not self.is_nvr or not self._GetChannelStatus_present:
            return True
        if channel not in self._channel_online:
            return False
        return self._channel_online[channel]

    def camera_model(self, channel: int | None) -> str:
        if channel is None:
            return self.model
        if not self.is_nvr and channel not in self._channel_models and channel in self._stream_channels and channel != 0:
            return self.camera_model(0)  # Dual lens cameras
        if channel not in self._channel_models:
            return "Unknown"
        return self._channel_models[channel]

    def camera_hardware_version(self, channel: int | None) -> str:
        if channel is None:
            return self.hardware_version
        if not self.is_nvr and channel not in self._channel_hw_version and channel in self._stream_channels and channel != 0:
            return self.camera_hardware_version(0)  # Dual lens cameras
        if channel not in self._channel_hw_version:
            if not self.is_nvr:
                return self.hardware_version
            return "Unknown"
        return self._channel_hw_version[channel]

    def camera_sw_version(self, channel: int | None) -> str:
        if not self.is_nvr or channel is None:
            return self.sw_version
        if channel not in self._channel_sw_versions:
            return "Unknown"
        return self._channel_sw_versions[channel]

    def camera_sw_version_object(self, channel: int | None) -> SoftwareVersion:
        if not self.is_nvr or channel is None:
            return self.sw_version_object
        if channel not in self._channel_sw_version_objects:
            return SoftwareVersion(None)
        return self._channel_sw_version_objects[channel]

    def camera_sw_version_required(self, channel: int | None) -> SoftwareVersion:
        """Return the minimum required firmware version for a connected IPC camera for proper operation of this library"""
        if self.camera_model(channel) == "Unknown" or self.camera_hardware_version(channel) == "Unknown":
            return SoftwareVersion(None)

        return SoftwareVersion(MINIMUM_FIRMWARE.get(self.camera_model(channel), {}).get(self.camera_hardware_version(channel)))

    def camera_sw_version_update_required(self, channel: int | None) -> bool:
        """Check if a firmware version update is required for a connected IPC camera for proper operation of this library"""
        if self.camera_sw_version_object(channel) == SoftwareVersion(None):
            return False

        return self.camera_sw_version_object(channel) < self.camera_sw_version_required(channel)

    def is_doorbell(self, channel: int) -> bool:
        """Wether or not the camera is a doorbell"""
        return channel in self._is_doorbell and self._is_doorbell[channel]

    def motion_detected(self, channel: int) -> bool:
        """Return the motion detection state (polled)."""
        return channel in self._motion_detection_states and self._motion_detection_states[channel]

    def ai_detected(self, channel: int, object_type: str) -> bool:
        """Return the AI object detection state (polled)."""
        if channel not in self._ai_detection_states or self._ai_detection_states[channel] is None:
            return False

        for key, value in self._ai_detection_states[channel].items():
            if key == object_type or (object_type == PERSON_DETECTION_TYPE and key == "people") or (object_type == PET_DETECTION_TYPE and key == "dog_cat"):
                return value

        return False

    def ai_detection_states(self, channel: int) -> dict[str, bool]:
        """Return all the AI object detection state."""
        return self._ai_detection_states[channel]

    def visitor_detected(self, channel: int) -> bool:
        """Return the visitor detection state (polled)."""
        return channel in self._visitor_states and self._visitor_states[channel]

    def ai_supported(self, channel: int, object_type: Optional[str] = None) -> bool:
        """Return if the AI object type detection is supported or not."""
        if channel not in self._ai_detection_support or not self._ai_detection_support[channel]:
            return False

        if object_type is not None:
            for key, value in self._ai_detection_support[channel].items():
                if key == object_type or (object_type == PERSON_DETECTION_TYPE and key == "people") or (object_type == PET_DETECTION_TYPE and key == "dog_cat"):
                    return value
            return False

        return True

    def ai_supported_types(self, channel: int) -> list[str]:
        """Return a list of supported AI types."""
        if channel not in self._ai_detection_support:
            return []

        ai_types = []
        for key, value in self._ai_detection_support[channel].items():
            if value and key != "other":
                ai_types.append(key)

        return ai_types

    def audio_alarm_enabled(self, channel: int) -> bool:
        if channel not in self._audio_alarm_settings:
            return False

        if self.api_version("GetAudioAlarm") >= 1:
            return self._audio_alarm_settings[channel]["Audio"]["enable"] == 1

        return self._audio_alarm_settings[channel]["Audio"]["schedule"]["enable"] == 1

    def ir_enabled(self, channel: int) -> bool:
        return channel in self._ir_settings and self._ir_settings[channel]["IrLights"]["state"] == "Auto"

    def status_led_enabled(self, channel: int) -> bool:
        if channel not in self._status_led_settings:
            return False

        return self._status_led_settings[channel]["PowerLed"].get("state", "Off") == "On"

    def doorbell_led(self, channel: int) -> str:
        if channel not in self._status_led_settings:
            return "Off"

        return self._status_led_settings[channel]["PowerLed"].get("eDoorbellLightState", "Off")

    def doorbell_led_list(self, channel: int) -> list[str]:
        mode_values = []
        if self.api_version("supportDoorbellLightKeepOff", channel) > 0:
            mode_values.append(StatusLedEnum.stayoff)
        mode_values.extend([StatusLedEnum.auto, StatusLedEnum.alwaysonatnight])
        if self.api_version("supportDoorbellLightKeepOn", channel) > 0:
            mode_values.append(StatusLedEnum.alwayson)

        return [val.name for val in mode_values]

    def ftp_enabled(self, channel: int | None = None) -> bool:
        if channel is None:
            if self.api_version("GetFtp") >= 1:
                return all(self._ftp_settings[ch]["Ftp"]["enable"] == 1 for ch in self._channels if ch in self._ftp_settings)

            return all(self._ftp_settings[ch]["Ftp"]["schedule"]["enable"] == 1 for ch in self._channels if ch in self._ftp_settings)

        if channel not in self._ftp_settings:
            return False

        if self.api_version("GetFtp") >= 1:
            return self._ftp_settings[channel]["Ftp"]["scheduleEnable"] == 1

        return self._ftp_settings[channel]["Ftp"]["schedule"]["enable"] == 1

    def email_enabled(self, channel: int | None = None) -> bool:
        if channel is None:
            if self.api_version("GetEmail") >= 1:
                return all(self._email_settings[ch]["Email"]["enable"] == 1 for ch in self._channels if ch in self._email_settings)

            return all(self._email_settings[ch]["Email"]["schedule"]["enable"] == 1 for ch in self._channels if ch in self._email_settings)

        if channel not in self._email_settings:
            return False

        if self.api_version("GetEmail") >= 1:
            return self._email_settings[channel]["Email"]["scheduleEnable"] == 1

        return self._email_settings[channel]["Email"]["schedule"]["enable"] == 1

    def push_enabled(self, channel: int | None = None) -> bool:
        if channel is None:
            if self.supported(None, "push_config"):
                return self._push_config["PushCfg"]["enable"] == 1

            if self.api_version("GetPush") >= 1:
                return all(self._push_settings[ch]["Push"]["enable"] == 1 for ch in self._channels if ch in self._push_settings)

            return all(self._push_settings[ch]["Push"]["schedule"]["enable"] == 1 for ch in self._channels if ch in self._push_settings)

        if channel not in self._push_settings:
            return False

        if self.api_version("GetPush") >= 1:
            return self._push_settings[channel]["Push"]["scheduleEnable"] == 1

        return self._push_settings[channel]["Push"]["schedule"]["enable"] == 1

    def recording_enabled(self, channel: int | None = None) -> bool:
        if channel is None:
            if self.api_version("GetRec") >= 1:
                return all(self._recording_settings[ch]["Rec"]["enable"] == 1 for ch in self._channels if ch in self._recording_settings)

            return all(self._recording_settings[ch]["Rec"]["schedule"]["enable"] == 1 for ch in self._channels if ch in self._recording_settings)

        if channel not in self._recording_settings:
            return False

        if self.api_version("GetRec") >= 1:
            return self._recording_settings[channel]["Rec"]["scheduleEnable"] == 1

        return self._recording_settings[channel]["Rec"]["schedule"]["enable"] == 1

    def manual_record_enabled(self, channel: int) -> bool:
        if channel not in self._manual_record_settings:
            return False

        return self._manual_record_settings[channel]["Rec"]["enable"] == 1

    def buzzer_enabled(self, channel: int | None = None) -> bool:
        if channel is None:
            return all(self._buzzer_settings[ch]["Buzzer"]["enable"] == 1 for ch in self._channels if ch in self._buzzer_settings)

        if channel not in self._buzzer_settings:
            return False

        return self._buzzer_settings[channel]["Buzzer"]["scheduleEnable"] == 1

    def whiteled_state(self, channel: int) -> bool:
        return channel in self._whiteled_settings and self._whiteled_settings[channel]["WhiteLed"]["state"] == 1

    def whiteled_mode(self, channel: int) -> Optional[int]:
        if channel not in self._whiteled_settings:
            return None

        return self._whiteled_settings[channel]["WhiteLed"].get("mode")

    def whiteled_mode_list(self, channel: int) -> list[str]:
        mode_values = [SpotlightModeEnum.off]
        if self.api_version("supportFLIntelligent", channel) > 0:
            mode_values.extend([SpotlightModeEnum.auto])
        if self.api_version("supportFLSchedule", channel) > 0:
            mode_values.extend([SpotlightModeEnum.schedule])
        if self.api_version("supportFLKeepOn", channel) > 0:
            mode_values.extend([SpotlightModeEnum.onatnight])
        if self.api_version("supportLightAutoBrightness", channel) > 0:
            mode_values.extend([SpotlightModeEnum.adaptive, SpotlightModeEnum.autoadaptive])
        return [val.name for val in mode_values]

    def whiteled_brightness(self, channel: int) -> Optional[int]:
        if channel not in self._whiteled_settings:
            return None

        return self._whiteled_settings[channel]["WhiteLed"].get("bright")

    def whiteled_schedule(self, channel: int) -> Optional[dict]:
        """Return the spotlight state."""
        if channel in self._whiteled_settings:
            return self._whiteled_settings[channel]["WhiteLed"]["LightingSchedule"]

        return None

    def whiteled_settings(self, channel: int) -> Optional[dict]:
        """Return the spotlight state."""
        if channel in self._whiteled_settings:
            return self._whiteled_settings[channel]

        return None

    def battery_percentage(self, channel: int) -> Optional[int]:
        if channel not in self._battery:
            return None

        return self._battery[channel]["batteryPercent"]

    def battery_temperature(self, channel: int) -> Optional[int]:
        if channel not in self._battery:
            return None

        return self._battery[channel]["temperature"]

    def battery_status(self, channel: int) -> int:
        if channel not in self._battery:
            return BatteryEnum.discharging.value

        return self._battery[channel]["chargeStatus"]

    def sleeping(self, channel: int) -> bool:
        if channel not in self._sleep:
            return False

        return self._sleep[channel]

    def daynight_state(self, channel: int) -> Optional[str]:
        if channel not in self._isp_settings:
            return None

        return self._isp_settings[channel]["Isp"]["dayNight"]

    def HDR_on(self, channel: int) -> bool | None:
        if channel not in self._isp_settings:
            return None

        hdr = self._isp_settings[channel]["Isp"].get("hdr")
        if hdr is None:
            return None

        return hdr > 0

    def HDR_state(self, channel: int) -> int:
        if channel not in self._isp_settings:
            return -1

        return self._isp_settings[channel]["Isp"].get("hdr", -1)

    def daynight_threshold(self, channel: int) -> int | None:
        if channel not in self._isp_settings:
            return None

        return self._isp_settings[channel]["Isp"].get("dayNightThreshold")

    def backlight_state(self, channel: int) -> Optional[str]:
        if channel not in self._isp_settings:
            return None

        return self._isp_settings[channel]["Isp"]["backLight"]

    def image_brightness(self, channel: int) -> int | None:
        if channel not in self._image_settings:
            return None

        return self._image_settings[channel]["Image"].get("bright")

    def image_contrast(self, channel: int) -> int | None:
        if channel not in self._image_settings:
            return None

        return self._image_settings[channel]["Image"].get("contrast")

    def image_saturation(self, channel: int) -> int | None:
        if channel not in self._image_settings:
            return None

        return self._image_settings[channel]["Image"].get("saturation")

    def image_sharpness(self, channel: int) -> int | None:
        if channel not in self._image_settings:
            return None

        return self._image_settings[channel]["Image"].get("sharpen")

    def image_hue(self, channel: int) -> int | None:
        if channel not in self._image_settings:
            return None

        return self._image_settings[channel]["Image"].get("hue")

    def audio_record(self, channel: int) -> bool:
        if channel not in self._enc_settings:
            return False

        return self._enc_settings[channel]["Enc"]["audio"] == 1

    def volume(self, channel: int) -> int:
        if channel not in self._audio_settings:
            return 100

        return self._audio_settings[channel]["AudioCfg"]["volume"]

    def doorbell_button_sound(self, channel: int) -> bool:
        if channel not in self._audio_settings:
            return False

        return self._audio_settings[channel]["AudioCfg"].get("visitorLoudspeaker") == 1

    def hub_alarm_tone_id(self, channel: int) -> int:
        """Ringtone id played by the hub for a alarm event"""
        if channel not in self._hub_audio_settings:
            return 0
        return self._hub_audio_settings[channel]["AudioCfg"]["alarmRingToneId"]

    def hub_visitor_tone_id(self, channel: int) -> int:
        """Ringtone id played by the hub for a visitor event"""
        if channel not in self._hub_audio_settings:
            return 0
        return self._hub_audio_settings[channel]["AudioCfg"]["ringToneId"]

    def quick_reply_dict(self, channel: int) -> dict[int, str]:
        audio_dict = {-1: "off"}
        if channel not in self._audio_file_list:
            return audio_dict

        if self._audio_file_list[channel]["AudioFileList"] is None:
            return audio_dict

        for audio_file in self._audio_file_list[channel]["AudioFileList"]:
            audio_dict[audio_file["id"]] = audio_file["fileName"]
        return audio_dict

    def quick_reply_enabled(self, channel: int) -> bool:
        if channel not in self._auto_reply_settings:
            return False

        return self._auto_reply_settings[channel]["AutoReply"]["enable"] == 1

    def quick_reply_file(self, channel: int) -> int:
        """Return the quick replay audio file id, -1 means quick replay is off."""
        if channel not in self._auto_reply_settings:
            return -1

        return self._auto_reply_settings[channel]["AutoReply"]["fileId"]

    def quick_reply_time(self, channel: int) -> int:
        if channel not in self._auto_reply_settings:
            return 0

        return self._auto_reply_settings[channel]["AutoReply"]["timeout"]

    def audio_alarm_settings(self, channel: int) -> dict:
        if channel in self._audio_alarm_settings:
            return self._audio_alarm_settings[channel]

        return {}

    def pir_enabled(self, channel: int) -> bool | None:
        if channel not in self._pir:
            return None

        return self._pir[channel]["enable"] > 0

    def pir_reduce_alarm(self, channel: int) -> bool | None:
        if channel not in self._pir:
            return None

        return self._pir[channel]["reduceAlarm"] > 0

    def pir_sensitivity(self, channel: int) -> int:
        if channel not in self._pir:
            return 0

        return 101 - self._pir[channel]["sensitive"]

    def md_sensitivity(self, channel: int) -> int:
        if channel not in self._md_alarm_settings:
            return 0

        if self.api_version("GetMdAlarm") >= 1:
            if self._md_alarm_settings[channel]["MdAlarm"].get("useNewSens", 0) == 1:
                return 51 - self._md_alarm_settings[channel]["MdAlarm"]["newSens"]["sensDef"]

            sensitivities = [sens["sensitivity"] for sens in self._md_alarm_settings[channel]["MdAlarm"]["sens"]]
            return 51 - mean(sensitivities)

        sensitivities = [sens["sensitivity"] for sens in self._md_alarm_settings[channel]["Alarm"]["sens"]]
        return 51 - mean(sensitivities)

    def ai_sensitivity(self, channel: int, ai_type: str) -> int:
        if channel not in self._ai_alarm_settings or ai_type not in self._ai_alarm_settings[channel]:
            return 0

        return self._ai_alarm_settings[channel][ai_type]["sensitivity"]

    def ai_delay(self, channel: int, ai_type: str) -> int:
        """AI detection delay time in seconds"""
        if channel not in self._ai_alarm_settings or ai_type not in self._ai_alarm_settings[channel]:
            return 0

        return self._ai_alarm_settings[channel][ai_type]["stay_time"]

    def zoom_range(self, channel: int) -> dict:
        return self._zoom_focus_range[channel]

    def enable_https(self, enable: bool):
        self._use_https = enable
        self.refresh_base_url()

    def refresh_base_url(self):
        if self._use_https:
            self._url = f"https://{self._host}:{self._port}/cgi-bin/api.cgi"
        else:
            self._url = f"http://{self._host}:{self._port}/cgi-bin/api.cgi"

    async def login(self) -> None:
        if self._port is None or self._use_https is None:
            await self._login_try_ports()
            return  # succes

        if self._token is not None and self._lease_time is not None and self._lease_time > (datetime.now() + timedelta(seconds=300)):
            return  # succes

        await self._login_mutex.acquire()
        try:
            if self._token is not None and self._lease_time is not None and self._lease_time > (datetime.now() + timedelta(seconds=300)):
                _LOGGER.debug(
                    "Host %s:%s, after login mutex aquired, login already completed by another coroutine",
                    self._host,
                    self._port,
                )
                return  # succes, in case multiple async coroutine are waiting on login_mutex.acquire

            await self.logout(login_mutex_owned=True)  # Ensure there would be no "max session" error

            _LOGGER.debug(
                "Host %s:%s, trying to login with user %s...",
                self._host,
                self._port,
                self._username,
            )

            body: typings.reolink_json = [
                {
                    "cmd": "Login",
                    "action": 0,
                    "param": {
                        "User": {
                            "userName": self._username,
                            "password": self._password,
                        }
                    },
                }
            ]
            param = {"cmd": "Login"}

            try:
                json_data = await self.send(body, param, expected_response_type="json")
            except ApiError as err:
                raise LoginError(f"API error during login of host {self._host}:{self._port}: {str(err)}") from err
            except ReolinkConnectionError as err:
                raise LoginError(f"Client connector error during login of host {self._host}:{self._port}: {str(err)}") from err
            except InvalidContentTypeError as err:
                raise LoginError(f"Invalid content error during login of host {self._host}:{self._port}: {str(err)}") from err
            except NoDataError as err:
                raise LoginError(f"Error receiving Reolink login response of host {self._host}:{self._port}") from err

            _LOGGER.debug("Got login response from %s:%s: %s", self._host, self._port, json_data)

            try:
                if json_data[0]["code"] != 0:
                    raise LoginError(f"API returned error code {json_data[0]['code']} during login of host {self._host}:{self._port}")

                self._lease_time = datetime.now() + timedelta(seconds=float(json_data[0]["value"]["Token"]["leaseTime"]))
                self._token = str(json_data[0]["value"]["Token"]["name"])
            except Exception as err:
                self.clear_token()
                raise LoginError(f"Login error, unknown response format from host {self._host}:{self._port}: {json_data}") from err

            _LOGGER.debug(
                "Logged in at host %s:%s. Leasetime %s, token %s",
                self._host,
                self._port,
                self._lease_time.strftime("%d-%m-%Y %H:%M"),
                self._token,
            )
            # Looks like some devices fail with not-logged-in if subsequent command sent with no delay, not sure 100% though...
            # I've seen RLC-520A failed with 0.5s, but did not try to set more. Need to gather some more logging data from users...
            # asyncio.sleep(0.5)
            return  # succes
        finally:
            self._login_mutex.release()

    async def _login_try_ports(self) -> None:
        # try HTTPs port
        first_exc = None
        self._port = 443
        self.enable_https(True)
        try:
            await self.login()
            return
        except LoginError as exc:
            first_exc = exc

        # try HTTP port
        self._port = 80
        self.enable_https(False)
        try:
            await self.login()
            return
        except LoginError:
            pass

        # see which ports are enabled using baichuan protocol on port 9000
        try:
            await self.baichuan.get_ports()
        except ReolinkError as exc:
            _LOGGER.debug(exc)
            # Raise original exception instead of the retry fallback exception
            raise first_exc from exc

        if self.baichuan.https_enabled is None and self.baichuan.http_enabled is None:
            raise LoginError(
                f"Reolink device '{self._host}' does not have a HTTPs API, "
                "a Reolink Home Hub or NVR is required to use this device with reolink_aio, "
                "connect this device to the Reolink Home Hub/NVR and connect to the Hub/NVR instead to access this device"
            )

        # open the HTTPs, RTSP and ONVIF ports if needed
        if (not self.baichuan.https_enabled and not self.baichuan.http_enabled) or not self.baichuan.rtsp_enabled or not self.baichuan.onvif_enabled:
            try:
                if not self.baichuan.https_enabled and not self.baichuan.http_enabled:
                    await self.baichuan.set_port_enabled(PortType.https, True)
                if not self.baichuan.rtsp_enabled:
                    await self.baichuan.set_port_enabled(PortType.rtsp, True)
                if not self.baichuan.onvif_enabled:
                    await self.baichuan.set_port_enabled(PortType.onvif, True)
                await self.baichuan.get_ports()
            except ReolinkError as exc:
                # Raise original exception instead of the retry fallback exception
                raise first_exc from exc

            # give the camera some time to startup the HTTP API server
            await asyncio.sleep(5)

        if self.baichuan.https_enabled and self.baichuan.https_port is not None:
            self._port = self.baichuan.https_port
            self.enable_https(True)
        elif self.baichuan.http_enabled and self.baichuan.http_port is not None:
            self._port = self.baichuan.http_port
            self.enable_https(False)
        else:
            raise LoginError(f"Failed to open HTTPs port on host '{self._host}' using baichuan, altough no errors returned by API") from first_exc

        # check the firmware version
        try:
            await self.baichuan.get_info()
        except ReolinkError:
            pass

        # update info such that minimum firmware can be assest
        self._nvr_model = self.baichuan.model()
        self._nvr_hw_version = self.baichuan.hardware_version()
        self._nvr_item_number = self.baichuan.item_number()
        self._nvr_sw_version = self.baichuan.sw_version()
        if self.baichuan.sw_version() is not None:
            self._nvr_sw_version_object = SoftwareVersion(self.baichuan.sw_version())

        # retry login now that the port is open, this will also logout the baichuan session
        try:
            await self.login()
            return
        except LoginError as exc:
            if (self.baichuan.rtmp_enabled or self.baichuan.rtmp_enabled is None) and not self.sw_version_update_required:
                raise LoginError(f"Failed to login after opening HTTPs port on host '{self._host}' using baichuan protocol: {exc}") from exc

        # open the RTMP port
        if not self.baichuan.rtmp_enabled and self.baichuan.rtmp_enabled is not None:
            try:
                await self.baichuan.set_port_enabled(PortType.rtmp, True)
                await self.baichuan.get_ports()
            except ReolinkError as exc:
                # Raise original exception instead of the retry fallback exception
                raise first_exc from exc

            # give the camera some time to startup the HTTP API server
            await asyncio.sleep(5)

            # retry login now that the RTMP port is also open
            try:
                await self.login()
                return
            except LoginError as exc:
                if not self.sw_version_update_required:
                    raise LoginError(f"Failed to login after opening HTTPs and RTMP port on host '{self._host}' using baichuan protocol: {exc}") from exc

        raise LoginFirmwareError(
            f"Failed to login to host '{self._host}', "
            f"please update the firmware to version {self.sw_version_required.version_string} "
            "using the Reolink Download Center: https://reolink.com/download-center, "
            f"currently version {self.sw_version} is installed"
        )

    async def _login_open_port(self) -> None:
        if self._port is None or self._use_https is None:
            await self._login_try_ports()
            return

        first_exc = None
        try:
            await self.login()
            return
        except LoginError as exc:
            first_exc = exc

        # see which ports are enabled using baichuan protocol on port 9000
        try:
            await self.baichuan.get_ports()
        except ReolinkError as exc:
            _LOGGER.debug(exc)
            # Raise original exception instead of the retry fallback exception
            raise first_exc from exc

        _LOGGER.warning("HTTP(s) login failed while Baichuan login succeeded, re-opening HTTP(s) port and looking up correct port on host %s", self._host)

        # open the HTTP(s) port
        if not self.baichuan.https_enabled and not self.baichuan.http_enabled:
            try:
                if self._use_https or self._use_https is None:
                    await self.baichuan.set_port_enabled(PortType.https, True)
                else:
                    await self.baichuan.set_port_enabled(PortType.http, True)
                await self.baichuan.get_ports()
            except ReolinkError as exc:
                # Raise original exception instead of the retry fallback exception
                raise first_exc from exc

            # give the camera some time to startup the HTTP API server
            await asyncio.sleep(5)

        # select preferred port
        if self._use_https and self.baichuan.https_enabled and self.baichuan.https_port is not None:
            self._port = self.baichuan.https_port
            self.enable_https(True)
        elif not self._use_https and self.baichuan.http_enabled and self.baichuan.http_port is not None:
            self._port = self.baichuan.http_port
            self.enable_https(False)
        elif self.baichuan.https_enabled and self.baichuan.https_port is not None:
            self._port = self.baichuan.https_port
            self.enable_https(True)
        elif self.baichuan.http_enabled and self.baichuan.http_port is not None:
            self._port = self.baichuan.http_port
            self.enable_https(False)
        else:
            raise LoginError(f"Failed to open HTTP(s) port on host '{self._host}' using baichuan, altough no errors returned by API") from first_exc

        # retry login now that the port is open, this will also logout the baichuan session
        try:
            await self.login()
            return
        except LoginError as exc:
            if self.baichuan.rtmp_enabled or self.baichuan.rtmp_enabled is None:
                raise LoginError(f"Failed to login after opening HTTPs port on host '{self._host}' using baichuan protocol: {exc}") from exc

        # open the RTMP port
        try:
            await self.baichuan.set_port_enabled(PortType.rtmp, True)
            await self.baichuan.get_ports()
            # give the camera some time to startup the HTTP API server
            await asyncio.sleep(5)
        except ReolinkError as exc:
            # Raise original exception instead of the retry fallback exception
            raise first_exc from exc

        # retry login now that the RTMP port is also open
        try:
            await self.login()
            return
        except LoginError as exc:
            raise LoginError(f"Failed to login after opening HTTPs and RTMP port on host '{self._host}' using baichuan protocol: {exc}") from exc

    async def logout(self, login_mutex_owned=False):
        body = [{"cmd": "Logout", "action": 0, "param": {}}]

        if not login_mutex_owned:
            await self._login_mutex.acquire()

        try:
            if self._token:
                param = {"cmd": "Logout"}
                try:
                    # logout sometimes responds with a string of seemingly random caracters, which are always the same for a given camera.
                    await self.send(body, param, expected_response_type="text/html")
                except ReolinkError as err:
                    _LOGGER.warning("Error while logging out: %s", str(err))
            # Reolink has a bug in some cameras' firmware: the Logout command issued without a token breaks the subsequent commands:
            # even if Login command issued AFTER that successfully returns a token, any command with that token would return "Please login first" error.
            # Thus it is not available for now to exit the previous "stuck" sessions after sudden crash or power failure:
            # Reolink has restricted amount of sessions on a device, so in such case the component would not be able to login
            # into a device before some previos session expires an hour later...
            # If Reolink fixes this and makes Logout work with login/pass pair instead of a token - this can be uncommented...
            # else:
            #     body  = [{"cmd": "Logout", "action": 0, "param": {"User": {"userName": self._username, "password": self._password}}}]
            #     param = {"cmd": "Logout"}
            #     await self.send(body, param, expected_response_type = "text/html")

            self.clear_token()
            if not login_mutex_owned and self._aiohttp_session_internall:
                await self._aiohttp_session.close()
        finally:
            if not login_mutex_owned:
                self._login_mutex.release()

        await self.baichuan.logout()

    async def expire_session(self, unsubscribe: bool = True):
        if self._lease_time is not None:
            self._lease_time = datetime.now() - timedelta(seconds=5)
        if unsubscribe:
            await self.unsubscribe()
        if self._aiohttp_session_internall:
            await self._aiohttp_session.close()

    def clear_token(self):
        self._token = None
        self._lease_time = None

    @property
    def capabilities(self) -> dict[int | str, set[str]]:
        return self._capabilities

    @property
    def checked_api_versions(self) -> dict[str, int]:
        return self._api_version

    @property
    def abilities(self) -> dict[str, Any]:
        return self._abilities

    def construct_capabilities(self, warnings=True) -> None:
        """Construct the capabilities list of the NVR/camera."""
        # Host capabilities
        self._capabilities["Host"] = set()

        if self.api_version("onvif") > 0 and self._onvif_port is not None:
            self._capabilities["Host"].add("ONVIF")
        if self.api_version("rtsp") > 0 and self._rtsp_port is not None:
            self._capabilities["Host"].add("RTSP")
        if self.api_version("rtmp") > 0 and self._rtmp_port is not None:
            self._capabilities["Host"].add("RTMP")

        if self._nvr_uid is not None:
            self._capabilities["Host"].add("UID")

        if self.sw_version_object.date > datetime(year=2021, month=6, day=1):
            # Check if this camera publishes its inital state upon ONVIF subscription
            self._capabilities["Host"].add("initial_ONVIF_state")

        if self._ftp_settings and not self._is_hub:
            self._capabilities["Host"].add("ftp")

        if self._push_settings and not self._is_hub:
            self._capabilities["Host"].add("push")

        if self._push_config.get("PushCfg", {}).get("enable") is not None:
            self._capabilities["Host"].add("push_config")

        if self._recording_settings and not self._is_hub:
            self._capabilities["Host"].add("recording")

        if self._email_settings and not self._is_hub:
            self._capabilities["Host"].add("email")

        if self.api_version("supportBuzzer") > 0 and not self._is_hub:
            self._capabilities["Host"].add("buzzer")

        self._capabilities["Host"].add("firmware")
        if self.api_version("upgrade") >= 2:
            self._capabilities["Host"].add("update")

        if self.api_version("wifi") > 0:
            self._capabilities["Host"].add("wifi")

        if self.api_version("performance") > 0 and self.cpu_usage is not None:
            self._capabilities["Host"].add("performance")

        if "enable" in self._state_light:
            self._capabilities["Host"].add("state_light")

        if self.api_version("GetDeviceAudioCfg") > 0:
            self._capabilities["Host"].add("hub_audio")

        if self.hdd_info:
            self._capabilities["Host"].add("hdd")

        if self.api_version("reboot") > 0:
            self._capabilities["Host"].add("reboot")

        # Stream capabilities
        for channel in self._stream_channels:
            self._capabilities[channel] = set()

            if self.api_version("recReplay", channel) > 0:
                self._capabilities[channel].add("replay")

        # Channel capabilities
        for channel in self._channels:
            self._capabilities.setdefault(channel, set())

            if self.camera_uid(channel) != "Unknown":
                self._capabilities[channel].add("UID")

            if self.is_nvr and self.api_version("supportAutoTrackStream", channel) > 0:
                self._capabilities[channel].add("autotrack_stream")

            if channel in self._motion_detection_states:
                self._capabilities[channel].add("motion_detection")

            if self.api_version("supportAiAnimal", channel) and self.ai_supported(channel, PET_DETECTION_TYPE):
                self._capabilities[channel].add("ai_animal")

            if channel > 0 and self.model in DUAL_LENS_DUAL_MOTION_MODELS:
                continue

            if self.is_nvr and self.camera_hardware_version(channel) != "Unknown" and self.camera_model(channel) != "Unknown":
                self._capabilities[channel].add("firmware")

            if self.api_version("supportWebhook", channel) > 0:
                self._capabilities[channel].add("webhook")

            if channel in self._ftp_settings and (self.api_version("GetFtp") < 1 or "scheduleEnable" in self._ftp_settings[channel]["Ftp"]):
                self._capabilities[channel].add("ftp")

            if channel in self._push_settings and (self.api_version("GetPush") < 1 or "scheduleEnable" in self._push_settings[channel]["Push"]):
                self._capabilities[channel].add("push")

            if channel in self._recording_settings and (self.api_version("GetRec") < 1 or "scheduleEnable" in self._recording_settings[channel]["Rec"]):
                self._capabilities[channel].add("recording")

            if channel in self._manual_record_settings and "enable" in self._recording_settings[channel]["Rec"]:
                self._capabilities[channel].add("manual_record")

            if channel in self._email_settings and (self.api_version("GetEmail") < 1 or "scheduleEnable" in self._email_settings[channel]["Email"]):
                self._capabilities[channel].add("email")

            if channel in self._buzzer_settings and self.api_version("supportBuzzer") > 0 and "scheduleEnable" in self._buzzer_settings[channel]["Buzzer"]:
                self._capabilities[channel].add("buzzer")

            if self.api_version("ledControl", channel) > 0 and channel in self._ir_settings:
                self._capabilities[channel].add("ir_lights")

            if self.api_version("powerLed", channel) > 0 or self.api_version("indicatorLight", channel) > 0:
                # powerLed == statusLed = doorbell_led
                self._capabilities[channel].add("status_led")  # internal use only
                self._capabilities[channel].add("power_led")
            if self.api_version("supportDoorbellLight", channel) > 0 or self.is_doorbell(channel):
                # powerLed == statusLed = doorbell_led
                self._capabilities[channel].add("status_led")  # internal use only
                self._capabilities[channel].add("doorbell_led")

            if self.api_version("GetWhiteLed") > 0 and (
                self.api_version("floodLight", channel) > 0 or self.api_version("supportFLswitch", channel) > 0 or self.api_version("supportFLBrightness", channel) > 0
            ):
                # floodlight == spotlight == WhiteLed
                self._capabilities[channel].add("floodLight")

            if self.api_version("GetAudioCfg") > 0:
                self._capabilities[channel].add("volume")
                if self.api_version("supportVisitorLoudspeaker", channel) > 0:
                    self._capabilities[channel].add("doorbell_button_sound")

            if self.api_version("GetDeviceAudioCfg") > 0:
                self._capabilities[channel].add("hub_audio")

            if (self.api_version("supportAudioFileList", channel) > 0) or (not self.is_nvr and self.api_version("supportAudioFileList") > 0):
                if self.api_version("supportAutoReply", channel) > 0 or (not self.is_nvr and self.api_version("supportAutoReply") > 0):
                    self._capabilities[channel].add("quick_reply")
                if self.api_version("supportAudioPlay", channel) > 0 or self.api_version("supportQuickReplyPlay", channel) > 0:
                    self._capabilities[channel].add("play_quick_reply")

            if self.api_version("supportDingDongCtrl", channel) > 0 and self._GetDingDong_present.get(channel):
                self._capabilities[channel].add("chime")

            if (self.api_version("alarmAudio", channel) > 0 or self.api_version("supportAudioAlarm", channel) > 0) and channel in self._audio_alarm_settings:
                self._capabilities[channel].add("siren")
                self._capabilities[channel].add("siren_play")  # if self.api_version("supportAoAdjust", channel) > 0

            if self.audio_record(channel) is not None:
                self._capabilities[channel].add("audio")

            ptz_ver = self.api_version("ptzType", channel)
            if ptz_ver != 0:
                self._capabilities[channel].add("ptz")
                if ptz_ver in [1, 2, 5]:
                    self._capabilities[channel].add("zoom_basic")
                    min_zoom = self._zoom_focus_range.get(channel, {}).get("zoom", {}).get("pos", {}).get("min")
                    max_zoom = self._zoom_focus_range.get(channel, {}).get("zoom", {}).get("pos", {}).get("max")
                    if min_zoom is None or max_zoom is None:
                        if warnings:
                            _LOGGER.warning("Camera %s reported to support zoom, but zoom range not available", self.camera_name(channel))
                    else:
                        self._capabilities[channel].add("zoom")
                        self._capabilities[channel].add("focus")
                        if self.api_version("disableAutoFocus", channel) > 0:
                            self._capabilities[channel].add("auto_focus")
                if ptz_ver in [2, 3, 5]:
                    self._capabilities[channel].add("tilt")
                if ptz_ver in [2, 3, 5, 7]:
                    self._capabilities[channel].add("pan_tilt")
                    self._capabilities[channel].add("pan")
                    if self.api_version("supportPtzCalibration", channel) > 0 or self.api_version("supportPtzCheck", channel) > 0:
                        self._capabilities[channel].add("ptz_callibrate")
                    if self.api_version("GetPtzGuard", channel) > 0:
                        self._capabilities[channel].add("ptz_guard")
                    if self.api_version("GetPtzCurPos", channel) > 0:
                        self._capabilities[channel].add("ptz_position")
                        if self.ptz_pan_position(channel) is not None:
                            self._capabilities[channel].add("ptz_pan_position")
                        if self.ptz_tilt_position(channel) is not None:
                            self._capabilities[channel].add("ptz_tilt_position")
                if ptz_ver in [2, 3]:
                    self._capabilities[channel].add("ptz_speed")
                if channel in self._ptz_presets and len(self._ptz_presets[channel]) != 0:
                    self._capabilities[channel].add("ptz_presets")
                if channel in self._ptz_patrols and len(self._ptz_patrols[channel]) != 0:
                    self._capabilities[channel].add("ptz_patrol")

            if self.api_version("supportDigitalZoom", channel) > 0 and "zoom" not in self._capabilities[channel]:
                min_zoom = self._zoom_focus_range.get(channel, {}).get("zoom", {}).get("pos", {}).get("min")
                max_zoom = self._zoom_focus_range.get(channel, {}).get("zoom", {}).get("pos", {}).get("max")
                if min_zoom is not None and max_zoom is not None:
                    self._capabilities[channel].add("zoom")
                else:
                    if warnings:
                        _LOGGER.debug("Camera %s reported to support zoom, but zoom range not available", self.camera_name(channel))

            if self.api_version("aiTrack", channel) > 0:
                self._capabilities[channel].add("auto_track")
                track_method = self._auto_track_range.get(channel, {}).get("aiTrack", False)
                if isinstance(track_method, list):
                    if len(track_method) > 1 and sorted(track_method) != [0, 1]:
                        self._capabilities[channel].add("auto_track_method")
                if self.auto_track_disappear_time(channel) > 0:
                    self._capabilities[channel].add("auto_track_disappear_time")
                if self.auto_track_stop_time(channel) > 0:
                    self._capabilities[channel].add("auto_track_stop_time")

            if self.api_version("supportAITrackLimit", channel) > 0:
                self._capabilities[channel].add("auto_track_limit")

            if self.api_version("battery", channel) > 0:
                self._capabilities[channel].add("battery")
                if channel in self._sleep:
                    self._capabilities[channel].add("sleep")
                    if "sleep" not in self._capabilities["Host"]:
                        self._capabilities["Host"].add("sleep")
            if self.api_version("mdWithPir", channel) > 0:
                self._capabilities[channel].add("PIR")

            if channel in self._md_alarm_settings and not self.supported(channel, "PIR"):
                self._capabilities[channel].add("md_sensitivity")

            if self.api_version("supportAiSensitivity", channel) > 0:
                self._capabilities[channel].add("ai_sensitivity")

            if self.api_version("supportAiStayTime", channel) > 0:
                self._capabilities[channel].add("ai_delay")

            if self.api_version("ispHue", channel) > 0:
                self._capabilities[channel].add("isp_hue")
            if self.api_version("ispSatruation", channel) > 0:
                self._capabilities[channel].add("isp_satruation")
            if self.api_version("ispSharpen", channel) > 0:
                self._capabilities[channel].add("isp_sharpen")
            if self.api_version("ispContrast", channel) > 0:
                self._capabilities[channel].add("isp_contrast")
            if self.api_version("ispBright", channel) > 0:
                self._capabilities[channel].add("isp_bright")
            if self.api_version("supportIspHdr", channel, no_key_return=1) > 0 and self.HDR_state(channel) >= 0:
                self._capabilities[channel].add("HDR")

            if self.api_version("ispDayNight", channel, no_key_return=1) > 0 and self.daynight_state(channel) is not None:
                self._capabilities[channel].add("dayNight")
                if self.daynight_threshold(channel) is not None:
                    self._capabilities[channel].add("dayNightThreshold")

            if self.backlight_state(channel) is not None:
                self._capabilities[channel].add("backLight")

    def supported(self, channel: int | None, capability: str) -> bool:
        """Return if a capability is supported by a camera channel."""
        if channel is None:
            return capability in self._capabilities["Host"]

        if channel not in self._capabilities:
            return False

        return capability in self._capabilities[channel]

    def api_version(self, capability: str, channel: int | None = None, no_key_return: int = 0) -> int:
        """Return the api version of a capability, 0=not supported, >0 is supported"""
        if capability in self._api_version:
            return self._api_version[capability]

        if channel is None:
            return self._abilities.get(capability, {}).get("ver", 0)

        if channel >= len(self._abilities.get("abilityChn", [])):
            if channel not in self._channels and channel in self._stream_channels and len(self._abilities.get("abilityChn", [])) >= 1:
                channel = 0  # Dual lens camera
            else:
                return 0

        return self._abilities["abilityChn"][channel].get(capability, {}).get("ver", no_key_return)

    async def get_state(self, cmd: str) -> None:
        body = []
        channels = []
        chime_ids = []
        for channel in self._stream_channels:
            ch_body = []
            if cmd == "GetEnc":
                ch_body = [{"cmd": "GetEnc", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetRtspUrl":
                ch_body = [{"cmd": "GetRtspUrl", "action": 0, "param": {"channel": channel}}]
            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))
            chime_ids.extend([-1] * len(ch_body))

        for channel in self._channels:
            ch_body = []
            if cmd == "GetIsp":
                ch_body = [{"cmd": "GetIsp", "action": 0, "param": {"channel": channel}}]

            if channel > 0 and self.model in DUAL_LENS_DUAL_MOTION_MODELS:
                body.extend(ch_body)
                channels.extend([channel] * len(ch_body))
                chime_ids.extend([-1] * len(ch_body))
                continue

            if cmd == "GetIrLights" and self.supported(channel, "ir_lights"):
                ch_body = [{"cmd": "GetIrLights", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetPowerLed" and self.supported(channel, "status_led"):
                ch_body = [{"cmd": "GetPowerLed", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetWhiteLed" and self.supported(channel, "floodLight"):
                ch_body = [{"cmd": "GetWhiteLed", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetBatteryInfo" and self.supported(channel, "battery"):
                ch_body = [{"cmd": "GetBatteryInfo", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetPirInfo" and self.supported(channel, "PIR"):
                ch_body = [{"cmd": "GetPirInfo", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetWebHook" and self.supported(channel, "webhook"):
                ch_body = [{"cmd": "GetWebHook", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetPtzPreset" and self.supported(channel, "ptz_presets"):
                ch_body = [{"cmd": "GetPtzPreset", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetPtzPatrol" and self.supported(channel, "ptz_patrol"):
                ch_body = [{"cmd": "GetPtzPatrol", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetAutoFocus" and self.supported(channel, "auto_focus"):
                ch_body = [{"cmd": "GetAutoFocus", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetZoomFocus" and self.supported(channel, "zoom"):
                ch_body = [{"cmd": "GetZoomFocus", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetPtzGuard" and self.supported(channel, "ptz_guard"):
                ch_body = [{"cmd": "GetPtzGuard", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetPtzCurPos" and self.supported(channel, "ptz_position"):
                ch_body = [{"cmd": "GetPtzCurPos", "action": 0, "param": {"PtzCurPos": {"channel": channel}}}]
            elif cmd == "GetAiCfg" and self.supported(channel, "auto_track"):
                ch_body = [{"cmd": "GetAiCfg", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetPtzTraceSection" and self.supported(channel, "auto_track_limit"):
                ch_body = [{"cmd": "GetPtzTraceSection", "action": 0, "param": {"PtzTraceSection": {"channel": channel}}}]
            elif cmd == "GetAudioCfg" and self.supported(channel, "volume"):
                ch_body = [{"cmd": "GetAudioCfg", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetDeviceAudioCfg" and self.supported(channel, "hub_audio"):
                ch_body = [{"cmd": "GetDeviceAudioCfg", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetAudioFileList" and self.supported(channel, "quick_reply"):
                ch_body = [{"cmd": "GetAudioFileList", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetDingDongList" and self.supported(channel, "chime"):
                ch_body = [{"cmd": "GetDingDongList", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetDingDongCfg" and self.supported(channel, "chime"):
                ch_body = [{"cmd": "GetDingDongCfg", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetAutoReply" and self.supported(channel, "quick_reply"):
                ch_body = [{"cmd": "GetAutoReply", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetManualRec" and self.supported(channel, "manual_record"):
                ch_body.append({"cmd": "GetManualRec", "action": 0, "param": {"channel": channel}})
            elif cmd == "GetOsd":
                ch_body = [{"cmd": "GetOsd", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetImage" and (
                self.supported(channel, "isp_hue")
                or self.supported(channel, "isp_satruation")
                or self.supported(channel, "isp_sharpen")
                or self.supported(channel, "isp_contrast")
                or self.supported(channel, "isp_bright")
            ):
                ch_body = [{"cmd": "GetImage", "action": 0, "param": {"channel": channel}}]
            elif cmd == "GetBuzzerAlarmV20" and (self.supported(channel, "buzzer") or (self.supported(None, "buzzer") and channel == 0)):
                ch_body = [{"cmd": "GetBuzzerAlarmV20", "action": 0, "param": {"channel": channel}}]
            elif cmd in ["GetAlarm", "GetMdAlarm"] and self.supported(channel, "md_sensitivity"):
                if self.api_version("GetMdAlarm") >= 1:
                    ch_body = [{"cmd": "GetMdAlarm", "action": 0, "param": {"channel": channel}}]
                else:
                    ch_body = [{"cmd": "GetAlarm", "action": 0, "param": {"Alarm": {"channel": channel, "type": "md"}}}]
            elif cmd == "GetAiAlarm" and self.supported(channel, "ai_sensitivity"):
                ch_body = []
                for ai_type in self.ai_supported_types(channel):
                    ch_body.append({"cmd": "GetAiAlarm", "action": 0, "param": {"channel": channel, "ai_type": ai_type}})
            elif cmd in ["GetEmail", "GetEmailV20"] and (self.supported(channel, "email") or (self.supported(None, "email") and channel == 0)):
                if self.api_version("GetEmail") >= 1:
                    ch_body = [{"cmd": "GetEmailV20", "action": 0, "param": {"channel": channel}}]
                else:
                    ch_body = [{"cmd": "GetEmail", "action": 0, "param": {"channel": channel}}]
            elif cmd in ["GetPush", "GetPushV20"] and (self.supported(channel, "push") or (self.supported(None, "push") and channel == 0)):
                if self.api_version("GetPush") >= 1:
                    ch_body = [{"cmd": "GetPushV20", "action": 0, "param": {"channel": channel}}]
                else:
                    ch_body = [{"cmd": "GetPush", "action": 0, "param": {"channel": channel}}]
            elif cmd in ["GetFtp", "GetFtpV20"] and (self.supported(channel, "ftp") or (self.supported(None, "ftp") and channel == 0)):
                if self.api_version("GetFtp") >= 1:
                    ch_body = [{"cmd": "GetFtpV20", "action": 0, "param": {"channel": channel}}]
                else:
                    ch_body = [{"cmd": "GetFtp", "action": 0, "param": {"channel": channel}}]
            elif cmd in ["GetRec", "GetRecV20"] and (self.supported(channel, "recording") or (self.supported(None, "recording") and channel == 0)):
                if self.api_version("GetRec") >= 1:
                    ch_body = [{"cmd": "GetRecV20", "action": 0, "param": {"channel": channel}}]
                else:
                    ch_body = [{"cmd": "GetRec", "action": 0, "param": {"channel": channel}}]
            elif cmd in ["GetAudioAlarm", "GetAudioAlarmV20"] and self.supported(channel, "siren"):
                if self.api_version("GetAudioAlarm") >= 1:
                    ch_body = [{"cmd": "GetAudioAlarmV20", "action": 0, "param": {"channel": channel}}]
                else:
                    ch_body = [{"cmd": "GetAudioAlarm", "action": 0, "param": {"channel": channel}}]
            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))
            chime_ids.extend([-1] * len(ch_body))

        # chime states
        if not channels and cmd == "DingDongOpt":
            for chime_id, chime in self._chime_list.items():
                if not chime.online:
                    continue
                chime_ch = chime.channel
                body.append({"cmd": "DingDongOpt", "action": 0, "param": {"DingDong": {"channel": chime_ch, "option": 2, "id": chime_id}}})
                channels.append(chime_ch)
                chime_ids.append(chime_id)

        if not channels:
            if cmd == "GetChannelstatus":
                body = [{"cmd": "GetChannelstatus"}]
            elif cmd == "GetDevInfo":
                body = [{"cmd": "GetDevInfo", "action": 0, "param": {}}]
            elif cmd == "GetLocalLink":
                body = [{"cmd": "GetLocalLink", "action": 0, "param": {}}]
            elif cmd == "GetWifiSignal":
                body = [{"cmd": "GetWifiSignal", "action": 0, "param": {}}]
            elif cmd == "GetPerformance" and self.supported(None, "performance"):
                body = [{"cmd": "GetPerformance", "action": 0, "param": {}}]
            elif cmd == "GetStateLight" and self.supported(None, "state_light"):
                body = [{"cmd": "GetStateLight", "action": 0, "param": {}}]
            elif cmd == "GetNetPort":
                body = [{"cmd": "GetNetPort", "action": 0, "param": {}}]
            elif cmd == "GetHddInfo":
                body = [{"cmd": "GetHddInfo", "action": 0, "param": {}}]
            elif cmd == "GetUser":
                body = [{"cmd": "GetUser", "action": 0, "param": {}}]
            elif cmd == "GetNtp":
                body = [{"cmd": "GetNtp", "action": 0, "param": {}}]
            elif cmd == "GetTime":
                body = [{"cmd": "GetTime", "action": 0, "param": {}}]
            elif cmd == "GetPushCfg":
                body = [{"cmd": "GetPushCfg", "action": 0, "param": {}}]
            elif cmd == "GetAbility":
                body = [{"cmd": "GetAbility", "action": 0, "param": {"User": {"userName": self._username}}}]

        if body:
            try:
                json_data = await self.send(body, expected_response_type="json")
            except InvalidContentTypeError as err:
                raise InvalidContentTypeError(f"get_state cmd '{body[0]['cmd']}': {str(err)}") from err
            except NoDataError as err:
                raise NoDataError(f"Host: {self._host}:{self._port}: error obtaining get_state response for cmd '{body[0]['cmd']}'") from err

            if channels:
                self.map_channels_json_response(json_data, channels, chime_ids)
            else:
                self.map_host_json_response(json_data)

        return

    async def get_states(self, cmd_list: typings.cmd_list_type = None, wake: bool = True) -> None:
        body = []
        channels = []
        chime_ids = []
        if cmd_list is None:
            # cmd_list example: {"GetZoomFocus": {None: 6, 1: 3, 3: 3}, "GetHddInfo": {None: 1}}
            #                       command       host  #  ch #  ch #
            cmd_list = {}

        any_battery = any(self.supported(ch, "battery") for ch in self._channels)
        if any_battery and wake:
            _LOGGER.debug("Host %s:%s: Waking the battery cameras for the get_states update", self._host, self._port)

        def inc_host_cmd(cmd):
            return (cmd in cmd_list or not cmd_list) and (wake or not any_battery or cmd not in WAKING_COMMANDS)

        def inc_cmd(cmd, channel):
            return (channel in cmd_list.get(cmd, []) or not cmd_list or len(cmd_list.get(cmd, [])) == 1) and (
                wake or cmd not in WAKING_COMMANDS or not self.supported(channel, "battery")
            )

        def inc_wake(cmd, channel):
            return wake or cmd not in WAKING_COMMANDS or not self.supported(channel, "battery")

        def inc_host_wake(cmd):
            return wake or not any_battery or cmd not in WAKING_COMMANDS

        for channel in self._stream_channels:
            ch_body = []
            if inc_cmd("GetEnc", channel):
                ch_body.append({"cmd": "GetEnc", "action": 0, "param": {"channel": channel}})
            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))
            chime_ids.extend([-1] * len(ch_body))

        for channel in self._channels:
            ch_body = []
            if inc_cmd("GetIsp", channel):
                ch_body.append({"cmd": "GetIsp", "action": 0, "param": {"channel": channel}})

            if self.api_version("GetEvents") >= 1:
                ch_body.append({"cmd": "GetEvents", "action": 0, "param": {"channel": channel}})
            else:
                ch_body.append({"cmd": "GetMdState", "action": 0, "param": {"channel": channel}})
                if self.ai_supported(channel):
                    ch_body.append({"cmd": "GetAiState", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "ir_lights") and inc_cmd("GetIrLights", channel):
                ch_body.append({"cmd": "GetIrLights", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "floodLight") and inc_cmd("GetWhiteLed", channel):
                ch_body.append({"cmd": "GetWhiteLed", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "battery") and inc_cmd("GetBatteryInfo", channel):
                ch_body.append({"cmd": "GetBatteryInfo", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "PIR") and inc_cmd("GetPirInfo", channel):
                ch_body.append({"cmd": "GetPirInfo", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "status_led") and inc_cmd("GetPowerLed", channel):
                ch_body.append({"cmd": "GetPowerLed", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "zoom") and inc_cmd("GetZoomFocus", channel):
                ch_body.append({"cmd": "GetZoomFocus", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "auto_focus") and inc_cmd("GetAutoFocus", channel):
                ch_body.append({"cmd": "GetAutoFocus", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "ptz_guard") and inc_cmd("GetPtzGuard", channel):
                ch_body.append({"cmd": "GetPtzGuard", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "ptz_position") and inc_cmd("GetPtzCurPos", channel):
                ch_body.append({"cmd": "GetPtzCurPos", "action": 0, "param": {"PtzCurPos": {"channel": channel}}})

            if self.supported(channel, "auto_track") and inc_cmd("GetAiCfg", channel):
                ch_body.append({"cmd": "GetAiCfg", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "auto_track_limit") and inc_cmd("GetPtzTraceSection", channel):
                ch_body.append({"cmd": "GetPtzTraceSection", "action": 0, "param": {"PtzTraceSection": {"channel": channel}}})

            if self.supported(channel, "volume") and inc_cmd("GetAudioCfg", channel):
                ch_body.append({"cmd": "GetAudioCfg", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "hub_audio") and inc_cmd("GetDeviceAudioCfg", channel):
                ch_body.append({"cmd": "GetDeviceAudioCfg", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "quick_reply") and inc_cmd("GetAutoReply", channel):
                ch_body.append({"cmd": "GetAutoReply", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "chime") and inc_wake("GetDingDongList", channel):  # always include to discover new chimes and update "online" status
                ch_body.append({"cmd": "GetDingDongList", "action": 0, "param": {"channel": channel}})
            if self.supported(channel, "chime") and inc_cmd("GetDingDongCfg", channel):
                ch_body.append({"cmd": "GetDingDongCfg", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "manual_record") and inc_cmd("GetManualRec", channel):
                ch_body.append({"cmd": "GetManualRec", "action": 0, "param": {"channel": channel}})

            if (
                self.supported(channel, "isp_hue")
                or self.supported(channel, "isp_satruation")
                or self.supported(channel, "isp_sharpen")
                or self.supported(channel, "isp_contrast")
                or self.supported(channel, "isp_bright")
            ) and inc_cmd("GetImage", channel):
                ch_body.append({"cmd": "GetImage", "action": 0, "param": {"channel": channel}})

            if (self.supported(channel, "buzzer") or (self.supported(None, "buzzer") and channel == 0)) and inc_cmd("GetBuzzerAlarmV20", channel):
                ch_body.append({"cmd": "GetBuzzerAlarmV20", "action": 0, "param": {"channel": channel}})

            if (self.supported(channel, "email") or (self.supported(None, "email") and channel == 0)) and inc_cmd("GetEmail", channel):
                if self.api_version("GetEmail") >= 1:
                    ch_body.append({"cmd": "GetEmailV20", "action": 0, "param": {"channel": channel}})
                else:
                    ch_body.append({"cmd": "GetEmail", "action": 0, "param": {"channel": channel}})

            if (self.supported(channel, "push") or (self.supported(None, "push") and channel == 0)) and inc_cmd("GetPush", channel):
                if self.api_version("GetPush") >= 1:
                    ch_body.append({"cmd": "GetPushV20", "action": 0, "param": {"channel": channel}})
                else:
                    ch_body.append({"cmd": "GetPush", "action": 0, "param": {"channel": channel}})

            if (self.supported(channel, "ftp") or (self.supported(None, "ftp") and channel == 0)) and inc_cmd("GetFtp", channel):
                if self.api_version("GetFtp") >= 1:
                    ch_body.append({"cmd": "GetFtpV20", "action": 0, "param": {"channel": channel}})
                else:
                    ch_body.append({"cmd": "GetFtp", "action": 0, "param": {"channel": channel}})

            if (self.supported(channel, "recording") or (self.supported(None, "recording") and channel == 0)) and inc_cmd("GetRec", channel):
                if self.api_version("GetRec") >= 1:
                    ch_body.append({"cmd": "GetRecV20", "action": 0, "param": {"channel": channel}})
                else:
                    ch_body.append({"cmd": "GetRec", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "siren") and inc_cmd("GetAudioAlarm", channel):
                if self.api_version("GetAudioAlarm") >= 1:
                    ch_body.append({"cmd": "GetAudioAlarmV20", "action": 0, "param": {"channel": channel}})
                else:
                    ch_body.append({"cmd": "GetAudioAlarm", "action": 0, "param": {"channel": channel}})

            if self.supported(channel, "md_sensitivity") and inc_cmd("GetMdAlarm", channel):
                if self.api_version("GetMdAlarm") >= 1:
                    ch_body.append({"cmd": "GetMdAlarm", "action": 0, "param": {"channel": channel}})
                else:
                    ch_body.append({"cmd": "GetAlarm", "action": 0, "param": {"Alarm": {"channel": channel, "type": "md"}}})

            if self.supported(channel, "ai_sensitivity") and inc_cmd("GetAiAlarm", channel):
                for ai_type in self.ai_supported_types(channel):
                    ch_body.append({"cmd": "GetAiAlarm", "action": 0, "param": {"channel": channel, "ai_type": ai_type}})

            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))
            chime_ids.extend([-1] * len(ch_body))

        # chime states
        for chime_id, chime in self._chime_list.items():
            chime_ch = chime.channel
            if inc_cmd("DingDongOpt", channel) and chime.online:
                body.append({"cmd": "DingDongOpt", "action": 0, "param": {"DingDong": {"channel": chime_ch, "option": 2, "id": chime_id}}})
                channels.append(chime_ch)
                chime_ids.append(chime_id)

        # host states
        host_body = []
        if self.supported(None, "wifi") and self.wifi_connection and inc_host_cmd("GetWifiSignal"):
            host_body.append({"cmd": "GetWifiSignal", "action": 0, "param": {}})
        if self.supported(None, "performance") and inc_host_cmd("GetPerformance"):
            host_body.append({"cmd": "GetPerformance", "action": 0, "param": {}})
        if self.supported(None, "state_light") and inc_host_cmd("GetStateLight"):
            host_body.append({"cmd": "GetStateLight", "action": 0, "param": {}})
        if self.supported(None, "hdd") and inc_host_cmd("GetHddInfo"):
            host_body.append({"cmd": "GetHddInfo", "action": 0, "param": {}})
        if (self.supported(None, "sleep") and inc_host_cmd("GetChannelstatus")) or (self.is_nvr and self._GetChannelStatus_present and inc_host_wake("GetChannelstatus")):
            host_body.append({"cmd": "GetChannelstatus", "action": 0, "param": {}})  # always include to discover new cameras
        if self.supported(None, "push_config") and (inc_host_cmd("GetPush") or inc_host_cmd("GetPushCfg")):
            host_body.append({"cmd": "GetPushCfg", "action": 0, "param": {}})

        body.extend(host_body)
        channels.extend([-1] * len(host_body))
        chime_ids.extend([-1] * len(host_body))

        if not body:
            _LOGGER.debug(
                "Host %s:%s: get_states, no channels connected so skipping request.",
                self._host,
                self._port,
            )
            return
        if _LOGGER.isEnabledFor(logging.DEBUG):
            cmd_list_log = dict(cmd_list)
            for key in cmd_list_log:
                cmd_list_item = list(cmd_list_log[key])
                if None in cmd_list_item:
                    cmd_list_item.remove(None)
                cmd_list_log[key] = cmd_list_item
            _LOGGER.debug("Host %s:%s: get_states update cmd list: %s", self._host, self._port, cmd_list_log)

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"channel-state: {str(err)}") from err
        except NoDataError as err:
            raise NoDataError(f"Host: {self._host}:{self._port}: error obtaining channel-state response") from err

        self.map_channels_json_response(json_data, channels, chime_ids)

    async def get_host_data(self) -> None:
        """Fetch the host settings/capabilities."""
        body: typings.reolink_json = [
            {"cmd": "GetChannelstatus"},
            {"cmd": "GetDevInfo", "action": 0, "param": {}},
            {"cmd": "GetLocalLink", "action": 0, "param": {}},
            {"cmd": "GetNetPort", "action": 0, "param": {}},
            {"cmd": "GetP2p", "action": 0, "param": {}},
            {"cmd": "GetHddInfo", "action": 0, "param": {}},
            {"cmd": "GetUser", "action": 0, "param": {}},
            {"cmd": "GetNtp", "action": 0, "param": {}},
            {"cmd": "GetTime", "action": 0, "param": {}},
            {"cmd": "GetPushCfg", "action": 0, "param": {}},
            {"cmd": "GetAbility", "action": 0, "param": {"User": {"userName": self._username}}},
        ]

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Get host-settings error: {str(err)}") from err
        except NoDataError as err:
            raise NoDataError(f"Host: {self._host}:{self._port}: returned no data when obtaining host-settings") from err

        self.map_host_json_response(json_data)
        self.construct_capabilities(warnings=False)

        # Check for invalid NVT-IPC cameras
        for channel in self._channels:
            if self.camera_name(channel) == "NVT":
                body = [{"cmd": "GetChnTypeInfo", "action": 0, "param": {"channel": channel}}]
                try:
                    json_data = await self.send(body, expected_response_type="json")
                except ReolinkError as err:
                    self._channels.remove(channel)
                    _LOGGER.debug("Reolink camera on channel %s, called 'NVT' with error received getting its model, removing this channel, err: %s", channel, str(err))
                else:
                    self.map_channel_json_response(json_data, channel)
                    if self.camera_model(channel) == "IPC":
                        self._channels.remove(channel)
                        _LOGGER.debug("Reolink camera on channel %s, called 'NVT' with model 'IPC', removing this invalid channel", channel)

        if self.model in DUAL_LENS_SINGLE_MOTION_MODELS or (not self.is_nvr and self.api_version("supportAutoTrackStream", 0) > 0):
            self._stream_channels = [0, 1]
            self._nvr_num_channels = 1
            self._channels = [0]
        else:
            self._stream_channels = self._channels

        body = []
        channels = []
        for channel in self._stream_channels:
            ch_body = [
                {"cmd": "GetEnc", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetRtspUrl", "action": 0, "param": {"channel": channel}},
            ]
            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))

        for channel in self._channels:
            ch_body = [
                {"cmd": "GetChnTypeInfo", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetMdState", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetAiState", "action": 0, "param": {"channel": channel}},  # to capture AI capabilities
                {"cmd": "GetEvents", "action": 0, "param": {"channel": channel}},
                {"cmd": "GetIsp", "action": 0, "param": {"channel": channel}},
            ]

            if channel > 0 and self.model in DUAL_LENS_DUAL_MOTION_MODELS:
                body.extend(ch_body)
                channels.extend([channel] * len(ch_body))
                continue

            ch_body.extend(
                [
                    {"cmd": "GetWhiteLed", "action": 0, "param": {"channel": channel}},
                    {"cmd": "GetIrLights", "action": 0, "param": {"channel": channel}},
                    {"cmd": "GetAudioCfg", "action": 0, "param": {"channel": channel}},
                    {"cmd": "GetManualRec", "action": 0, "param": {"channel": channel}},
                ]
            )
            if self.is_nvr:
                ch_body.append({"cmd": "GetDeviceAudioCfg", "action": 0, "param": {"channel": channel}})
            # one time values
            ch_body.append({"cmd": "GetOsd", "action": 0, "param": {"channel": channel}})
            if self.supported(channel, "quick_reply"):
                ch_body.append({"cmd": "GetAudioFileList", "action": 0, "param": {"channel": channel}})
            if self.api_version("supportDingDongCtrl", channel) > 0:
                ch_body.append({"cmd": "GetDingDongList", "action": 0, "param": {"channel": channel}})
            if self.supported(channel, "webhook"):
                ch_body.append({"cmd": "GetWebHook", "action": 0, "param": {"channel": channel}})
            # checking range
            if self.supported(channel, "zoom_basic") or self.api_version("supportDigitalZoom", channel) > 0:
                ch_body.append({"cmd": "GetZoomFocus", "action": 1, "param": {"channel": channel}})
            if self.supported(channel, "pan_tilt") and self.api_version("ptzPreset", channel) >= 1:
                ch_body.append({"cmd": "GetPtzPreset", "action": 0, "param": {"channel": channel}})
                ch_body.append({"cmd": "GetPtzPatrol", "action": 0, "param": {"channel": channel}})
                ch_body.append({"cmd": "GetPtzGuard", "action": 0, "param": {"channel": channel}})
                ch_body.append({"cmd": "GetPtzCurPos", "action": 0, "param": {"PtzCurPos": {"channel": channel}}})
            if self.supported(channel, "auto_track"):
                ch_body.append({"cmd": "GetAiCfg", "action": 1, "param": {"channel": channel}})
            # checking API versions
            if self.api_version("supportBuzzer") > 0:
                ch_body.append({"cmd": "GetBuzzerAlarmV20", "action": 0, "param": {"channel": channel}})
            if self.api_version("scheduleVersion") >= 1:
                ch_body.extend(
                    [
                        {"cmd": "GetEmailV20", "action": 0, "param": {"channel": channel}},
                        {"cmd": "GetPushV20", "action": 0, "param": {"channel": channel}},
                        {"cmd": "GetFtpV20", "action": 0, "param": {"channel": channel}},
                        {"cmd": "GetRecV20", "action": 0, "param": {"channel": channel}},
                        {"cmd": "GetAudioAlarmV20", "action": 0, "param": {"channel": channel}},
                        {"cmd": "GetMdAlarm", "action": 0, "param": {"channel": channel}},
                    ]
                )
            else:
                ch_body.extend(
                    [
                        {"cmd": "GetEmail", "action": 0, "param": {"channel": channel}},
                        {"cmd": "GetPush", "action": 0, "param": {"channel": channel}},
                        {"cmd": "GetFtp", "action": 0, "param": {"channel": channel}},
                        {"cmd": "GetRec", "action": 0, "param": {"channel": channel}},
                        {"cmd": "GetAudioAlarm", "action": 0, "param": {"channel": channel}},
                        {"cmd": "GetAlarm", "action": 0, "param": {"Alarm": {"channel": channel, "type": "md"}}},
                    ]
                )

            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))

        # checking host command support
        host_body = []
        if self.api_version("performance") > 0:
            host_body.append({"cmd": "GetPerformance", "action": 0, "param": {}})
        if self.is_nvr:
            host_body.append({"cmd": "GetStateLight", "action": 0, "param": {}})

        body.extend(host_body)
        channels.extend([-1] * len(host_body))

        if not body:
            _LOGGER.debug(
                "Host %s:%s: get_host_data, no channels connected so skipping channel specific requests.",
                self._host,
                self._port,
            )
            return

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Channel-settings: {str(err)}") from err
        except NoDataError as err:
            raise NoDataError(f"Host: {self._host}:{self._port}: returned no data when obtaining initial channel-settings") from err

        self.map_channels_json_response(json_data, channels)

        # Baichuan fallbacks
        for channel in self._channels:
            if self.camera_hardware_version(channel) == "Unknown":
                try:
                    await self.baichuan.get_info(channel)
                except ReolinkError:
                    continue
                self._channel_hw_version[channel] = self.baichuan.hardware_version(channel)

        # Let's assume all channels of an NVR or multichannel-camera always have the same versions of commands... Not sure though...
        def check_command_exists(cmd: str) -> int:
            for x in json_data:
                if x["cmd"] == cmd:
                    return 1
            return 0

        self._api_version["GetEvents"] = check_command_exists("GetEvents")
        self._api_version["GetWhiteLed"] = check_command_exists("GetWhiteLed")
        self._api_version["GetAudioCfg"] = check_command_exists("GetAudioCfg")
        self._api_version["GetDeviceAudioCfg"] = check_command_exists("GetDeviceAudioCfg")
        self._api_version["GetPtzGuard"] = check_command_exists("GetPtzGuard")
        self._api_version["GetPtzCurPos"] = check_command_exists("GetPtzCurPos")
        if self.api_version("scheduleVersion") >= 1:
            self._api_version["GetEmail"] = check_command_exists("GetEmailV20")
            self._api_version["GetPush"] = check_command_exists("GetPushV20")
            self._api_version["GetFtp"] = check_command_exists("GetFtpV20")
            self._api_version["GetRec"] = check_command_exists("GetRecV20")
            self._api_version["GetAudioAlarm"] = check_command_exists("GetAudioAlarmV20")
            self._api_version["GetMdAlarm"] = check_command_exists("GetMdAlarm")
        else:
            self._api_version["GetEmail"] = 0
            self._api_version["GetPush"] = 0
            self._api_version["GetFtp"] = 0
            self._api_version["GetRec"] = 0
            self._api_version["GetAudioAlarm"] = 0
            self._api_version["GetMdAlarm"] = 0

        self.construct_capabilities()

        # Check for special chars in password
        self.valid_password()

        if self.protocol == "rtsp":
            # Cache the RTSP urls
            for channel in self._stream_channels:
                await self.get_rtsp_stream_source(channel, "sub")
                await self.get_rtsp_stream_source(channel, "main")

        self._startup = False

    async def get_motion_state(self, channel: int) -> Optional[bool]:
        if channel not in self._channels:
            return None

        if self.api_version("GetEvents") >= 1:
            # Needed because battery cams use PIR detection with the "other" item
            body = [{"cmd": "GetEvents", "action": 0, "param": {"channel": channel}}]
        else:
            body = [{"cmd": "GetMdState", "action": 0, "param": {"channel": channel}}]

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError:
            _LOGGER.error(
                "Host %s:%s: error translating motion detection state response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._motion_detection_states[channel] = False
            return False
        except NoDataError:
            _LOGGER.error(
                "Host %s:%s: error obtaining motion state response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._motion_detection_states[channel] = False
            return False

        self.map_channel_json_response(json_data, channel)

        return None if channel not in self._motion_detection_states else self._motion_detection_states[channel]

    async def get_ai_state(self, channel: int) -> Optional[dict[str, bool]]:
        if channel not in self._channels:
            return None

        body = [{"cmd": "GetAiState", "action": 0, "param": {"channel": channel}}]

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError:
            _LOGGER.error(
                "Host %s:%s: error translating AI detection state response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._ai_detection_states[channel] = {}
            return None
        except NoDataError:
            _LOGGER.error(
                "Host %s:%s: error obtaining AI detection state response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._ai_detection_states[channel] = {}
            return None

        self.map_channel_json_response(json_data, channel)

        return (
            None
            if self._ai_detection_states is None or channel not in self._ai_detection_states or self._ai_detection_states[channel] is None
            else self._ai_detection_states[channel]
        )

    async def get_ai_state_all_ch(self) -> bool:
        """Fetch Ai and visitor state all channels at once (AI + visitor)."""
        body = []
        channels = []
        for channel in self._channels:
            if self.api_version("GetEvents") >= 1:
                ch_body = [{"cmd": "GetEvents", "action": 0, "param": {"channel": channel}}]
            else:
                if not self.ai_supported(channel):
                    continue
                ch_body = [{"cmd": "GetAiState", "action": 0, "param": {"channel": channel}}]
            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))

        if not body:
            _LOGGER.warning(
                "Host %s:%s: get_ai_state_all_ch called while none of the channels support AI detection",
                self._host,
                self._port,
            )
            return False

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError as err:
            _LOGGER.error(
                "Host %s:%s: error translating AI states all channel response: %s",
                self._host,
                self._port,
                str(err),
            )
            return False
        except NoDataError:
            _LOGGER.error(
                "Host %s:%s: error obtaining AI states all channel response.",
                self._host,
                self._port,
            )
            return False

        try:
            self.map_channels_json_response(json_data, channels)
        except UnexpectedDataError as err:
            _LOGGER.error("Error in get_ai_state_all_ch: %s", err)
            return False

        return True

    async def get_all_motion_states(self, channel: int) -> Optional[bool]:
        """Fetch All motions states at once (regular + AI + visitor)."""
        if channel not in self._channels:
            return None

        if self.api_version("GetEvents") >= 1:
            body = [{"cmd": "GetEvents", "action": 0, "param": {"channel": channel}}]
        else:
            body = [{"cmd": "GetMdState", "action": 0, "param": {"channel": channel}}]
            if self.ai_supported(channel):
                body.append({"cmd": "GetAiState", "action": 0, "param": {"channel": channel}})

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError:
            _LOGGER.error(
                "Host %s:%s: error translating All Motion States response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._motion_detection_states[channel] = False
            self._ai_detection_states[channel] = {}
            return False
        except NoDataError:
            _LOGGER.error(
                "Host %s:%s: error obtaining All Motion States response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            self._motion_detection_states[channel] = False
            self._ai_detection_states[channel] = {}
            return False

        self.map_channel_json_response(json_data, channel)

        return None if channel not in self._motion_detection_states else self._motion_detection_states[channel]

    async def get_motion_state_all_ch(self) -> bool:
        """Fetch All motions states of all channels at once (regular + AI + visitor)."""
        body = []
        channels = []
        for channel in self._channels:
            if self.api_version("GetEvents") >= 1:
                ch_body = [{"cmd": "GetEvents", "action": 0, "param": {"channel": channel}}]
            else:
                ch_body = [{"cmd": "GetMdState", "action": 0, "param": {"channel": channel}}]
                if self.ai_supported(channel):
                    ch_body.append({"cmd": "GetAiState", "action": 0, "param": {"channel": channel}})
            body.extend(ch_body)
            channels.extend([channel] * len(ch_body))

        if not body:
            _LOGGER.debug(
                "Host %s:%s: get_motion_state_all_ch, no channels connected so skipping request.",
                self._host,
                self._port,
            )
            return True

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError as err:
            _LOGGER.error(
                "Host %s:%s: error translating All Motion States response: %s",
                self._host,
                self._port,
                str(err),
            )
            for channel in self._channels:
                self._motion_detection_states[channel] = False
                self._ai_detection_states[channel] = {}
            return False
        except NoDataError:
            _LOGGER.error(
                "Host %s:%s: error obtaining All Motion States response.",
                self._host,
                self._port,
            )
            for channel in self._channels:
                self._motion_detection_states[channel] = False
                self._ai_detection_states[channel] = {}
            return False

        try:
            self.map_channels_json_response(json_data, channels)
        except UnexpectedDataError as err:
            _LOGGER.error("Error in get_motion_state_all_ch: %s", err)
            return False

        return True

    async def _check_reolink_firmware(self, channel: int | None = None) -> NewSoftwareVersion:
        """Check for new firmware from reolink.com"""
        if not self.supported(channel, "firmware"):
            raise NotSupportedError(f"check firmware online: not supported by {self.camera_name(channel)}")

        ch_hw = self.camera_hardware_version(channel)
        ch_mod = self.camera_model(channel)
        now = time_now()
        if now - self._last_sw_id_check > 300 and (channel not in self._sw_hardware_id or channel not in self._sw_model_id):
            self._last_sw_id_check = now

            request_URL = "https://reolink.com/wp-json/reo-v2/download/hardware-version/selection-list"
            json_data = await self.send_reolink_com(request_URL)

            chs: list[int | None] = [None]
            chs.extend(self.channels)
            for ch in chs:  # update the cache of all devices in one go
                hw_ch_i = strip_model_str(self.camera_hardware_version(ch))
                mod_ch_i = strip_model_str(self.camera_model(ch))
                for device in json_data["data"]:
                    hw_json = strip_model_str(device["title"])
                    mod_json = strip_model_str(device["dlProduct"]["title"])
                    if hw_json == hw_ch_i and mod_json == mod_ch_i:
                        self._sw_hardware_id[ch] = device["id"]
                        self._sw_model_id[ch] = device["dlProduct"]["id"]

        if channel not in self._sw_hardware_id or channel not in self._sw_model_id:
            raise UnexpectedDataError(f"Could not find model '{ch_mod}' hardware '{ch_hw}' in list from reolink.com")

        if f"{ch_mod}-{ch_hw}" in self._latest_sw_model_version and now - self._latest_sw_model_version[f"{ch_mod}-{ch_hw}"].last_check < 300:
            # this hardware-model was checked less than 5 min ago, return previous result
            return self._latest_sw_model_version[f"{ch_mod}-{ch_hw}"]

        request_URL = f"https://reolink.com/wp-json/reo-v2/download/firmware/?dlProductId={self._sw_model_id[channel]}&hardwareVersion={self._sw_hardware_id[channel]}&lang=en"
        json_data = await self.send_reolink_com(request_URL)

        ch_hw_match = strip_model_str(ch_hw)
        ch_mod_match = strip_model_str(ch_mod)
        key = f"{ch_mod}-{ch_hw}"
        firmware_info_list = json_data["data"][0]["firmwares"]
        for firmware_info in firmware_info_list:
            hw_ver = strip_model_str(firmware_info["hardwareVersion"][0]["title"])
            mod_ver = strip_model_str(firmware_info["hardwareVersion"][0]["dlProduct"]["title"])
            if hw_ver != ch_hw_match or mod_ver != ch_mod_match:
                raise UnexpectedDataError(
                    f"Hardware version of firmware info from reolink.com does not match: '{hw_ver}' != '{ch_hw_match}' or '{mod_ver}' != '{ch_mod_match}'"
                )
            new_sw_version = NewSoftwareVersion(firmware_info["version"], download_url=firmware_info["url"], release_notes=firmware_info["new"], last_check=now)
            if key not in self._latest_sw_model_version or new_sw_version >= self._latest_sw_model_version[key]:
                self._latest_sw_model_version[key] = new_sw_version

        return self._latest_sw_model_version[key]

    async def check_new_firmware(self, ch_list: list[None | int] | None = None) -> Literal[False] | NewSoftwareVersion | str:
        """check for new firmware using camera API, returns False if no new firmware available."""
        new_firmware = 0
        ch: int | None
        if not ch_list:
            ch_list = [None]
            ch_list.extend(self.channels)

        # check current firmware version and check for host update using API
        body: typings.reolink_json = [{"cmd": "GetDevInfo", "action": 0, "param": {}}]
        channels = [-1]
        if self.supported(None, "update"):
            body.append({"cmd": "CheckFirmware"})
            channels.append(-1)
        for ch in self.channels:
            if ch in ch_list and self.supported(ch, "firmware"):
                body.append({"cmd": "GetChnTypeInfo", "action": 0, "param": {"channel": ch}})
                channels.append(ch)

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Check firmware: {str(err)}") from err
        except NoDataError as err:
            raise NoDataError(f"Host: {self._host}:{self._port}: error obtaining CheckFirmware response") from err

        self.map_channels_json_response(json_data, channels)

        if self.supported(None, "update"):
            try:
                new_firmware = json_data[1]["value"]["newFirmware"]
            except KeyError as err:
                raise UnexpectedDataError(f"Host {self._host}:{self._port}: received an unexpected response from check_new_firmware: {json_data}") from err

        # check latest available firmware version online
        for ch in ch_list:
            self._latest_sw_version[ch] = False
        for ch in ch_list:
            if not self.supported(ch, "firmware"):
                continue
            try:
                self._latest_sw_version[ch] = await self._check_reolink_firmware(ch)
            except (NotSupportedError, UnexpectedDataError) as err:
                _LOGGER.debug(err)
            except ApiError as err:
                if err.rspCode == 429:
                    _LOGGER.warning(err)
                else:
                    _LOGGER.debug(err)
                break
            except ReolinkError as err:
                _LOGGER.debug(err)
                break

        # compare software versions
        for ch in ch_list:
            if self.camera_sw_version_object(ch) == SoftwareVersion(None):
                self._latest_sw_version[ch] = False
                continue
            if not isinstance(self._latest_sw_version[ch], NewSoftwareVersion):
                continue
            if self.camera_sw_version_object(ch) >= self._latest_sw_version[ch]:
                self._latest_sw_version[ch] = False

        # check host online update result
        latest_sw_version_host = self._latest_sw_version[None]
        if new_firmware != 0 and latest_sw_version_host is False:
            if new_firmware == 1:
                latest_sw_version_host = "New firmware available"
            else:
                latest_sw_version_host = str(new_firmware)
        if isinstance(latest_sw_version_host, NewSoftwareVersion):
            latest_sw_version_host.online_update_available = new_firmware == 1

        self._latest_sw_version[None] = latest_sw_version_host
        return latest_sw_version_host

    def firmware_update_available(self, channel: int | None = None) -> Literal[False] | NewSoftwareVersion | str:
        if channel not in self._latest_sw_version:
            return False

        return self._latest_sw_version[channel]

    async def update_firmware(self, channel: int | None = None) -> None:
        """check for new firmware."""
        try:
            await self.get_state(cmd="GetDevInfo")
        except ReolinkError:
            pass

        if not self.supported(channel, "update"):
            raise NotSupportedError(f"update_firmware: not supported by {self.camera_name(channel)}")

        body = [{"cmd": "UpgradeOnline"}]
        try:
            await self.send_setting(body)
        except ApiError as err:
            if err.rspCode == -30:  # same version
                raise ApiError(
                    "Reolink device could not find new firmware, "
                    "try downloading from the Reolink download center (https://reolink.com/download-center) and update manually",
                    rspCode=err.rspCode,
                ) from err
            raise err

    async def update_progress(self) -> bool | int:
        """check progress of firmware update, returns False if not in progress."""
        if not self.supported(None, "update"):
            raise NotSupportedError(f"update_progress: not supported by {self.nvr_name}")

        body = [{"cmd": "UpgradeStatus"}]

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Update progress: {str(err)}") from err
        except NoDataError as err:
            raise NoDataError(f"Host: {self._host}:{self._port}: error obtaining update progress response") from err

        if json_data[0]["code"] != 0:
            return False

        return json_data[0]["value"]["Status"]["Persent"]

    async def reboot(self) -> None:
        """Reboot the camera."""
        if not self.supported(None, "reboot"):
            raise NotSupportedError(f"Reboot: not supported by {self.nvr_name}")

        body = [{"cmd": "Reboot"}]
        json_data = await self.send(body, expected_response_type="json")

        if json_data[0]["code"] != 0 or json_data[0].get("value", {}).get("rspCode", -1) != 200:
            rspCode = json_data[0].get("value", json_data[0]["error"])["rspCode"]
            detail = json_data[0].get("value", json_data[0]["error"]).get("detail", "")
            raise ApiError(f"Reboot: API returned error code {json_data[0]['code']}, response code {rspCode}/{detail}", rspCode=rspCode)

    async def get_snapshot(self, channel: int, stream: Optional[str] = None) -> bytes | None:
        """Get the still image."""
        if channel not in self._stream_channels:
            return None

        if stream is None:
            stream = "main"

        param: dict[str, Any] = {"cmd": "Snap", "channel": channel}

        if stream.startswith("autotrack_"):
            param["iLogicChannel"] = 1
            stream = stream.removeprefix("autotrack_")

        if stream.startswith("snapshots_"):
            stream = stream.removeprefix("snapshots_")

        if stream not in ["main", "sub"]:
            stream = "main"

        param["snapType"] = stream

        if stream == "sub":
            height = self._enc_settings.get(channel, {}).get("Enc", {}).get(f"{stream}Stream", {}).get("height")
            width = self._enc_settings.get(channel, {}).get("Enc", {}).get(f"{stream}Stream", {}).get("width")
            if height is not None and width is not None:
                param["width"] = width
                param["height"] = height

        body: typings.reolink_json = [{}]
        response = await self.send(body, param, expected_response_type="image/jpeg")
        if response is None or response == b"":
            _LOGGER.error(
                "Host: %s:%s: error obtaining still image response for channel %s.",
                self._host,
                self._port,
                channel,
            )
            await self.expire_session(unsubscribe=False)
            return None

        return response

    def get_flv_stream_source(self, channel: int, stream: Optional[str] = None) -> Optional[str]:
        if channel not in self._stream_channels:
            return None

        if stream is None:
            stream = self._stream

        if self._use_https:
            http_s = "https"
        else:
            http_s = "http"

        # FLV needs unencoded password
        return f"{http_s}://{self._host}:{self._port}/flv?port={self._rtmp_port}&app=bcs&stream=channel{channel}_{stream}.bcs&user={self._username}&password={self._password}"

    def get_rtmp_stream_source(self, channel: int, stream: Optional[str] = None) -> Optional[str]:
        if channel not in self._stream_channels:
            return None

        if stream is None:
            stream = self._stream

        stream_type = None
        if stream in ["sub", "autotrack_sub", "telephoto_sub"]:
            stream_type = 1
        else:
            stream_type = 0
        if self._rtmp_auth_method == DEFAULT_RTMP_AUTH_METHOD:
            # RTMP needs unencoded password
            return (
                f"rtmp://{self._host}:{self._rtmp_port}/bcs/"
                f"channel{channel}_{stream}.bcs?channel={channel}&stream={stream_type}&user={self._username}&password={self._password}"
            )

        return f"rtmp://{self._host}:{self._rtmp_port}/bcs/channel{channel}_{stream}.bcs?channel={channel}&stream={stream_type}&token={self._token}"

    async def get_encoding(self, channel: int, stream: str = "main") -> str:
        if not self._enc_settings:
            try:
                await self.get_state(cmd="GetEnc")
            except ReolinkError:
                pass

        encoding = self._enc_settings.get(channel, {}).get("Enc", {}).get(f"{stream}Stream", {}).get("vType")
        if encoding is not None:
            return encoding
        if stream == "sub":
            return "h264"
        if self.api_version("mainEncType", channel) > 0:
            return "h265"
        return "h264"

    async def _check_rtsp_url(self, url: str, channel: int, stream: str) -> bool:
        if channel not in self._rtsp_verified:
            self._rtsp_verified[channel] = {}
        if stream not in self._rtsp_verified[channel]:
            # save the first tried URL (based on camera capabilities) in case all attempts fail
            self._rtsp_verified[channel][stream] = url

        url_clean = url.replace(f"{self._username}:{self._enc_password}@", "")

        try:
            async with RTSPConnection(host=self._host, port=self._rtsp_port, username=self._username, password=self._password, logger=_LOGGER_RTSP) as rtsp_conn:
                response = await rtsp_conn.send_request("DESCRIBE", url_clean)
        except RTSPError as err:
            _LOGGER.debug("Error while checking RTSP url '%s': %s", url_clean, err)
            return False

        if response.status != 200 or response.status_msg != "OK":
            _LOGGER.debug("Error while checking RTSP url '%s': status %s, message %s", url_clean, response.status, response.status_msg)
            return False

        self._rtsp_verified[channel][stream] = url
        return True

    async def get_rtsp_stream_source(self, channel: int, stream: Optional[str] = None) -> Optional[str]:
        if channel not in self._stream_channels:
            return None

        if stream is None:
            stream = self._stream

        if channel in self._rtsp_verified and stream in self._rtsp_verified[channel]:
            return self._rtsp_verified[channel][stream]

        _LOGGER.debug("Checking RTSP urls host %s:%s, channel %s, stream %s", self._host, self._port, channel, stream)

        if self.api_version("rtsp") >= 3 and stream == "main" and channel in self._rtsp_mainStream:
            if await self._check_rtsp_url(self._rtsp_mainStream[channel], channel, stream):
                return self._rtsp_mainStream[channel]
        if self.api_version("rtsp") >= 3 and stream == "sub" and channel in self._rtsp_subStream:
            if await self._check_rtsp_url(self._rtsp_subStream[channel], channel, stream):
                return self._rtsp_subStream[channel]

        if not self._enc_settings:
            try:
                await self.get_state(cmd="GetEnc")
            except ReolinkError:
                pass

        encoding = self._enc_settings.get(channel, {}).get("Enc", {}).get(f"{stream}Stream", {}).get("vType")
        if encoding is None and stream == "main" and channel in self._rtsp_mainStream:
            if await self._check_rtsp_url(self._rtsp_mainStream[channel], channel, stream):
                return self._rtsp_mainStream[channel]

        if encoding is None and stream == "sub" and channel in self._rtsp_subStream:
            if await self._check_rtsp_url(self._rtsp_subStream[channel], channel, stream):
                return self._rtsp_subStream[channel]

        if encoding is None and stream == "main":
            if self.api_version("mainEncType", channel) > 0:
                encoding = "h265"
            else:
                encoding = "h264"

        if encoding is None:
            _LOGGER.debug(
                "Host %s:%s rtsp stream: GetEnc incomplete, GetRtspUrl unavailable, falling back to h264 encoding for channel %i, Enc: %s",
                self._host,
                self._port,
                channel,
                self._enc_settings,
            )
            encoding = "h264"

        channel_str = f"{channel + 1:02d}"

        # RTSP needs encoded password
        url = f"rtsp://{self._username}:{self._enc_password}@{self._host}:{self._rtsp_port}/{encoding}Preview_{channel_str}_{stream}"
        if await self._check_rtsp_url(url, channel, stream):
            return url

        encoding = "h265" if encoding == "h264" else "h264"
        url = f"rtsp://{self._username}:{self._enc_password}@{self._host}:{self._rtsp_port}/{encoding}Preview_{channel_str}_{stream}"
        if await self._check_rtsp_url(url, channel, stream):
            return url

        url = f"rtsp://{self._username}:{self._enc_password}@{self._host}:{self._rtsp_port}/Preview_{channel_str}_{stream}"
        if await self._check_rtsp_url(url, channel, stream):
            return url

        # return the first tried URL (based on camera capabilities as above)
        _LOGGER.error("Host %s:%s, could not verify a working RTSP url for channel %s, stream %s", self._host, self._port, channel, stream)
        return self._rtsp_verified[channel][stream]

    async def get_stream_source(self, channel: int, stream: Optional[str] = None) -> Optional[str]:
        """Return the stream source url."""
        try:
            await self.login()
        except LoginError:
            return None

        if stream is None:
            stream = self._stream

        if stream not in ["main", "sub", "ext", "autotrack_sub", "telephoto_sub"]:
            return None
        if self.protocol == "rtmp":
            return self.get_rtmp_stream_source(channel, stream)
        if self.protocol == "flv" or stream in ["autotrack_sub", "telephoto_sub"]:
            return self.get_flv_stream_source(channel, stream)
        if self.protocol == "rtsp":
            return await self.get_rtsp_stream_source(channel, stream)
        return None

    async def get_vod_source(
        self,
        channel: int,
        filename: str,
        stream: Optional[str] = None,
        request_type: VodRequestType = VodRequestType.FLV,
    ) -> tuple[str, str]:
        """Return the VOD source url."""
        if channel not in self._stream_channels:
            raise InvalidParameterError(f"get_vod_source: no camera connected to channel '{channel}'")

        # Since no request is made, make sure we are logged in.
        await self.login()

        if stream is None:
            stream = self._stream

        if self._is_nvr and request_type in [VodRequestType.RTMP]:
            _LOGGER.warning("get_vod_source: NVRs do not yet support '%s' vod requests, using FLV instead", request_type.value)
            request_type = VodRequestType.FLV

        if stream == "sub":
            stream_type = 1
        else:
            stream_type = 0

        if request_type in [VodRequestType.FLV, VodRequestType.RTMP]:
            mime = "application/x-mpegURL"
            # Looks like it only works with login/password method, not with token
            # RTMP and FLV need unencoded password
            credentials = f"&user={self._username}&password={self._password}"
        else:
            mime = "video/mp4"
            credentials = f"&token={self._token}"

        if request_type == VodRequestType.RTMP:
            # RTMP port needs to be enabled for playback to work
            if self._rtmp_enabled is None:
                await self.get_state("GetNetPort")
            if self._rtmp_enabled is False:
                await self.set_net_port(enable_rtmp=True)

            url = f"rtmp://{self._host}:{self._rtmp_port}/vod/{filename.replace('/', '%20')}?channel={channel}&stream={stream_type}"
        elif request_type == VodRequestType.FLV:
            if self._use_https:
                http_s = "https"
            else:
                http_s = "http"

            # seek = start x seconds into the file
            url = f"{http_s}://{self._host}:{self._port}/flv?port={self._rtmp_port}&app=bcs&stream=playback.bcs&channel={channel}&type={stream_type}&start={filename}&seek=0"
        elif request_type == VodRequestType.PLAYBACK:
            start_time = ""
            match = re.match(r".*Rec(\w{3})(?:_|_DST)(\d{8})_(\d{6})_.*", filename)
            if match is not None:
                start_time = f"&start={match.group(2)}{match.group(3)}"

            url = f"{self._url}?cmd=Playback&source={filename}&output={filename.replace('/', '_')}{start_time}"
        else:
            raise InvalidParameterError(f"get_vod_source: unsupported request_type '{request_type.value}'")

        return (mime, f"{url}{credentials}")

    async def _generate_NVR_download_vod(
        self,
        filename: str,
        start_time: str,
        end_time: str,
        channel: str,
        stream: str,
    ) -> str:
        start = datetime_to_reolink_time(start_time)
        end = datetime_to_reolink_time(end_time)
        body = [
            {
                "cmd": "NvrDownload",
                "action": 1,
                "param": {
                    "NvrDownload": {
                        "channel": channel,
                        "iLogicChannel": 0,
                        "streamType": stream,
                        "StartTime": start,
                        "EndTime": end,
                    }
                },
            }
        ]

        try:
            json_data = await self.send(body, expected_response_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Request NvrDownload error: {str(err)}") from err
        except NoDataError as err:
            raise NoDataError(f"Request NvrDownload error: {str(err)}") from err

        if json_data[0].get("code") != 0:
            raise ApiError(f"Host: {self._host}:{self._port}: Request NvrDownload: API returned error code {json_data[0].get('code', -1)}, response: {json_data}")

        max_filesize = 0
        for file in json_data[0]["value"]["fileList"]:
            filesize = int(file["fileSize"])
            if filesize > max_filesize:
                max_filesize = filesize
                filename = file["fileName"]

        _LOGGER.debug("NVR prepared file %s", filename)

        return filename

    async def download_vod(
        self,
        filename: str,
        wanted_filename: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        channel: Optional[str] = None,
        stream: Optional[str] = None,
    ) -> typings.VOD_download:
        if wanted_filename is None:
            wanted_filename = filename.replace("/", "_")

        body: typings.reolink_json
        if self.is_nvr:
            # NVRs require a additional NvrDownload command to prepare the file for download
            if start_time is None or end_time is None or channel is None or stream is None:
                raise InvalidParameterError("download_vod: for a NVR 'start_time', 'end_time', 'channel' and 'stream' parameters are required")

            filename = await self._generate_NVR_download_vod(filename, start_time, end_time, channel, stream)

        param: dict[str, Any] = {"cmd": "Download", "source": filename, "output": wanted_filename}
        body = [{}]
        response = await self.send(body, param, expected_response_type="application/octet-stream")

        if response.content_length is None:
            response.release()
            raise UnexpectedDataError(f"Host {self._host}:{self._port}: Download VOD: no 'content_length' in the response")
        if response.content_disposition is None or response.content_disposition.filename is None:
            response.release()
            raise UnexpectedDataError(f"Host {self._host}:{self._port}: Download VOD: no 'content_disposition.filename' in the response")

        return typings.VOD_download(response.content_length, response.content_disposition.filename, response.content, response.release, response.headers.get("ETag"))

    def ensure_channel_uid_unique(self):
        """Make sure the channel UIDs are all unique."""
        rev_channel_uids = {}
        for key, value in self._channel_uids.items():
            rev_channel_uids.setdefault(value, set()).add(key)
        duplicate_uids = [values for key, values in rev_channel_uids.items() if len(values) > 1]
        for duplicate in duplicate_uids:
            for ch in duplicate:
                self._channel_uids[ch] = f"{self._channel_uids[ch]}_{ch}"

    def map_host_json_response(self, json_data: typings.reolink_json):
        """Map the JSON objects to internal cache-objects."""
        for data in json_data:
            try:
                if data["code"] == 1:  # Error, like "ability error"
                    continue

                if data["cmd"] == "GetChannelstatus":
                    cur_status = data["value"]["status"]

                    if not self._GetChannelStatus_present and (self._nvr_num_channels == 0 or len(self._channels) == 0):
                        self._channels.clear()
                        self._is_doorbell.clear()

                        self._nvr_num_channels = data["value"]["count"]
                        if self._nvr_num_channels > 0:
                            # Not all Reolink devices respond with "name" attribute.
                            if "name" in cur_status[0]:
                                self._GetChannelStatus_has_name = True
                                self._channel_names.clear()
                            else:
                                self._GetChannelStatus_has_name = False

                            for ch_info in cur_status:
                                if ch_info["online"] == 1:
                                    cur_channel = ch_info["channel"]

                                    if "typeInfo" in ch_info:  # Not all Reolink devices respond with "typeInfo" attribute.
                                        self._channel_models[cur_channel] = ch_info["typeInfo"]
                                        self._is_doorbell[cur_channel] = "Doorbell" in self._channel_models[cur_channel]

                                    if "uid" in ch_info:
                                        self._channel_uids[cur_channel] = ch_info["uid"]

                                    self._channels.append(cur_channel)

                            self.ensure_channel_uid_unique()
                        else:
                            self._channel_names.clear()

                    for ch_info in cur_status:
                        cur_channel = ch_info["channel"]
                        online = ch_info["online"] == 1
                        self._channel_online[cur_channel] = online
                        if online:
                            if "name" in ch_info and ch_info["name"] not in ["0", "1"]:
                                self._channel_names[cur_channel] = ch_info["name"]
                            if "sleep" in ch_info:
                                self._sleep[cur_channel] = ch_info["sleep"] == 1

                    if not self._GetChannelStatus_present:
                        self._GetChannelStatus_present = True

                    if not self._startup:
                        # check for new devices
                        if data["value"]["count"] != self._nvr_num_channels:
                            _LOGGER.info(
                                "New Reolink device discovered connected to %s, number of channels now %s and was %s",
                                self.nvr_name,
                                data["value"]["count"],
                                self._nvr_num_channels,
                            )
                            self._new_devices = True
                        for ch_info in cur_status:
                            if ch_info["online"] == 1:
                                cur_channel = ch_info["channel"]
                                if cur_channel not in self._channels:
                                    _LOGGER.info(
                                        "New Reolink device discovered connected to %s, new channel %s",
                                        self.nvr_name,
                                        cur_channel,
                                    )
                                    self._new_devices = True
                                    break
                                if "uid" in ch_info:
                                    if not self.camera_uid(cur_channel).startswith(ch_info["uid"]):
                                        _LOGGER.info(
                                            "New Reolink device discovered connected to %s, new UID %s",
                                            self.nvr_name,
                                            ch_info["uid"],
                                        )
                                        self._new_devices = True
                                        break
                                if "typeInfo" in ch_info:  # Not all Reolink devices respond with "typeInfo" attribute.
                                    if self.camera_model(cur_channel) != ch_info["typeInfo"]:
                                        _LOGGER.info(
                                            "New Reolink device discovered connected to %s, new model %s",
                                            self.nvr_name,
                                            ch_info["typeInfo"],
                                        )
                                        self._new_devices = True
                                        break

                    break

            except Exception as err:  # pylint: disable=bare-except
                _LOGGER.error(
                    "Host %s:%s failed mapping JSON data: %s, traceback:\n%s\n",
                    self._host,
                    self._port,
                    str(err),
                    traceback.format_exc(),
                )
                continue

        for data in json_data:
            try:
                if data["code"] == 1:  # Error, like "ability error"
                    continue

                if data["cmd"] == "GetDevInfo":
                    dev_info = data["value"]["DevInfo"]
                    self._is_nvr = dev_info.get("exactType", "IPC") in ["NVR", "WIFI_NVR", "HOMEHUB"]
                    self._is_nvr = self._is_nvr or dev_info.get("type", "IPC") in ["NVR", "WIFI_NVR", "HOMEHUB"]
                    self._is_hub = dev_info.get("exactType", "IPC") == "HOMEHUB" or dev_info.get("type", "IPC") == "HOMEHUB"
                    self._nvr_serial = dev_info["serial"]
                    self._nvr_name = dev_info["name"]
                    self._nvr_model = dev_info["model"]
                    self._nvr_item_number = dev_info.get("itemNo")
                    self._nvr_hw_version = dev_info["hardVer"]
                    self._nvr_sw_version = dev_info["firmVer"]
                    if self._nvr_sw_version is not None:
                        self._nvr_sw_version_object = SoftwareVersion(self._nvr_sw_version)

                    # In case the "GetChannelStatus" command not supported by the device.
                    if not self._GetChannelStatus_present and self._nvr_num_channels == 0:
                        self._channels.clear()

                        self._nvr_num_channels = dev_info["channelNum"]

                        if self._is_nvr:
                            _LOGGER.warning(
                                "Your %s NVR doesn't support the 'Getchannelstatus' command. "
                                "Probably you need to update your firmware.\n"
                                "No way to recognize active channels, all %s channels will be considered 'active' as a result",
                                self._nvr_name,
                                self._nvr_num_channels,
                            )

                        if self._nvr_num_channels > 0:
                            for ch in range(self._nvr_num_channels):
                                self._channels.append(ch)
                                if ch not in self._channel_models and self._nvr_model is not None:
                                    self._channel_models[ch] = self._nvr_model
                                    self._is_doorbell[ch] = "Doorbell" in self._nvr_model

                elif data["cmd"] == "GetHddInfo":
                    self._hdd_info = data["value"]["HddInfo"]

                elif data["cmd"] == "GetLocalLink":
                    self._local_link = data["value"]
                    self._mac_address = data["value"]["LocalLink"]["mac"]

                elif data["cmd"] == "GetWifiSignal":
                    self._wifi_signal = data["value"]["wifiSignal"]

                elif data["cmd"] == "GetPerformance":
                    self._performance = data["value"]["Performance"]

                elif data["cmd"] == "GetStateLight":
                    self._state_light = data["value"]["stateLight"]

                elif data["cmd"] == "GetNetPort":
                    self._netport_settings = data["value"]
                    net_port = data["value"]["NetPort"]
                    self._rtsp_port = net_port.get("rtspPort", 554)
                    self._rtmp_port = net_port.get("rtmpPort", 1935)
                    self._onvif_port = net_port.get("onvifPort", 8000)
                    self._rtsp_enabled = net_port.get("rtspEnable", 1) == 1
                    self._rtmp_enabled = net_port.get("rtmpEnable", 1) == 1
                    self._onvif_enabled = net_port.get("onvifEnable", 1) == 1
                    self._subscribe_url = f"http://{self._host}:{self._onvif_port}/onvif/event_service"

                elif data["cmd"] == "GetP2p":
                    self._nvr_uid = data["value"]["P2p"]["uid"]

                elif data["cmd"] == "GetUser":
                    self._users = data["value"]["User"]

                elif data["cmd"] == "GetNtp":
                    self._ntp_settings = data["value"]

                elif data["cmd"] == "GetTime":
                    time_diffrence = (datetime.now() - reolink_time_to_datetime(data["value"]["Time"])).total_seconds()
                    if abs(time_diffrence) < 10:
                        time_diffrence = 0
                    self._host_time_difference = time_diffrence
                    self._time_settings = data["value"]

                elif data["cmd"] == "GetPushCfg":
                    self._push_config = data["value"]

                elif data["cmd"] == "GetAbility":
                    self._abilities = data["value"]["Ability"]

            except Exception as err:  # pylint: disable=bare-except
                _LOGGER.error(
                    "Host %s:%s failed mapping JSON data: %s, traceback:\n%s\n",
                    self._host,
                    self._port,
                    str(err),
                    traceback.format_exc(),
                )
                continue

    def map_channels_json_response(self, json_data, channels: list[int], chime_ids: list[int] | None = None):
        if len(json_data) != len(channels):
            raise UnexpectedDataError(
                f"Host {self._host}:{self._port} error mapping response to channels, received {len(json_data)} responses while requesting {len(channels)} responses",
            )

        if not chime_ids:
            chime_ids = [-1] * len(channels)
        elif len(channels) != len(chime_ids):
            raise UnexpectedDataError(
                f"Host {self._host}:{self._port} error mapping response to channels, channel length {len(channels)} != chime id length {len(chime_ids)}",
            )

        for data, channel, chime_id in zip(json_data, channels, chime_ids):
            if channel == -1:
                self.map_host_json_response([data])
                continue

            self.map_channel_json_response([data], channel, chime_id)

    def map_channel_json_response(self, json_data, channel: int, chime_id: int = -1):
        """Map the JSON objects to internal cache-objects."""
        response_channel = channel
        for data in json_data:
            try:
                if data["code"] == 1:  # -->Error, like "ability error"
                    _LOGGER.debug("Host %s:%s received response error code: %s", self._host, self._port, data)
                    continue

                if data["cmd"] == "GetChnTypeInfo":
                    self._channel_models[channel] = data["value"]["typeInfo"]
                    self._is_doorbell[channel] = "Doorbell" in self._channel_models[channel]
                    if "firmVer" in data["value"]:
                        self._channel_sw_versions[channel] = data["value"]["firmVer"]
                        if self._channel_sw_versions[channel] is not None:
                            self._channel_sw_version_objects[channel] = SoftwareVersion(self._channel_sw_versions[channel])
                    if "boardInfo" in data["value"]:
                        self._channel_hw_version[channel] = data["value"]["boardInfo"]

                if data["cmd"] == "GetEvents":
                    response_channel = data["value"]["channel"]
                    if response_channel != channel:
                        _LOGGER.error("Host %s:%s: GetEvents response channel %s does not equal requested channel %s", self._host, self._port, response_channel, channel)
                        continue
                    if "ai" in data["value"]:
                        self._ai_detection_states[channel] = {}
                        self._ai_detection_support[channel] = {}
                        for key, value in data["value"]["ai"].items():
                            supported: bool = value.get("support", 0) == 1
                            self._ai_detection_states[channel][key] = supported and value.get("alarm_state", 0) == 1
                            self._ai_detection_support[channel][key] = supported
                        if "other" in data["value"]["ai"]:  # Battery cams use PIR detection with the "other" item
                            if data["value"]["ai"]["other"].get("support", 0) == 1:
                                self._motion_detection_states[channel] = data["value"]["ai"]["other"]["alarm_state"] == 1
                    if "md" in data["value"]:
                        if data["value"]["md"].get("support", 1) == 1:  # Battery cams use PIR detection with the "other" item
                            self._motion_detection_states[channel] = data["value"]["md"]["alarm_state"] == 1
                    if "visitor" in data["value"]:
                        value = data["value"]["visitor"]
                        supported = value.get("support", 0) == 1
                        self._visitor_states[channel] = supported and value.get("alarm_state", 0) == 1
                        self._is_doorbell[channel] = supported

                elif data["cmd"] == "GetMdState":
                    self._motion_detection_states[channel] = data["value"]["state"] == 1

                elif data["cmd"] == "GetAlarm":
                    self._md_alarm_settings[channel] = data["value"]

                elif data["cmd"] == "GetMdAlarm":
                    self._md_alarm_settings[channel] = data["value"]

                elif data["cmd"] == "GetAiAlarm":
                    ai_type = data["value"]["AiAlarm"]["ai_type"]
                    if channel not in self._ai_alarm_settings:
                        self._ai_alarm_settings[channel] = {}
                    self._ai_alarm_settings[channel][ai_type] = data["value"]["AiAlarm"]

                elif data["cmd"] == "GetAiState":
                    self._ai_detection_states[channel] = {}
                    self._ai_detection_support[channel] = {}
                    response_channel = data["value"].get("channel", channel)
                    if response_channel != channel:
                        _LOGGER.error("Host %s:%s: GetAiState response channel %s does not equal requested channel %s", self._host, self._port, response_channel, channel)
                        continue

                    for key, value in data["value"].items():
                        if key == "channel":
                            continue

                        if isinstance(value, int):  # compatibility with firmware < 3.0.0-494
                            self._ai_detection_states[channel][key] = value == 1
                            self._ai_detection_support[channel][key] = True
                        else:
                            # from firmware 3.0.0.0-494 there is a new json structure:
                            # [
                            #     {
                            #         "cmd" : "GetAiState",
                            #         "code" : 0,
                            #         "value" : {
                            #             "channel" : 0,
                            #             "face" : {
                            #                 "alarm_state" : 0,
                            #                 "support" : 0
                            #             },
                            #             "people" : {
                            #                 "alarm_state" : 0,
                            #                 "support" : 1
                            #             },
                            #             "vehicle" : {
                            #                 "alarm_state" : 0,
                            #                 "support" : 1
                            #             }
                            #         }
                            #     }
                            # ]
                            supported = value.get("support", 0) == 1
                            self._ai_detection_states[channel][key] = supported and value.get("alarm_state", 0) == 1
                            self._ai_detection_support[channel][key] = supported

                elif data["cmd"] == "GetOsd":
                    response_channel = data["value"]["Osd"]["channel"]
                    self._osd_settings[channel] = data["value"]
                    if not self._GetChannelStatus_present or not self._GetChannelStatus_has_name:
                        self._channel_names[channel] = data["value"]["Osd"]["osdChannel"]["name"]

                elif data["cmd"] == "GetFtp":
                    self._ftp_settings[channel] = data["value"]

                elif data["cmd"] == "GetFtpV20":
                    self._ftp_settings[channel] = data["value"]

                elif data["cmd"] == "GetPush":
                    self._push_settings[channel] = data["value"]

                elif data["cmd"] == "GetPushV20":
                    self._push_settings[channel] = data["value"]

                elif data["cmd"] == "GetWebHook":
                    self._webhook_settings[channel] = data["value"]

                elif data["cmd"] == "GetEnc":
                    # GetEnc returns incorrect channel for DUO camera
                    # response_channel = data["value"]["Enc"]["channel"]
                    self._enc_settings[channel] = data["value"]

                elif data["cmd"] == "GetRtspUrl":
                    response_channel = data["value"]["rtspUrl"]["channel"]
                    mainStream = data["value"]["rtspUrl"]["mainStream"]
                    subStream = data["value"]["rtspUrl"]["subStream"]
                    self._rtsp_mainStream[channel] = mainStream.replace("rtsp://", f"rtsp://{self._username}:{self._enc_password}@")
                    self._rtsp_subStream[channel] = subStream.replace("rtsp://", f"rtsp://{self._username}:{self._enc_password}@")

                elif data["cmd"] == "GetEmail":
                    self._email_settings[channel] = data["value"]

                elif data["cmd"] == "GetEmailV20":
                    self._email_settings[channel] = data["value"]

                elif data["cmd"] == "GetBuzzerAlarmV20":
                    self._buzzer_settings[channel] = data["value"]

                elif data["cmd"] == "GetIsp":
                    response_channel = data["value"]["Isp"]["channel"]
                    self._isp_settings[channel] = data["value"]

                elif data["cmd"] == "GetImage":
                    self._image_settings[channel] = data["value"]

                elif data["cmd"] == "GetIrLights":
                    self._ir_settings[channel] = data["value"]

                elif data["cmd"] == "GetPowerLed":
                    # GetPowerLed returns incorrect channel
                    # response_channel = data["value"]["PowerLed"]["channel"]
                    self._status_led_settings[channel] = data["value"]

                elif data["cmd"] == "GetWhiteLed":
                    response_channel = data["value"]["WhiteLed"]["channel"]
                    self._whiteled_settings[channel] = data["value"]

                elif data["cmd"] == "GetBatteryInfo":
                    self._battery[channel] = data["value"]["Battery"]

                elif data["cmd"] == "GetPirInfo":
                    self._pir[channel] = data["value"]["pirInfo"]

                elif data["cmd"] == "GetRec":
                    self._recording_settings[channel] = data["value"]

                elif data["cmd"] == "GetRecV20":
                    self._recording_settings[channel] = data["value"]

                elif data["cmd"] == "GetManualRec":
                    self._manual_record_settings[channel] = data["value"]

                elif data["cmd"] == "GetPtzPreset":
                    self._ptz_presets_settings[channel] = data["value"]
                    self._ptz_presets[channel] = {}
                    for preset in data["value"]["PtzPreset"]:
                        if int(preset["enable"]) == 1:
                            preset_name = preset["name"]
                            preset_id = int(preset["id"])
                            self._ptz_presets[channel][preset_name] = preset_id

                elif data["cmd"] == "GetPtzPatrol":
                    self._ptz_patrol_settings[channel] = data["value"]
                    self._ptz_patrols[channel] = {}
                    for patrol in data["value"]["PtzPatrol"]:
                        if int(patrol["enable"]) == 1:
                            patrol_name = patrol.get("name", f"patrol {patrol['id']}")
                            patrol_id = int(patrol["id"])
                            self._ptz_patrols[channel][patrol_name] = patrol_id

                elif data["cmd"] == "GetPtzGuard":
                    self._ptz_guard_settings[channel] = data["value"]

                elif data["cmd"] == "GetPtzCurPos":
                    self._ptz_position[channel] = data["value"]

                elif data["cmd"] == "GetAiCfg":
                    self._auto_track_settings[channel] = data["value"]
                    if "range" in data:
                        self._auto_track_range[channel] = data["range"]

                elif data["cmd"] == "GetPtzTraceSection":
                    self._auto_track_limits[channel] = data["value"]

                elif data["cmd"] == "GetAudioCfg":
                    self._audio_settings[channel] = data["value"]

                elif data["cmd"] == "GetDeviceAudioCfg":
                    self._hub_audio_settings[channel] = data["value"]

                elif data["cmd"] == "GetAudioAlarm":
                    self._audio_alarm_settings[channel] = data["value"]

                elif data["cmd"] == "GetAudioAlarmV20":
                    self._audio_alarm_settings[channel] = data["value"]

                elif data["cmd"] == "GetAudioFileList":
                    self._audio_file_list[channel] = data["value"]

                elif data["cmd"] == "GetDingDongList":
                    self._GetDingDong_present[channel] = True
                    id_list = []
                    for dev in data["value"]["DingDongList"].get("pairedlist", {}):
                        chime_id = dev["deviceId"]
                        id_list.append(chime_id)
                        if chime_id not in self._chime_list:
                            self._chime_list[chime_id] = Chime(
                                host=self,
                                dev_id=chime_id,
                                channel=channel,
                            )
                            if not self._startup:
                                _LOGGER.info(
                                    "New Reolink chime discovered connected to %s, new chime ID %s",
                                    self.nvr_name,
                                    chime_id,
                                )
                                self._new_devices = True
                        chime = self._chime_list[chime_id]
                        if dev["deviceName"]:
                            chime.name = dev["deviceName"]
                        chime.connect_state = dev["netState"]

                    for dev_id, chime in self._chime_list.items():
                        if dev_id not in id_list:
                            chime.connect_state = -1

                elif data["cmd"] == "GetDingDongCfg":
                    self._GetDingDong_present[channel] = True
                    for dev in data["value"]["DingDongCfg"]["pairedlist"]:
                        chime_id = dev["ringId"]
                        if chime_id < 0:
                            continue
                        if chime_id not in self._chime_list:
                            self._chime_list[chime_id] = Chime(
                                host=self,
                                dev_id=chime_id,
                                channel=channel,
                            )
                            if not self._startup:
                                _LOGGER.info(
                                    "New Reolink chime discovered connected to %s, new chime ID %s",
                                    self.nvr_name,
                                    chime_id,
                                )
                                self._new_devices = True
                        chime = self._chime_list[chime_id]
                        chime.name = dev["ringName"]
                        chime.event_info = dev["type"]

                elif data["cmd"] == "DingDongOpt":
                    if chime_id not in self._chime_list:
                        continue
                    chime = self._chime_list[chime_id]
                    value = data["value"]["DingDong"]
                    chime.name = value["name"]
                    chime.led_state = value["ledState"] == 1
                    chime.volume = value["volLevel"]

                elif data["cmd"] == "GetAutoReply":
                    self._auto_reply_settings[channel] = data["value"]

                elif data["cmd"] == "GetAutoFocus":
                    self._auto_focus_settings[channel] = data["value"]

                elif data["cmd"] == "GetZoomFocus":
                    self._zoom_focus_settings[channel] = data["value"]
                    if "range" in data:
                        self._zoom_focus_range[channel] = data["range"]["ZoomFocus"]

            except Exception as err:
                _LOGGER.error(
                    "Host %s:%s (channel %s) failed mapping JSON data: %s, traceback:\n%s\n",
                    self._host,
                    self._port,
                    channel,
                    str(err),
                    traceback.format_exc(),
                )
                continue
            if response_channel != channel:
                _LOGGER.error("Host %s:%s: command %s response channel %s does not equal requested channel %s", self._host, self._port, data["cmd"], response_channel, channel)

    async def set_net_port(
        self,
        enable_onvif: bool | None = None,
        enable_rtmp: bool | None = None,
        enable_rtsp: bool | None = None,
    ) -> None:
        """Set Network Port parameters on the host (NVR or camera)."""
        if self._netport_settings is None:
            await self.get_state("GetNetPort")

        if self._netport_settings is None:
            raise NotSupportedError(f"set_net_port: failed to retrieve current NetPort settings from {self._host}:{self._port}")

        body: typings.reolink_json = [{"cmd": "SetNetPort", "param": self._netport_settings}]

        if enable_onvif is not None:
            body[0]["param"]["NetPort"]["onvifEnable"] = 1 if enable_onvif else 0
        if enable_rtmp is not None:
            body[0]["param"]["NetPort"]["rtmpEnable"] = 1 if enable_rtmp else 0
        if enable_rtsp is not None:
            body[0]["param"]["NetPort"]["rtspEnable"] = 1 if enable_rtsp else 0

        await self.send_setting(body)
        await self.expire_session()  # When changing network port settings, tokens are invalidated.

    async def set_time(self, dateFmt=None, hours24=None, tzOffset=None) -> None:
        """Set time on the host (NVR or camera).
        Arguments:
        dateFmt (string) Format of the date in the OSD timestamp
        hours24 (boolean) True selects 24h format, False selects 12h format
        tzoffset (int) Timezone offset versus UTC in seconds

        Always get current time first"""
        await self.get_state("GetTime")
        if self._time_settings is None:
            raise NotSupportedError(f"set_time: failed to retrieve current time settings from {self._host}:{self._port}")

        body: typings.reolink_json = [{"cmd": "SetTime", "action": 0, "param": self._time_settings}]

        if dateFmt is not None:
            if dateFmt in ["DD/MM/YYYY", "MM/DD/YYYY", "YYYY/MM/DD"]:
                body[0]["param"]["Time"]["timeFmt"] = dateFmt
            else:
                raise InvalidParameterError(f"set_time: date format {dateFmt} not in ['DD/MM/YYYY', 'MM/DD/YYYY', 'YYYY/MM/DD']")

        if hours24 is not None:
            if hours24:
                body[0]["param"]["Time"]["hourFmt"] = 0
            else:
                body[0]["param"]["Time"]["hourFmt"] = 1

        if tzOffset is not None:
            if not isinstance(tzOffset, int):
                raise InvalidParameterError(f"set_time: time zone offset {tzOffset} is not integer")
            if tzOffset < -43200 or tzOffset > 50400:
                raise InvalidParameterError(f"set_time: time zone offset {tzOffset} not in range -43200..50400")
            body[0]["param"]["Time"]["timeZone"] = tzOffset

        await self.send_setting(body)

    async def set_ntp(self, enable: bool | None = None, server: str | None = None, port: int | None = None, interval: int | None = None) -> None:
        """
        Set NTP parameters on the host (NVR or camera).
        Arguments:
        enable (boolean) Enable synchronization
        server (string) Name or IP-Address of time server (or pool)
        port (int) Port number in range of (1..65535)
        interval (int) Interval of synchronization in minutes in range of (60-65535)
        """
        if self._ntp_settings is None:
            await self.get_state("GetNtp")

        if self._ntp_settings is None:
            raise NotSupportedError(f"set_ntp: failed to retrieve current NTP settings from {self._host}:{self._port}")

        body: typings.reolink_json = [{"cmd": "SetNtp", "action": 0, "param": self._ntp_settings}]

        if enable is not None:
            if enable:
                body[0]["param"]["Ntp"]["enable"] = 1
            else:
                body[0]["param"]["Ntp"]["enable"] = 0

        if server is not None:
            body[0]["param"]["Ntp"]["server"] = server

        if port is not None:
            if not isinstance(port, int):
                raise InvalidParameterError(f"set_ntp: Invalid NTP port {port} specified, type is not integer")
            if port < 1 or port > 65535:
                raise InvalidParameterError(f"set_ntp: Invalid NTP port {port} specified, out of valid range 1...65535")
            body[0]["param"]["Ntp"]["port"] = port

        if interval is not None:
            if not isinstance(interval, int):
                raise InvalidParameterError(f"set_ntp: Invalid NTP interval {interval} specified, type is not integer")
            if interval < 60 or interval > 65535:
                raise InvalidParameterError(f"set_ntp: Invalid NTP interval {interval} specified, out of valid range 60...65535")
            body[0]["param"]["Ntp"]["interval"] = interval

        await self.send_setting(body)

    async def sync_ntp(self) -> None:
        """Sync date and time on the host via NTP now."""
        if self._ntp_settings is None:
            await self.get_state("GetNtp")

        if self._ntp_settings is None:
            raise NotSupportedError(f"set_ntp: failed to retrieve current NTP settings from {self._host}:{self._port}")

        body: typings.reolink_json = [{"cmd": "SetNtp", "action": 0, "param": self._ntp_settings}]
        body[0]["param"]["Ntp"]["interval"] = 0

        await self.send_setting(body)

    async def set_state_light(self, enable: bool) -> None:
        """Set the state light of the NVR/Hub."""
        if not self.supported(None, "state_light"):
            raise NotSupportedError(f"set_state_light: not supported by {self.nvr_name}")

        body: typings.reolink_json = [{"cmd": "SetStateLight", "param": {"stateLight": {"enable": 1 if enable else 0}}}]
        await self.send_setting(body)

    def get_focus(self, channel: int) -> int:
        """Get absolute focus value."""
        if channel not in self._channels:
            raise InvalidParameterError(f"get_focus: no camera connected to channel '{channel}'")
        if channel not in self._zoom_focus_settings or not self._zoom_focus_settings[channel]:
            raise NotSupportedError(f"get_focus: ZoomFocus on camera {self.camera_name(channel)} is not available")

        return self._zoom_focus_settings[channel]["ZoomFocus"]["focus"]["pos"]

    async def set_focus(self, channel: int, focus: int) -> None:
        """Set absolute focus value.
        Parameters:
        focus (int) 0..223"""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_focus: no camera connected to channel '{channel}'")
        if not self.supported(channel, "focus"):
            raise NotSupportedError(f"set_focus: not supported by camera {self.camera_name(channel)}")
        min_focus = self.zoom_range(channel)["focus"]["pos"]["min"]
        max_focus = self.zoom_range(channel)["focus"]["pos"]["max"]
        if not isinstance(focus, int):
            raise InvalidParameterError(f"set_focus: focus value {focus} not integer")
        if focus not in range(min_focus, max_focus + 1):
            raise InvalidParameterError(f"set_focus: focus value {focus} not in range {min_focus}..{max_focus}")

        body = [
            {
                "cmd": "StartZoomFocus",
                "action": 0,
                "param": {"ZoomFocus": {"channel": channel, "op": "FocusPos", "pos": focus}},
            }
        ]

        await self.send_setting(body)
        await asyncio.sleep(3)
        await self.get_state(cmd="GetZoomFocus")

    def autofocus_enabled(self, channel: int) -> bool:
        """Auto focus enabled."""
        if channel not in self._auto_focus_settings:
            return True

        return self._auto_focus_settings[channel]["AutoFocus"]["disable"] == 0

    async def set_autofocus(self, channel: int, enable: bool) -> None:
        """Enable/Disable AutoFocus on a camera."""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_autofocus: no camera connected to channel '{channel}'")
        if channel not in self._auto_focus_settings:
            raise NotSupportedError(f"set_autofocus: AutoFocus on camera {self.camera_name(channel)} is not available")

        body: typings.reolink_json = [{"cmd": "SetAutoFocus", "action": 0, "param": {"AutoFocus": {"disable": 0 if enable else 1, "channel": channel}}}]
        await self.send_setting(body)

    def get_zoom(self, channel: int) -> int:
        """Get absolute zoom value."""
        if channel not in self._channels:
            raise InvalidParameterError(f"get_zoom: no camera connected to channel '{channel}'")
        if channel not in self._zoom_focus_settings or not self._zoom_focus_settings[channel]:
            raise NotSupportedError(f"get_zoom: ZoomFocus on camera {self.camera_name(channel)} is not available")

        return self._zoom_focus_settings[channel]["ZoomFocus"]["zoom"]["pos"]

    async def set_zoom(self, channel: int, zoom: int) -> None:
        """Set absolute zoom value.
        Parameters:
        zoom (int) 0..33"""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_zoom: no camera connected to channel '{channel}'")
        if not self.supported(channel, "zoom"):
            raise NotSupportedError(f"set_zoom: not supported by camera {self.camera_name(channel)}")
        min_zoom = self.zoom_range(channel)["zoom"]["pos"]["min"]
        max_zoom = self.zoom_range(channel)["zoom"]["pos"]["max"]
        if not isinstance(zoom, int):
            raise InvalidParameterError(f"set_zoom: zoom value {zoom} not integer")
        if zoom not in range(min_zoom, max_zoom + 1):
            raise InvalidParameterError(f"set_zoom: zoom value {zoom} not in range {min_zoom}..{max_zoom}")

        body = [
            {
                "cmd": "StartZoomFocus",
                "action": 0,
                "param": {"ZoomFocus": {"channel": channel, "op": "ZoomPos", "pos": zoom}},
            }
        ]

        await self.send_setting(body)
        await asyncio.sleep(3)
        await self.get_state(cmd="GetZoomFocus")

    def ptz_presets(self, channel: int) -> dict:
        if channel not in self._ptz_presets:
            return {}

        return self._ptz_presets[channel]

    def ptz_patrols(self, channel: int) -> dict:
        if channel not in self._ptz_patrols:
            return {}

        return self._ptz_patrols[channel]

    async def ctrl_ptz_patrol(self, channel: int, value: bool) -> None:
        """Start/Stop PTZ patrol."""
        if not self.supported(channel, "ptz_patrol"):
            raise NotSupportedError(f"ctrl_ptz_patrol: ptz patrol on camera {self.camera_name(channel)} is not available")

        patrol = list(self.ptz_patrols(channel).values())[0]
        if value:
            cmd = "StartPatrol"
        else:
            cmd = "StopPatrol"

        await self.set_ptz_command(channel, command=cmd, patrol=patrol)

    async def set_ptz_command(self, channel: int, command: str | None = None, preset: int | str | None = None, speed: int | None = None, patrol: int | None = None) -> None:
        """Send PTZ command to the camera, list of possible commands see PtzEnum."""

        if channel not in self._channels:
            raise InvalidParameterError(f"set_ptz_command: no camera connected to channel '{channel}'")
        if speed is not None and not isinstance(speed, int):
            raise InvalidParameterError(f"set_ptz_command: speed {speed} is not integer")
        if speed is not None and not self.supported(channel, "ptz_speed"):
            raise NotSupportedError(f"set_ptz_command: ptz speed on camera {self.camera_name(channel)} is not available")
        command_list = [com.value for com in PtzEnum]
        if command is not None and command not in command_list and patrol is None:
            raise InvalidParameterError(f"set_ptz_command: command {command} not in {command_list}")

        if preset is not None:
            command = "ToPos"
            if isinstance(preset, str):
                if preset not in self.ptz_presets(channel):
                    raise InvalidParameterError(f"set_ptz_command: preset '{preset}' not in available presets {list(self.ptz_presets(channel).keys())}")
                preset = self.ptz_presets(channel)[preset]
            if not isinstance(preset, int):
                raise InvalidParameterError(f"set_ptz_command: preset {preset} is not integer")

        if command is None:
            raise InvalidParameterError("set_ptz_command: No command or preset specified.")

        body: typings.reolink_json = [
            {
                "cmd": "PtzCtrl",
                "action": 0,
                "param": {"channel": channel, "op": command},
            }
        ]

        if speed:
            body[0]["param"]["speed"] = speed
        if preset:
            body[0]["param"]["id"] = preset
        if patrol:
            body[0]["param"]["id"] = patrol

        await self.send_setting(body)

    def ptz_pan_position(self, channel: int) -> int | None:
        """pan position"""
        return self._ptz_position.get(channel, {}).get("PtzCurPos", {}).get("Ppos")

    def ptz_tilt_position(self, channel: int) -> int | None:
        """tilt position"""
        return self._ptz_position.get(channel, {}).get("PtzCurPos", {}).get("Tpos")

    def ptz_guard_enabled(self, channel: int) -> bool:
        if channel not in self._ptz_guard_settings:
            return False

        values = self._ptz_guard_settings[channel]["PtzGuard"]
        return values["benable"] == 1 and values["bexistPos"] == 1

    def ptz_guard_time(self, channel: int) -> int:
        """Guard point return time in seconds"""
        if channel not in self._ptz_guard_settings:
            return 60

        return self._ptz_guard_settings[channel]["PtzGuard"]["timeout"]

    async def set_ptz_guard(self, channel: int, command: str | None = None, enable: bool | None = None, time: int | None = None) -> None:
        """Send PTZ guard."""

        if channel not in self._channels:
            raise InvalidParameterError(f"set_ptz_guard: no camera connected to channel '{channel}'")
        if time is not None and not isinstance(time, int):
            raise InvalidParameterError(f"set_ptz_guard: guard time {time} is not integer")
        command_list = [com.value for com in GuardEnum]
        if command is not None and command not in command_list:
            raise InvalidParameterError(f"set_ptz_guard: command {command} not in {command_list}")

        params: dict[str, Any] = {"channel": channel}
        if command is not None:
            params["cmdStr"] = command
            if command == "setPos":
                params["bSaveCurrentPos"] = 1
        if command is None:
            params["cmdStr"] = "setPos"
        if enable is not None:
            params["benable"] = 1 if enable else 0
        if time is not None:
            params["timeout"] = time

        body: typings.reolink_json = [{"cmd": "SetPtzGuard", "action": 0, "param": {"PtzGuard": params}}]
        await self.send_setting(body)

    async def ptz_callibrate(self, channel: int) -> None:
        """Callibrate PTZ of the camera."""
        if channel not in self._channels:
            raise InvalidParameterError(f"ptz_callibrate: no camera connected to channel '{channel}'")

        body: typings.reolink_json = [{"cmd": "PtzCheck", "action": 0, "param": {"channel": channel}}]
        await self.send_setting(body)

    def auto_track_enabled(self, channel: int) -> bool:
        if channel not in self._auto_track_settings:
            return False

        if "bSmartTrack" in self._auto_track_settings[channel]:
            return self._auto_track_settings[channel]["bSmartTrack"] == 1

        return self._auto_track_settings[channel]["aiTrack"] == 1

    def auto_track_disappear_time(self, channel: int) -> int:
        if channel not in self._auto_track_settings:
            return -1

        return self._auto_track_settings[channel].get("aiDisappearBackTime", -1)

    def auto_track_stop_time(self, channel: int) -> int:
        if channel not in self._auto_track_settings:
            return -1

        return self._auto_track_settings[channel].get("aiStopBackTime", -1)

    def auto_track_method(self, channel: int) -> Optional[int]:
        if channel not in self._auto_track_settings:
            return None

        return self._auto_track_settings[channel].get("aiTrack")

    async def set_auto_tracking(
        self, channel: int, enable: bool | None = None, disappear_time: int | None = None, stop_time: int | None = None, method: int | str | None = None
    ) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_auto_tracking: no camera connected to channel '{channel}'")
        if not self.supported(channel, "auto_track"):
            raise NotSupportedError(f"set_auto_tracking: Auto tracking on camera {self.camera_name(channel)} is not available")

        params = {"channel": channel}
        if enable is not None:
            if "bSmartTrack" in self._auto_track_settings[channel]:
                params["bSmartTrack"] = 1 if enable else 0
            else:
                params["aiTrack"] = 1 if enable else 0
        if disappear_time is not None:
            params["aiDisappearBackTime"] = disappear_time
        if stop_time is not None:
            params["aiStopBackTime"] = stop_time
        if method is not None:
            if isinstance(method, str):
                method_int = TrackMethodEnum[method].value
            else:
                method_int = method
            params["aiTrack"] = method_int
            method_list = [val.value for val in TrackMethodEnum]
            if method_int not in method_list:
                raise InvalidParameterError(f"set_auto_tracking: method {method_int} not in {method_list}")

        body = [{"cmd": "SetAiCfg", "action": 0, "param": params}]
        await self.send_setting(body)

    def auto_track_limit_left(self, channel: int) -> int:
        """-1 = limit not set"""
        if channel not in self._auto_track_limits:
            return -1

        return self._auto_track_limits[channel]["PtzTraceSection"]["LimitLeft"]

    def auto_track_limit_right(self, channel: int) -> int:
        """-1 = limit not set"""
        if channel not in self._auto_track_limits:
            return -1

        return self._auto_track_limits[channel]["PtzTraceSection"]["LimitRight"]

    async def set_auto_track_limit(self, channel: int, left: int | None = None, right: int | None = None) -> None:
        """-1 = disable limit"""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_auto_track_limit: no camera connected to channel '{channel}'")
        if not self.supported(channel, "auto_track_limit"):
            raise NotSupportedError(f"set_auto_track_limit: Auto track limits on camera {self.camera_name(channel)} is not available")
        if left is None and right is None:
            raise InvalidParameterError("set_auto_track_limit: either left or right limit needs to be specified")
        if left is not None and (left < -1 or left > 2700):
            raise InvalidParameterError(f"set_auto_track_limit: left limit {left} not in range -1...2700")
        if right is not None and (right < -1 or right > 2700):
            raise InvalidParameterError(f"set_auto_track_limit: right limit {right} not in range -1...2700")

        params = {"channel": channel}
        if left is not None:
            params["LimitLeft"] = left
        if right is not None:
            params["LimitRight"] = right

        body = [{"cmd": "SetPtzTraceSection", "action": 0, "param": {"PtzTraceSection": params}}]
        await self.send_setting(body)

    def validate_osd_pos(self, pos) -> bool:
        """Helper function for validating an OSD position
        Returns True, if a valid position is specified"""
        return pos in [
            "Upper Left",
            "Upper Right",
            "Top Center",
            "Bottom Center",
            "Lower Left",
            "Lower Right",
        ]

    async def set_osd(self, channel: int, namePos=None, datePos=None, enableWaterMark=None) -> None:
        """Set OSD parameters.
        Parameters:
        namePos (string) specifies the position of the camera name - "Off" disables this OSD
        datePos (string) specifies the position of the date - "Off" disables this OSD
        enableWaterMark (boolean) enables/disables the Logo (WaterMark) if supported"""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_osd: no camera connected to channel '{channel}'")

        await self.get_state("GetOsd")

        if channel not in self._osd_settings or not self._osd_settings[channel]:
            raise NotSupportedError(f"set_osd: OSD on camera {self.camera_name(channel)} is not available")

        body: typings.reolink_json = [{"cmd": "SetOsd", "action": 0, "param": self._osd_settings[channel]}]

        if namePos is not None:
            if namePos == "Off":
                body[0]["param"]["Osd"]["osdChannel"]["enable"] = 0
            else:
                if not self.validate_osd_pos(namePos):
                    raise InvalidParameterError(f"set_osd: Invalid name OSD position specified '{namePos}'")
                body[0]["param"]["Osd"]["osdChannel"]["enable"] = 1
                body[0]["param"]["Osd"]["osdChannel"]["pos"] = namePos

        if datePos is not None:
            if datePos == "Off":
                body[0]["param"]["Osd"]["osdTime"]["enable"] = 0
            else:
                if not self.validate_osd_pos(datePos):
                    raise InvalidParameterError(f"set_osd: Invalid date OSD position specified '{datePos}'")
                body[0]["param"]["Osd"]["osdTime"]["enable"] = 1
                body[0]["param"]["Osd"]["osdTime"]["pos"] = datePos

        if enableWaterMark is not None:
            if "watermark" in body[0]["param"]["Osd"]:
                if enableWaterMark:
                    body[0]["param"]["Osd"]["watermark"] = 1
                else:
                    body[0]["param"]["Osd"]["watermark"] = 0
            else:
                _LOGGER.debug(
                    'Ignoring "enable watermark" request. Not supported by camera %s.',
                    self.camera_name(channel),
                )

        await self.send_setting(body)

    async def set_push(self, channel: int | None, enable: bool) -> None:
        """Set the Push-notifications parameter."""
        if not self.supported(channel, "push"):
            raise NotSupportedError(f"set_push: push-notifications on camera {self.camera_name(channel)} are not available")

        body: typings.reolink_json
        on_off = 1 if enable else 0
        if channel is None:
            if self.api_version("GetPush") >= 1:
                body = [{"cmd": "SetPushV20", "action": 0, "param": {"Push": {"enable": on_off}}}]
                await self.send_setting(body)
                if self.supported(None, "push_config"):
                    await self.get_state(cmd="GetPushCfg")
                return

            for ch in self._channels:
                if self.supported(ch, "push"):
                    body = [{"cmd": "SetPush", "action": 0, "param": {"Push": {"schedule": {"enable": on_off, "channel": ch}}}}]
                    await self.send_setting(body)
            if self.supported(None, "push_config"):
                await self.get_state(cmd="GetPushCfg")
            return

        if channel not in self._channels:
            raise InvalidParameterError(f"set_push: no camera connected to channel '{channel}'")

        if self.api_version("GetPush") >= 1:
            body = [{"cmd": "SetPushV20", "action": 0, "param": {"Push": {"scheduleEnable": on_off, "schedule": {"channel": channel}}}}]
        else:
            body = [{"cmd": "SetPush", "action": 0, "param": {"Push": {"schedule": {"enable": on_off, "channel": channel}}}}]

        await self.send_setting(body)

    async def set_ftp(self, channel: int | None, enable: bool) -> None:
        """Set the FTP-notifications parameter."""
        if not self.supported(channel, "ftp"):
            raise NotSupportedError(f"set_ftp: FTP on camera {self.camera_name(channel)} is not available")

        body: typings.reolink_json
        on_off = 1 if enable else 0
        if channel is None:
            if self.api_version("GetFtp") >= 1:
                body = [{"cmd": "SetFtpV20", "action": 0, "param": {"Ftp": {"enable": on_off}}}]
                await self.send_setting(body)
                return

            for ch in self._channels:
                if self.supported(ch, "ftp"):
                    body = [{"cmd": "SetFtp", "action": 0, "param": {"Ftp": {"schedule": {"enable": on_off, "channel": ch}}}}]
                    await self.send_setting(body)
            return

        if channel not in self._channels:
            raise InvalidParameterError(f"set_ftp: no camera connected to channel '{channel}'")

        if self.api_version("GetFtp") >= 1:
            body = [{"cmd": "SetFtpV20", "action": 0, "param": {"Ftp": {"scheduleEnable": on_off, "schedule": {"channel": channel}}}}]
        else:
            body = [{"cmd": "SetFtp", "action": 0, "param": {"Ftp": {"schedule": {"enable": on_off, "channel": channel}}}}]

        await self.send_setting(body)

    async def set_email(self, channel: int | None, enable: bool) -> None:
        if not self.supported(channel, "email"):
            raise NotSupportedError(f"set_email: Email on camera {self.camera_name(channel)} is not available")

        body: typings.reolink_json
        on_off = 1 if enable else 0
        if channel is None:
            if self.api_version("GetEmail") >= 1:
                body = [{"cmd": "SetEmailV20", "action": 0, "param": {"Email": {"enable": on_off}}}]
                await self.send_setting(body)
                return

            for ch in self._channels:
                if self.supported(ch, "email"):
                    body = [{"cmd": "SetEmail", "action": 0, "param": {"Email": {"schedule": {"enable": on_off, "channel": ch}}}}]
                    await self.send_setting(body)
            return

        if channel not in self._channels:
            raise InvalidParameterError(f"set_email: no camera connected to channel '{channel}'")

        if self.api_version("GetEmail") >= 1:
            body = [{"cmd": "SetEmailV20", "action": 0, "param": {"Email": {"scheduleEnable": on_off, "schedule": {"channel": channel}}}}]
        else:
            body = [{"cmd": "SetEmail", "action": 0, "param": {"Email": {"schedule": {"enable": on_off, "channel": channel}}}}]

        await self.send_setting(body)

    async def set_recording(self, channel: int | None, enable: bool) -> None:
        """Set the recording parameter."""
        if not self.supported(channel, "recording"):
            raise NotSupportedError(f"set_recording: recording on camera {self.camera_name(channel)} is not available")

        body: typings.reolink_json
        on_off = 1 if enable else 0
        if channel is None:
            if self.api_version("GetRec") >= 1:
                body = [{"cmd": "SetRecV20", "action": 0, "param": {"Rec": {"enable": on_off}}}]
                await self.send_setting(body)
                return

            for ch in self._channels:
                if self.supported(ch, "recording"):
                    body = [{"cmd": "SetRec", "action": 0, "param": {"Rec": {"schedule": {"enable": on_off, "channel": ch}}}}]
                    await self.send_setting(body)
            return

        if channel not in self._channels:
            raise InvalidParameterError(f"set_recording: no camera connected to channel '{channel}'")

        if self.api_version("GetRec") >= 1:
            body = [{"cmd": "SetRecV20", "action": 0, "param": {"Rec": {"scheduleEnable": on_off, "schedule": {"channel": channel}}}}]
        else:
            body = [{"cmd": "SetRec", "action": 0, "param": {"Rec": {"schedule": {"enable": on_off, "channel": channel}}}}]

        await self.send_setting(body)

    async def set_manual_record(self, channel: int, enable: bool) -> None:
        """Start/Stop manual recording."""
        if not self.supported(channel, "manual_record"):
            raise NotSupportedError(f"set_manual_record: manual recording on camera {self.camera_name(channel)} is not available")
        if channel not in self._channels:
            raise InvalidParameterError(f"set_manual_record: no camera connected to channel '{channel}'")

        on_off = 1 if enable else 0
        body = [{"cmd": "SetManualRec", "action": 0, "param": {"Rec": {"channel": channel, "enable": on_off}}}]
        await self.send_setting(body)

    async def set_buzzer(self, channel: int | None, enable: bool) -> None:
        """Set the NVR buzzer parameter."""
        if not self.supported(channel, "buzzer"):
            raise NotSupportedError(f"set_buzzer: NVR buzzer on camera {self.camera_name(channel)} is not available")

        body: typings.reolink_json
        on_off = 1 if enable else 0
        if channel is None:
            body = [{"cmd": "SetBuzzerAlarmV20", "action": 0, "param": {"Buzzer": {"enable": on_off}}}]
            await self.send_setting(body)
            return

        if channel not in self._channels:
            raise InvalidParameterError(f"set_recording: no camera connected to channel '{channel}'")

        body = [{"cmd": "SetBuzzerAlarmV20", "action": 0, "param": {"Buzzer": {"scheduleEnable": on_off, "schedule": {"channel": channel}}}}]
        await self.send_setting(body)

    async def set_audio(self, channel: int, enable: bool) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_audio: no camera connected to channel '{channel}'")
        await self.get_state(cmd="GetEnc")
        if channel not in self._enc_settings:
            raise NotSupportedError(f"set_audio: Audio on camera {self.camera_name(channel)} is not available")

        body: typings.reolink_json = [{"cmd": "SetEnc", "action": 0, "param": self._enc_settings[channel]}]
        body[0]["param"]["Enc"]["audio"] = 1 if enable else 0

        await self.send_setting(body)

    async def set_ir_lights(self, channel: int, enable: bool) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_ir_lights: no camera connected to channel '{channel}'")
        if channel not in self._ir_settings:
            raise NotSupportedError(f"set_ir_lights: IR light on camera {self.camera_name(channel)} is not available")

        state = "Auto" if enable else "Off"
        body: typings.reolink_json = [
            {
                "cmd": "SetIrLights",
                "action": 0,
                "param": {"IrLights": {"channel": channel, "state": state}},
            }
        ]

        await self.send_setting(body)

    async def set_status_led(self, channel: int, state: bool | str, doorbell: bool = False) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_status_led: no camera connected to channel '{channel}'")
        if not self.supported(channel, "status_led"):
            raise NotSupportedError(f"set_status_led: Status led on camera {self.camera_name(channel)} is not available")

        if isinstance(state, bool):
            value = "On" if state else "Off"
        else:
            value = state

        val_list = [val.value for val in StatusLedEnum]
        if value not in val_list:
            raise InvalidParameterError(f"set_status_led: value {value} not in {val_list}")

        if doorbell:
            param = {"channel": channel, "eDoorbellLightState": value}
        else:
            param = {"channel": channel, "state": value}

        body: typings.reolink_json = [{"cmd": "SetPowerLed", "action": 0, "param": {"PowerLed": param}}]
        await self.send_setting(body)

    async def set_whiteled(self, channel: int, state: bool | None = None, brightness: int | None = None, mode: int | str | None = None) -> None:
        """
        Set the WhiteLed parameter.
        with Reolink Duo GetWhiteLed returns an error state
        SetWhiteLed appears to require 4 parameters
          state - two values 0/1 possibly OFF/ON
          channel - appears to default to 0
          mode - three values I think
            0  Night Mode Off
            1  Night Mode On , AUTO on
            3  Night Mode On, Set Time On
          brightness - brigtness level range 0 to 100

          TO BE CONFIRMED
          There may be an extra set of parameters with Duo - dont know with others
          LightingSchedule : { EndHour , EndMin, StartHour,StartMin  }
        """
        if channel not in self._channels:
            raise InvalidParameterError(f"set_whiteled: no camera connected to channel '{channel}'")
        if channel not in self._whiteled_settings or not self._whiteled_settings[channel]:
            raise NotSupportedError(f"set_whiteled: White Led on camera {self.camera_name(channel)} is not available")

        settings = {"channel": channel}
        if state is not None:
            settings["state"] = 1 if state else 0
        if brightness is not None:
            settings["bright"] = brightness
            if brightness < 0 or brightness > 100:
                raise InvalidParameterError(f"set_whiteled: brightness {brightness} not in range 0..100")
        if mode is not None:
            if isinstance(mode, str):
                mode_int = SpotlightModeEnum[mode].value
            else:
                mode_int = mode
            settings["mode"] = mode_int
            mode_list = [mode.value for mode in SpotlightModeEnum]
            if mode_int not in mode_list:
                raise InvalidParameterError(f"set_whiteled: mode {mode_int} not in {mode_list}")

        body = [
            {
                "cmd": "SetWhiteLed",
                "param": {"WhiteLed": settings},
            }
        ]

        await self.send_setting(body, wait_before_get=3)

    async def set_spotlight_lighting_schedule(self, channel: int, endhour=6, endmin=0, starthour=18, startmin=0) -> None:
        """Stub to handle setting the time period where spotlight (WhiteLed) will be on when NightMode set and AUTO is off.
        Time in 24-hours format"""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_spotlight_lighting_schedule: no camera connected to channel '{channel}'")
        if channel not in self._whiteled_settings or not self._whiteled_settings[channel]:
            raise NotSupportedError(f"set_spotlight_lighting_schedule: White Led on camera {self.camera_name(channel)} is not available")

        if (
            endhour < 0
            or endhour > 23
            or endmin < 0
            or endmin > 59
            or starthour < 0
            or starthour > 23
            or startmin < 0
            or startmin > 59
            or (endhour == starthour and endmin < startmin)
            or (not (endhour < 12 and starthour > 16) and (endhour < starthour))
        ):
            raise InvalidParameterError(
                f"set_spotlight_lighting_schedule: Parameter error on camera {self.camera_name(channel)} start time: {starthour}:{startmin}, end time: {endhour}:{endmin}"
            )

        body = [
            {
                "cmd": "SetWhiteLed",
                "param": {
                    "WhiteLed": {
                        "LightingSchedule": {
                            "EndHour": endhour,
                            "EndMin": endmin,
                            "StartHour": starthour,
                            "StartMin": startmin,
                        },
                        "channel": channel,
                        "mode": 3,
                    }
                },
            }
        ]

        await self.send_setting(body)

    async def set_spotlight(self, channel: int, enable: bool) -> None:
        """Simply calls set_whiteled with brightness 100, mode 3 after setting lightning schedule to on all the time 0000 to 2359."""
        if enable:
            await self.set_spotlight_lighting_schedule(channel, 23, 59, 0, 0)
            await self.set_whiteled(channel, enable, 100, 3)
            return

        await self.set_spotlight_lighting_schedule(channel, 0, 0, 0, 0)
        await self.set_whiteled(channel, enable, 100, 1)

    async def set_volume(self, channel: int, volume: int | None = None, doorbell_button_sound: bool | None = None) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_volume: no camera connected to channel '{channel}'")
        if volume is not None:
            if not self.supported(channel, "volume"):
                raise NotSupportedError(f"set_volume: Volume control on camera {self.camera_name(channel)} is not available")
            if not isinstance(volume, int):
                raise InvalidParameterError(f"set_volume: volume {volume} not integer")
            if volume < 0 or volume > 100:
                raise InvalidParameterError(f"set_volume: volume {volume} not in range 0...100")
        if doorbell_button_sound is not None:
            if not self.supported(channel, "doorbell_button_sound"):
                raise NotSupportedError(f"set_volume: Doorbell button sound control on camera {self.camera_name(channel)} is not available")
            if not isinstance(doorbell_button_sound, bool):
                raise InvalidParameterError(f"set_volume: doorbell_button_sound {doorbell_button_sound} not boolean")

        params = {"channel": channel}
        if volume is not None:
            params["volume"] = volume
        if doorbell_button_sound is not None:
            params["visitorLoudspeaker"] = 1 if doorbell_button_sound else 0

        body = [{"cmd": "SetAudioCfg", "action": 0, "param": {"AudioCfg": params}}]
        await self.send_setting(body)

    async def set_hub_audio(
        self,
        channel: int | None = None,
        alarm_volume: int | None = None,
        message_volume: int | None = None,
        alarm_tone_id: int | None = None,
        visitor_tone_id: int | None = None,
    ) -> None:
        if channel is None:
            channel = self._channels[0]
        if channel not in self._channels:
            raise InvalidParameterError(f"set_hub_audio: no camera connected to channel '{channel}'")
        if not self.supported(channel, "hub_audio"):
            raise NotSupportedError(f"set_hub_audio: Hub audio control is not available on {self.nvr_name}")
        if alarm_volume is not None:
            if not isinstance(alarm_volume, int):
                raise InvalidParameterError(f"set_hub_audio: alarm_volume {alarm_volume} not integer")
            if alarm_volume < 0 or alarm_volume > 100:
                raise InvalidParameterError(f"set_hub_audio: alarm_volume {alarm_volume} not in range 0...100")
        if message_volume is not None:
            if not isinstance(message_volume, int):
                raise InvalidParameterError(f"set_hub_audio: message_volume {message_volume} not integer")
            if message_volume < 0 or message_volume > 100:
                raise InvalidParameterError(f"set_hub_audio: message_volume {message_volume} not in range 0...100")
        tone_id_list = [val.value for val in HubToneEnum]
        if alarm_tone_id is not None and alarm_tone_id not in tone_id_list:
            raise InvalidParameterError(f"set_hub_audio: alarm_tone_id {alarm_tone_id} not in {tone_id_list}")
        if visitor_tone_id is not None:
            if visitor_tone_id not in tone_id_list:
                raise InvalidParameterError(f"set_hub_audio: visitor_tone_id {visitor_tone_id} not in {tone_id_list}")
            if not self.is_doorbell(channel):
                raise InvalidParameterError("set_hub_audio: visitor_tone_id only supported for doorbells")

        params = {"channel": channel}
        if alarm_volume is not None:
            params["alarmVolume"] = alarm_volume
        if message_volume is not None:
            params["cuesVolume"] = message_volume
        if alarm_tone_id is not None:
            params["alarmRingToneId"] = alarm_tone_id
        if visitor_tone_id is not None:
            params["ringToneId"] = visitor_tone_id

        body = [{"cmd": "SetDeviceAudioCfg", "action": 0, "param": {"DeviceAudioCfg": params}}]
        await self.send_setting(body)

    async def play_quick_reply(self, channel: int, file_id: int) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"play_quick_reply: no camera connected to channel '{channel}'")
        if not self.supported(channel, "play_quick_reply"):
            raise NotSupportedError(f"play_quick_reply: Play quick reply on camera {self.camera_name(channel)} is not available")
        if file_id is not None and not isinstance(file_id, int):
            raise InvalidParameterError(f"play_quick_reply: file_id {file_id} not integer")
        if file_id is not None and file_id not in self.quick_reply_dict(channel):
            raise InvalidParameterError(f"play_quick_reply: file_id {file_id} not in {list(self.quick_reply_dict(channel))}")

        body = [{"cmd": "QuickReplyPlay", "action": 0, "param": {"id": file_id, "channel": channel}}]
        await self.send_setting(body)

    async def set_quick_reply(self, channel: int, enable: bool | None = None, file_id: int | None = None, time: int | None = None) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_quick_reply: no camera connected to channel '{channel}'")
        if not self.supported(channel, "quick_reply"):
            raise NotSupportedError(f"set_quick_reply: Quick reply on camera {self.camera_name(channel)} is not available")
        if file_id is not None and not isinstance(file_id, int):
            raise InvalidParameterError(f"set_quick_reply: file_id {file_id} not integer")
        if file_id is not None and file_id not in self.quick_reply_dict(channel):
            raise InvalidParameterError(f"set_quick_reply: file_id {file_id} not in {list(self.quick_reply_dict(channel))}")
        if time is not None and not isinstance(time, int):
            raise InvalidParameterError(f"set_quick_reply: time {time} not integer")
        if time is not None and time < 0:
            raise InvalidParameterError(f"set_quick_reply: time {time} can not be < 0")

        params: dict[str, Any] = {"channel": channel}
        if enable is not None:
            if enable:
                params["enable"] = 1
                current_file_id = self.quick_reply_file(channel)
                if current_file_id == -1:
                    current_file_id = list(self.quick_reply_dict(channel))[1]
                params["fileId"] = current_file_id
            else:
                params["enable"] = 0
        if file_id is not None:
            if file_id >= 0:
                params["enable"] = 1
                params["fileId"] = file_id
            else:
                params["enable"] = 0
        if time is not None:
            params["timeout"] = time

        body = [{"cmd": "SetAutoReply", "action": 0, "param": {"AutoReply": params}}]
        await self.send_setting(body)

    async def set_audio_alarm(self, channel: int, enable: bool) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_audio_alarm: no camera connected to channel '{channel}'")
        if not self.supported(channel, "siren"):
            raise NotSupportedError(f"set_audio_alarm: AudioAlarm on camera {self.camera_name(channel)} is not available")

        if self.api_version("GetAudioAlarm") >= 1:
            body = [{"cmd": "SetAudioAlarmV20", "param": {"Audio": {"enable": 1 if enable else 0, "schedule": {"channel": channel}}}}]
        else:
            body = [
                {
                    "cmd": "SetAudioAlarm",
                    "param": {
                        "Audio": {
                            "schedule": {
                                "enable": 1 if enable else 0,
                                "channel": channel,
                            }
                        }
                    },
                }
            ]

        await self.send_setting(body)

    async def set_siren(self, channel: int, enable: bool = True, duration: int | None = 2) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_siren: no camera connected to channel '{channel}'")
        if duration is not None and not isinstance(duration, int):
            raise InvalidParameterError(f"set_siren: duration '{duration}' is not integer")
        if not self.supported(channel, "siren_play"):
            raise NotSupportedError(f"set_siren: AudioAlarmPlay on camera {self.camera_name(channel)} is not available")

        if enable:
            if duration is not None:
                params = {
                    "alarm_mode": "times",
                    "times": duration,
                    "channel": channel,
                }
            else:
                params = {
                    "alarm_mode": "manul",
                    "manual_switch": 1,
                    "channel": channel,
                }
        else:
            params = {
                "alarm_mode": "manul",
                "manual_switch": 0,
                "channel": channel,
            }

        body = [
            {
                "cmd": "AudioAlarmPlay",
                "action": 0,
                "param": params,
            }
        ]

        await self.send_setting(body)

    async def set_daynight(self, channel: int, value: str) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_daynight: no camera connected to channel '{channel}'")
        await self.get_state(cmd="GetIsp")
        if channel not in self._isp_settings or not self._isp_settings[channel]:
            raise NotSupportedError(f"set_daynight: ISP on camera {self.camera_name(channel)} is not available")

        val_list = [val.value for val in DayNightEnum]
        if value not in val_list:
            raise InvalidParameterError(f"set_daynight: value {value} not in {val_list}")

        body: typings.reolink_json = [{"cmd": "SetIsp", "action": 0, "param": self._isp_settings[channel]}]
        body[0]["param"]["Isp"]["dayNight"] = value

        await self.send_setting(body)

    async def set_HDR(self, channel: int, value: bool | int) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_HDR: no camera connected to channel '{channel}'")
        if not self.supported(channel, "HDR"):
            raise NotSupportedError(f"set_HDR: ISP HDR on camera {self.camera_name(channel)} is not available")
        await self.get_state(cmd="GetIsp")
        if channel not in self._isp_settings or not self._isp_settings[channel]:
            raise NotSupportedError(f"set_HDR: ISP on camera {self.camera_name(channel)} is not available")

        val_list = [val.value for val in HDREnum]
        val_list.extend([True, False])
        if value not in val_list:
            raise InvalidParameterError(f"set_HDR: value {value} not in {val_list}")

        body: typings.reolink_json = [{"cmd": "SetIsp", "action": 0, "param": self._isp_settings[channel]}]
        if isinstance(value, bool):
            body[0]["param"]["Isp"]["hdr"] = 2 if value else 0
        else:
            body[0]["param"]["Isp"]["hdr"] = value

        await self.send_setting(body)

    async def set_daynight_threshold(self, channel: int, value: int) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_daynight_threshold: no camera connected to channel '{channel}'")
        await self.get_state(cmd="GetIsp")
        if channel not in self._isp_settings or not self._isp_settings[channel]:
            raise NotSupportedError(f"set_daynight_threshold: ISP on camera {self.camera_name(channel)} is not available")
        if value < 0 or value > 100:
            raise InvalidParameterError(f"set_daynight_threshold: value {value} not in 0-100")

        body: typings.reolink_json = [{"cmd": "SetIsp", "action": 0, "param": self._isp_settings[channel]}]
        body[0]["param"]["Isp"]["dayNightThreshold"] = value

        await self.send_setting(body)

    async def set_backlight(self, channel: int, value: str) -> None:
        if channel not in self._channels:
            raise InvalidParameterError(f"set_backlight: no camera connected to channel '{channel}'")
        await self.get_state(cmd="GetIsp")
        if channel not in self._isp_settings or not self._isp_settings[channel]:
            raise NotSupportedError(f"set_backlight: ISP on camera {self.camera_name(channel)} is not available")

        if value not in ["BackLightControl", "DynamicRangeControl", "Off"]:
            raise InvalidParameterError(f"set_backlight: value {value} not in ['BackLightControl', 'DynamicRangeControl', 'Off']")

        body: typings.reolink_json = [{"cmd": "SetIsp", "action": 0, "param": self._isp_settings[channel]}]
        body[0]["param"]["Isp"]["backLight"] = value

        await self.send_setting(body)

    async def set_motion_detection(self, channel: int, enable: bool) -> None:
        """Set the motion detection parameter."""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_motion_detection: no camera connected to channel '{channel}'")
        if channel not in self._md_alarm_settings:
            raise NotSupportedError(f"set_motion_detection: alarm on camera {self.camera_name(channel)} is not available")

        body: typings.reolink_json = [{"cmd": "SetAlarm", "action": 0, "param": self._md_alarm_settings[channel]}]
        body[0]["param"]["Alarm"]["enable"] = 1 if enable else 0

        await self.send_setting(body)

    async def set_pir(self, channel: int, enable: bool | None = None, reduce_alarm: bool | None = None, sensitivity: int | None = None) -> None:
        """Set PIR settings."""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_pir: no camera connected to channel '{channel}'")
        if channel not in self._pir:
            raise NotSupportedError(f"set_pir: PIR settings on camera {self.camera_name(channel)} are not available")
        if sensitivity is not None and not isinstance(sensitivity, int):
            raise InvalidParameterError(f"set_pir: sensitivity '{sensitivity}' is not integer")
        if sensitivity is not None and (sensitivity < 1 or sensitivity > 100):
            raise InvalidParameterError(f"set_pir: sensitivity {sensitivity} not in range 1...100")

        pir = {"channel": channel}
        if enable is not None:
            pir["enable"] = 1 if enable else 0
        if reduce_alarm is not None:
            pir["reduceAlarm"] = 1 if reduce_alarm else 0
        if sensitivity is not None:
            pir["sensitive"] = int(101 - sensitivity)

        body: typings.reolink_json = [{"cmd": "SetPirInfo", "action": 0, "param": {"pirInfo": pir}}]
        await self.send_setting(body)

    async def set_md_sensitivity(self, channel: int, value: int) -> None:
        """Set motion detection sensitivity.
        Here the camera web and windows application show a completely different value than set.
        So the calculation <51 - value> makes the "real" value.
        """
        if channel not in self._channels:
            raise InvalidParameterError(f"set_md_sensitivity: no camera connected to channel '{channel}'")
        if channel not in self._md_alarm_settings:
            raise NotSupportedError(f"set_md_sensitivity: md sensitivity on camera {self.camera_name(channel)} is not available")
        if not isinstance(value, int):
            raise InvalidParameterError(f"set_md_sensitivity: sensitivity '{value}' is not integer")
        if value < 1 or value > 50:
            raise InvalidParameterError(f"set_md_sensitivity: sensitivity {value} not in range 1...50")

        body: typings.reolink_json
        if self.api_version("GetMdAlarm") >= 1:
            body = [{"cmd": "SetMdAlarm", "action": 0, "param": {"MdAlarm": {"channel": channel, "useNewSens": 1, "newSens": {"sensDef": int(51 - value)}}}}]
        else:
            body = [
                {
                    "cmd": "SetAlarm",
                    "action": 0,
                    "param": {
                        "Alarm": {
                            "channel": channel,
                            "type": "md",
                            "sens": self._md_alarm_settings[channel]["Alarm"]["sens"],
                        }
                    },
                }
            ]
            for setting in body[0]["param"]["Alarm"]["sens"]:
                setting["sensitivity"] = int(51 - value)

        await self.send_setting(body)

    async def set_ai_sensitivity(self, channel: int, value: int, ai_type: str) -> None:
        """Set AI detection sensitivity."""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_ai_sensitivity: no camera connected to channel '{channel}'")
        if channel not in self._ai_alarm_settings:
            raise NotSupportedError(f"set_ai_sensitivity: ai sensitivity on camera {self.camera_name(channel)} is not available")
        if not isinstance(value, int):
            raise InvalidParameterError(f"set_ai_sensitivity: sensitivity '{value}' is not integer")
        if value < 0 or value > 100:
            raise InvalidParameterError(f"set_ai_sensitivity: sensitivity {value} not in range 0...100")
        if ai_type not in self.ai_supported_types(channel):
            raise InvalidParameterError(f"set_ai_sensitivity: ai type '{ai_type}' not supported for channel {channel}, supported types are {self.ai_supported_types(channel)}")

        body: typings.reolink_json = [{"cmd": "SetAiAlarm", "action": 0, "param": {"AiAlarm": {"channel": channel, "ai_type": ai_type, "sensitivity": value}}}]
        await self.send_setting(body)

    async def set_ai_delay(self, channel: int, value: int, ai_type: str) -> None:
        """Set AI detection delay time in seconds."""
        if channel not in self._channels:
            raise InvalidParameterError(f"set_ai_delay: no camera connected to channel '{channel}'")
        if channel not in self._ai_alarm_settings:
            raise NotSupportedError(f"set_ai_delay: ai delay on camera {self.camera_name(channel)} is not available")
        if not isinstance(value, int):
            raise InvalidParameterError(f"set_ai_delay: delay '{value}' is not integer")
        if value < 0 or value > 8:
            raise InvalidParameterError(f"set_ai_delay: delay {value} not in range 0...8")
        if ai_type not in self.ai_supported_types(channel):
            raise InvalidParameterError(f"set_ai_delay: ai type '{ai_type}' not supported for channel {channel}, supported types are {self.ai_supported_types(channel)}")

        body: typings.reolink_json = [{"cmd": "SetAiAlarm", "action": 0, "param": {"AiAlarm": {"channel": channel, "ai_type": ai_type, "stay_time": value}}}]
        await self.send_setting(body)

    async def set_image(
        self, channel: int, bright: int | None = None, contrast: int | None = None, saturation: int | None = None, hue: int | None = None, sharpen: int | None = None
    ) -> None:
        """Set image adjustments."""
        _image = {"Image": {"channel": channel}}

        if bright is not None:
            if not self.supported(channel, "isp_bright"):
                raise NotSupportedError(f"set_image: bright on camera {self.camera_name(channel)} is not available")
            if not isinstance(bright, int):
                raise InvalidParameterError(f"set_image: bright '{bright}' is not integer")
            if bright < 0 or bright > 255:
                raise InvalidParameterError(f"set_image: bright {bright} not in range 0...255")
            _image["Image"]["bright"] = bright

        if contrast is not None:
            if not self.supported(channel, "isp_contrast"):
                raise NotSupportedError(f"set_image: bright on camera {self.camera_name(channel)} is not available")
            if not isinstance(contrast, int):
                raise InvalidParameterError(f"set_image: contrast '{contrast}' is not integer")
            if contrast < 0 or contrast > 255:
                raise InvalidParameterError(f"set_image: contrast {contrast} not in range 0...255")
            _image["Image"]["contrast"] = contrast

        if saturation is not None:
            if not self.supported(channel, "isp_satruation"):
                raise NotSupportedError(f"set_image: bright on camera {self.camera_name(channel)} is not available")
            if not isinstance(saturation, int):
                raise InvalidParameterError(f"set_image: saturation '{saturation}' is not integer")
            if saturation < 0 or saturation > 255:
                raise InvalidParameterError(f"set_image: saturation {saturation} not in range 0...255")
            _image["Image"]["saturation"] = saturation

        if hue is not None:
            if not self.supported(channel, "isp_hue"):
                raise NotSupportedError(f"set_image: bright on camera {self.camera_name(channel)} is not available")
            if not isinstance(hue, int):
                raise InvalidParameterError(f"set_image: hue '{hue}' is not integer")
            if hue < 0 or hue > 255:
                raise InvalidParameterError(f"set_image: hue {hue} not in range 0...255")
            _image["Image"]["hue"] = hue

        if sharpen is not None:
            if not self.supported(channel, "isp_sharpen"):
                raise NotSupportedError(f"set_image: bright on camera {self.camera_name(channel)} is not available")
            if not isinstance(sharpen, int):
                raise InvalidParameterError(f"set_image: sharpen '{sharpen}' is not integer")
            if sharpen < 0 or sharpen > 255:
                raise InvalidParameterError(f"set_image: sharpen {sharpen} not in range 0...255")
            _image["Image"]["sharpen"] = sharpen

        body: typings.reolink_json = [{"cmd": "SetImage", "param": _image}]
        await self.send_setting(body)

    async def request_vod_files(
        self,
        channel: int,
        start: datetime,
        end: datetime,
        status_only: bool = False,
        stream: Optional[str] = None,
    ) -> tuple[list[typings.VOD_search_status], list[typings.VOD_file]]:
        """Send search VOD-files command."""
        if channel not in self._stream_channels:
            raise InvalidParameterError(f"Request VOD files: no camera connected to channel '{channel}'")
        if start > end:
            raise InvalidParameterError(f"Request VOD files: start date '{start}' needs to be before end date '{end}'")

        iLogicChannel = 0
        if stream is None:
            stream = self._stream
        if stream.startswith("autotrack_"):
            iLogicChannel = 1
            stream = stream.removeprefix("autotrack_")

        times = [(start, end)]
        if status_only:
            times = []
            end_month = end.month + (end.year - start.year) * 12
            for month_year in range(end_month, start.month - 1, -2):
                month = int((month_year - 0.5) % 12 + 0.5)
                year = start.year + int((month_year - 0.5) / 12)
                if month_year > start.month:
                    start_month = start.replace(year=year, month=month, day=1, hour=0, minute=0) - timedelta(minutes=5)
                else:
                    start_month = start.replace(year=year, month=month, day=1, hour=0, minute=0)
                times.append((start_month, start.replace(year=year, month=month, day=1, hour=0, minute=5)))

        body = []
        for time in times:
            search_body: dict = {
                "cmd": "Search",
                "action": 0,
                "param": {
                    "Search": {
                        "channel": channel,
                        "onlyStatus": 1 if status_only else 0,
                        "streamType": stream,
                        "StartTime": datetime_to_reolink_time(time[0]),
                        "EndTime": datetime_to_reolink_time(time[1]),
                    }
                },
            }
            if iLogicChannel:
                search_body["param"]["Search"]["iLogicChannel"] = 1
            body.append(search_body)

        if not body:
            raise InvalidParameterError(f"Request VOD files: no search body, start date '{start}' end date '{end}'")

        try:
            json_data = await self.send(body, {"cmd": "Search"}, expected_response_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Request VOD files error: {str(err)}") from err
        except NoDataError as err:
            raise NoDataError(f"Request VOD files error: {str(err)}") from err

        statuses = []
        vod_files = []
        for data in json_data:
            if data.get("code", -1) != 0:
                raise ApiError(f"Host: {self._host}:{self._port}: Request VOD files: API returned error code {data.get('code', -1)}, response: {json_data}")

            search_result = data.get("value", {}).get("SearchResult", {})
            if "Status" not in search_result:
                continue

            statuses.extend([typings.VOD_search_status(status) for status in search_result["Status"]])
            if status_only:
                continue

            if "File" not in search_result:
                # When there are now recordings available in the indicated time window, "File" will not be in the response.
                continue

            vod_files.extend([typings.VOD_file(file, self.timezone()) for file in search_result["File"]])

        if not statuses:
            # When there are now recordings at all, their will be no "Status"
            _LOGGER.debug("Host %s:%s: Request VOD files: no 'Status' in the response, most likely their are no recordings: %s", self._host, self._port, json_data)

        return statuses, vod_files

    async def send_setting(self, body: typings.reolink_json, wait_before_get: int = 0, getcmd: str = "") -> None:
        command = body[0]["cmd"]
        _LOGGER.debug(
            'Sending command: "%s" to: %s:%s with body: %s',
            command,
            self._host,
            self._port,
            body,
        )

        try:
            json_data = await self.send(body, {"cmd": command}, expected_response_type="json")
        except InvalidContentTypeError as err:
            raise InvalidContentTypeError(f"Command '{command}': {str(err)}") from err
        except NoDataError as err:
            raise NoDataError(f"Host: {self._host}:{self._port}: error receiving response for command '{command}'") from err

        _LOGGER.debug("Response from cmd '%s' from %s:%s: %s", command, self._host, self._port, json_data)

        try:
            if json_data[0]["code"] != 0 or json_data[0].get("value", {}).get("rspCode", -1) != 200:
                _LOGGER.debug("ApiError for command '%s', response: %s", command, json_data)
                rspCode = json_data[0].get("value", json_data[0]["error"])["rspCode"]
                detail = json_data[0].get("value", json_data[0]["error"]).get("detail", "")
                raise ApiError(f"cmd '{command}': API returned error code {json_data[0]['code']}, response code {rspCode}/{detail}", rspCode=rspCode)
        except KeyError as err:
            raise UnexpectedDataError(f"Host {self._host}:{self._port}: received an unexpected response from command '{command}': {json_data}") from err

        if not getcmd and command[:3] == "Set":
            getcmd = command.replace("Set", "Get")
        if getcmd:
            if wait_before_get > 0:
                await asyncio.sleep(wait_before_get)
            await self.get_state(cmd=getcmd)

    @overload
    async def send(
        self,
        body: typings.reolink_json,
        param: dict[str, Any] | None,
        expected_response_type: Literal["json"],
        retry: int = RETRY_ATTEMPTS,
    ) -> typings.reolink_json: ...

    @overload
    async def send(
        self,
        body: typings.reolink_json,
        param: dict[str, Any] | None,
        expected_response_type: Literal["image/jpeg"],
        retry: int = RETRY_ATTEMPTS,
    ) -> bytes: ...

    @overload
    async def send(
        self,
        body: typings.reolink_json,
        param: dict[str, Any] | None,
        expected_response_type: Literal["text/html"],
        retry: int = RETRY_ATTEMPTS,
    ) -> str: ...

    @overload
    async def send(
        self,
        body: typings.reolink_json,
        param: dict[str, Any] | None,
        expected_response_type: Literal["application/octet-stream"],
        retry: int = RETRY_ATTEMPTS,
    ) -> aiohttp.ClientResponse: ...

    @overload
    async def send(
        self,
        body: typings.reolink_json,
        *,
        expected_response_type: Literal["json"],
        retry: int = RETRY_ATTEMPTS,
    ) -> typings.reolink_json: ...

    @overload
    async def send(
        self,
        body: typings.reolink_json,
        *,
        expected_response_type: Literal["image/jpeg"],
        retry: int = RETRY_ATTEMPTS,
    ) -> bytes: ...

    @overload
    async def send(
        self,
        body: typings.reolink_json,
        *,
        expected_response_type: Literal["text/html"],
        retry: int = RETRY_ATTEMPTS,
    ) -> str: ...

    @overload
    async def send(
        self,
        body: typings.reolink_json,
        *,
        expected_response_type: Literal["application/octet-stream"],
        retry: int = RETRY_ATTEMPTS,
    ) -> aiohttp.ClientResponse: ...

    async def send(
        self,
        body: typings.reolink_json,
        param: dict[str, Any] | None = None,
        expected_response_type: Literal["json", "image/jpeg", "text/html", "application/octet-stream"] = "json",
        retry: int = RETRY_ATTEMPTS,
    ) -> typings.reolink_json | bytes | str | aiohttp.ClientResponse:
        """
        If a body contains more than MAX_CHUNK_ITEMS requests, split it up in chunks.
        Otherwise you get a 'error': {'detail': 'send failed', 'rspCode': -16} response.
        """
        len_body = len(body)
        if len_body <= MAX_CHUNK_ITEMS or expected_response_type != "json":
            return await self.send_chunk(body, param, expected_response_type, retry)

        response: typings.reolink_json = []
        for chunk in range(0, len_body, MAX_CHUNK_ITEMS):
            chunk_end = min(chunk + MAX_CHUNK_ITEMS, len_body)
            _LOGGER.debug("sending chunks %i:%i of total %i requests", chunk + 1, chunk_end, len_body)
            response.extend(await self.send_chunk(body[chunk:chunk_end], param, expected_response_type, retry))

        return response

    @overload
    async def send_chunk(
        self,
        body: typings.reolink_json,
        param: dict[str, Any] | None,
        expected_response_type: Literal["json"],
        retry: int,
    ) -> typings.reolink_json: ...

    @overload
    async def send_chunk(
        self,
        body: typings.reolink_json,
        param: dict[str, Any] | None,
        expected_response_type: Literal["image/jpeg"],
        retry: int,
    ) -> bytes: ...

    @overload
    async def send_chunk(
        self,
        body: typings.reolink_json,
        param: dict[str, Any] | None,
        expected_response_type: Literal["text/html"],
        retry: int,
    ) -> str: ...

    @overload
    async def send_chunk(
        self,
        body: typings.reolink_json,
        param: dict[str, Any] | None,
        expected_response_type: Literal["application/octet-stream"],
        retry: int,
    ) -> aiohttp.ClientResponse: ...

    async def send_chunk(
        self,
        body: typings.reolink_json,
        param: dict[str, Any] | None,
        expected_response_type: Literal["json", "image/jpeg", "text/html", "application/octet-stream"],
        retry: int,
    ) -> typings.reolink_json | bytes | str | aiohttp.ClientResponse:
        """Generic send method."""
        retry = retry - 1

        if expected_response_type in ["image/jpeg", "application/octet-stream"]:
            cur_command = "" if param is None else param.get("cmd", "")
            is_login_logout = False
        else:
            cur_command = "" if len(body) == 0 else body[0].get("cmd", "")
            is_login_logout = cur_command in ["Login", "Logout"]

        if not is_login_logout:
            await self._login_open_port()

        if not param:
            param = {}
        if cur_command == "Login":
            param["token"] = "null"
        elif self._token is not None:
            param["token"] = self._token

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("%s/%s:%s::send() HTTP Request params =\n%s\n", self.nvr_name, self._host, self._port, self._hide_password(param))

        if self._aiohttp_session.closed:
            self._aiohttp_session = self._get_aiohttp_session()

        try:
            data: bytes | str
            if expected_response_type == "image/jpeg":
                async with self._send_mutex:
                    response = await self._aiohttp_session.get(url=self._url, params=param, allow_redirects=False, timeout=self._timeout)

                data = await response.read()  # returns bytes
            elif expected_response_type == "application/octet-stream":
                async with self._send_mutex:
                    dl_timeout = aiohttp.ClientTimeout(connect=self.timeout, sock_read=self.timeout)
                    response = await self._aiohttp_session.get(url=self._url, params=param, allow_redirects=False, timeout=dl_timeout)

                data = ""  # Response will be a file and be large, pass the response instead of reading it here.
                if response.content_type == "text/html":
                    data = await response.text(encoding="utf-8")  # Error occured, read the error message
            else:
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("%s/%s:%s::send() HTTP Request body =\n%s\n", self.nvr_name, self._host, self._port, self._hide_password(body))

                async with self._send_mutex:
                    response = await self._aiohttp_session.post(url=self._url, json=body, params=param, allow_redirects=False, timeout=self._timeout)

                data = await response.text(encoding="utf-8")  # returns str

            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "%s/%s:%s::send() HTTP Response status = %s, content-type = (%s).", self.nvr_name, self._host, self._port, response.status, response.content_type
                )
                if cur_command == "Search" and len(data) > 10000:
                    _LOGGER_DATA.debug("%s/%s:%s::send() HTTP Response (VOD search) data scrapped because it's too large.", self.nvr_name, self._host, self._port)
                elif cur_command in ["Snap", "Download"]:
                    _LOGGER_DATA.debug("%s/%s:%s::send() HTTP Response (snapshot/download) data scrapped because it's too large.", self.nvr_name, self._host, self._port)
                else:
                    _LOGGER_DATA.debug("%s/%s:%s::send() HTTP Response data:\n%s\n", self.nvr_name, self._host, self._port, self._hide_password(data))

            if len(data) < 500 and response.content_type == "text/html":
                if isinstance(data, bytes):
                    login_err = b'detail" : "please login first' in data and cur_command != "Logout"
                    cred_err = (
                        b'"detail" : "invalid user"' in data
                        or b'"detail" : "login failed"' in data
                        or b'"detail" : "password wrong"' in data
                        or b"Login has been locked" in data
                    )
                else:
                    login_err = ('"detail" : "please login first"' in data) and cur_command != "Logout"
                    cred_err = (
                        '"detail" : "invalid user"' in data or '"detail" : "login failed"' in data or '"detail" : "password wrong"' in data or "Login has been locked" in data
                    )
                if login_err or cred_err:
                    try:
                        json_data = json_loads(data)
                        detail = json_data[0]["error"]["detail"]
                    except Exception:
                        detail = ""
                    response.release()
                    await self.expire_session()
                    if cred_err and cur_command == "Login":
                        raise CredentialsInvalidError(f"Host {self._host}:{self._port}: Invalid credentials during login, '{detail}'")
                    if retry <= 0:
                        if cred_err and cur_command != "Logout":
                            raise CredentialsInvalidError(f"Host {self._host}:{self._port}: Invalid credentials after retries, '{detail}'")
                        raise LoginError(f"Host {self._host}:{self._port}: LoginError: received '{detail}'")
                    _LOGGER.debug(
                        'Host %s:%s: "invalid login" response, trying to login again and retry the command.',
                        self._host,
                        self._port,
                    )
                    return await self.send(body, param, expected_response_type, retry)

            if is_login_logout and response.status == 300:
                response.release()
                raise ApiError(f"API returned HTTP status ERROR code {response.status}/{response.reason}, this may happen if you use HTTP and the camera expects HTTPS")

            if response.status in [404, 502] and retry > 0:
                _LOGGER.debug("Host %s:%s: %s/%s response, trying to login again and retry the command.", self._host, self._port, response.status, response.reason)
                response.release()
                await self.expire_session()
                return await self.send(body, param, expected_response_type, retry)

            if response.status >= 400 or (is_login_logout and response.status != 200):
                response.release()
                raise ApiError(f"API returned HTTP status ERROR code {response.status}/{response.reason}")

            expected_content_type: list[str] = [expected_response_type]
            if expected_response_type == "json":
                expected_content_type = ["text/html"]
            if expected_response_type == "application/octet-stream":
                # Reolink typo "apolication/octet-stream" instead of "application/octet-stream"
                expected_content_type = ["application/octet-stream", "apolication/octet-stream"]
            if response.content_type not in expected_content_type:
                response.release()
                err_mess = f"Expected type '{expected_content_type[0]}' but received '{response.content_type}'"
                if response.content_type == "text/html":
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    err_mess = f"{err_mess}, response: {data}"
                raise InvalidContentTypeError(err_mess)

            if expected_response_type == "json" and isinstance(data, str):
                try:
                    json_data = json_loads(data)
                except (TypeError, JSONDecodeError) as err:
                    if retry <= 0:
                        raise InvalidContentTypeError(
                            f"Error translating JSON response: {str(err)}, from commands {[cmd.get('cmd') for cmd in body]}, "
                            f"content type '{response.content_type}', data:\n{data}\n"
                        ) from err
                    _LOGGER.debug("Error translating JSON response: %s, trying again, data:\n%s\n", str(err), self._hide_password(data))
                    await self.expire_session(unsubscribe=False)
                    return await self.send(body, param, expected_response_type, retry)
                if json_data is None:
                    await self.expire_session(unsubscribe=False)
                    raise NoDataError(f"Host {self._host}:{self._port}: returned no data: {data}")
                if len(json_data) != len(body) and len(body) != 1:
                    if retry <= 0:
                        raise UnexpectedDataError(
                            f"Host {self._host}:{self._port} error mapping responses to requests, received {len(json_data)} responses while requesting {len(body)} responses",
                        )
                    if retry == 1:
                        _LOGGER.debug(
                            "Host %s:%s error mapping responses to requests, received %s responses while requesting %s responses, retrying by sending each command separately",
                            self._host,
                            self._port,
                            len(json_data),
                            len(body),
                        )
                        json_data_sep = []
                        for command in body:
                            try:
                                # since len(body) will be 1, it is safe to increase retry to 2 for the individual command, this can not be reached again.
                                json_data_sep.extend(await self.send([command], param, expected_response_type, retry + 1))
                            except ReolinkError as err:
                                raise UnexpectedDataError(
                                    f"Host {self._host}:{self._port} error mapping responses to requests, originally received {len(json_data)} responses "
                                    f"while requesting {len(body)} responses, during separete sending retry of cmd '{command}' got error: {str(err)}",
                                ) from err
                        return json_data_sep

                    _LOGGER.debug(
                        "Host %s:%s error mapping responses to requests, received %s responses while requesting %s responses, trying again",
                        self._host,
                        self._port,
                        len(json_data),
                        len(body),
                    )
                    await self.expire_session(unsubscribe=False)
                    return await self.send(body, param, expected_response_type, retry)
                # retry commands that have not been received by the camera (battery cam waking from sleep)
                retry_cmd = []
                retry_idxs = []
                for idx, cmd_data in enumerate(json_data):
                    if cmd_data["code"] != 0 and cmd_data.get("error", {}).get("rspCode", 0) in [-12, -13, -17]:
                        retry_cmd.append(body[idx])
                        retry_idxs.append(idx)
                if retry_cmd and retry > 0:
                    _LOGGER.debug(
                        "cmd %s: returned response code %s/%s, retrying in 1.0 s",
                        [cmd.get("cmd") for cmd in retry_cmd],
                        [json_data[idx].get("error", {}).get("rspCode") for idx in retry_idxs],
                        [json_data[idx].get("error", {}).get("detail") for idx in retry_idxs],
                    )
                    await asyncio.sleep(1.0)  # give the battery cam time to wake
                    retry_data = await self.send(retry_cmd, param, expected_response_type, retry)
                    for idx, retry_resp in enumerate(retry_data):
                        json_data[retry_idxs[idx]] = retry_resp
                return json_data

            if expected_response_type == "image/jpeg" and isinstance(data, bytes):
                return data

            if expected_response_type == "text/html" and isinstance(data, str):
                return data

            if expected_response_type == "application/octet-stream":
                # response needs to be read or released from the calling function
                return response

            response.release()
            raise InvalidContentTypeError(f"Expected {expected_response_type}, unexpected data received: {data!r}")
        except (aiohttp.ClientConnectorError, aiohttp.ClientOSError, aiohttp.ServerConnectionError, aiohttp.ClientPayloadError) as err:
            if retry <= 0:
                _LOGGER.debug("Host %s:%s: connection error: %s", self._host, self._port, str(err))
                await self.expire_session()
                raise ReolinkConnectionError(f"Host {self._host}:{self._port}: connection error: {str(err)}") from err
            _LOGGER.debug("Host %s:%s: connection error, trying again: %s", self._host, self._port, str(err))
            return await self.send(body, param, expected_response_type, retry)
        except UnicodeDecodeError as err:
            if retry <= 0:
                raise InvalidContentTypeError(
                    f"Error decoding response to text: {str(err)}, from commands {[cmd.get('cmd') for cmd in body]}, "
                    f"content type '{response.content_type}', charset '{response.charset}'"
                ) from err
            _LOGGER.debug("Error decoding response to text: %s, trying again", str(err))
            await self.expire_session(unsubscribe=False)
            return await self.send(body, param, expected_response_type, retry)
        except asyncio.TimeoutError as err:
            if retry <= 0:
                _LOGGER.debug(
                    "Host %s:%s: connection timeout. Please check the connection to this host.",
                    self._host,
                    self._port,
                )
                await self.expire_session()
                raise ReolinkTimeoutError(f"Host {self._host}:{self._port}: Timeout error: {str(err)}") from err
            _LOGGER.debug(
                "Host %s:%s: connection timeout, trying again.",
                self._host,
                self._port,
            )
            return await self.send(body, param, expected_response_type, retry)
        except RuntimeError as err:
            if self._aiohttp_session.closed and retry > 0:
                # catch RuntimeError("Session is closed") from aiohttp, can happen due to async
                _LOGGER.debug("Host %s:%s: aiohttp session closed, retrying.", self._host, self._port)
                return await self.send(body, param, expected_response_type, retry)
            _LOGGER.error('Host %s:%s: RuntimeError "%s" occurred, traceback:\n%s\n', self._host, self._port, str(err), traceback.format_exc())
            await self.expire_session()
            raise err
        except ApiError as err:
            _LOGGER.error("Host %s:%s: API error: %s.", self._host, self._port, str(err))
            await self.expire_session(unsubscribe=False)
            raise err
        except CredentialsInvalidError as err:
            _LOGGER.error(str(err))
            raise err
        except InvalidContentTypeError as err:
            _LOGGER.debug("Host %s:%s: content type error: %s.", self._host, self._port, str(err))
            await self.expire_session(unsubscribe=False)
            raise err
        except Exception as err:
            _LOGGER.error('Host %s:%s: unknown exception "%s" occurred, traceback:\n%s\n', self._host, self._port, str(err), traceback.format_exc())
            await self.expire_session()
            raise err

    async def send_reolink_com(
        self,
        URL: str,
        expected_response_type: Literal["application/json"] = "application/json",
    ) -> dict[str, Any]:
        """Generic send method for reolink.com site."""

        if self._aiohttp_session.closed:
            self._aiohttp_session = self._get_aiohttp_session()

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("%s requesting reolink.com '%s'", self.nvr_name, URL)

        com_timeout = aiohttp.ClientTimeout(total=2 * self.timeout)
        try:
            response = await self._aiohttp_session.get(url=URL, headers={"user-agent": "reolink_aio"}, timeout=com_timeout)
        except (aiohttp.ClientConnectorError, aiohttp.ClientOSError, aiohttp.ServerConnectionError) as err:
            raise ReolinkConnectionError(f"Connetion error to {URL}: {str(err)}") from err
        except asyncio.TimeoutError as err:
            raise ReolinkTimeoutError(f"Timeout requesting {URL}: {str(err)}") from err

        if response.status != 200:
            response.release()
            raise ApiError(f"Request to {URL} returned HTTP status ERROR code {response.status}/{response.reason}", rspCode=response.status)

        if response.content_type != expected_response_type:
            response.release()
            raise InvalidContentTypeError(f"Request to {URL}, expected type '{expected_response_type}' but received '{response.content_type}'")

        try:
            data = await response.text()
        except (aiohttp.ClientConnectorError, aiohttp.ClientOSError, aiohttp.ServerConnectionError) as err:
            raise ReolinkConnectionError(f"Connetion error reading response from {URL}: {str(err)}") from err
        except asyncio.TimeoutError as err:
            raise ReolinkTimeoutError(f"Timeout reading response from {URL}: {str(err)}") from err

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER_DATA.debug("%s reolink.com response: %s\n", self.nvr_name, data)

        try:
            json_data = json_loads(data)
        except (TypeError, JSONDecodeError) as err:
            raise InvalidContentTypeError(f"Error translating JSON response: {str(err)}, from {URL}, " f"content type '{response.content_type}', data:\n{data}\n") from err

        if json_data is None:
            raise NoDataError(f"Request to {URL} returned no data: {data}")

        resp_code = json_data.get("result", {}).get("code")
        if resp_code != 0:
            raise ApiError(f"Request to {URL} returned error code {resp_code}, data:\n{json_data}", rspCode=resp_code)

        return json_data

    ##############################################################################
    # WEBHOOK managing
    async def webhook_add(self, channel: int, webhook_url: str):
        """
        Add a new webhook, reolink web interface -> settings -> survailance -> push -> For developers.
        Webhook will be called on motion or AI person/vehicle/animal.
        Webhook will only be called if push is on, the corresponding type is on and the scheduale on that time is enabled.
        """
        if not self.supported(channel, "webhook"):
            raise NotSupportedError(f"Webhooks not supported on camera {self.camera_name(channel)}")

        await self.get_state("GetWebHook")

        index = None
        # use same index if webhook already used before
        for webhook in self._webhook_settings[channel]["WebHook"]:
            if webhook_url == webhook["hookUrl"]:
                index = webhook["index"]
                break

        # if not used before find the first index not in use, if all in use default to idx 0
        if index is None:
            index = 0
            for webhook in self._webhook_settings[channel]["WebHook"]:
                if webhook["indexEnable"] == -1:
                    index = webhook["index"]
                    break

        body: typings.reolink_json = [
            {
                "cmd": "SetWebHook",
                "action": 0,
                "param": {"WebHook": {"channel": channel, "index": index, "indexEnable": 1, "hookUrl": webhook_url, "bCustom": 0, "hookBody": ""}},
            }
        ]
        await self.send_setting(body)

    async def webhook_test(self, channel: int, webhook_url: str):
        """Send a test message to a webhook"""
        if not self.supported(channel, "webhook"):
            raise NotSupportedError(f"Webhooks not supported on camera {self.camera_name(channel)}")

        body: typings.reolink_json = [
            {
                "cmd": "TestWebHook",
                "action": 0,
                "param": {"WebHook": {"alarmChannel": channel, "type": 3, "source": "hook", "hookUrl": webhook_url, "bCustom": 0, "hookBody": ""}},
            }
        ]
        try:
            await self.send_setting(body)
        except ApiError as err:
            raise ApiError(f"Webhook test for url '{webhook_url}' failed: {str(err)}") from err

    async def webhook_remove(self, channel: int, webhook_url: str):
        """Remove a webhook"""
        if not self.supported(channel, "webhook"):
            raise NotSupportedError(f"Webhooks not supported on camera {self.camera_name(channel)}")

        await self.get_state("GetWebHook")

        index = None
        for webhook in self._webhook_settings[channel]["WebHook"]:
            if webhook_url == webhook["hookUrl"]:
                index = webhook["index"]
                break

        if index is None:
            return

        body: typings.reolink_json = [
            {
                "cmd": "SetWebHook",
                "action": 0,
                "param": {"WebHook": {"channel": channel, "index": index, "indexEnable": -1, "hookUrl": webhook_url, "bCustom": 0, "hookBody": ""}},
            }
        ]
        await self.send_setting(body)

    async def webhook_disable(self, channel: int, webhook_url: str):
        """Disable a webhook"""
        if not self.supported(channel, "webhook"):
            raise NotSupportedError(f"Webhooks not supported on camera {self.camera_name(channel)}")

        await self.get_state("GetWebHook")

        index = None
        for webhook in self._webhook_settings[channel]["WebHook"]:
            if webhook_url == webhook["hookUrl"]:
                index = webhook["index"]
                break

        if index is None:
            return

        body: typings.reolink_json = [
            {
                "cmd": "SetWebHook",
                "action": 0,
                "param": {"WebHook": {"channel": channel, "index": index, "indexEnable": 0, "hookUrl": webhook_url, "bCustom": 0, "hookBody": ""}},
            }
        ]
        await self.send_setting(body)

    ##############################################################################
    # SUBSCRIPTION managing
    def renewtimer(self, sub_type: SubType = SubType.all) -> int:
        """Return the renew time in seconds. Negative if expired."""
        if sub_type == SubType.all:
            t_push = self.renewtimer(SubType.push)
            t_long_poll = self.renewtimer(SubType.long_poll)
            if t_long_poll == -1:
                return t_push
            if t_push == -1:
                return t_long_poll
            return min(t_push, t_long_poll)

        if sub_type not in self._subscription_time_difference or sub_type not in self._subscription_termination_time:
            return -1

        diff = self._subscription_termination_time[sub_type] - datetime.utcnow()
        return int(diff.total_seconds())

    def subscribed(self, sub_type: Literal[SubType.push, SubType.long_poll] = SubType.push) -> bool:
        return sub_type in self._subscription_manager_url and self.renewtimer(sub_type) > 0

    def convert_time(self, time) -> Optional[datetime]:
        """Convert time object to printable."""
        try:
            return datetime.strptime(time, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None

    async def calc_time_difference(self, local_time, remote_time) -> float:
        """Calculate the time difference between local and remote."""
        return remote_time.timestamp() - local_time.timestamp()

    async def get_digest(self) -> dict:
        """Get the authorisation digest."""
        time_created = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

        raw_nonce = uuid.uuid4().bytes
        nonce = base64.b64encode(raw_nonce)

        sha1 = hashlib.sha1()
        sha1.update(raw_nonce + time_created.encode("utf8") + self._password.encode("utf8"))
        raw_digest = sha1.digest()
        digest_pwd = base64.b64encode(raw_digest)

        return {
            "UsernameToken": str(uuid.uuid4()),
            "Username": self._username,
            "PasswordDigest": digest_pwd.decode("utf8"),
            "Nonce": nonce.decode("utf8"),
            "Created": time_created,
        }

    async def subscription_send(self, headers, data, timeout: aiohttp.ClientTimeout | None = None, mutex: asyncio.Lock | None = None) -> str:
        """Send subscription data to the camera."""
        if self._subscribe_url is None:
            await self.get_state("GetNetPort")

        if self._subscribe_url is None:
            raise NotSupportedError(f"subscription_send: failed to retrieve subscribe_url from {self._host}:{self._port}")

        _LOGGER.debug(
            "Host %s:%s: subscription request data:\n%s\n",
            self._host,
            self._port,
            data,
        )

        if self._aiohttp_session.closed:
            self._aiohttp_session = self._get_aiohttp_session()

        if timeout is None:
            timeout = self._timeout
        if mutex is None:
            mutex = self._send_mutex

        try:
            async with mutex:
                response = await self._aiohttp_session.post(
                    url=self._subscribe_url,
                    data=data,
                    headers=headers,
                    allow_redirects=False,
                    timeout=timeout,
                )

            response_text = await response.text()
            _LOGGER.debug(
                "Host %s:%s: subscription got response status: %s. Payload:\n%s\n",
                self._host,
                self._port,
                response.status,
                response_text,
            )

            if response.status != 200:
                if response.status == 400 and "SOAP-ENV:Fault" in response_text and self.api_version("onvif") <= 1:
                    raise NotSupportedError(
                        f"Host {self._host}:{self._port}: subscription request got HTTP status response "
                        f"{response.status}: {response.reason} with 'SOAP-ENV:Fault' as response text"
                    )
                raise ApiError(f"Host {self._host}:{self._port}: subscription request got a response with wrong HTTP status {response.status}: {response.reason}")

            return response_text

        except (aiohttp.ClientConnectorError, aiohttp.ClientOSError, aiohttp.ServerConnectionError) as err:
            raise ReolinkConnectionError(f"Host {self._host}:{self._port}: connection error: {str(err)}.") from err
        except asyncio.TimeoutError as err:
            raise ReolinkTimeoutError(f"Host {self._host}:{self._port}: connection timeout exception.") from err

    async def subscribe(self, webhook_url: str | None = None, sub_type: Literal[SubType.push, SubType.long_poll] = SubType.push, retry: bool = False):
        """Subscribe to ONVIF events."""
        headers = templates.HEADERS
        if sub_type == SubType.push:
            headers.update(templates.SUBSCRIBE_ACTION)
            template = templates.SUBSCRIBE_XML
        elif sub_type == SubType.long_poll:
            headers.update(templates.PULLPOINT_ACTION)
            template = templates.PULLPOINT_XML
        else:
            raise SubscriptionError(f"Host {self._host}:{self._port}: subscription type '{sub_type}' not supported")

        parameters = {
            "InitialTerminationTime": f"PT{SUBSCRIPTION_TERMINATION_TIME}M",
        }
        if webhook_url is not None and sub_type == SubType.push:
            parameters["Address"] = webhook_url

        parameters.update(await self.get_digest())
        local_time = datetime.utcnow()

        xml = template.format(**parameters)

        try:
            response = await self.subscription_send(headers, xml)
        except NotSupportedError as err:
            raise err
        except ReolinkError as err:
            if not retry:
                _LOGGER.debug("Reolink %s subscribe error: %s", sub_type, str(err))
                await self.unsubscribe_all(sub_type)
                return await self.subscribe(webhook_url, sub_type, retry=True)
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to subscribe {sub_type}: {str(err)}") from err
        root = XML.fromstring(response)

        address_element = root.find(".//{http://www.w3.org/2005/08/addressing}Address")
        if address_element is None:
            if not retry:
                await self.unsubscribe_all(sub_type)
                return await self.subscribe(webhook_url, sub_type, retry=True)
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to subscribe {sub_type}, could not find subscription manager url")
        sub_manager_url = address_element.text

        if sub_manager_url is None:
            if not retry:
                await self.unsubscribe_all(sub_type)
                return await self.subscribe(webhook_url, sub_type, retry=True)
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to subscribe {sub_type}, subscription manager url not available")
        self._subscription_manager_url[sub_type] = sub_manager_url

        current_time_element = root.find(".//{http://docs.oasis-open.org/wsn/b-2}CurrentTime")
        if current_time_element is None:
            if not retry:
                await self.unsubscribe_all(sub_type)
                return await self.subscribe(webhook_url, sub_type, retry=True)
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to subscribe {sub_type}, could not find CurrentTime")
        remote_time = self.convert_time(current_time_element.text)

        if remote_time is None:
            if not retry:
                await self.unsubscribe_all(sub_type)
                return await self.subscribe(webhook_url, sub_type, retry=True)
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to subscribe {sub_type}, CurrentTime not available")

        self._subscription_time_difference[sub_type] = await self.calc_time_difference(local_time, remote_time)

        termination_time_element = root.find(".//{http://docs.oasis-open.org/wsn/b-2}TerminationTime")
        if termination_time_element is None:
            if not retry:
                await self.unsubscribe_all(sub_type)
                return await self.subscribe(webhook_url, sub_type, retry=True)
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to subscribe {sub_type}, could not find TerminationTime")

        termination_time = self.convert_time(termination_time_element.text)
        if termination_time is None:
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to subscribe {sub_type}, TerminationTime not available")

        self._subscription_termination_time[sub_type] = termination_time - timedelta(seconds=self._subscription_time_difference[sub_type])

        _LOGGER.debug(
            "%s, local time: %s, camera time: %s (difference: %s), termination time: %s",
            sub_type,
            local_time.strftime("%Y-%m-%d %H:%M"),
            remote_time.strftime("%Y-%m-%d %H:%M"),
            self._subscription_time_difference[sub_type],
            self._subscription_termination_time[sub_type].strftime("%Y-%m-%d %H:%M"),
        )

        return

    async def renew(self, sub_type: Literal[SubType.push, SubType.long_poll] = SubType.push):
        """Renew the ONVIF event subscription."""
        if not self.subscribed(sub_type):
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to renew {sub_type} subscription, not previously subscribed")

        headers = templates.HEADERS
        headers.update(templates.RENEW_ACTION)
        template = templates.RENEW_XML

        parameters = {
            "To": self._subscription_manager_url[sub_type],
            "TerminationTime": f"PT{SUBSCRIPTION_TERMINATION_TIME}M",
        }

        parameters.update(await self.get_digest())
        local_time = datetime.utcnow()

        xml = template.format(**parameters)

        try:
            response = await self.subscription_send(headers, xml)
        except ReolinkError as err:
            await self.unsubscribe_all(sub_type)
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to renew {sub_type} subscription: {str(err)}") from err
        root = XML.fromstring(response)

        current_time_element = root.find(".//{http://docs.oasis-open.org/wsn/b-2}CurrentTime")
        if current_time_element is None:
            await self.unsubscribe_all(sub_type)
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to renew {sub_type} subscription, could not find CurrentTime")
        remote_time = self.convert_time(current_time_element.text)

        if remote_time is None:
            await self.unsubscribe_all(sub_type)
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to renew {sub_type} subscription, CurrentTime not available")

        self._subscription_time_difference[sub_type] = await self.calc_time_difference(local_time, remote_time)

        # The Reolink renew functionality has a bug: it always returns the INITIAL TerminationTime.
        # By adding the duration to the CurrentTime parameter, the new termination time can be calculated.
        # This will not work before the Reolink bug gets fixed on all devices
        # termination_time_element = root.find('.//{http://docs.oasis-open.org/wsn/b-2}TerminationTime')
        # if termination_time_element is None:
        #     await self.unsubscribe_all(sub_type)
        #     raise SubscriptionError(f"Host {self._host}:{self._port}: failed to renew {sub_type} subscription, unexpected response")
        # remote_termination_time = self.convert_time(termination_time_element.text)
        # if remote_termination_time is None:
        #     await self.unsubscribe_all(sub_type)
        #     raise SubscriptionError(f"Host {self._host}:{self._port}: failed to renew {sub_type} subscription, unexpected response")
        # self._subscription_termination_time[sub_type] = remote_termination_time - timedelta(seconds = self._subscription_time_difference[sub_type])
        self._subscription_termination_time[sub_type] = local_time + timedelta(minutes=SUBSCRIPTION_TERMINATION_TIME)

        _LOGGER.debug(
            "Renewed subscription successfully, local time: %s, camera time: %s (difference: %s), termination time: %s",
            local_time.strftime("%Y-%m-%d %H:%M"),
            remote_time.strftime("%Y-%m-%d %H:%M"),
            self._subscription_time_difference[sub_type],
            self._subscription_termination_time[sub_type].strftime("%Y-%m-%d %H:%M"),
        )

        return

    async def pull_point_request(self):
        """Request message from ONVIF pull point."""
        if not self.subscribed(SubType.long_poll):
            raise SubscriptionError(f"Host {self._host}:{self._port}: failed to request pull point message, not yet subscribed")

        headers = templates.HEADERS
        headers.update(templates.PULLMESSAGE_ACTION)
        template = templates.PULLMESSAGE_XML

        parameters = {
            "To": self._subscription_manager_url[SubType.long_poll],
            "Timeout": f"PT{LONG_POLL_TIMEOUT}M",
        }
        parameters.update(await self.get_digest())

        xml = template.format(**parameters)
        _LOGGER.debug("Reolink %s requesting ONVIF pull point message", self.nvr_name)

        timeout = aiohttp.ClientTimeout(total=LONG_POLL_TIMEOUT * 60 + 30, connect=self.timeout)

        try:
            response = await self.subscription_send(headers, xml, timeout, mutex=self._long_poll_mutex)
        except ReolinkError as err:
            raise SubscriptionError(f"Failed to request pull point message: {str(err)}") from err

        root = XML.fromstring(response)
        if root.find(".//{http://docs.oasis-open.org/wsn/b-2}NotificationMessage") is None:
            _LOGGER.debug("Reolink %s received ONVIF pull point message without event", self.nvr_name)
            return []

        _LOGGER.debug("Reolink %s received ONVIF pull point event", self.nvr_name)

        return await self.ONVIF_event_callback(response, root)

    async def unsubscribe(self, sub_type: SubType = SubType.all):
        """Unsubscribe from ONVIF events."""
        if sub_type == SubType.all:
            await self.unsubscribe(SubType.push)
            await self.unsubscribe(SubType.long_poll)
            return

        if sub_type in self._subscription_manager_url:
            headers = templates.HEADERS
            headers.update(templates.UNSUBSCRIBE_ACTION)
            template = templates.UNSUBSCRIBE_XML

            parameters = {"To": self._subscription_manager_url[sub_type]}
            parameters.update(await self.get_digest())

            xml = template.format(**parameters)

            try:
                await self.subscription_send(headers, xml)
            except ReolinkError as err:
                _LOGGER.error("Error while unsubscribing %s: %s", sub_type, str(err))

            self._subscription_manager_url.pop(sub_type, None)

        self._subscription_termination_time.pop(sub_type, None)
        self._subscription_time_difference.pop(sub_type, None)
        return

    async def unsubscribe_all(self, sub_type: SubType = SubType.all):
        """Unsubscribe from ONVIF events. Normally only needed during entry initialization/setup, to free possibly dangling subscriptions."""
        await self.unsubscribe(sub_type)

        if self._is_nvr and sub_type in [SubType.push, SubType.all]:
            _LOGGER.debug("Attempting to unsubscribe previous (dead) sessions notifications...")

            headers = templates.HEADERS
            headers.update(templates.UNSUBSCRIBE_ACTION)
            template = templates.UNSUBSCRIBE_XML

            # These work for RLN8-410 NVR, so up to 3 maximum subscriptions on it
            for idx in range(0, 3):
                parameters = {"To": f"http://{self._host}:{self._onvif_port}/onvif/Notification?Idx=00_{idx}"}
                parameters.update(await self.get_digest())
                xml = template.format(**parameters)
                try:
                    await self.subscription_send(headers, xml)
                except ReolinkError as err:
                    _LOGGER.debug("Expected error from unsubscribing all: %s", str(err))

        return True

    async def ONVIF_event_callback(self, data: str, root: XML.Element | None = None) -> list[int] | None:
        """Handle incoming ONVIF event from the webhook called by the Reolink device."""
        _LOGGER_DATA.debug("ONVIF event callback from '%s' received payload:\n%s", self.nvr_name, data)

        event_channels: list[int] = []
        contains_channels = False

        sub_type: Literal[SubType.push, SubType.long_poll]
        if root is None:
            sub_type = SubType.push
            root = XML.fromstring(data)
        else:
            sub_type = SubType.long_poll
        for message in root.iter("{http://docs.oasis-open.org/wsn/b-2}NotificationMessage"):
            channel = None

            # find NotificationMessage Rule (type of event)
            topic_element = message.find("{http://docs.oasis-open.org/wsn/b-2}Topic[@Dialect='http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet']")
            if topic_element is None or topic_element.text is None:
                continue
            rule = basename(topic_element.text)
            if not rule:
                continue

            # find camera channel
            if self.num_cameras == 1:
                channel = self.channels[0]
            else:
                source_element = message.find(".//{http://www.onvif.org/ver10/schema}SimpleItem[@Name='Source']")
                if source_element is None:
                    source_element = message.find(".//{http://www.onvif.org/ver10/schema}SimpleItem[@Name='VideoSourceConfigurationToken']")
                if source_element is not None and "Value" in source_element.attrib:
                    try:
                        channel = int(source_element.attrib["Value"])
                    except ValueError:
                        if f"ONVIF_{rule}_invalid_channel" not in self._log_once:
                            self._log_once.append(f"ONVIF_{rule}_invalid_channel")
                            _LOGGER.warning("Reolink ONVIF event '%s' data contained invalid channel '%s', issuing poll instead", rule, source_element.attrib["Value"])

            if channel is None:
                # Unknown which channel caused the event, poll all channels
                if f"ONVIF_{rule}_no_channel" not in self._log_once:
                    self._log_once.append(f"ONVIF_{rule}_no_channel")
                    _LOGGER.warning("Reolink ONVIF event '%s' does not contain channel", rule)
                if not await self.get_motion_state_all_ch():
                    _LOGGER.error("Could not poll motion state after receiving ONVIF event with unknown channel")
                return None

            if channel not in self.channels:
                # Channel has no camera connected, ignoring this notification
                contains_channels = True
                continue

            key = "State"
            if rule == "Motion":
                key = "IsMotion"
            data_element = message.find(f".//\u007bhttp://www.onvif.org/ver10/schema\u007dSimpleItem[@Name='{key}']")
            if data_element is None or "Value" not in data_element.attrib:
                if f"ONVIF_{rule}_no_data" not in self._log_once:
                    self._log_once.append(f"ONVIF_{rule}_no_data")
                    _LOGGER.warning("ONVIF event '%s' did not contain data:\n%s", rule, data)
                continue

            if rule not in ["Motion", "MotionAlarm", "FaceDetect", "PeopleDetect", "VehicleDetect", "DogCatDetect", "Package", "Visitor"]:
                if f"ONVIF_unknown_{rule}" not in self._log_once:
                    self._log_once.append(f"ONVIF_unknown_{rule}")
                    _LOGGER.warning("ONVIF event with unknown rule: '%s'", rule)
                continue

            if channel not in event_channels:
                event_channels.append(channel)
            if rule in ["FaceDetect", "PeopleDetect", "VehicleDetect", "DogCatDetect", "Package", "Visitor"]:
                self._onvif_only_motion[sub_type] = False

            state = data_element.attrib["Value"] == "true"
            _LOGGER.debug("Reolink %s ONVIF event channel %s, %s: %s", self.nvr_name, channel, rule, state)

            if rule == "Motion":
                self._motion_detection_states[channel] = state
            elif rule == "MotionAlarm":
                self._motion_detection_states[channel] = state
            elif rule == "FaceDetect":
                self._ai_detection_states[channel]["face"] = state
            elif rule == "PeopleDetect":
                self._ai_detection_states[channel]["people"] = state
            elif rule == "VehicleDetect":
                self._ai_detection_states[channel]["vehicle"] = state
            elif rule == "DogCatDetect":
                self._ai_detection_states[channel]["dog_cat"] = state
            elif rule == "Package":
                self._ai_detection_states[channel]["package"] = state
            elif rule == "Visitor":
                self._visitor_states[channel] = state

        if not event_channels and not contains_channels:
            # ONVIF notification withouth known events
            if "ONVIF_no_known" not in self._log_once:
                self._log_once.append("ONVIF_no_known")
                _LOGGER.warning("Reolink ONVIF notification received withouth any known events:\n%s", data)
            if not await self.get_motion_state_all_ch():
                _LOGGER.error("Could not poll motion state after receiving ONVIF event without any known events")
            return None

        if self._onvif_only_motion[sub_type] and any(self.ai_supported(ch) for ch in event_channels):
            # Poll all other states since not all cameras have rich notifications including the specific events
            if f"ONVIF_only_motion_{sub_type}" not in self._log_once:
                self._log_once.append(f"ONVIF_only_motion_{sub_type}")
                _LOGGER.debug("Reolink model '%s' appears to not support rich notifications for %s", self.model, sub_type)
            if not await self.get_ai_state_all_ch():
                _LOGGER.error("Could not poll AI event state after receiving ONVIF event with only motion event")
            return None

        return event_channels


class Chime:
    """Reolink chime class."""

    def __init__(self, host: Host, dev_id: int, channel: int):
        self.host = host
        self.dev_id = dev_id
        self.channel = channel
        self.name: str = "Chime"
        self.volume: int | None = None
        self.led_state: bool | None = None
        self.connect_state: int | None = None
        self.event_info: dict[str, dict[str, int]] | None = None

    def __repr__(self):
        return f"<Chime name: {self.name}, id: {self.dev_id}, ch: {self.channel}, volume: {self.volume}, online: {self.online}>"

    @property
    def chime_event_types(self) -> list[str]:
        if self.event_info is None:
            return []
        return list(self.event_info.keys())

    @property
    def online(self) -> bool:
        if self.connect_state == 2:
            return True
        return False

    def tone(self, event_type: str) -> int | None:
        if self.event_info is None:
            return None
        state = self.event_info.get(event_type, {}).get("switch")
        if state is None:
            return None
        if state != 1:
            return -1
        return self.event_info.get(event_type, {}).get("musicId")

    async def play(self, tone_id: int) -> None:
        tone_id_list = [val.value for val in ChimeToneEnum]
        tone_id_list.remove(-1)
        if tone_id not in tone_id_list:
            raise InvalidParameterError(f"play_chime: tone_id {tone_id} not in {tone_id_list}")

        body = [{"cmd": "DingDongOpt", "action": 0, "param": {"DingDong": {"channel": self.channel, "id": self.dev_id, "option": 4, "musicId": tone_id}}}]
        await self.host.send_setting(body)

    async def set_option(self, volume: int | None = None, led: bool | None = None) -> None:
        if self.volume is None or self.led_state is None:
            await self.host.get_state("DingDongOpt")

        if volume is None:
            volume = self.volume
        elif volume < 0 or volume > 4:
            raise InvalidParameterError(f"set_chime_volume: value {volume} not in 0-4")
        if led is None:
            led = self.led_state
        led_state = 1 if led else 0

        body = [
            {
                "cmd": "DingDongOpt",
                "action": 0,
                "param": {"DingDong": {"channel": self.channel, "id": self.dev_id, "option": 3, "name": self.name, "volLevel": volume, "ledState": led_state}},
            }
        ]
        await self.host.send_setting(body, getcmd="DingDongOpt")

    async def set_tone(self, event_type: str, tone_id: int) -> None:
        tone_id_list = [val.value for val in ChimeToneEnum]
        if tone_id not in tone_id_list:
            raise InvalidParameterError(f"set_chime_tone: tone_id {tone_id} not in {tone_id_list}")
        if event_type not in self.chime_event_types:
            raise InvalidParameterError(f"set_chime_tone: event type '{event_type}' not supported, supported types are {self.chime_event_types}")

        state = 1
        if tone_id == -1:
            current_tone_id = self.tone(event_type)
            if current_tone_id == -1 or current_tone_id is None:
                tone_id = 1
            else:
                tone_id = current_tone_id
            state = 0

        body = [
            {
                "cmd": "SetDingDongCfg",
                "action": 0,
                "param": {"DingDongCfg": {"channel": self.channel, "ringId": self.dev_id, "type": {event_type: {"switch": state, "musicId": tone_id}}}},
            }
        ]
        await self.host.send_setting(body)

    async def remove(self) -> None:
        body = [{"cmd": "DingDongOpt", "action": 0, "param": {"DingDong": {"channel": self.channel, "id": self.dev_id, "option": 1}}}]
        await self.host.send_setting(body)
