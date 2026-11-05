"""Read-only, tenant-scoped catalog. One JSON source for the storefront and the model tools.

The seed and the image manifest are validated once at startup. Every read takes the tenant
from authenticated context and filters by it before ranking, counting or returning anything.
Unknown and foreign identifiers raise the same `Denied`, so a caller cannot tell them apart.
"""

import hashlib
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .errors import Denied

DATA = Path(__file__).parent / "data"
TENANTS = ("retailer-a", "retailer-b")
CATEGORIES = ("jackets", "rain-shells", "fleeces", "vests", "hats", "gloves")
# A category filter also matches its subcategories: a rain shell is a jacket to a shopper.
CATEGORY_GROUPS = {"jackets": ("jackets", "rain-shells")}
SIZES = ("XS", "S", "M", "L", "XL", "OS")
REVIEW_STATES = ("pending review", "approved", "rejected")
IMAGE_ROUTE = "/v1/catalog/images/"
SCOPE_DENIED = "catalog_scope_denied"
MAX_RESULTS = 3
JPEG_SIGNATURE = b"\xff\xd8\xff"

Tenant = Literal["retailer-a", "retailer-b"]
Id = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
ImageId = Annotated[str, StringConstraints(pattern=r"^img-[a-z0-9][a-z0-9-]{0,63}$")]


