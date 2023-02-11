""" Enums for reolink features """

from enum import Enum


class SpotlightModeEnum(Enum):
    """Options for the spotlight mode"""

    Off = 0
    Auto = 1
    Schedule = 3


class DayNightEnum(Enum):
    """Options for the DayNight setting"""

    Auto = "Auto"
    Color = "Color"
    BlackWhite = "Black&White"
