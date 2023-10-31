""" Enums for reolink features """

from enum import Enum


class SubType(str, Enum):
    """Subscription type"""

    push = "push"
    long_poll = "long_poll"
    all = "all"

    def __repr__(self) -> str:
        return self.value

    def __str__(self) -> str:
        return self.value


class SpotlightModeEnum(Enum):
    """Options for the spotlight mode"""

    off = 0
    auto = 1
    onatnight = 2
    schedule = 3
    adaptive = 5
    autoadaptive = 4


class StatusLedEnum(Enum):
    """Options for the status led mode"""

    stayoff = "KeepOff"
    auto = "Off"
    alwaysonatnight = "On"


class DayNightEnum(Enum):
    """Options for the DayNight setting"""

    auto = "Auto"
    color = "Color"
    blackwhite = "Black&White"


class PtzEnum(Enum):
    """Options for PTZ control"""

    stop = "Stop"
    left = "Left"
    right = "Right"
    up = "Up"
    down = "Down"
    zoomin = "ZoomInc"
    zoomout = "ZoomDec"


class GuardEnum(Enum):
    """Options for PTZ Guard"""

    set = "setPos"
    goto = "toPos"


class TrackMethodEnum(Enum):
    """Options for AI Track Method"""

    # off = 0
    # pantilt = 1
    digital = 2
    digitalfirst = 3
    pantiltfirst = 4
