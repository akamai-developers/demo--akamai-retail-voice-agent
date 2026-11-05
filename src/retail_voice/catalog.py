"""Tool dispatcher. The tenant always comes from the session, never from model arguments.

Every argument is re-validated here before the repository sees it. The repository enforces
tenant scope again on every read, so a bad dispatcher check still cannot leak a record.
"""

import re
from decimal import Decimal

from .catalog_repository import (
    MAX_RESULTS,
    default_repository,
    normalize_category,
    normalize_color,
    normalize_size,
)
from .errors import Denied, ToolArgumentError

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
QUERY_LIMIT = 200
PRICE_CEILING = re.compile(
    r"\b(?:under|below|less than|up to|at most|max(?:imum)?)\s*\$?\s*"
    r"(\d{1,5}(?:\.\d{1,2})?)(?![\d.,])",
    re.I,
)


def explicit_search_budget(text):
    """One unambiguous positive dollar ceiling in a shopping search request."""
    matches = list(PRICE_CEILING.finditer(text))
    if len(matches) != 1 or re.search(r"\b(?:don't|don’t|do not|not|ignore|forget)\b", text, re.I):
        return None
    if not re.search(
        r"\b(?:show|find|need|want|looking|any|anything|budget|keep|actually|do you have)\b",
        text,
        re.I,
    ):
        return None
    amount = int(Decimal(matches[0].group(1)) * 100)
    return amount if amount > 0 else None


# name: (allowed arguments, required arguments)
SHOPPING_SCHEMA = {
    "search_products": (
        {"query", "category", "color", "size", "max_price_minor", "limit"},
        {"query"},
    ),
    "get_product_details": ({"product_id"}, {"product_id"}),
    "check_availability": ({"product_id", "variant_id", "store_id"}, {"product_id", "variant_id"}),
    "add_to_cart": ({"product_id", "variant_id", "quantity"}, {"product_id", "variant_id"}),
}


def lookup_inventory(tenant, sku, store_id, repository=None):
    """Legacy diagnostic tool: navy medium stock for one product at one store."""
    repo = repository or default_repository()
    try:
        variant_id = repo.variant_for(tenant, sku, "navy", "M")
        stock = repo.availability(tenant, sku, variant_id, store_id)
    except Denied:
        raise Denied("catalog_scope_denied") from None
    return {"store": stock["store_id"], "navy_medium": stock["quantity"]}


def _identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ToolArgumentError("tool_arguments_denied")
    return value


def _integer(value, low, high):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ToolArgumentError("tool_arguments_denied")
    return value


def search_arguments(args):
    """Validated, normalized search arguments. Raises ToolArgumentError on any bad value."""
    query = args.get("query", "")
    if not isinstance(query, str) or len(query) > QUERY_LIMIT:
        raise ToolArgumentError("tool_arguments_denied")
    filters = {}
    try:
        for key, normalize in (
            ("category", normalize_category),
            ("color", normalize_color),
            ("size", normalize_size),
        ):
            if key in args:
                if not isinstance(args[key], str) or len(args[key]) > 40:
                    raise ValueError(key)
                filters[key] = normalize(args[key])
    except ValueError:
        raise ToolArgumentError("tool_arguments_denied") from None
    if "max_price_minor" in args:
        ceiling = _integer(args["max_price_minor"], 0, 10_000_000)
        # Models send 0 when the shopper drops the budget. A $0 ceiling would match nothing,
        # so 0 means no ceiling; applied_filters in the result shows what was applied.
        if ceiling:
            filters["max_price_minor"] = ceiling
    limit = _integer(args.get("limit", MAX_RESULTS), 1, MAX_RESULTS)
    return query, filters, limit


def execute_tool(
    tenant, name, arguments, *, stores=None, default_store=None, repository=None, cart=()
):
    """Run one validated tool call. `cart` is the session cart so far, including this turn's
    earlier adds; only add_to_cart reads it."""
    if name == "lookup_inventory":
        if not isinstance(arguments, dict) or set(arguments) != {"sku", "store_id"}:
            raise Denied("tool_scope_denied")
        if not all(isinstance(v, str) and 0 < len(v) <= 80 for v in arguments.values()):
            raise Denied("tool_arguments_denied")
        return lookup_inventory(tenant, repository=repository, **arguments)
    if name not in SHOPPING_SCHEMA:
        raise Denied("tool_scope_denied")
    if not isinstance(arguments, dict):
        raise ToolArgumentError("tool_arguments_denied")
    # Models often send explicit nulls for optional arguments. Treat them as absent.
    args = {k: v for k, v in arguments.items() if v is not None}
    allowed, required = SHOPPING_SCHEMA[name]
    if set(args) - allowed or required - set(args):
        raise ToolArgumentError("tool_arguments_denied")
    repo = repository or default_repository()
    if name == "search_products":
        query, filters, limit = search_arguments(args)
        return repo.search(tenant, query, filters, limit)
    if name == "get_product_details":
        return repo.details(tenant, _identifier(args["product_id"]))
    product_id, variant_id = _identifier(args["product_id"]), _identifier(args["variant_id"])
    if name == "add_to_cart":
        # The same ownership and variant checks as a stock lookup, at the shopper's store. The
        # cart itself lives in session memory; this returns the validated line item.
        quantity = _integer(args.get("quantity", 1), 1, 5)
        store_id = default_store or repo.default_store(tenant)
        fact = repo.availability(tenant, product_id, variant_id, store_id)
        card = repo.card(tenant, product_id)
        # The cart is not a reservation, but it never holds more of a variant than the catalog
        # says is available. `in_cart` is what the cart already holds of this exact variant.
        held = sum(item["quantity"] for item in cart if item["variant_id"] == variant_id)
        return {
            "added": held + quantity <= fact["quantity"],
            "product_id": product_id,
            "product_name": fact["product_name"],
            "variant_id": variant_id,
            "color": fact["color"],
            "size": fact["size"],
            "quantity": quantity,
            "price_minor": card["price_minor"],
            "price_display": card["price_display"],
            "store_id": store_id,
            "store_name": fact["store_name"],
            "available": fact["quantity"],
            "in_cart": held,
        }
    permitted = (
        list(stores) if stores is not None else [s.store_id for s in repo.tenant_stores(tenant)]
    )
    store_id = args.get("store_id", default_store or repo.default_store(tenant))
    if not isinstance(store_id, str) or store_id not in permitted:
        raise Denied("catalog_scope_denied")
    return repo.availability(tenant, product_id, variant_id, store_id)


def result_count(name, output):
    return len(output["products"]) if name == "search_products" else 1
