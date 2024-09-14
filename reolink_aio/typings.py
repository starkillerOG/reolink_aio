""" Typings for type validation and documentation """

from __future__ import annotations

import logging
from enum import IntFlag, auto
from typing import (
    Any,
    Callable,
    ClassVar,
    Collection,
    DefaultDict,
    Iterator,
    Mapping,
    NamedTuple,
    Optional,
    TypedDict,
)
import datetime as dtc
from typing_extensions import SupportsIndex

from aiohttp import StreamReader
from .utils import reolink_time_to_datetime

_LOGGER = logging.getLogger(__name__)

reolink_json = list[dict[str, Any]]

cmd_list_type = DefaultDict[str, DefaultDict[int | None, int]] | dict[str, list[int]] | None

SearchStatus = TypedDict("SearchStatus", {"mon": int, "table": str, "year": int})

SearchTime = TypedDict(
    "SearchTime",
    {"year": int, "mon": int, "day": int, "hour": int, "min": int, "sec": int},
)

SearchFile = TypedDict(
    "SearchFile",
    {
        "StartTime": SearchTime,
        "EndTime": SearchTime,
        "frameRate": int,
        "height": int,
        "width": int,
        "name": str,
        "size": int,
        "type": str,
    },
)

GetTimeDst = TypedDict(
    "GetTimeDst",
    {
        "enable": bool,
        "offset": int,
    },
)

GetTimeDstRule = TypedDict(
    "GetTimeDstRule",
    {
        "Mon": int,
        "Week": int,
        "Weekday": int,
        "Hour": int,
        "Min": int,
        "Sec": int,
    },
)

GetTime = TypedDict(
    "GetTime",
    {
        "year": int,
        "mon": int,
        "day": int,
        "hour": int,
        "min": int,
        "sec": int,
        "hourFmt": int,
        "timeFmt": str,
        "timeZone": int,
    },
)

GetTimeResponse = TypedDict(
    "GetTimeResponse",
    {
        "Dst": GetTimeDst,
        "Time": GetTime,
    },
)


