"""Exceptions used by PyNest."""


class PynestException(Exception):
    """Base class for all exceptions raised by pynest."""

    pass


class NestServiceException(Exception):
    """Raised when service is not available."""

    pass


class BadCredentialsException(Exception):
    """Raised when credentials are incorrect."""

    pass


class NotAuthenticatedException(Exception):
    """Raised when session is invalid."""

    pass


class GatewayTimeoutException(NestServiceException):
    """Raised when server times out."""

    pass


class BadGatewayException(NestServiceException):
    """Raised when server returns Bad Gateway."""

    pass


class EmptyResponseException(NestServiceException):
    """Raised when server returns Status 200 (OK), but empty response."""

    pass
