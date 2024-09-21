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


class VodRequestType(Enum):
    """VOD url request types"""

    RTMP = "RTMP"
    PLAYBACK = "playback"
    FLV = "FLV"


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
    alwayson = "Always"


class DayNightEnum(Enum):
    """Options for the DayNight setting"""

    auto = "Auto"
    color = "Color"
    blackwhite = "Black&White"


class HDREnum(Enum):
    """Options for the HDR setting"""

    off = 0
    on = 2
    auto = 1


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


class BatteryEnum(Enum):
    """Battery status"""

    discharging = 0
    charging = 1
    chargecomplete = 2


class ChimeToneEnum(Enum):
    """Chime ringtone"""

    off = -1
    citybird = 0
    originaltune = 1
    pianokey = 2
    loop = 3
    attraction = 4
    hophop = 5
    goodday = 6
    operetta = 7
    moonlight = 8
    waybackhome = 9


class HubToneEnum(Enum):
    """Hub ringtone"""

    alarm = -1
    citybird = 0
    originaltune = 1
    pianokey = 2
    loop = 3
    attraction = 4
    hophop = 5
    goodday = 6
    operetta = 7
    moonlight = 8
    waybackhome = 9
