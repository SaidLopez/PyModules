"""API error handling.

Provides APIError exception hierarchy and FastAPI error handlers.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class ErrorCode(str, Enum):
    """Standard error codes for the API."""

    # Authentication errors (401)
    UNAUTHORIZED = "unauthorized"
    TOKEN_EXPIRED = "token_expired"
    INVALID_TOKEN = "invalid_token"

    # Authorization errors (403)
    FORBIDDEN = "forbidden"
    INSUFFICIENT_PERMISSIONS = "insufficient_permissions"

    # Not found errors (404)
    NOT_FOUND = "not_found"
    RESOURCE_NOT_FOUND = "resource_not_found"

    # Validation errors (400/422)
    VALIDATION_ERROR = "validation_error"
    INVALID_INPUT = "invalid_input"
    MISSING_FIELD = "missing_field"

    # Conflict errors (409)
    CONFLICT = "conflict"
    DUPLICATE_RESOURCE = "duplicate_resource"

    # Internal errors (500)
    INTERNAL_ERROR = "internal_error"
    EVENT_HANDLING_ERROR = "event_handling_error"

    # Service errors (503)
    SERVICE_UNAVAILABLE = "service_unavailable"


# Map error codes to HTTP status codes
ERROR_CODE_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.INVALID_TOKEN: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.INSUFFICIENT_PERMISSIONS: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.MISSING_FIELD: 400,
    ErrorCode.CONFLICT: 409,
    ErrorCode.DUPLICATE_RESOURCE: 409,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.EVENT_HANDLING_ERROR: 500,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
}


class ErrorResponse(BaseModel):
    """Standard error response model."""

    error: str
    code: str
    message: str
    details: dict[str, Any] | None = None


class APIError(Exception):
    """Base exception for API errors.

    Attributes:
        message: Human-readable error message
        code: The error code enum value (optional)
        status_code: HTTP status code
        details: Additional error details
    """

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ):
        self.message = message
        self.code = code or ErrorCode.INTERNAL_ERROR
        self.status_code = status_code if status_code != 500 else ERROR_CODE_STATUS_MAP.get(
            self.code, 500
        )
        self.details = details
        super().__init__(message)

    def to_response(self) -> ErrorResponse:
        """Convert to an ErrorResponse model."""
        return ErrorResponse(
            error=self.code.name.lower(),
            code=self.code.value,
            message=self.message,
            details=self.details,
        )


class NotFoundError(APIError):
    """Resource not found error."""

    def __init__(
        self,
        resource_type: str,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.resource_type = resource_type
        self.resource_id = resource_id
        message = f"{resource_type} not found"
        if resource_id:
            message = f"{resource_type} with id '{resource_id}' not found"
        super().__init__(
            message, code=ErrorCode.RESOURCE_NOT_FOUND, status_code=404, details=details
        )


class ValidationError(APIError):
    """Input validation error."""

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message, code=ErrorCode.VALIDATION_ERROR, status_code=422, details=details
        )


class AuthenticationError(APIError):
    """Authentication error."""

    def __init__(
        self,
        message: str = "Authentication required",
        code: ErrorCode = ErrorCode.UNAUTHORIZED,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, code=code, status_code=401, details=details)


class AuthorizationError(APIError):
    """Authorization error."""

    def __init__(
        self,
        message: str = "Insufficient permissions",
        required_permissions: list[str] | None = None,
    ):
        self.required_permissions = required_permissions
        details = None
        if required_permissions:
            details = {"required_permissions": required_permissions}
        super().__init__(
            message, code=ErrorCode.INSUFFICIENT_PERMISSIONS, status_code=403, details=details
        )


def register_error_handlers(app: Any) -> None:
    """Register error handlers with a FastAPI application.

    Args:
        app: FastAPI application instance
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        """FastAPI exception handler for APIError."""
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response().model_dump(),
        )

    async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """FastAPI exception handler for unhandled exceptions."""
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="internal_error",
                code=ErrorCode.INTERNAL_ERROR.value,
                message="An unexpected error occurred",
                details=None,
            ).model_dump(),
        )

    app.add_exception_handler(APIError, api_error_handler)
