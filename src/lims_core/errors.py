from __future__ import annotations


class LimsError(Exception):
    """Base class for all platform errors."""


class ValidationError(LimsError):
    def __init__(self, message: str, errors: dict | None = None):
        super().__init__(message)
        self.errors = errors or {}


class PermissionDenied(LimsError):
    pass


class NotFoundError(LimsError):
    pass


class TransitionError(LimsError):
    pass


class EntitlementError(LimsError):
    pass


class IntegrityError(LimsError):
    pass


class SignatureError(LimsError):
    pass


class ComplianceError(LimsError):
    """A GxP rule was violated (uncalibrated instrument, unauthorised retest...)."""
