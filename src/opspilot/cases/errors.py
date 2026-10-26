"""Service errors, each with the API status and code it maps to (`06-api.md` §3).

The case service raises these; the web layer (P11) turns them into the JSON
error shape without deciding anything itself.
"""

from typing import Any


class ServiceError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details


class ForbiddenError(ServiceError):
    """403: the actor's role or authority doesn't allow this."""

    status = 403
    code = "forbidden"


class NotFoundError(ServiceError):
    status = 404
    code = "not_found"


class ConflictError(ServiceError):
    """409: state, version or safeguard conflict."""

    status = 409
    code = "invalid_transition"


class ValidationFailedError(ServiceError):
    """422: a field or evidence problem; `details` says what."""

    status = 422
    code = "validation_failed"


class NotReadyError(ServiceError):
    """503: something the system needs (seed data, say) isn't there yet."""

    status = 503
    code = "not_ready"
