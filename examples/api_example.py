"""API layer example.

This example demonstrates how to use the pymodules.contrib.api layer for:
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

from pymodules import (
    Command,
    CommandRequest,
    CommandResponse,
    Module,
    ModuleHost,
    handles,
    module,
)
from pymodules.contrib.api import (
    ModuleRouter,
    api_endpoint,
    exclude_from_api,
    register_error_handlers,
)

# =============================================================================
# Command Definitions
# =============================================================================


@dataclass
class CreateProductInput(CommandRequest):
    """Request payload for creating a product."""

    name: str
    price: float
    description: str = ""


@dataclass
class CreateProductOutput(CommandResponse):
    """Response after creating a product."""

    id: str
    name: str
    price: float


@dataclass
class GetProductInput(CommandRequest):
    """Request payload for getting a product."""

    product_id: str


@dataclass
class GetProductOutput(CommandResponse):
    """Response for a single product."""

    id: str
    name: str
    price: float
    description: str


@dataclass
class ListProductsInput(CommandRequest):
    """Request payload for listing products."""

    limit: int = 10
    offset: int = 0


@dataclass
class ListProductsOutput(CommandResponse):
    """Response for product listing."""

    products: list[dict[str, Any]]
    total: int


@dataclass
class SearchProductsInput(CommandRequest):
    """Request payload for searching products."""

    query: str
    min_price: float | None = None
    max_price: float | None = None


@dataclass
class SearchProductsOutput(CommandResponse):
    """Response for product search."""

    products: list[dict[str, Any]]
    total: int


# =============================================================================
# Command Classes
# =============================================================================


class CreateProduct(Command[CreateProductInput, CreateProductOutput]):
    """Command to create a new product.

    Convention: 'create' + 'Product' -> POST /products
    """

    pass


class GetProduct(Command[GetProductInput, GetProductOutput]):
    """Command to get a product by ID.

    Convention: 'get' + 'Product' -> GET /products/{id}
    """

    pass


class ListProducts(Command[ListProductsInput, ListProductsOutput]):
    """Command to list all products.

    Convention: 'list' + 'Products' -> GET /products
    """

    pass


@api_endpoint(
    path="/products/search",
    tags=["Products", "Search"],
    summary="Search products by criteria",
)
class SearchProducts(Command[SearchProductsInput, SearchProductsOutput]):
    """Command to search products with custom endpoint.

    Uses @api_endpoint to override convention-based routing.
    """

    pass


@exclude_from_api
class InternalProductSync(Command[CommandRequest, CommandResponse]):
    """Internal command for product sync - not exposed via API.

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
    """Module that handles all product-related commands."""

    @handles(CreateProduct)
    async def create_product(self, command: CreateProduct) -> CreateProductOutput:
        """Handle product creation."""
        global _next_id

        product_id = str(_next_id)
        _next_id += 1

        product = {
            "id": product_id,
            "name": command.request.name,
            "price": command.request.price,
            "description": command.request.description,
        }
        _products[product_id] = product

        return CreateProductOutput(
            id=product_id,
            name=product["name"],
            price=product["price"],
        )

    @handles(GetProduct)
    async def get_product(self, command: GetProduct) -> GetProductOutput:
        """Handle getting a single product."""
        product = _products.get(command.request.product_id)
        if not product:
            raise ValueError(f"Product {command.request.product_id} not found")

        return GetProductOutput(
            id=product["id"],
            name=product["name"],
            price=product["price"],
            description=product["description"],
        )

    @handles(ListProducts)
    async def list_products(self, command: ListProducts) -> ListProductsOutput:
        """Handle product listing."""
        all_products = list(_products.values())
        start = command.request.offset
        end = start + command.request.limit
        page = all_products[start:end]

        return ListProductsOutput(
            products=page,
            total=len(all_products),
        )

    @handles(SearchProducts)
    async def search_products(self, command: SearchProducts) -> SearchProductsOutput:
        """Handle product search with name and price filters."""
        query = command.request.query.lower()
        results = []

        for product in _products.values():
            # Name match
            if query not in product["name"].lower():
                continue

            # Price filters
            if command.request.min_price and product["price"] < command.request.min_price:
                continue
            if command.request.max_price and product["price"] > command.request.max_price:
                continue

            results.append(product)

        return SearchProductsOutput(
            products=results,
            total=len(results),
        )


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

    # Create ModuleRouter and register commands
    router = ModuleRouter(host)
    router.register_command(CreateProduct)
    router.register_command(GetProduct)
    router.register_command(ListProducts)
    router.register_command(SearchProducts)

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
