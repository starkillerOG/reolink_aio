""" Utility functions for Reolink """
from __future__ import annotations

from datetime import datetime, tzinfo as _TzInfo
from typing import Optional


def reolink_time_to_datetime(time: dict[str, int], tzinfo: Optional[_TzInfo] = None) -> datetime:
    return datetime(year=time["year"], month=time["mon"], day=time["day"], hour=time["hour"], minute=time["min"], second=time["sec"], tzinfo=tzinfo)


def datetime_to_reolink_time(time: datetime | str) -> dict[str, int]:
    if isinstance(time, str):
        return {
            "year": int(time[0:4]),
            "mon": int(time[4:6]),
            "day": int(time[6:8]),
            "hour": int(time[8:10]),
            "min": int(time[10:12]),
            "sec": int(time[12:14]),
        }

    t_dict = {
        "year": time.year,
        "mon": time.month,
        "day": time.day,
        "hour": time.hour,
        "min": time.minute,
        "sec": time.second,
    }
    return t_dict
