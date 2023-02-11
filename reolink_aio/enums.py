""" Enums for reolink features """

from enum import Enum

class SpotlightModeEnum(Enum):
    Off = 0
    Auto = 1
    Schedule = 3

class DayNightEnum(Enum):
    Auto = "Auto"
    Color = "Color"
    BlackWhite = "Black&White"