class Reolink_timezone(dtc.tzinfo):
    """Reolink DST timezone implementation"""

    __slots__ = ("_dst", "_offset", "_year_cache", "_name")
    _cache: ClassVar[dict[tuple, dtc.tzinfo]] = {}

    @staticmethod
    def _create_dst_rule_calculator(data: Mapping[str, Any], prefix: str):
        month: int = data[f"{prefix}Mon"]
        week: int = data[f"{prefix}Week"]
        weekday: int = data[f"{prefix}Weekday"]
        # reolink start of week is sunday, python start of week is monday
        weekday = (weekday - 1) % 7
        hour: int = data[f"{prefix}Hour"]
        minute: int = data[f"{prefix}Min"]
        second: int = data[f"{prefix}Sec"]
        __time = dtc.time(hour, minute, second)

        def _date(year: int):
            # 5 is the last week of the month, not necessarily the 5th week
            if week == 5:
                if month < 12:
                    _month = month + 1
                else:
                    _month = 1
                    year += 1
                __date = dtc.date(year, _month, 1)
            else:
                __date = dtc.date(year, month, 1) + dtc.timedelta(weeks=week)
            if __date.weekday() < weekday:
                __date -= dtc.timedelta(weeks=1)
            __date += dtc.timedelta(days=__date.weekday() - weekday)
            return __date

        def _datetime(year: int):
            return dtc.datetime.combine(_date(year), __time)

        return _datetime

    @classmethod
    def create_or_get(cls, data: Mapping[str, Any]) -> dtc.tzinfo:
        """return cached or new tzinfo instance for the provided GetTimeResponse"""

        key: tuple = (data["Time"]["timeZone"],)
        # if dst is not enabled, just make a tz off of the offset only
        if bool(data["Dst"]["enable"]):
            # key is full dst ruleset incase two devices (or the rules on a device) change/are different
            dst_keys = list(data["Dst"].items())
            dst_keys.sort(key=lambda kvp: kvp[0])
            key += tuple(dst_keys)
        if cached := cls._cache.get(key):
            return cached
        if not bool(data["Dst"]["enable"]):
            # simple tz info
            offset = dtc.timedelta(seconds=data["Time"]["timeZone"])
            return cls._cache.setdefault(key, dtc.timezone(offset))

        return cls._cache.setdefault(key, cls(data))

    def __init__(self, data: Mapping[str, Any]) -> None:
        super().__init__()

        self._dst = dtc.timedelta(hours=data["Dst"]["offset"]) if bool(data["Dst"]["enable"]) else dtc.timedelta(hours=0)
        # Reolink does a positive UTC offset but python expects a negative one
        self._offset = dtc.timedelta(seconds=-data["Time"]["timeZone"])

        start_rule = self._create_dst_rule_calculator(data["Dst"], "start")
        end_rule = self._create_dst_rule_calculator(data["Dst"], "end")

        class _Cache(dict[int, tuple[dtc.datetime, dtc.datetime]]):
            def __missing__(self, key):
                self[key] = (start_rule(key), end_rule(key))
                return self[key]

        self._year_cache = _Cache()
        self._name: str | None = None

    def tzname(self, __dt: dtc.datetime | None) -> str:
        if not (isinstance(__dt, dtc.datetime) or __dt is None):
            raise TypeError("tzname() argument must be a datetime instance or None")

        if __dt is None:
            if self._name is None:
                self._name = f"UTC{self._delta_str(self._offset)}"
            return self._name
        delta = self.utcoffset(__dt)
        return f"UTC{self._delta_str(delta)}"

    @staticmethod
    def _delta_str(delta: dtc.timedelta):
        if not delta:
            return ""
        if delta < dtc.timedelta(0):
            sign = "-"
            delta = -delta
        else:
            sign = "+"
        hours, rest = divmod(delta, dtc.timedelta(hours=1))
        minutes, rest = divmod(rest, dtc.timedelta(minutes=1))
        seconds = rest.seconds
        microseconds = rest.microseconds
        if microseconds:
            return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}" f".{microseconds:06d}"
        if seconds:
            return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"

        return f"{sign}{hours:02d}:{minutes:02d}"

    def _normalize(self, __dt: dtc.datetime) -> dtc.datetime:
        if __dt.tzinfo is not None:
            if __dt.tzinfo is not self:
                # flatten to utc plus our offset to shift into our timezone
                __dt = __dt.astimezone(dtc.timezone.utc) + self._offset
            # treat datetime as "local time" by removing tzinfo
            __dt = __dt.replace(tzinfo=None)
        return __dt

    def utcoffset(self, __dt: dtc.datetime | None) -> dtc.timedelta:
        if not (isinstance(__dt, dtc.datetime) or __dt is None):
            raise TypeError("utcoffset() argument must be a datetime instance or None")

        if __dt is None:
            return self._offset
        __dt = self._normalize(__dt)
        (start, end) = self._year_cache[__dt.year]
        if start <= __dt <= end:
            return self._offset + self._dst
        return self._offset

    def dst(self, __dt: dtc.datetime | None) -> dtc.timedelta:
        if not (isinstance(__dt, dtc.datetime) or __dt is None):
            raise TypeError("dst() argument must be a datetime instance or None")

        if __dt is None:
            return self._dst
        __dt = self._normalize(__dt)
        (start, end) = self._year_cache[__dt.year]
        if start <= __dt <= end:
            return self._dst
        return dtc.timedelta(0)

    def __repr__(self):
        return f"{self.__class__.__module__}.{self.__class__.__qualname__}(dst={repr(self._dst)}, offset={repr(self._offset)})"

    def __str__(self):
        return self.tzname(None)


