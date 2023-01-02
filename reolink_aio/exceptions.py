"""Reolink NVR/camera API."""


class ReolinkError(Exception):
    """Base Reolink error class"""


class ApiError(ReolinkError):
    """Raised when API returns an error code"""


class InvalidContentTypeError(ReolinkError):
    """Raised when a command returns an unexpected content type"""


class CredentialsInvalidError(ReolinkError):
    """Raised when an API call returns credentials issue"""
