""" Typings for type validation and documentation """

from typing import Any, ClassVar, Mapping, TypedDict
from datetime import datetime, timedelta, timezone, date, time, tzinfo
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


class Reolink_timezone(tzinfo):
    """Reolink DST timezone implementation"""

    __slots__ = ("_dst", "_offset", "_year_cache")
    _cache: ClassVar[dict[tuple, tzinfo]] = {}

    @classmethod
    def get(cls, data: GetTimeResponse) -> tzinfo:
        """get or create timezone based on data"""

        # if dst is not enabled, just make a tz off of the offset only
        if not bool(data["Dst"]["enable"]):
            key = tuple(data["Dst"]["enable"], data["Time"]["timeZone"])
        else:
            # key is full dst ruleset incase two devices (or the rules on a device) change/are different
            key = tuple([data["Time"]["timeZone"]]) + tuple(data["Dst"].values())
        _tzinfo = cls._cache.get(key)
        if _tzinfo is not None:
            return _tzinfo
        if not bool(data["Dst"]["enable"]):
            # simple tz info
            offset = timedelta(seconds=data["Time"]["timeZone"])
            return cls._cache.setdefault(key, timezone(offset))
        # this class
        return cls._cache.setdefault(key, cls(data))

    @staticmethod
    def _create_dst_rule_caclulator(data: dict[str, Any], prefix: str):
        month: int = data[f"{prefix}Mon"]
        week: int = data[f"{prefix}Week"]
        weekday: int = data[f"{prefix}Weekday"]
        # reolink start of week is sunday, python start of week is monday
        weekday = (weekday - 1) % 7
        hour: int = data[f"{prefix}Hour"]
        minute: int = data[f"{prefix}Min"]
        second: int = data[f"{prefix}Sec"]
        __time = time(hour, minute, second)

        def _date(year: int):
            # 5 is the last week of the month, not necessarily the 5th week
            if week == 5:
                if month < 12:
                    _month = month + 1
                else:
                    _month = 1
                    year += 1
                __date = date(year, _month, 1)
            else:
                __date = date(year, month, 1) + timedelta(weeks=week)
            if __date.weekday() < weekday:
                __date -= timedelta(weeks=1)
            __date += timedelta(days=__date.weekday() - weekday)
            return __date

        def _datetime(year: int):
            return datetime.combine(_date(year), __time)

        return _datetime

    @classmethod
    def _create_cache_missed(cls, data: dict[str, Any]):
        start_rule = cls._create_dst_rule_caclulator(data, "start")
        end_rule = cls._create_dst_rule_caclulator(data, "end")

        def __missing__(self: dict, key: int):
            self[key] = tuple(start_rule(key), end_rule(key))
            return self[key]

        return __missing__

    def __init__(self, data: GetTimeResponse) -> None:
        super().__init__()
        self._dst = timedelta(hours=data["Dst"]["offset"]) if bool(data["Dst"]["enable"]) else timedelta(hours=0)
        # Reolink does a positive UTC offset but python expects a negative one
        self._offset = timedelta(seconds=-data["Time"]["timeZone"])
        self._year_cache: Mapping[int, tuple[datetime, datetime]] = {}
        setattr(self._year_cache, "__missing__", self._create_cache_missed(data))

    def tzname(self, __dt: datetime | None) -> str | None:
        # we have no friendly name though we could do GMT/UTC{self._offset}
        return None

    def _normalize(self, __dt: datetime) -> datetime:
        if __dt.tzinfo is not None:
            if __dt.tzinfo is not self:
                # flatten to utc plus our offset to shift into our timezone
                __dt = __dt.astimezone(timezone.utc) + self._offset
            # treat datetime as "local time" by removing tzinfo
            __dt = __dt.replace(tzinfo=None)
        return __dt

    def utcoffset(self, __dt: datetime | None) -> timedelta | None:
        if __dt is None:
            return self._offset
        __dt = self._normalize(__dt)
        (start, end) = self._year_cache[__dt.year]
        if start <= __dt <= end:
            return self._offset + self._dst
        return self._offset

    def dst(self, __dt: datetime | None) -> timedelta | None:
        if __dt is None:
            return self._dst
        __dt = self._normalize(__dt)
        (start, end) = self._year_cache[__dt.year]
        if start <= __dt <= end:
            return self._dst
        return timedelta(seconds=0)


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
    def start_time(self) -> datetime:
        """Start time of the recording."""
        return reolink_time_to_datetime(self.data["StartTime"])

    @property
    def utc_start_time(self) -> datetime:
        """Start time of the recording."""
        cam_hour_offset = round(self.time_offset / 3600)
        utc_offset = datetime.now(timezone.utc).astimezone().utcoffset()
        if utc_offset is None:
            utc_offset = timedelta(0)
        return self.start_time - timedelta(seconds=utc_offset.total_seconds(), hours=cam_hour_offset)

    @property
    def end_time(self) -> datetime:
        """End time of the recording."""
        return reolink_time_to_datetime(self.data["EndTime"])

    @property
    def playback_time(self) -> datetime:
        """Playback time of the recording."""
        return reolink_time_to_datetime(self.data["PlaybackTime"])

    @property
    def duration(self) -> timedelta:
        """duration of the recording."""
        return self.end_time - self.start_time

    @property
    def file_name(self) -> str:
        """duration of the recording."""
        if "name" in self.data:
            return self.data["name"]

        return self.utc_start_time.strftime("%Y%m%d%H%M%S")
