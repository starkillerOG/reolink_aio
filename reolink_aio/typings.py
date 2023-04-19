""" Typings for type validation and documentation """

from typing import TYPE_CHECKING, Any, Callable, ClassVar, TypedDict
from datetime import datetime, timedelta, timezone, date, time, tzinfo
from .utils import reolink_time_to_datetime

if TYPE_CHECKING:
    from typing import cast

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
    }
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
    }
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
    }
)

GetTimeResponse = TypedDict(
    "GetTimeResponse",
    {
        "Dst": GetTimeDst,
        "Time": GetTime,
    }
)

class _DST_Rule:

    def __init__(self, data:dict[str,Any], prefix:str) -> None:
        self.data:GetTimeDstRule = data
        self.prefix = prefix

    @property
    def month(self)->int:
        """rule month"""
        return self.data[f"{self.prefix}Mon"]
    
    @property
    def week(self)->int:
        """rule week"""
        return self.data[f"{self.prefix}Week"]

    @property
    def weekday(self)->int:
        """rule weekday"""
        # reolink sun=0, python mon = 0, sun = 6
        return (self.data[f"{self.prefix}Weekday"] - 1) % 7
    
    @property
    def hour(self)->int:
        """rule hour"""
        return self.data[f"{self.prefix}Hour"]

    @property
    def minute(self)->int:
        """rule minute"""
        return self.data[f"{self.prefix}Min"]

    @property
    def second(self)->int:
        """rule second"""
        return self.data[f"{self.prefix}Sec"]

    def time(self)->time:
        """rule as time"""
        return time(self.hour, self.minute, self.second)
    
    def date(self, year:int)->date:
        """rule as date with year"""
        weekday =  self.weekday
        # 5 is the last week of the month, not necessarily the 5th week
        if self.week == 5:
            if self.month < 12:
                month = self.month + 1
            else:
                month = 1
                year += 1
            __date = date(year, month, 1)
            if __date.weekday() < weekday:
                __date -= timedelta(weeks=1)
            __date -= timedelta(days=__date.weekday() - weekday)
        else:
            __date = date(year, self.month, 1) + timedelta(weeks=self.week)
            __date += timedelta(days=__date.weekday() - weekday)
        return __date

    def datetime(self, year:int)->datetime:
        """rule as datetime with year"""
        return datetime.combine(self.date(year), self.time())

class _YearStartStopCache(dict[str,tuple[datetime,datetime]]):
    def __init__(self, factory:Callable[[int], tuple[datetime,datetime]]):
        super().__init__()
        self.factory = factory

    def __missing__(self, key:int)->tuple[datetime,datetime]:
        self[key] = self.factory(key)
        return self[key]


class Reolink_timezone(tzinfo):
    """Reolink DST timezone implementation"""

    _cache:ClassVar[dict[tuple,"Reolink_timezone"]] = {}

    @classmethod
    def get(cls, data:dict[str,Any])->"Reolink_timezone":
        """get or create timzeone based on data"""
        if TYPE_CHECKING:
            __data:GetTimeResponse = data
        else:
            __data = data

        key = tuple([__data["Time"]["timeZone"]]) + tuple(__data["Dst"].values())
        return cls._cache.setdefault(key, cls(data))

    def __init__(self, data:dict[str,Any]) -> None:
        super().__init__()
        if TYPE_CHECKING:
            __data = cast(GetTimeResponse, data)
        else:
            __data = data
        self._dst = timedelta(hours=__data["Dst"]["offset"]) if bool(__data["Dst"]["enable"]) else timedelta(hours=0)
        # Reolink does a positive UTC offset but python expects a negative one
        self._offset = timedelta(seconds=-__data["Time"]["timeZone"])
        start_rule = _DST_Rule(__data["Dst"], "start")
        end_rule = _DST_Rule(__data["Dst"], "end")
        self._year_cache = _YearStartStopCache(lambda year: (start_rule.datetime(year), end_rule.datetime(year)))

    def tzname(self, __dt: datetime | None) -> str | None:
        # we have no friendly name though we could do GMT/UTC{self._offset}
        return None

    def utcoffset(self, __dt: datetime | None) -> timedelta | None:
        if __dt is None:
            return self._offset
        if __dt.tzinfo is not None:
            __dt = __dt.replace(tzinfo=None)
        (start, end) = self._year_cache[__dt.year]
        if start <= __dt <= end:
            return self._offset + self._dst
        return self._offset

    def dst(self, __dt: datetime | None) -> timedelta | None:
        if __dt is None:
            return self._dst
        if __dt.tzinfo is not None:
            __dt = __dt.replace(tzinfo=None)
        (start, end) = self._year_cache[__dt.year]
        if start <= __dt <= end:
            self._dst
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
