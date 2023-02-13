""" Enums for reolink features """

from enum import Enum


class SpotlightModeEnum(Enum):
    """Options for the spotlight mode"""

    off = 0
    auto = 1
    schedule = 3


class DayNightEnum(Enum):
    """Options for the DayNight setting"""

    auto = "Auto"
    color = "Color"
    blackwhite = "Black&White"
