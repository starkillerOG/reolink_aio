"""Reolink NVR/camera API."""
from __future__ import annotations

import re
from datetime import datetime

from .exceptions import UnexpectedDataError

MINIMUM_FIRMWARE = {
    "RLN8-410": {
        "N3MB01": "v3.3.0.226_23031609",
        "H3MB02": "v2.0.0.4732_1728_21062800",
        "H3MB16": "v2.0.0.280_21060101",
        "N2MB02": "v3.3.0.226_23031644",
        "H3MB18": "v3.3.0.226_23031644",
        "N7MB01": "v3.3.0.226_23031632",
    },
    "RLN16-410": {
        "H3MB02": "v2.0.0.4732_1728_21062800",
        "H3MB18": "v3.3.0.226_23031645",
        "N6MB01": "v3.3.0.226_23031621",
    },
    "RLN36": {
        "N5MB01": "v3.3.0.226_23031612",
    },
    "E1 Zoom": {
        "IPC_566SD65MP": "v3.1.0.1349_22092302",
        "IPC_515BSD6": "v3.0.0.1107_22070508",
        "IPC_515SD6": "v3.0.0.1107_22070508",
    },
    "RLC-410": {
        "IPC-515B16M5M": "V3.0.0.136_20121100",
        "IPC_51316M": "V3.0.0.136_20121100",
        "IPC_51516M5M": "V3.0.0.136_20121100",
    },
    "RLC-410-5MP": {
        "IPC_51516M5M": "V3.0.0.136_20121100",
    },
    "RLC-410W": {
        "IPC_30K128M4MP": "v3.1.0.739_22042505",
        "IPC_51516M5M": "v3.0.0.136_20121102",
        "IPC_515B16M5M": "v3.0.0.136_20121102",
    },
    "RLC-420": {
        "IPC_51316M": "v3.0.0.136_20121101",
        "IPC_51516M5M": "v3.0.0.136_20121101",
        "IPC_515B16M5M": "v3.0.0.136_20121101",
    },
    "RLC-510A": {
        "IPC_523128M5MP": "v3.1.0.951_22041567",
    },
    "RLC-511": {
        "IPC_51516M5M": "v3.0.0.142_20121803",
    },
    "RLC-511W": {
        "IPC_51516M5M": "v3.0.0.142_20121804",
    },
    "RLC-511WA": {
        "IPC_523128M5MP": "v3.1.0.956_22041509",
    },
    "RLC-520": {
        "IPC_51516M5M": "v3.0.0.136_20121112",
        "IPC_515B16M5M": "v3.0.0.136_20121112",
    },
    "RLC-520A": {
        "IPC_523128M5MP": "v3.1.0.951_22041566",
    },
    "RLC-522": {
        "IPC_51516M5M": "v3.0.0.136_20121111",
    },
    "RLC-810A": {
        "IPC_523128M8MP": "v3.0.0.494_21073003",
    },
    "RLC-811A": {
        "IPC_523128M8MP": "v3.1.0.989_22051908",
    },
    "RLC-812A": {
        "IPC_523B188MP": "v3.1.0.920_22040613",
    },
    "RLC-820A": {
        "IPC_523128M8MP": "v3.1.0.956_22041501",
    },
    "RLC-822A": {
        "IPC_523128M8MP": "v3.1.0.989_22081907",
    },
    "RLC-823A": {
        "IPC_523128M8MP": "v3.1.0.989_22051911_v1.0.0.30",
    },
    "Reolink Video Doorbell PoE": {
        "DB_566128M5MP_P": "v3.0.0.2033_23041302",
    },
    "Reolink Video Doorbell WiFi": {
        "DB_566128M5MP_W": "v3.0.0.2033_23041300",
    },
    "Reolink TrackMix PoE": {
        "IPC_529SD78MP": "v3.0.0.1817_23022700",
    },
    "Reolink TrackMix WiFi": {
        "IPC_529SD78MP": "v3.0.0.1817_23022701",
    },
    "Reolink Duo PoE": {
        "IPC_528B174MP": "v3.0.0.1388_22100600",
    },
    "Reolink Duo WiFi": {
        "IPC_528B174MP": "v3.0.0.1388_22100601",
    },
    "Reolink Duo 2 POE": {
        "IPC_529B17B8MP": "v3.0.0.1337_22091900",
    },
    "Reolink Duo 2 WiFi": {
        "IPC_529B17B8MP": "v3.0.0.1337_22091901",
    },
}

DEFAULT_VERSION_DATA = datetime(2000, 1, 1, 0, 0)  # 2000-01-01 00:00