class VOD_search_status(Collection[dtc.date]):
    """Contains information about a VOD search."""

    def __init__(self, data: dict[str, Any]) -> None:
        # {'year': 2023, 'mon': 4, 'table':'001000000000001000000000001000'}
        self.data = data
        # "cache" so we only parse the table once on demand
        self._days: Optional[tuple[int, ...]] = None

    def __repr__(self):
        return f"<VOD_search_status: year {self.year}, month {self.month}, days {self.days}>"

    @property
    def year(self) -> int:
        """Year of the status"""
        return self.data["year"]

    @property
    def month(self) -> int:
        """Month of the status"""
        return self.data["mon"]

    def _ensure_days(self):
        if self._days is None:
            self._days = tuple(day for day, set in enumerate(self.data["table"], 1) if set == "1")
        return self._days

    @property
    def days(self) -> tuple[int, ...]:
        return self._ensure_days()

    def __getitem__(self, __index: SupportsIndex) -> dtc.date:
        return dtc.date(self.year, self.month, self._ensure_days()[__index])

    def __iter__(self) -> Iterator[dtc.date]:
        def _date(day: int):
            return dtc.date(self.year, self.month, day)

        return map(_date, self._ensure_days())

    def __len__(self) -> int:
        if self._days is None:
            return 0
        return len(self._days)

    def __contains__(self, __x: object) -> bool:
        return isinstance(__x, dtc.date) and self.year == __x.year and self.month == __x.month and __x.day in self._ensure_days()


class VOD_trigger(IntFlag):
    """VOD triggers"""

    NONE = 0
    TIMER = auto()
    MOTION = auto()
    VEHICLE = auto()
    ANIMAL = auto()
    PERSON = auto()
    DOORBELL = auto()
    PACKAGE = auto()


Parsed_VOD_file_name = NamedTuple(
    "Parsed_VOD_file_name",
    [
        ("path", str),
        ("ext", str),
        ("date", dtc.date),
        ("start", dtc.time),
        ("end", dtc.time),
        ("triggers", VOD_trigger),
    ],
)

VOD_download = NamedTuple(
    "VOD_download",
    [
        ("length", int),
        ("filename", str),
        ("stream", StreamReader),
        ("close", Callable[[], None]),
        ("etag", Optional[str]),
    ],
)


class VOD_file:
    """Contains information about the VOD file."""

    def __init__(self, data: dict[str, Any], tzinfo: Optional[dtc.tzinfo] = None) -> None:
        # {'EndTime': {'day': 17, 'hour': 2, 'min': 43, 'mon': 4, 'sec': 50, 'year': 2023},
        # 'PlaybackTime': {'day': 16, 'hour': 23, 'min': 59, 'mon': 4, 'sec': 57, 'year': 2023},
        # 'StartTime': {'day': 17, 'hour': 1, 'min': 59, 'mon': 4, 'sec': 57, 'year': 2023},
        # 'frameRate': 0,
        # 'height': 0,
        # 'size': '113246208',
        # 'type': 'sub',
        # 'width': 0}
        self.data = data
        self.tzinfo = tzinfo
        self.__parsed_name: Parsed_VOD_file_name | None = None

    def __repr__(self):
        return f"<VOD_file: {self.type} stream, start {self.start_time}, duration {self.duration}>"

    @property
    def type(self) -> str:
        """Playback time of the recording."""
        return self.data["type"]

    @property
    def start_time(self) -> dtc.datetime:
        """Start time of the recording."""
        return reolink_time_to_datetime(self.data["StartTime"], self.tzinfo)

    @property
    def end_time(self) -> dtc.datetime:
        """End time of the recording."""
        return reolink_time_to_datetime(self.data["EndTime"], self.tzinfo)

    @property
    def playback_time(self) -> dtc.datetime:
        """Playback time of the recording."""
        return reolink_time_to_datetime(self.data["PlaybackTime"], dtc.timezone.utc)

    @property
    def duration(self) -> dtc.timedelta:
        """duration of the recording."""
        return self.end_time - self.start_time

    @property
    def file_name(self) -> str:
        """file name of recording."""
        if "name" in self.data:
            return self.data["name"]

        return self.playback_time.astimezone(tz=dtc.timezone.utc).strftime("%Y%m%d%H%M%S")

    @property
    def size(self) -> int:
        """file size of the recording."""
        return self.data["size"]

    @property
    def triggers(self) -> VOD_trigger:
        """events that triggered the recording"""
        parsed = self._ensure_parsed_file_name()
        if parsed is None:
            return VOD_trigger.NONE
        return parsed.triggers

    def _ensure_parsed_file_name(self) -> Parsed_VOD_file_name | None:
        if "name" not in self.data:
            return None
        if self.__parsed_name is None:
            self.__parsed_name = parse_file_name(self.data["name"], self.tzinfo)
        return self.__parsed_name


