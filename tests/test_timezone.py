"""Tests for the Reolink DST timezone."""

from __future__ import annotations

import datetime as dtc
import unittest
from typing import Any
from zoneinfo import ZoneInfo

from reolink_aio.typings import Reolink_timezone


def _time_settings(utc_offset: int, start: tuple[int, int], end: tuple[int, int]) -> dict[str, Any]:
    """GetTime value with DST on Sundays from start (month, week) 02:00 to end 03:00."""
    return {
        "Time": {"timeZone": -utc_offset * 3600},
        "Dst": {
            "enable": 1,
            "offset": 1,
            "startMon": start[0],
            "startWeek": start[1],
            "startWeekday": 0,
            "startHour": 2,
            "startMin": 0,
            "startSec": 0,
            "endMon": end[0],
            "endWeek": end[1],
            "endWeekday": 0,
            "endHour": 3,
            "endMin": 0,
            "endSec": 0,
        },
    }


class TestReolinkTimezone(unittest.TestCase):
    def assert_matches_zoneinfo(self, data: dict[str, Any], zone: str) -> None:
        tz = Reolink_timezone.create_or_get(data)
        ref = ZoneInfo(zone)
        wrong: list[dtc.date] = []
        day = dtc.datetime(2024, 1, 1, 12)
        while day.year < 2031:
            aware = day.replace(tzinfo=ref)
            if (tz.utcoffset(day), tz.dst(day)) != (aware.utcoffset(), aware.dst()):
                wrong.append(day.date())
            day += dtc.timedelta(days=1)
        self.assertEqual(wrong, [])

    def test_last_week(self) -> None:
        """Last Sunday of March to last Sunday of October."""
        self.assert_matches_zoneinfo(_time_settings(1, (3, 5), (10, 5)), "Europe/Berlin")

    def test_nth_week(self) -> None:
        """Second Sunday of March to first Sunday of November."""
        self.assert_matches_zoneinfo(_time_settings(-5, (3, 2), (11, 1)), "America/New_York")

    def test_start_after_end(self) -> None:
        """First Sunday of October to first Sunday of April."""
        self.assert_matches_zoneinfo(_time_settings(10, (10, 1), (4, 1)), "Australia/Sydney")


if __name__ == "__main__":
    unittest.main()
