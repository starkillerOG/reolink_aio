"""Utility functions for Reolink"""

from __future__ import annotations

import re
from datetime import datetime
from datetime import tzinfo as _TzInfo
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .typings import reolink_json


def reolink_time_to_datetime(time: dict[str, int] | str, tzinfo: Optional[_TzInfo] = None) -> datetime:
    if isinstance(time, str):
        return datetime(year=int(time[0:4]), month=int(time[4:6]), day=int(time[6:8]), hour=int(time[8:10]), minute=int(time[10:12]), second=int(time[12:14]), tzinfo=tzinfo)
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


def to_reolink_time_id(time: dict[str, int] | datetime) -> str:
    if isinstance(time, dict):
        return f"{time['year']}{time['mon']:02}{time['day']:02}{time['hour']:02}{time['min']:02}{time['sec']:02}"
    return f"{time.year}{time.month:02}{time.day:02}{time.hour:02}{time.minute:02}{time.second:02}"


def strip_model_str(string: str) -> str:
    string = re.sub("[(].*?[)]", "", string)
    string = re.sub("[（].*?[）]", "", string)
    return string.replace(" ", "")


def search_channel(body: reolink_json) -> int | None:
    ch: int | None = None
    try:
        par = body[0].get("param", {})
        ch = par.get("channel")
        if ch is None:
            params: dict = next(iter(par.values()), {})
            ch = params.get("channel")
    except AttributeError:
        pass
    return ch
