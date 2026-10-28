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


class NestLockException(PynestException):
    """Raised when the gRPC-web lock transport fails."""


class NestLockAuthException(NestLockException):
    """Raised when gRPC-web rejects the session credentials."""


class NestLockCommandException(NestLockException):
    """Raised when a lock or unlock command is rejected."""