class CatalogError(ValueError):
    """The packaged seed or manifest is not safe to serve. The gateway refuses to start."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Retailer(Strict):
    tenant_id: Tenant
    name: str = Field(min_length=1, max_length=80)


class Store(Strict):
    tenant_id: Tenant
    store_id: Id
    name: str = Field(min_length=1, max_length=80)
    # IANA zone for date and time answers. The startup check below confirms the zone exists.
    timezone: str = Field(
        max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$"
    )


class Variant(Strict):
    variant_id: Id
    color: str = Field(pattern=r"^[a-z]{3,20}$")
    size: Literal["XS", "S", "M", "L", "XL", "OS"]
    image_id: ImageId | None = None


class Product(Strict):
    tenant_id: Tenant
    product_id: Id
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    category: Literal["jackets", "rain-shells", "fleeces", "vests", "hats", "gloves"]
    tags: tuple[str, ...] = Field(min_length=1, max_length=12)
    price_minor: int = Field(ge=0, le=10_000_000)
    image_ids: tuple[ImageId, ...] = Field(min_length=1)
    variants: tuple[Variant, ...] = Field(min_length=1, max_length=12)


class Stock(Strict):
    tenant_id: Tenant
    store_id: Id
    product_id: Id
    variant_id: Id
    quantity: int = Field(ge=0, le=100_000)
    provenance: Literal["synthetic"]


class CatalogFile(Strict):
    schema_version: Literal[1]
    catalog_version: str = Field(pattern=r"^[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,4}$")
    currency: str
    retailers: tuple[Retailer, ...] = Field(min_length=1)
    stores: tuple[Store, ...] = Field(min_length=1)
    products: tuple[Product, ...] = Field(min_length=1)
    inventory: tuple[Stock, ...]
    assets: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*\.json$")


class Asset(Strict):
    image_id: ImageId
    tenant_id: Tenant
    product_id: Id
    variant_ids: tuple[Id, ...] = Field(min_length=1)
    color: str = Field(pattern=r"^[a-z]{3,20}$")
    filename: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,80}\.jpg$")
    mime_type: Literal["image/jpeg"]
    alt_text: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: Literal["synthetic"]
    generator: str = Field(min_length=1, max_length=200)
    created: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    redistribution_basis: str = Field(min_length=1, max_length=200)
    review_status: Literal["pending review", "approved", "rejected"]


class AssetFile(Strict):
    schema_version: Literal[1]
    catalog_version: str
    assets: tuple[Asset, ...] = Field(min_length=1)


# Search normalization. Both the query and the catalog text go through the same steps, so a
# synonym or plural rule can never make one side match text the other side cannot see.
STOPWORDS = frozenset(
    """a an the i im me my we our you your for with and or to of in on at it its is are be
    need needs want wants looking look find show something some any anything one ones that this
    these those please can could would like good nice best new get buy shop under below less than
    around about dollar dollars usd budget price priced cost cheap cheaper affordable weather day
    days small medium large size sized xs xl item items thing things product products gear do does
    have has come comes there here what which also just really very""".split()
)
SYNONYMS = {
    "rainy": "rain",
    # A compound word expands to its catalog words, so "raincoat" ranks jackets, not hats.
    "raincoat": "rain jacket",
    "raincoats": "rain jacket",
    "rainjacket": "rain jacket",
    "wet": "rain",
    "grey": "gray",
    "coat": "jacket",
    "light": "lightweight",
    "lighter": "lightweight",
    "ultralight": "lightweight",
    "windy": "wind",
    "windproof": "wind",
    "cold": "warm",
    "chilly": "warm",
    "freezing": "warm",
    "hood": "hooded",
    "hoodie": "hooded",
    "hike": "hiking",
    "trail": "hiking",
    "sail": "sailing",
    "boat": "sailing",
    "boating": "sailing",
    "mitt": "mitten",
    "mittens": "mitten",
    "mitts": "mitten",
}
CATEGORY_WORDS = {
    "jacket": "jackets",
    "jackets": "jackets",
    "rain shell": "rain-shells",
    "rain shells": "rain-shells",
    "rain-shell": "rain-shells",
    "rain-shells": "rain-shells",
    "shell": "rain-shells",
    "shells": "rain-shells",
    "fleece": "fleeces",
    "fleeces": "fleeces",
    "vest": "vests",
    "vests": "vests",
    "hat": "hats",
    "hats": "hats",
    "glove": "gloves",
    "gloves": "gloves",
}
SIZE_WORDS = {
    "xs": "XS",
    "extra small": "XS",
    "s": "S",
    "small": "S",
    "m": "M",
    "med": "M",
    "medium": "M",
    "l": "L",
    "large": "L",
    "xl": "XL",
    "extra large": "XL",
    "os": "OS",
    "one size": "OS",
}
# Field weights. Each query word scores once, with its best field.
WEIGHTS = (("tags", 3), ("category", 3), ("name", 2), ("color", 1), ("description", 1))
# When a query names a kind of product, results must be that kind: "rain jacket" never
# falls back to a rain hat.
TYPE_WORDS = frozenset(
    "jacket shell softshell anorak parka fleece vest hat beanie cap glove mitten".split()
)


def words(text):
    text = unicodedata.normalize("NFKC", text).lower()
    out = []
    for found in re.findall(r"[a-z0-9]+", text):
        for word in SYNONYMS.get(found, found).split():
            if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
                word = word[:-1]
            word = SYNONYMS.get(word, word)
            if word not in STOPWORDS:
                out.append(word)
    return out


def phrase(value):
    return " ".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", value).lower()))


def normalize_category(value):
    category = CATEGORY_WORDS.get(phrase(value))
    if category is None:
        raise ValueError("unknown_category")
    return category


def normalize_size(value):
    size = SIZE_WORDS.get(phrase(value))
    if size is None:
        raise ValueError("unknown_size")
    return size


def normalize_color(value):
    color = phrase(value).replace(" ", "")
    color = SYNONYMS.get(color, color)
    if not re.fullmatch(r"[a-z]{3,20}", color):
        raise ValueError("invalid_color")
    return color


def format_price(minor):
    return f"${minor // 100:,}.{minor % 100:02d}"


def _unique(values, kind):
    seen = set()
    for value in values:
        if value in seen:
            raise CatalogError(f"duplicate_{kind}: {value}")
        seen.add(value)


class CatalogRepository:
    def __init__(self, catalog_path=None):
        path = Path(catalog_path) if catalog_path else DATA / "catalog.json"
        self.root = path.parent
        try:
            catalog = CatalogFile.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise CatalogError(f"invalid_catalog: {type(exc).__name__}") from exc
        try:
            manifest = AssetFile.model_validate_json((self.root / catalog.assets).read_bytes())
        except (OSError, ValidationError) as exc:
            raise CatalogError(f"invalid_asset_manifest: {type(exc).__name__}") from exc
        if catalog.currency != "USD":
            raise CatalogError("unsupported_currency")
        if manifest.catalog_version != catalog.catalog_version:
            raise CatalogError("asset_manifest_version_mismatch")
        self.catalog_version = catalog.catalog_version
        self.currency = catalog.currency
        self._index(catalog)
        self._load_assets(manifest)

    def _index(self, catalog):
        _unique([r.tenant_id for r in catalog.retailers], "retailer")
        _unique([s.store_id for s in catalog.stores], "store")
        _unique([p.product_id for p in catalog.products], "product")
        _unique([v.variant_id for p in catalog.products for v in p.variants], "variant")
        _unique([i for p in catalog.products for i in p.image_ids], "image_reference")
        self.retailers = {r.tenant_id: r for r in catalog.retailers}
        self.stores = {s.store_id: s for s in catalog.stores}
        self.products = {p.product_id: p for p in catalog.products}
        self.variants: dict[str, tuple[Product, Variant]] = {}
        for store in catalog.stores:
            if store.tenant_id not in self.retailers:
                raise CatalogError(f"store_without_retailer: {store.store_id}")
            try:
                ZoneInfo(store.timezone)
            except (KeyError, ValueError, OSError):
                # Needs the system time zone database (/usr/share/zoneinfo) or the tzdata package.
                raise CatalogError(f"invalid_timezone: {store.store_id}") from None
        for tenant in self.retailers:
            if not any(s.tenant_id == tenant for s in catalog.stores):
                raise CatalogError(f"retailer_without_store: {tenant}")
        for product in catalog.products:
            if product.tenant_id not in self.retailers:
                raise CatalogError(f"product_without_retailer: {product.product_id}")
            if len(set(product.tags)) != len(product.tags) or not all(
                re.fullmatch(r"[a-z][a-z-]{1,23}", t) for t in product.tags
            ):
                raise CatalogError(f"invalid_tags: {product.product_id}")
            for variant in product.variants:
                if variant.image_id is not None and variant.image_id not in product.image_ids:
                    raise CatalogError(f"broken_variant_image: {variant.variant_id}")
                self.variants[variant.variant_id] = (product, variant)
        self.stock: dict[tuple[str, str], int] = {}
        for row in catalog.inventory:
            store = self.stores.get(row.store_id)
            pair = self.variants.get(row.variant_id)
            if store is None or pair is None or pair[0].product_id != row.product_id:
                raise CatalogError(f"broken_inventory_reference: {row.variant_id}")
            if not row.tenant_id == store.tenant_id == pair[0].tenant_id:
                raise CatalogError(f"cross_tenant_inventory: {row.variant_id}")
            if (row.store_id, row.variant_id) in self.stock:
                raise CatalogError(f"duplicate_inventory: {row.store_id} {row.variant_id}")
            self.stock[(row.store_id, row.variant_id)] = row.quantity
        # Search text is derived once from the validated records.
        self._text: dict[str, dict[str, set[str]]] = {}
        for product in catalog.products:
            self._text[product.product_id] = {
                "tags": set(words(" ".join(t.replace("-", " ") for t in product.tags))),
                "category": set(words(product.category.replace("-", " "))),
                "name": set(words(product.name)),
                "color": {v.color for v in product.variants},
                "description": set(words(product.description)),
            }
        # Query stemming only maps onto words this catalog actually uses.
        self._vocab: set[str] = {
            word
            for text in self._text.values()
            for field in ("tags", "category", "name", "description")
            for word in text[field]
        }

    def _load_assets(self, manifest):
        _unique([a.image_id for a in manifest.assets], "asset")
        _unique([a.filename for a in manifest.assets], "asset_file")
        folder = (self.root / "products").resolve()
        referenced = {i: p for p in self.products.values() for i in p.image_ids}
        self.assets: dict[str, Asset] = {}
        self.images: dict[str, bytes] = {}
        for asset in manifest.assets:
            product = referenced.get(asset.image_id)
            if product is None:
                raise CatalogError(f"unreferenced_asset: {asset.image_id}")
            if (asset.tenant_id, asset.product_id) != (product.tenant_id, product.product_id):
                raise CatalogError(f"asset_ownership_mismatch: {asset.image_id}")
            for variant_id in asset.variant_ids:
                pair = self.variants.get(variant_id)
                if pair is None or pair[0].product_id != product.product_id:
                    raise CatalogError(f"asset_variant_mismatch: {asset.image_id}")
                if pair[1].image_id != asset.image_id or pair[1].color != asset.color:
                    raise CatalogError(f"asset_variant_mismatch: {asset.image_id}")
            if asset.review_status == "rejected":
                raise CatalogError(f"asset_rejected_in_review: {asset.image_id}")
            path = (folder / asset.filename).resolve()
            if path.parent != folder:
                raise CatalogError(f"asset_outside_directory: {asset.image_id}")
            try:
                data = path.read_bytes()
            except OSError:
                raise CatalogError(f"missing_asset_file: {asset.image_id}") from None
            if hashlib.sha256(data).hexdigest() != asset.sha256:
                raise CatalogError(f"asset_checksum_mismatch: {asset.image_id}")
            if not data.startswith(JPEG_SIGNATURE):
                raise CatalogError(f"asset_type_mismatch: {asset.image_id}")
            self.assets[asset.image_id] = asset
            self.images[asset.image_id] = data
        missing = sorted(set(referenced) - set(self.assets))
        if missing:
            raise CatalogError(f"missing_asset: {missing[0]}")

    # Scoped lookups. Every public read starts here.

    def _product(self, tenant, product_id):
        product = self.products.get(product_id) if isinstance(product_id, str) else None
        if product is None or product.tenant_id != tenant:
            raise Denied(SCOPE_DENIED)
        return product

    def _store(self, tenant, store_id):
        store = self.stores.get(store_id) if isinstance(store_id, str) else None
        if store is None or store.tenant_id != tenant:
            raise Denied(SCOPE_DENIED)
        return store

    def owns_product(self, tenant, product_id):
        try:
            self._product(tenant, product_id)
        except Denied:
            return False
        return True

    def tenant_stores(self, tenant):
        return [s for s in self.stores.values() if s.tenant_id == tenant]

    def default_store(self, tenant):
        stores = self.tenant_stores(tenant)
        if not stores:
            raise Denied(SCOPE_DENIED)
        return stores[0].store_id

    def asset_review(self):
        return sorted({a.review_status for a in self.assets.values()})

    def image_path(self, image_id):
        return IMAGE_ROUTE + image_id if image_id else None

    def card(self, tenant, product_id):
        """Shopper card resolved from validated catalog facts only."""
        p = self._product(tenant, product_id)
        image = self.assets[p.image_ids[0]]
        return {
            "product_id": p.product_id,
            "name": p.name,
            "category": p.category,
            "description": p.description,
            "price_minor": p.price_minor,
            "price_display": format_price(p.price_minor),
            "image_path": self.image_path(image.image_id),
            "image_alt": image.alt_text,
            "colors": list(dict.fromkeys(v.color for v in p.variants)),
            "sizes": [s for s in SIZES if any(v.size == s for v in p.variants)],
            "variants": [
                {
                    "variant_id": v.variant_id,
                    "color": v.color,
                    "size": v.size,
                    "image_path": self.image_path(v.image_id),
                }
                for v in p.variants
            ],
        }

    def storefront(self, tenant):
        if tenant not in self.retailers:
            raise Denied(SCOPE_DENIED)
        return {
            "tenant": tenant,
            "retailer_name": self.retailers[tenant].name,
            "catalog_version": self.catalog_version,
            "currency": self.currency,
            "inventory_provenance": "synthetic",
            "asset_review": self.asset_review(),
            "default_store_id": self.default_store(tenant),
            "stores": [
                {"store_id": s.store_id, "name": s.name} for s in self.tenant_stores(tenant)
            ],
            "products": [
                self.card(tenant, p.product_id)
                for p in self.products.values()
                if p.tenant_id == tenant
            ],
        }

    def product_detail(self, tenant, product_id):
        """Storefront detail: the card plus tags and every approved image of the product."""
        p = self._product(tenant, product_id)
        return self.card(tenant, product_id) | {
            "tags": list(p.tags),
            "images": [
                {"image_path": self.image_path(i), "alt": self.assets[i].alt_text, "color": c}
                for i, c in ((i, self.assets[i].color) for i in p.image_ids)
            ],
            "catalog_version": self.catalog_version,
        }

    def image(self, tenant, image_id):
        asset = self.assets.get(image_id) if isinstance(image_id, str) else None
        if asset is None or asset.tenant_id != tenant:
            raise Denied(SCOPE_DENIED)
        return self.images[image_id], asset.mime_type

    def stem(self, term):
        """Map a comparative or superlative onto a catalog word: warmest and warmer become
        warm, coziest becomes cozy. Only when that base word exists in this catalog, so winter,
        water and liner keep their meaning. Without this, "warmest jacket" scored every jacket
        the same and the cheapest three won."""
        if term in self._vocab:
            return term
        for suffix in ("iest", "ier", "est", "er"):
            if term.endswith(suffix) and len(term) > len(suffix) + 2:
                base = term[: -len(suffix)]
                candidates = (base + "y",) if suffix.startswith("i") else (base, base + "e")
                for candidate in candidates:
                    if candidate in self._vocab:
                        return candidate
        return term

    def search(self, tenant, query="", filters=None, limit=MAX_RESULTS):
        """Tenant filter first, then explicit filters, then text score. Stable ordering."""
        if tenant not in self.retailers:
            raise Denied(SCOPE_DENIED)
        filters = {k: v for k, v in (filters or {}).items() if v is not None}
        if set(filters) - {"category", "color", "size", "max_price_minor"}:
            raise ValueError("unknown_filter")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RESULTS:
            raise ValueError("invalid_limit")
        category, color = filters.get("category"), filters.get("color")
        size, ceiling = filters.get("size"), filters.get("max_price_minor")
        if category is not None and category not in CATEGORIES:
            raise ValueError("unknown_category")
        if size is not None and size not in SIZES:
            raise ValueError("unknown_size")
        if color is not None and not (
            isinstance(color, str) and re.fullmatch(r"[a-z]{3,20}", color)
        ):
            raise ValueError("invalid_color")
        if ceiling is not None and (
            not isinstance(ceiling, int) or isinstance(ceiling, bool) or not 0 <= ceiling <= 10**7
        ):
            raise ValueError("invalid_max_price_minor")
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("invalid_query")
        terms = list(dict.fromkeys(self.stem(t) for t in words(query)))
        kinds = [t for t in terms if t in TYPE_WORDS]
        scored = []
        for product in self.products.values():
            if product.tenant_id != tenant:
                continue
            if category is not None and product.category not in CATEGORY_GROUPS.get(
                category, (category,)
            ):
                continue
            if ceiling is not None and product.price_minor > ceiling:
                continue
            if (color or size) and not any(
                (color is None or v.color == color) and (size is None or v.size == size)
                for v in product.variants
            ):
                continue
            text = self._text[product.product_id]
            if kinds and not any(
                k in text[field] for k in kinds for field in ("tags", "category", "name")
            ):
                continue
            score = sum(
                max((w for field, w in WEIGHTS if term in text[field]), default=0) for term in terms
            )
            if terms and score == 0:
                continue
            scored.append((score, product.price_minor, product.product_id))
        # Keep results that score at least half of the best match, so one shared word such as
        # "lightweight" does not pull gloves into a rain jacket search.
        best = max((score for score, _, _ in scored), default=0)
        ranked = sorted((-sc, price, pid) for sc, price, pid in scored if 2 * sc >= best)
        applied = dict(filters)
        if ceiling is not None:
            applied["max_price_display"] = format_price(ceiling)
        return {
            "catalog_version": self.catalog_version,
            "applied_filters": applied,
            "match_count": len(ranked),
            "products": [self.summary(tenant, pid) for _, _, pid in ranked[:limit]],
        }

    def summary(self, tenant, product_id):
        """Bounded model-facing summary of one scoped product."""
        p = self._product(tenant, product_id)
        return {
            "product_id": p.product_id,
            "name": p.name,
            "category": p.category,
            "price_minor": p.price_minor,
            "price_display": format_price(p.price_minor),
            "tags": list(p.tags),
            "description": p.description[:160],
            "variants": [
                {"variant_id": v.variant_id, "color": v.color, "size": v.size} for v in p.variants
            ],
        }

    def details(self, tenant, product_id):
        p = self._product(tenant, product_id)
        return {
            "catalog_version": self.catalog_version,
            "product": self.summary(tenant, product_id)
            | {"description": p.description, "image_ids": list(p.image_ids)},
        }

    def availability(self, tenant, product_id, variant_id, store_id=None):
        product = self._product(tenant, product_id)
        store = self._store(tenant, store_id or self.default_store(tenant))
        pair = self.variants.get(variant_id) if isinstance(variant_id, str) else None
        if pair is None or pair[0].product_id != product.product_id:
            raise Denied(SCOPE_DENIED)
        variant = pair[1]
        quantity = self.stock.get((store.store_id, variant.variant_id), 0)
        return {
            "catalog_version": self.catalog_version,
            "product_id": product.product_id,
            "product_name": product.name,
            "variant_id": variant.variant_id,
            "color": variant.color,
            "size": variant.size,
            "store_id": store.store_id,
            "store_name": store.name,
            "quantity": quantity,
            "in_stock": quantity > 0,
            "provenance": "synthetic",
        }

    def variant_for(self, tenant, product_id, color, size):
        product = self._product(tenant, product_id)
        for variant in product.variants:
            if variant.color == color and variant.size == size:
                return variant.variant_id
        raise Denied(SCOPE_DENIED)


@lru_cache(maxsize=1)
def default_repository():
    return CatalogRepository()
