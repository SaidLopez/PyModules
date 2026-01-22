"""API layer example.

This example demonstrates how to use the pymodules.api layer for:
- ModuleRouter with auto-discovery
- Convention-based REST routing
- Custom endpoint decorators
- Error handling

Requirements:
    pip install 'pymodules[api]'

Run:
    uvicorn examples.api_example:app --reload
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from pymodules import Event, EventInput, EventOutput, Module, ModuleHost, module
from pymodules.api import (
    ModuleRouter,
    api_endpoint,
    exclude_from_api,
    register_error_handlers,
)


# =============================================================================
# Event Definitions
# =============================================================================


@dataclass
class CreateProductInput(EventInput):
    """Input for creating a product."""

    name: str
    price: float
    description: str = ""


@dataclass
class CreateProductOutput(EventOutput):
    """Output after creating a product."""

    id: str
    name: str
    price: float


@dataclass
class GetProductInput(EventInput):
    """Input for getting a product."""

    product_id: str


@dataclass
class GetProductOutput(EventOutput):
    """Output for a single product."""

    id: str
    name: str
    price: float
    description: str


@dataclass
class ListProductsInput(EventInput):
    """Input for listing products."""

    limit: int = 10
    offset: int = 0


@dataclass
class ListProductsOutput(EventOutput):
    """Output for product listing."""

    products: list[dict[str, Any]]
    total: int


@dataclass
class SearchProductsInput(EventInput):
    """Input for searching products."""

    query: str
    min_price: float | None = None
    max_price: float | None = None


@dataclass
class SearchProductsOutput(EventOutput):
    """Output for product search."""

    products: list[dict[str, Any]]
    total: int


# =============================================================================
# Event Classes
# =============================================================================


class CreateProduct(Event[CreateProductInput, CreateProductOutput]):
    """Event to create a new product.

    Convention: 'create' + 'Product' -> POST /products
    """

    pass


class GetProduct(Event[GetProductInput, GetProductOutput]):
    """Event to get a product by ID.

    Convention: 'get' + 'Product' -> GET /products/{id}
    """

    pass


class ListProducts(Event[ListProductsInput, ListProductsOutput]):
    """Event to list all products.

    Convention: 'list' + 'Products' -> GET /products
    """

    pass


@api_endpoint(
    path="/products/search",
    tags=["Products", "Search"],
    summary="Search products by criteria",
)
class SearchProducts(Event[SearchProductsInput, SearchProductsOutput]):
    """Event to search products with custom endpoint.

    Uses @api_endpoint to override convention-based routing.
    """

    pass


@exclude_from_api
class InternalProductSync(Event[EventInput, EventOutput]):
    """Internal event for product sync - not exposed via API.

    Uses @exclude_from_api to prevent API endpoint generation.
    """

    pass


# =============================================================================
# Module Implementation
# =============================================================================

# In-memory product store for demo
_products: dict[str, dict[str, Any]] = {}
_next_id = 1


@module(name="products", description="Product management module")
class ProductModule(Module):
    """Module that handles all product-related events."""

    def can_handle(self, event: Event) -> bool:
        """Check if this module handles the event."""
        return isinstance(
            event, (CreateProduct, GetProduct, ListProducts, SearchProducts)
        )

    async def handle(self, event: Event) -> None:
        """Handle product events."""
        global _next_id

        if isinstance(event, CreateProduct):
            product_id = str(_next_id)
            _next_id += 1

            product = {
                "id": product_id,
                "name": event.input.name,
                "price": event.input.price,
                "description": event.input.description,
            }
            _products[product_id] = product

            event.output = CreateProductOutput(
                id=product_id,
                name=product["name"],
                price=product["price"],
            )
            event.handled = True

        elif isinstance(event, GetProduct):
            product = _products.get(event.input.product_id)
            if not product:
                raise ValueError(f"Product {event.input.product_id} not found")

            event.output = GetProductOutput(
                id=product["id"],
                name=product["name"],
                price=product["price"],
                description=product["description"],
            )
            event.handled = True

        elif isinstance(event, ListProducts):
            all_products = list(_products.values())
            start = event.input.offset
            end = start + event.input.limit
            page = all_products[start:end]

            event.output = ListProductsOutput(
                products=page,
                total=len(all_products),
            )
            event.handled = True

        elif isinstance(event, SearchProducts):
            query = event.input.query.lower()
            results = []

            for product in _products.values():
                # Name match
                if query not in product["name"].lower():
                    continue

                # Price filters
                if event.input.min_price and product["price"] < event.input.min_price:
                    continue
                if event.input.max_price and product["price"] > event.input.max_price:
                    continue

                results.append(product)

            event.output = SearchProductsOutput(
                products=results,
                total=len(results),
            )
            event.handled = True


# =============================================================================
# FastAPI Application Setup
# =============================================================================


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    # Create FastAPI app
    app = FastAPI(
        title="Product API",
        description="Example API using PyModules",
        version="1.0.0",
    )

    # Register error handlers
    register_error_handlers(app)

    # Create ModuleHost and register module
    host = ModuleHost()
    host.register(ProductModule())

    # Create ModuleRouter and register events
    router = ModuleRouter(host)
    router.register_event(CreateProduct)
    router.register_event(GetProduct)
    router.register_event(ListProducts)
    router.register_event(SearchProducts)

    # Mount router on app
    router.mount(app, prefix="/api/v1")

    # Add health endpoint
    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    return app


# Create app instance for uvicorn
app = create_app()


# =============================================================================
# Usage Example
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    print("""
=============================================================================
PyModules API Layer Example
=============================================================================

Starting server at http://localhost:8000

Available endpoints:
  GET  /health              - Health check
  POST /api/v1/products     - Create a product
  GET  /api/v1/products     - List products
  GET  /api/v1/products/{id} - Get a product
  POST /api/v1/products/search - Search products

Try with curl:
  curl -X POST http://localhost:8000/api/v1/products \\
       -H "Content-Type: application/json" \\
       -d '{"name": "Widget", "price": 9.99, "description": "A nice widget"}'

  curl http://localhost:8000/api/v1/products

OpenAPI docs: http://localhost:8000/docs
=============================================================================
""")

    uvicorn.run(app, host="0.0.0.0", port=8000)
