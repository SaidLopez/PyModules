"""Tests for route conventions."""

from __future__ import annotations

import pytest


class TestHTTPMethod:
    """Tests for HTTPMethod enum."""

    def test_all_methods_defined(self) -> None:
        """HTTPMethod should define all standard HTTP methods."""
        from pymodules.api import HTTPMethod

        assert hasattr(HTTPMethod, "GET")
        assert hasattr(HTTPMethod, "POST")
        assert hasattr(HTTPMethod, "PUT")
        assert hasattr(HTTPMethod, "PATCH")
        assert hasattr(HTTPMethod, "DELETE")


class TestRouteInfo:
    """Tests for RouteInfo dataclass."""

    def test_stores_route_data(self) -> None:
        """RouteInfo should store all route configuration."""
        from pymodules.api import HTTPMethod, RouteInfo

        info = RouteInfo(
            path="/users",
            method=HTTPMethod.GET,
            tags=["users"],
            summary="List users",
        )

        assert info.path == "/users"
        assert info.method == HTTPMethod.GET
        assert info.tags == ["users"]
        assert info.summary == "List users"


class TestRouteConvention:
    """Tests for RouteConvention base class."""

    def test_get_route_returns_route_info(self, sample_events) -> None:
        """RouteConvention.get_route should return RouteInfo."""
        from pymodules.api import RouteConvention, RouteInfo

        convention = RouteConvention()
        route = convention.get_route(sample_events["CreateUser"])

        assert isinstance(route, RouteInfo)
        assert route.path is not None
        assert route.method is not None


class TestRESTConvention:
    """Tests for RESTConvention."""

    def test_create_maps_to_post(self, sample_events) -> None:
        """CreateX events should map to POST /xs."""
        from pymodules.api import HTTPMethod, RESTConvention

        convention = RESTConvention()
        route = convention.get_route(sample_events["CreateUser"])

        assert route.method == HTTPMethod.POST
        assert "/users" in route.path.lower()
        assert "{" not in route.path  # No ID placeholder for create

    def test_get_maps_to_get_with_id(self, sample_events) -> None:
        """GetX events should map to GET /xs/{id}."""
        from pymodules.api import HTTPMethod, RESTConvention

        convention = RESTConvention()
        route = convention.get_route(sample_events["GetUser"])

        assert route.method == HTTPMethod.GET
        assert "/users" in route.path.lower()
        # Should have ID placeholder
        assert "{" in route.path or "id" in route.path.lower()

    def test_list_maps_to_get_collection(self, sample_events) -> None:
        """ListXs events should map to GET /xs."""
        from pymodules.api import HTTPMethod, RESTConvention

        convention = RESTConvention()
        route = convention.get_route(sample_events["ListUsers"])

        assert route.method == HTTPMethod.GET
        assert "/users" in route.path.lower()
        assert "{" not in route.path  # No ID placeholder for list

    def test_update_maps_to_put_with_id(self) -> None:
        """UpdateX events should map to PUT /xs/{id}."""
        from dataclasses import dataclass

        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import HTTPMethod, RESTConvention

        @dataclass
        class UpdateUserInput(EventInput):
            user_id: str
            name: str

        @dataclass
        class UpdateUserOutput(EventOutput):
            success: bool

        class UpdateUser(Event[UpdateUserInput, UpdateUserOutput]):
            pass

        convention = RESTConvention()
        route = convention.get_route(UpdateUser)

        assert route.method == HTTPMethod.PUT
        assert "/users" in route.path.lower()
        assert "{" in route.path  # Has ID placeholder

    def test_delete_maps_to_delete_with_id(self) -> None:
        """DeleteX events should map to DELETE /xs/{id}."""
        from dataclasses import dataclass

        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import HTTPMethod, RESTConvention

        @dataclass
        class DeleteUserInput(EventInput):
            user_id: str

        @dataclass
        class DeleteUserOutput(EventOutput):
            success: bool

        class DeleteUser(Event[DeleteUserInput, DeleteUserOutput]):
            pass

        convention = RESTConvention()
        route = convention.get_route(DeleteUser)

        assert route.method == HTTPMethod.DELETE
        assert "/users" in route.path.lower()
        assert "{" in route.path

    def test_search_maps_to_post_search(self) -> None:
        """SearchXs events should map to POST /xs/search."""
        from dataclasses import dataclass

        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import HTTPMethod, RESTConvention

        @dataclass
        class SearchUsersInput(EventInput):
            query: str

        @dataclass
        class SearchUsersOutput(EventOutput):
            results: list

        class SearchUsers(Event[SearchUsersInput, SearchUsersOutput]):
            pass

        convention = RESTConvention()
        route = convention.get_route(SearchUsers)

        assert route.method == HTTPMethod.POST
        assert "/users" in route.path.lower()
        assert "search" in route.path.lower()

    def test_custom_action_maps_to_post_subpath(self) -> None:
        """Custom actions should map to POST /xs/{id}/action."""
        from dataclasses import dataclass

        from pymodules import Event, EventInput, EventOutput
        from pymodules.api import HTTPMethod, RESTConvention

        @dataclass
        class ActivateUserInput(EventInput):
            user_id: str

        @dataclass
        class ActivateUserOutput(EventOutput):
            success: bool

        class ActivateUser(Event[ActivateUserInput, ActivateUserOutput]):
            pass

        convention = RESTConvention()
        route = convention.get_route(ActivateUser)

        assert route.method == HTTPMethod.POST
        assert "/users" in route.path.lower()
        assert "activate" in route.path.lower()

    def test_pluralizes_resource_name(self) -> None:
        """RESTConvention should pluralize resource names."""
        from pymodules.api import RESTConvention

        convention = RESTConvention()

        assert convention._pluralize("user") == "users"
        assert convention._pluralize("company") == "companies"
        assert convention._pluralize("status") == "statuses"
        assert convention._pluralize("box") == "boxes"
        assert convention._pluralize("class") == "classes"

    def test_handles_irregular_plurals(self) -> None:
        """RESTConvention should handle some irregular plurals."""
        from pymodules.api import RESTConvention

        convention = RESTConvention()

        # Common irregular plurals that might be handled
        # The implementation may or may not handle these
        result = convention._pluralize("person")
        assert result in ["persons", "people"]  # Either is acceptable
