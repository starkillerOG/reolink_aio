""" Typings for type validation and documentation """
from __future__ import annotations

from typing import Any, ClassVar, Mapping, TypedDict
import datetime as dtc
from .utils import reolink_time_to_datetime

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

        self._dst = (
            dtc.timedelta(hours=data["Dst"]["offset"])
            if bool(data["Dst"]["enable"])
            else dtc.timedelta(hours=0)
        )
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
            return (
                f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}" f".{microseconds:06d}"
            )
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


class VOD_file:
    """Contains information about the VOD file."""

    def __init__(self, data: dict[str, Any], time_offset: float = 0) -> None:
        # {'EndTime': {'day': 17, 'hour': 2, 'min': 43, 'mon': 4, 'sec': 50, 'year': 2023},
        # 'PlaybackTime': {'day': 16, 'hour': 23, 'min': 59, 'mon': 4, 'sec': 57, 'year': 2023},
        # 'StartTime': {'day': 17, 'hour': 1, 'min': 59, 'mon': 4, 'sec': 57, 'year': 2023},
        # 'frameRate': 0,
        # 'height': 0,
        # 'size': '113246208',
        # 'type': 'sub',
        # 'width': 0}
        self.data = data
        self.time_offset = time_offset

    def __repr__(self):
        return f"<VOD_file: {self.type} stream, start {self.start_time}, duration {self.duration}>"

    @property
    def type(self) -> str:
        """Playback time of the recording."""
        return self.data["type"]

    @property
    def start_time(self) -> dtc.datetime:
        """Start time of the recording."""
        return reolink_time_to_datetime(self.data["StartTime"])

    @property
    def utc_start_time(self) -> dtc.datetime:
        """Start time of the recording."""
        cam_hour_offset = round(self.time_offset / 3600)
        utc_offset = dtc.datetime.now(dtc.timezone.utc).astimezone().utcoffset()
        if utc_offset is None:
            utc_offset = dtc.timedelta(0)
        return self.start_time - dtc.timedelta(
            seconds=utc_offset.total_seconds(), hours=cam_hour_offset
        )

    @property
    def end_time(self) -> dtc.datetime:
        """End time of the recording."""
        return reolink_time_to_datetime(self.data["EndTime"])

    @property
    def playback_time(self) -> dtc.datetime:
        """Playback time of the recording."""
        return reolink_time_to_datetime(self.data["PlaybackTime"])

    @property
    def duration(self) -> dtc.timedelta:
        """duration of the recording."""
        return self.end_time - self.start_time

    @property
    def file_name(self) -> str:
        """duration of the recording."""
        if "name" in self.data:
            return self.data["name"]

        return self.utc_start_time.strftime("%Y%m%d%H%M%S")
