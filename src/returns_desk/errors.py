from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for errors the API turns into a structured HTTP response."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotAuthenticated(DomainError):
    status_code = 401
    code = "not_authenticated"


class Forbidden(DomainError):
    status_code = 403
    code = "forbidden"


class NotFound(DomainError):
    """Raised for unknown references and for ones the caller may not see. The two must be
    indistinguishable, so this error never carries details."""

    status_code = 404
    code = "not_found"

    def __init__(self) -> None:
        super().__init__("Not found")


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class InvalidRequest(DomainError):
    status_code = 422
    code = "invalid_request"
