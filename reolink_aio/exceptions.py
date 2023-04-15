"""Reolink NVR/camera API."""


from typing import Optional
from . import typings


class ReolinkError(Exception):
    """Base Reolink error class"""


class ApiError(ReolinkError):
    """Raised when API returns an error code"""


class InvalidContentTypeError(ReolinkError):
    """Raised when a command returns an unexpected content type"""


class CredentialsInvalidError(ReolinkError):
    """Raised when an API call returns credentials issue"""


class LoginError(ReolinkError):
    """Raised when a login attempt fails for another reason than the credentials"""


class NoDataError(ReolinkError):
    """Raised when an API call returns None instead of expected data"""


class UnexpectedDataError(ReolinkError):
    """Raised when an API call returns unexpected data which can not be handled properly"""


class UnexpectedSearchDataError(UnexpectedDataError):
    """Raised when the search API call returns unexpected data which can not be handled properly"""

    statuses: Optional[list[typings.SearchStatus]]

    def __init__(
        self, *args: object, statuses: Optional[list[typings.SearchStatus]] = None
    ) -> None:
        super().__init__(*args)
        self.statuses = statuses


class InvalidParameterError(ReolinkError):
    """Raised when a function is called with invalid parameters"""


class NotSupportedError(ReolinkError):
    """Raised when a function is called with invalid parameters"""


class SubscriptionError(ReolinkError):
    """Raised when a function is called with invalid parameters"""
