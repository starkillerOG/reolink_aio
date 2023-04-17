""" Typings for type validation and documentation """

from typing import Any, TypedDict
from datetime import datetime, timedelta, timezone
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
