""" Utility functions for Reolink """

from datetime import datetime


def reolink_time_to_datetime(time: dict[str, int]) -> datetime:
    return datetime(year=time["year"], month=time["mon"], day=time["day"], hour=time["hour"], minute=time["min"], second=time["sec"])


def datetime_to_reolink_time(time: datetime) -> dict[str, int]:
    t_dict = {
        "year": time.year,
        "mon": time.month,
        "day": time.day,
        "hour": time.hour,
        "min": time.minute,
        "sec": time.second,
    }
    return t_dict
