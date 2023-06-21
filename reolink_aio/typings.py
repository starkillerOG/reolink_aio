""" Typings for type validation and documentation """
from __future__ import annotations

import logging
from enum import IntFlag, auto
from typing import (
    Any,
    Callable,
    ClassVar,
    Collection,
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
    PET = auto()
    PERSON = auto()


Parsed_VOD_file_name = NamedTuple(
    "Parsed_VOD_file_name",
    [
        ("path", str),
        ("ext", str),
        ("dst", bool),
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
        return reolink_time_to_datetime(self.data["PlaybackTime"], self.tzinfo)

    @property
    def duration(self) -> dtc.timedelta:
        """duration of the recording."""
        return self.end_time - self.start_time

    @property
    def file_name(self) -> str:
        """file name of recording."""
        if "name" in self.data:
            return self.data["name"]

        return self.start_time.strftime("%Y%m%d%H%M%S")

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
            self.__parsed_name = self.parse_file_name(self.data["name"], self.tzinfo)
        return self.__parsed_name

    @staticmethod
    def parse_file_name(file_name: str, tzInfo: Optional[dtc.tzinfo] = None) -> Parsed_VOD_file_name | None:
        # Rec_20230517_043229_541_M.mp4
        # Mp4Record_2023-05-15_RecM02_20230515_071811_071835_6D28900_13CE8C7.mp4
        # Mp4Record/2023-04-26/RecS02_DST20230426_145918_150032_2B14808_32F1DF.mp4
        # Mp4Record_2020-12-21_RecM01_20201221_121551_121553_6D28808_2240A8.mp4
        # Mp4Record/2020-12-22/RecM01_20201222_075939_080140_6D28808_1A468F9.mp4
        # |----------name------------|YYYYMMDD|HHmmss|HHmmss|????Ttr|???????|ext
        # Y - year digit
        # M - month digit
        # D - day digit
        # H - hour digit
        # m - minute digit
        # s - second digit
        # T - trigger nibble1 -  1111 ?|Person|?|Vehicle
        # t - trigger nibble2 -  1111 ?|?|Pet|Timer
        # r - trigger nibble3 -  1111 Motion|?|?|?

        (name, ext) = file_name.rsplit(".", 2)
        if len(_split := name.rsplit("_", 6)) == 6:
            (name, start_date, start_time, end_time, _unk1, _unk2) = _split
            nibs = tuple(int(nib, 16) for nib in _unk1[-3:])
            _unk1 = _unk1[:-3]
        elif _split[-1] == "M":
            tzInfo = dtc.timezone.utc
            (name, start_date, start_time, _unk1) = _split
            end_time = "000000"
            nibs = (0, 0, 8)
        else:
            _LOGGER.debug("%s does not match known formats", file_name)
            return None

        triggers = VOD_trigger.NONE
        # if nibs[0] & 8 == 8:
        #     _LOGGER.debug("%s has unknown bit %s set in nibble %s", file_name, 8, 0)
        if nibs[0] & 4 == 4:
            triggers |= VOD_trigger.PERSON
        # if nibs[0] & 2 == 2:
        #     _LOGGER.debug("%s has unknown bit %s set in nibble %s", file_name, 2, 0)
        if nibs[0] & 1 == 1:
            triggers |= VOD_trigger.VEHICLE

        if nibs[1] & 8 == 8:
            triggers |= VOD_trigger.PET
        # if nibs[1] & 4 == 4:
        #     _LOGGER.debug("%s has unknown bit %s set in nibble %s", file_name, 4, 1)
        # if nibs[1] & 2 == 2:
        #     _LOGGER.debug("%s has unknown bit %s set in nibble %s", file_name, 2, 1)
        if nibs[1] & 1 == 1:
            triggers |= VOD_trigger.TIMER
        if nibs[2] & 8 == 8:
            triggers |= VOD_trigger.MOTION
        # if nibs[2] & 4 == 4:
        #     _LOGGER.debug("%s has unknown bit %s set in nibble %s", file_name, 4, 2)
        # if nibs[2] & 2 == 2:
        #     _LOGGER.debug("%s has unknown bit %s set in nibble %s", file_name, 2, 2)
        # if nibs[2] & 1 == 1:
        #     _LOGGER.debug("%s has unknown bit %s set in nibble %s", file_name, 1, 2)

        if start_date[0:3].lower() == "dst":
            dst = True
            start_date = start_date[3:]
        else:
            dst = False
        start = dtc.datetime.strptime(start_date + start_time, "%Y%m%d%H%M%S").replace(tzinfo=tzInfo)
        end = dtc.datetime.strptime(start_date + end_time, "%Y%m%d%H%M%S").replace(tzinfo=tzInfo) if end_time != "000000" else start
        return Parsed_VOD_file_name(name, ext, dst, start.date(), start.time(), end.time(), triggers)
