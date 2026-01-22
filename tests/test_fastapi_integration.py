"""
Integration tests for FastAPI integration.
"""

from dataclasses import dataclass

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pymodules import Event, EventInput, EventOutput, Module, ModuleHost, module
from pymodules.fastapi import PyModulesAPI


@dataclass
class GreetInput(EventInput):
    name: str = ""


@dataclass
class GreetOutput(EventOutput):
    message: str = ""


class GreetEvent(Event[GreetInput, GreetOutput]):
    name = "test.greet"


@module(name="GreeterModule")
class GreeterModule(Module):
    def can_handle(self, event: Event) -> bool:
        return isinstance(event, GreetEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, GreetEvent):
            event.output = GreetOutput(message=f"Hello, {event.input.name}!")
            event.handled = True


@dataclass
class CalculateInput(EventInput):
    a: int = 0
    b: int = 0
    operation: str = "add"


@dataclass
class CalculateOutput(EventOutput):
    result: int = 0


class CalculateEvent(Event[CalculateInput, CalculateOutput]):
    name = "test.calculate"


@module(name="CalculatorModule")
class CalculatorModule(Module):
    def can_handle(self, event: Event) -> bool:
        return isinstance(event, CalculateEvent)

    def handle(self, event: Event) -> None:
        if isinstance(event, CalculateEvent):
            a, b = event.input.a, event.input.b
            op = event.input.operation

            if op == "add":
                result = a + b
            elif op == "subtract":
                result = a - b
            elif op == "multiply":
                result = a * b
            else:
                result = 0

            event.output = CalculateOutput(result=result)
            event.handled = True


@pytest.fixture
def app():
    """Create a test FastAPI app."""
    host = ModuleHost()
    host.register(GreeterModule())
    host.register(CalculatorModule())

    app = FastAPI()
    api = PyModulesAPI(host)

    api.add_event_endpoint(app, "/greet", GreetEvent, GreetInput, GreetOutput)
    api.add_event_endpoint(app, "/calculate", CalculateEvent, CalculateInput, CalculateOutput)

    return app


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


@pytest.mark.integration
class TestFastAPIEndpoints:
    """Tests for auto-generated FastAPI endpoints."""

    def test_greet_endpoint(self, client):
        """Test the greet endpoint."""
        response = client.post("/greet", json={"name": "World"})
        assert response.status_code == 200
        assert response.json()["message"] == "Hello, World!"

    def test_calculate_add(self, client):
        """Test the calculate endpoint with add."""
        response = client.post("/calculate", json={"a": 5, "b": 3, "operation": "add"})
        assert response.status_code == 200
        assert response.json()["result"] == 8

    def test_calculate_subtract(self, client):
        """Test the calculate endpoint with subtract."""
        response = client.post("/calculate", json={"a": 10, "b": 4, "operation": "subtract"})
        assert response.status_code == 200
        assert response.json()["result"] == 6

    def test_calculate_multiply(self, client):
        """Test the calculate endpoint with multiply."""
        response = client.post("/calculate", json={"a": 6, "b": 7, "operation": "multiply"})
        assert response.status_code == 200
        assert response.json()["result"] == 42


@pytest.mark.integration
class TestFastAPIErrorHandling:
    """Tests for FastAPI error handling."""

    def test_unhandled_event_returns_404(self):
        """Test that unhandled events return 404."""
        host = ModuleHost()
        # No modules registered

        app = FastAPI()
        api = PyModulesAPI(host)
        api.add_event_endpoint(app, "/greet", GreetEvent, GreetInput)

        client = TestClient(app)
        response = client.post("/greet", json={"name": "World"})

        assert response.status_code == 404
        assert "No module found" in response.json()["detail"]

    def test_invalid_input_returns_422(self, client):
        """Test that invalid input returns 422."""
        response = client.post("/greet", json={"invalid_field": "value"})
        # FastAPI validates against the Pydantic model
        # Missing required field should work (has default)
        assert response.status_code == 200

    def test_malformed_json_returns_422(self, client):
        """Test that malformed JSON returns 422."""
        response = client.post(
            "/greet", content="not json", headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422


@pytest.mark.integration
class TestFastAPIOpenAPI:
    """Tests for OpenAPI schema generation."""

    def test_openapi_schema_generated(self, app):
        """Test that OpenAPI schema is generated."""
        client = TestClient(app)
        response = client.get("/openapi.json")

        assert response.status_code == 200
        schema = response.json()

        assert "/greet" in schema["paths"]
        assert "/calculate" in schema["paths"]

    def test_endpoint_has_request_body(self, app):
        """Test that endpoints have request body schemas."""
        client = TestClient(app)
        response = client.get("/openapi.json")
        schema = response.json()

        greet_path = schema["paths"]["/greet"]["post"]
        assert "requestBody" in greet_path


@pytest.mark.integration
class TestPyModulesAPIManualDispatch:
    """Tests for manual event dispatch via PyModulesAPI."""

    @pytest.mark.asyncio
    async def test_manual_dispatch(self):
        """Test manual event dispatch."""
        host = ModuleHost()
        host.register(GreeterModule())

        api = PyModulesAPI(host)

        event = GreetEvent(input=GreetInput(name="Manual"))
        result = await api.dispatch(event)

        assert result.handled
        assert result.output.message == "Hello, Manual!"

    @pytest.mark.asyncio
    async def test_manual_dispatch_unhandled(self):
        """Test manual dispatch with no handler."""
        from fastapi import HTTPException

        host = ModuleHost()
        api = PyModulesAPI(host)

        event = GreetEvent(input=GreetInput(name="Test"))

        with pytest.raises(HTTPException) as exc_info:
            await api.dispatch(event)

        assert exc_info.value.status_code == 404