def parse_file_name(file_name: str, tzInfo: Optional[dtc.tzinfo] = None) -> Parsed_VOD_file_name | None:
    # Mp4Record/2023-04-26/RecS02_DST20230426_145918_150032_2B14808_32F1DF.mp4
    # Mp4Record/2020-12-22/RecM01_20201222_075939_080140_6D28808_1A468F9.mp4
    # "/mnt/sda/<UID>-<NAME>/Mp4Record/2024-08-27/RecM02_DST20240827_090302_090334_0_800_800_033C820000_61B6F0.mp4"
    # https://github.com/sven337/ReolinkLinux/wiki/Figuring-out-the-file-names

    (path_name, ext) = file_name.rsplit(".", 2)
    name = path_name.rsplit("/", 1)[-1]
    split = name.split("_")

    if not split[0].startswith("Rec") or len(split[0]) != 6:
        _LOGGER.debug("%s does not match known formats, could not find version", file_name)
        return None
    version = int(split[0][5])

    dev_type = "cam"
    if len(split) == 6:
        # RecM01_20201222_075939_080140_6D28808_1A468F9
        (_, start_date, start_time, end_time, hex_value, _filesize) = split
    elif len(split) == 9:
        # RecM02_DST20240827_090302_090334_0_800_800_033C820000_61B6F0
        dev_type = "hub"
        (_, start_date, start_time, end_time, _animal_type, _width, _height, hex_value, _filesize) = split
    else:
        _LOGGER.debug("%s does not match known formats, unknown length", file_name)
        return None

    if version not in FLAGS_MAPPING[dev_type]:
        new_version = max(FLAGS_MAPPING[dev_type].keys())
        _LOGGER.debug("%s has version %s, with hex lenght %s which is not yet known, using version %s instead", file_name, version, len(hex_value), new_version)
        version = new_version

    if len(hex_value) != FLAGS_LENGTH[dev_type].get(version, 0):
        _LOGGER.debug("%s with version %s has unexpected hex lenght %s, expected %s", file_name, version, len(hex_value), FLAGS_LENGTH[dev_type].get(version, 0))

    flag_values = decode_hex_to_flags(hex_value, version, dev_type)

    triggers = VOD_trigger.NONE
    if flag_values["ai_pd"]:
        triggers |= VOD_trigger.PERSON
    if flag_values["ai_vd"]:
        triggers |= VOD_trigger.VEHICLE
    if flag_values["ai_ad"]:
        triggers |= VOD_trigger.ANIMAL
    if flag_values["is_schedule_record"]:
        triggers |= VOD_trigger.TIMER
    if flag_values["is_motion_record"]:
        triggers |= VOD_trigger.MOTION
    if flag_values["is_doorbell_record"]:
        triggers |= VOD_trigger.DOORBELL
    if flag_values.get("package_event"):
        triggers |= VOD_trigger.PACKAGE

    start_date = start_date.lower().replace("dst", "")
    start = dtc.datetime.strptime(start_date + start_time, "%Y%m%d%H%M%S").replace(tzinfo=tzInfo)
    end = dtc.datetime.strptime(start_date + end_time, "%Y%m%d%H%M%S").replace(tzinfo=tzInfo) if end_time != "000000" else start

    return Parsed_VOD_file_name(name, ext, start.date(), start.time(), end.time(), triggers)


