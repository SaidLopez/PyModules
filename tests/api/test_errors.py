"""Tests for API error handling."""

from __future__ import annotations


class TestAPIError:
    """Tests for APIError base class."""

    def test_has_message(self) -> None:
        """APIError should have a message."""
        from pymodules.api import APIError

        error = APIError(message="Test error")

        assert error.message == "Test error"

    def test_has_code(self) -> None:
        """APIError should have an error code."""
        from pymodules.api import APIError, ErrorCode

        error = APIError(message="Test", code=ErrorCode.VALIDATION_ERROR)

        assert error.code == ErrorCode.VALIDATION_ERROR

    def test_has_status_code(self) -> None:
        """APIError should have an HTTP status code."""
        from pymodules.api import APIError

        error = APIError(message="Test", status_code=400)

        assert error.status_code == 400


class TestSpecificErrors:
    """Tests for specific error types."""

    def test_not_found_error(self) -> None:
        """NotFoundError should have 404 status."""
        from pymodules.api import NotFoundError

        error = NotFoundError(resource_type="User", resource_id="123")

        assert error.status_code == 404
        assert "User" in error.message
        assert "123" in error.message

    def test_validation_error(self) -> None:
        """ValidationError should have 422 status."""
        from pymodules.api import ValidationError

        error = ValidationError(message="Invalid input", details={"field": "error"})

        assert error.status_code == 422
        assert error.details == {"field": "error"}

    def test_authentication_error(self) -> None:
        """AuthenticationError should have 401 status."""
        from pymodules.api import AuthenticationError

        error = AuthenticationError(message="Invalid token")

        assert error.status_code == 401

    def test_authorization_error(self) -> None:
        """AuthorizationError should have 403 status."""
        from pymodules.api import AuthorizationError

        error = AuthorizationError(message="Permission denied", required_permissions=["admin"])

        assert error.status_code == 403
        assert "admin" in str(error.required_permissions)


class TestErrorHandlers:
    """Tests for error handler registration."""

    def test_register_error_handlers(self, app) -> None:
        """register_error_handlers should add handlers to app."""
        from pymodules.api import register_error_handlers

        initial_handlers = len(app.exception_handlers)
        register_error_handlers(app)

        assert len(app.exception_handlers) > initial_handlers

    def test_api_error_returns_json(self, app, client) -> None:
        """APIError should return JSON response."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import APIError, register_error_handlers

        app = FastAPI()
        register_error_handlers(app)

        @app.get("/test-error")
        def raise_error():
            raise APIError(message="Test error", status_code=400)

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test-error")

        assert response.headers["content-type"] == "application/json"
        data = response.json()
        assert "error" in data or "message" in data

    def test_not_found_returns_404(self, app, client) -> None:
        """NotFoundError should return 404 status."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import NotFoundError, register_error_handlers

        app = FastAPI()
        register_error_handlers(app)

        @app.get("/test-not-found")
        def raise_not_found():
            raise NotFoundError(resource_type="Item", resource_id="999")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test-not-found")

        assert response.status_code == 404

    def test_validation_returns_422(self, app) -> None:
        """ValidationError should return 422 status."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import ValidationError, register_error_handlers

        app = FastAPI()
        register_error_handlers(app)

        @app.get("/test-validation")
        def raise_validation():
            raise ValidationError(message="Bad data")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test-validation")

        assert response.status_code == 422

    def test_auth_error_returns_401(self, app) -> None:
        """AuthenticationError should return 401 status."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from pymodules.api import AuthenticationError, register_error_handlers

        app = FastAPI()
        register_error_handlers(app)

        @app.get("/test-auth")
        def raise_auth():
            raise AuthenticationError(message="Invalid token")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/test-auth")

        assert response.status_code == 401
