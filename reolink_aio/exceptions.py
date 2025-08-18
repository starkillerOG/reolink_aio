"""Reolink NVR/camera API."""

from __future__ import annotations

from asyncio import TimeoutError as AsyncioTimeoutError


class ReolinkError(Exception):
    """Base Reolink error class"""

    def __init__(self, message: str, translation_key: str = ""):
        super().__init__(message)
        self.translation_key = translation_key


class ApiError(ReolinkError):
    """Raised when API returns an error code"""

    def __init__(self, message: str, translation_key: str = "", rspCode: int | None = None):
        super().__init__(message, translation_key)
        self.rspCode = rspCode


class InvalidContentTypeError(ReolinkError):
    """Raised when a command returns an unexpected content type"""


class CredentialsInvalidError(ReolinkError):
    """Raised when an API call returns credentials issue"""


class LoginError(ReolinkError):
    """Raised when a login attempt fails for another reason than the credentials"""


class LoginPrivacyModeError(LoginError):
    """Raised when a login attempt fails because privacy mode is turned on"""


class LoginFirmwareError(LoginError):
    """Raised when a login attempt fails and the minimum required firmware version is not met"""


class NoDataError(ReolinkError):
    """Raised when an API call returns None instead of expected data"""


class UnexpectedDataError(ReolinkError):
    """Raised when an API call returns unexpected data which can not be handled properly"""


class InvalidParameterError(ReolinkError):
    """Raised when a function is called with invalid parameters"""


class NotSupportedError(ReolinkError):
    """Raised when a function is not supported by that device"""


class SubscriptionError(ReolinkError):
    """Raised when a a error occurs related to a ONVIF subscription"""


class ReolinkConnectionError(ReolinkError):
    """Wraps around aiohttp.ClientConnectorError for API calls"""


class ReolinkTimeoutError(ReolinkError, AsyncioTimeoutError):
    """Wraps around asyncio.TimeoutError for API calls"""
