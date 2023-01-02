import re

version_regex = re.compile(r"^v(?P<major>[0-9]+)\.(?P<middle>[0-9]+)\.(?P<minor>[0-9]+).(?P<build>[0-9]+)_([0-9]+)")


class SoftwareVersion:

    def __init__(self, version_string: str):
        self.version_string = version_string

        self.is_unknown = False
        self.major      = 0
        self.middle     = 0
        self.minor      = 0
        self.build      = 0

        if version_string.lower() == 'unknown':
            self.is_unknown = True
            return

        match = version_regex.match(version_string)

        if match is None:
            raise Exception("version_string has invalid version format: {}".format(version_string))

        self.major  = int(match.group("major"))
        self.middle = int(match.group("middle"))
        self.minor  = int(match.group("minor"))
        build       = match.group("build")
        if build is None:
            self.build = 0
        else:
            self.build = int(match.group("build"))

    def is_greater_than(self, target_version: 'SoftwareVersion'):
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

        return False

    def is_greater_or_equal_than(self, target_version: 'SoftwareVersion'):
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

        return False

    def is_lower_than(self, target_version: 'SoftwareVersion'):
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
        return False

    def is_lower_or_equal_than(self, target_version: 'SoftwareVersion'):
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
        return False

    def equals(self, target_version: 'SoftwareVersion'):
        if target_version.major == self.major and target_version.middle == self.middle and \
                target_version.minor == self.minor and target_version.build == self.build:
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
        if target_version.major == self.major and target_version.middle == self.middle and \
                target_version.minor == self.minor and target_version.build == self.build:
            return True
        return False

    def generate_str_from_numbers(self):
        return "{}.{}.{}-{}".format(self.major, self.middle, self.minor, self.build)
#endof class SoftwareVersion