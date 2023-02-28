"""Reolink NVR/camera API."""

import re

from .exceptions import UnexpectedDataError
from datetime import datetime

MINIMUM_FIRMWARE = {
    "RLN8-410": {
        "N3MB01": "v3.2.0.218_23020151",
        "H3MB02": "v2.0.0.4732_1728_21062800",
        "H3MB16": "v2.0.0.280_21060101",
        "N2MB02": "v3.2.0.218_23020153",
        "H3MB18": "v3.2.0.218_23020153",
        "N7MB01": "v3.2.0.218_23011221",
    },
    "RLN16-410": {
        "H3MB02": "v2.0.0.4732_1728_21062800",
        "H3MB18": "v3.2.0.218_23020154",
        "N6MB01": "v3.2.0.218_23011215",
    },
    "RLN36": {
        "N5MB01": "v3.2.0.218_23011219",
    },
    "E1 Zoom": {
        "IPC_566SD65MP": "v3.1.0.1349_22092302",
        "IPC_515BSD6": "v3.0.0.1107_22070508",
        "IPC_515SD6": "v3.0.0.1107_22070508",
    },
    "RLC-410W": {
        "IPC_30K128M4MP": "v3.1.0.739_22042505",
        "IPC_51516M5M", "v3.0.0.136_20121102",
        "IPC_515B16M5M", "v3.0.0.136_20121102",
    },
    "RLC-420": {
        "IPC_51316M": "v3.0.0.136_20121101",
        "IPC_51516M5M": "v3.0.0.136_20121101",
        "IPC_515B16M5M": "v3.0.0.136_20121101",
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
        "IPC_523128M8MP": "v3.1.0.956_22041503",
    },
    "RLC-811A": {
        "IPC_523128M8MP": "v3.1.0.989_22051908",
    },
    "RLC-820A": {
        "IPC_523128M8MP": "v3.1.0.956_22041501",
    },
    "RLC-823A": {
        "IPC_523128M8MP": "v3.1.0.989_22051911_v1.0.0.30",
    },
}


version_regex = re.compile(r"^v(?P<major>[0-9]+)\.(?P<middle>[0-9]+)\.(?P<minor>[0-9]+).(?P<build>[0-9]+)_(?P<date>[0-9]+)")


class SoftwareVersion:
    """SoftwareVersion class"""

    def __init__(self, version_string: str):
        self.version_string = version_string.lower()

        self.is_unknown = False
        self.major = 0
        self.middle = 0
        self.minor = 0
        self.build = 0

        if self.version_string == "unknown":
            self.is_unknown = True
            return

        match = version_regex.match(self.version_string)

        if match is None:
            raise UnexpectedDataError(f"version_string has invalid version format: {version_string}")

        self.major = int(match.group("major"))
        self.middle = int(match.group("middle"))
        self.minor = int(match.group("minor"))
        build = match.group("build")
        if build is None:
            self.build = 0
        else:
            self.build = int(build)
        date = match.group("date")
        if date is None:
            date = "00010100"
        try:
            self.date = datetime.strptime(date, '%y%m%d%H')
        except ValueError:
            self.date = datetime.strptime("00010100", '%y%m%d%H')

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
        if self.date > target_version.date:
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
        if self.date >= target_version.date:
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
        if self.date < target_version.date:
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
        if self.date <= target_version.date:
            return True

        return False

    def equals(self, target_version: "SoftwareVersion"):
        if target_version.major == self.major and target_version.middle == self.middle and target_version.minor == self.minor and target_version.build == self.build and target_version.date == self.date:
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
