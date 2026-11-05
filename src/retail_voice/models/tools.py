"""Tool schemas sent to the model. The dispatcher in catalog.py re-validates every argument."""

from ..catalog_repository import CATEGORIES, MAX_RESULTS, SIZES

ID_PATTERN = "^[a-z0-9][a-z0-9-]{0,63}$"
QUERY_LIMIT = 200

LEGACY_INVENTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_inventory",
        "description": "Read synthetic navy medium inventory for the selected product and store.",
        "parameters": {
            "type": "object",
            "properties": {"sku": {"type": "string"}, "store_id": {"type": "string"}},
            "required": ["sku", "store_id"],
            "additionalProperties": False,
        },
    },
}


def shopping_tools(store_ids):
    """Three read-only shopping tools and the cart tool, which writes session memory only.
    Store choices are the session tenant's stores only."""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_products",
                "description": (
                    "Search this retailer's catalog. Returns up to 3 matching products in order, "
                    "with ids, prices, variants and the total match count. Zero matches is a "
                    "valid answer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "maxLength": QUERY_LIMIT,
                            "description": "Short keywords, for example 'lightweight rain jacket'.",
                        },
                        "category": {
                            "type": "string",
                            "enum": list(CATEGORIES),
                            "description": "Only when the shopper names one. jackets includes rain-shells.",
                        },
                        "color": {"type": "string", "maxLength": 20},
                        "size": {"type": "string", "enum": list(SIZES)},
                        "max_price_minor": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Budget ceiling in cents: $150 is 15000. "
                            "0 or leaving it out means no budget.",
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_product_details",
                "description": (
                    "Read one product: description, price, colors, sizes and variant ids. "
                    "It does not show stock."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string", "pattern": ID_PATTERN},
                    },
                    "required": ["product_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_availability",
                "description": (
                    "Read synthetic stock for one exact variant at one store. Use it for every "
                    "availability or in-stock question. Omit store_id for the shopper's store."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string", "pattern": ID_PATTERN},
                        "variant_id": {
                            "type": "string",
                            "pattern": ID_PATTERN,
                            "description": "Exact variant id from a search or details result.",
                        },
                        "store_id": {"type": "string", "enum": list(store_ids)},
                    },
                    "required": ["product_id", "variant_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_to_cart",
                "description": (
                    "Add one exact variant of this retailer's product to the shopper's cart, "
                    "when the shopper asks to add, take or buy it. Returns added false when "
                    "that variant is out of stock at the shopper's store. There is no checkout."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string", "pattern": ID_PATTERN},
                        "variant_id": {
                            "type": "string",
                            "pattern": ID_PATTERN,
                            "description": "Exact variant id from product_in_focus or a tool result.",
                        },
                        "quantity": {"type": "integer", "minimum": 1, "maximum": 5},
                    },
                    "required": ["product_id", "variant_id"],
                    "additionalProperties": False,
                },
            },
        },
    ]


SHOPPING_TOOL_NAMES = (
    "search_products",
    "get_product_details",
    "check_availability",
    "add_to_cart",
)