version_regex = re.compile(r"^v(?P<major>[0-9]+)\.(?P<middle>[0-9]+)\.(?P<minor>[0-9]+).(?P<build>[0-9]+)_(?P<date>[0-9]+)")
version_regex_no_date = re.compile(r"^v(?P<major>[0-9]+)\.(?P<middle>[0-9]+)\.(?P<minor>[0-9]+).(?P<build>[0-9]+)")


class SoftwareVersion:
    """SoftwareVersion class"""

    def __init__(self, version_string: str | None):
        self.is_unknown = False
        self.major = 0
        self.middle = 0
        self.minor = 0
        self.build = 0
        self.date = DEFAULT_VERSION_DATA

        if version_string is None:
            self.is_unknown = True
            return

        self.version_string = version_string.lower()
        if self.version_string == "unknown":
            self.is_unknown = True
            return

        match = version_regex.match(self.version_string)
        if match is None:
            match = version_regex_no_date.match(self.version_string)
            if match is None:
                raise UnexpectedDataError(f"version_string has invalid version format: {version_string}")

        self.major = int(match.group("major"))
        self.middle = int(match.group("middle"))
        self.minor = int(match.group("minor"))
        build = match.group("build")
        if build is not None:
            self.build = int(build)
        try:
            date = match.group("date")
        except IndexError:
            date = None
        if date is not None:
            try:
                self.date = datetime.strptime(date, "%y%m%d%M")
            except ValueError:
                pass

    def __repr__(self):
        return f"<SoftwareVersion: {self.version_string}>"

    def is_greater_than(self, target_version: "SoftwareVersion"):
        if self.major > target_version.major:
            return True
        if target_version.major == self.major:
            if self.middle > target_version.middle:
                return True
            if target_version.middle == self.middle:
                if self.minor > target_version.minor:
                    return True
                if target_version.minor == self.minor:
                    if self.build > target_version.build:
                        return True

        # for beta firmware releases
        if self.date > target_version.date and self.date != DEFAULT_VERSION_DATA and target_version.date != DEFAULT_VERSION_DATA:
            return True

        return False

    def is_greater_or_equal_than(self, target_version: "SoftwareVersion"):
        if self.major > target_version.major:
            return True
        if target_version.major == self.major:
            if self.middle > target_version.middle:
                return True
            if target_version.middle == self.middle:
                if self.minor > target_version.minor:
                    return True
                if target_version.minor == self.minor:
                    if self.build >= target_version.build:
                        return True

        # for beta firmware releases
        if self.date >= target_version.date and self.date != DEFAULT_VERSION_DATA and target_version.date != DEFAULT_VERSION_DATA:
            return True

        return False

    def is_lower_than(self, target_version: "SoftwareVersion"):
        if self.major < target_version.major:
            return True
        if target_version.major == self.major:
            if self.middle < target_version.middle:
                return True
            if target_version.middle == self.middle:
                if self.minor < target_version.minor:
                    return True
                if target_version.minor == self.minor:
                    if self.build < target_version.build:
                        return True

        # for beta firmware releases
        if self.date < target_version.date and self.date != DEFAULT_VERSION_DATA and target_version.date != DEFAULT_VERSION_DATA:
            return True

        return False

    def is_lower_or_equal_than(self, target_version: "SoftwareVersion"):
        if self.major < target_version.major:
            return True
        if target_version.major == self.major:
            if self.middle < target_version.middle:
                return True
            if target_version.middle == self.middle:
                if self.minor < target_version.minor:
                    return True
                if target_version.minor == self.minor:
                    if self.build <= target_version.build:
                        return True

        # for beta firmware releases
        if self.date <= target_version.date and self.date != DEFAULT_VERSION_DATA and target_version.date != DEFAULT_VERSION_DATA:
            return True

        return False

    def equals(self, target_version: "SoftwareVersion"):
        if (
            target_version.major == self.major
            and target_version.middle == self.middle
            and target_version.minor == self.minor
            and target_version.build == self.build
            and target_version.date == self.date
        ):
            return True
        return False

    def __lt__(self, other):
        return self.is_lower_than(other)

    def __le__(self, other):
        return self.is_lower_or_equal_than(other)

    def __gt__(self, other):
        return self.is_greater_than(other)

    def __ge__(self, other):
        return self.is_greater_or_equal_than(other)

    def __eq__(self, target_version):
        if target_version.major == self.major and target_version.middle == self.middle and target_version.minor == self.minor and target_version.build == self.build:
            return True
        return False

    def generate_str_from_numbers(self):
        return f"{self.major}.{self.middle}.{self.minor}-{self.build}"


class NewSoftwareVersion(SoftwareVersion):
    """SoftwareVersion class for available software updates"""

    def __init__(self, version_string: str | None, download_url: str | None = None, release_notes: str = "", online_update_available: bool = False):
        self.download_url = download_url
        self.release_notes = release_notes
        self.online_update_available = online_update_available
        super().__init__(version_string)
