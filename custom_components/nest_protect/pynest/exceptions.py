"""Exceptions used by PyNest."""


class PynestException(Exception):
    """Base class for all exceptions raised by pynest."""


class NestServiceException(Exception):
    """Raised when service is not available."""


class BadCredentialsException(Exception):
    """Raised when credentials are incorrect."""


class NotAuthenticatedException(Exception):
    """Raised when session is invalid."""


class GatewayTimeoutException(NestServiceException):
    """Raised when server times out."""


class BadGatewayException(NestServiceException):
    """Raised when server returns Bad Gateway."""


class EmptyResponseException(NestServiceException):
    """Raised when server returns Status 200 (OK), but empty response."""