def decode_hex_to_flags(hex_value: str, version: int, dev_type: str) -> dict[str, int]:
    hex_int = int(hex_value, 16)
    hex_int_rev = int(bin(hex_int)[2:].zfill(len(hex_value) * 4)[::-1], 2)  # reverse the binary
    flag_values = {}

    for flag, (bit_position, bit_size) in FLAGS_MAPPING[dev_type][version].items():
        mask = ((1 << bit_size) - 1) << bit_position
        flag_val_rev = (hex_int_rev & mask) >> bit_position
        flag_values[flag] = int(bin(flag_val_rev)[2:].zfill(bit_size)[::-1], 2)  # reverse the segment back

    return flag_values


FLAGS_CAM_V2 = {
    "resolution_index": (0, 7),
    "tv_system": (7, 1),
    "framerate": (8, 7),
    "audio_index": (15, 2),
    "ai_pd": (17, 1),  # person detection
    "ai_fd": (18, 1),  # face detection
    "ai_vd": (19, 1),  # vehicle detection
    "ai_ad": (20, 1),  # animal detection
    "encoder_type_index": (21, 2),
    "is_schedule_record": (23, 1),
    "is_motion_record": (24, 1),
    "is_rf_record": (25, 1),
    "is_doorbell_record": (26, 1),
    "ai_other": (27, 1),
}

FLAGS_HUB_V0 = {
    "resolution_index": (0, 7),
    "tv_system": (7, 1),
    "framerate": (8, 7),
    "audio_index": (15, 2),
    "ai_pd": (17, 1),  # person detection
    "ai_fd": (18, 1),  # face detection
    "ai_vd": (19, 1),  # vehicle detection
    "ai_ad": (20, 1),  # animal detection
    "encoder_type_index": (21, 2),
    "is_schedule_record": (23, 1),
    "is_motion_record": (24, 1),
    "is_rf_record": (25, 1),
    "is_doorbell_record": (26, 1),
    "is_ai_other_record": (27, 1),
    "picture_layout_index": (28, 7),
    "package_delivered": (35, 1),
    "package_takenaway": (36, 1),
}

FLAGS_HUB_V1 = FLAGS_HUB_V0
FLAGS_HUB_V1.update(
    {
        "package_event": (37, 1),
    }
)


FLAGS_LENGTH = {
    "cam": {
        2: 7,  # Version 2
        3: 7,  # Version 3
        4: 9,  # Version 4
        9: 14,  # Version 9
    },
    "hub": {
        2: 10,  # Version 2
    },
}

FLAGS_MAPPING = {
    "cam": {
        2: FLAGS_CAM_V2,
        3: FLAGS_CAM_V2,
        4: FLAGS_CAM_V2,
        9: FLAGS_CAM_V2,
    },
    "hub": {
        0: FLAGS_HUB_V0,
        1: FLAGS_HUB_V1,
        2: {  # Version 2
            "resolution_index": (0, 7),
            "tv_system": (7, 1),
            "framerate": (8, 7),
            "audio_index": (15, 2),
            "ai_pd": (17, 1),  # person detection
            "ai_fd": (18, 1),  # face detection
            "ai_vd": (19, 1),  # vehicle detection
            "ai_ad": (20, 1),  # animal detection
            "ai_other": (21, 2),
            "encoder_type_index": (23, 1),
            "is_schedule_record": (24, 1),
            "is_motion_record": (25, 1),
            "is_rf_record": (26, 1),
            "is_doorbell_record": (27, 1),
            "picture_layout_index": (28, 7),
            "package_delivered": (35, 1),
            "package_takenaway": (36, 1),
            "package_event": (37, 1),
            "upload_flag": (38, 1),
        },
    },
}
