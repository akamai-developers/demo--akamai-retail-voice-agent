import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema
from fastapi.testclient import TestClient

from retail_voice.api import create_app
from retail_voice.catalog import execute_tool, lookup_inventory
from retail_voice.catalog_repository import (
    DATA,
    CatalogError,
    CatalogRepository,
    default_repository,
    format_price,
)
from retail_voice.errors import Denied, ToolArgumentError

ROOT = Path(__file__).resolve().parents[1]
KEYS = {"catalog-test-key-tenant-a": "retailer-a", "catalog-test-key-tenant-b": "retailer-b"}
OPERATOR = "catalog-test-operator-key"
A = {"Authorization": "Bearer catalog-test-key-tenant-a"}
B = {"Authorization": "Bearer catalog-test-key-tenant-b"}


def schema(name):
    return json.loads((ROOT / "contracts" / name).read_text())


class SeedTests(unittest.TestCase):
    def test_seed_and_manifest_match_their_json_schemas(self):
        for data, contract in [
            ("catalog.json", "catalog.schema.json"),
            ("assets.json", "assets.schema.json"),
        ]:
            validator = jsonschema.Draft202012Validator(schema(contract))
            validator.check_schema(schema(contract))
            errors = list(validator.iter_errors(json.loads((DATA / data).read_text())))
            self.assertEqual(errors, [], data)

    def test_seed_shape_matches_the_plan(self):
        repo = default_repository()
        per_tenant = {
            t: [p for p in repo.products.values() if p.tenant_id == t] for t in repo.retailers
        }
        self.assertTrue(15 <= len(per_tenant["retailer-a"]) <= 20)
        self.assertTrue(5 <= len(per_tenant["retailer-b"]) <= 6)
        self.assertEqual(len(repo.tenant_stores("retailer-a")), 2)
        self.assertEqual(len(repo.tenant_stores("retailer-b")), 2)
        prices = [p.price_minor for p in per_tenant["retailer-a"]]
        self.assertEqual((min(prices), max(prices)), (2900, 24900))
        for product in repo.products.values():
            self.assertTrue(2 <= len(product.variants) <= 4, product.product_id)
            self.assertTrue(product.image_ids)
        categories = {p.category for p in per_tenant["retailer-a"]}
        self.assertEqual(
            categories, {"jackets", "rain-shells", "fleeces", "vests", "hats", "gloves"}
        )
        names = [{p.name for p in per_tenant[t]} for t in ("retailer-a", "retailer-b")]
        self.assertFalse(names[0] & names[1])

    def test_every_store_has_its_time_zone(self):
        zones = {s.store_id: s.timezone for s in default_repository().stores.values()}
        self.assertEqual(
            zones,
            {
                "store-a": "America/Denver",
                "store-a-riverside": "America/Denver",
                "store-b": "America/New_York",
                "store-b-market": "America/New_York",
            },
        )

    def test_every_asset_is_synthetic_checksummed_and_pending_human_review(self):
        manifest = json.loads((DATA / "assets.json").read_text())
        for asset in manifest["assets"]:
            data = (DATA / "products" / asset["filename"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), asset["sha256"])
            self.assertEqual(asset["provenance"], "synthetic")
            self.assertNotIn("creative commons", asset["redistribution_basis"].lower())
            self.assertIn(asset["review_status"], {"pending review", "approved"})


class BrokenSeedTests(unittest.TestCase):
    """Each broken copy of the packaged seed must stop the repository from loading."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "data"
        shutil.copytree(DATA, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def broken(self, mutate, expected):
        catalog = json.loads((self.root / "catalog.json").read_text())
        assets = json.loads((self.root / "assets.json").read_text())
        mutate(catalog, assets)
        (self.root / "catalog.json").write_text(json.dumps(catalog))
        (self.root / "assets.json").write_text(json.dumps(assets))
        with self.assertRaises(CatalogError) as caught:
            CatalogRepository(self.root / "catalog.json")
        self.assertIn(expected, str(caught.exception))

    def test_unchanged_copy_loads(self):
        repo = CatalogRepository(self.root / "catalog.json")
        self.assertEqual(repo.catalog_version, default_repository().catalog_version)

    def test_unsupported_currency(self):
        self.broken(lambda c, a: c.update(currency="EUR"), "unsupported_currency")

    def test_negative_price(self):
        self.broken(lambda c, a: c["products"][0].update(price_minor=-1), "invalid_catalog")

    def test_fractional_or_string_price(self):
        self.broken(lambda c, a: c["products"][0].update(price_minor="12900"), "invalid_catalog")

    def test_negative_quantity(self):
        self.broken(lambda c, a: c["inventory"][0].update(quantity=-3), "invalid_catalog")

    def test_duplicate_product_id(self):
        def mutate(c, a):
            c["products"][1]["product_id"] = c["products"][0]["product_id"]

        self.broken(mutate, "duplicate_product")

    def test_duplicate_variant_id(self):
        def mutate(c, a):
            c["products"][1]["variants"][0]["variant_id"] = c["products"][0]["variants"][0][
                "variant_id"
            ]

        self.broken(mutate, "duplicate_variant")

    def test_unknown_time_zone(self):
        self.broken(lambda c, a: c["stores"][0].update(timezone="Mars/Olympus"), "invalid_timezone")

    def test_missing_time_zone(self):
        self.broken(lambda c, a: c["stores"][0].pop("timezone"), "invalid_catalog")

    def test_time_zone_must_be_a_zone_name_not_a_path(self):
        self.broken(
            lambda c, a: c["stores"][0].update(timezone="../etc/localtime"), "invalid_catalog"
        )

    def test_duplicate_store_id(self):
        self.broken(lambda c, a: c["stores"][1].update(store_id="store-a"), "duplicate_store")

    def test_broken_inventory_reference(self):
        self.broken(
            lambda c, a: c["inventory"][0].update(variant_id="no-such-variant"),
            "broken_inventory_reference",
        )

    def test_broken_variant_image_reference(self):
        self.broken(
            lambda c, a: c["products"][0]["variants"][0].update(image_id="img-not-listed"),
            "broken_variant_image",
        )

    def test_cross_tenant_inventory_row(self):
        def mutate(c, a):
            row = next(r for r in c["inventory"] if r["tenant_id"] == "retailer-a")
            row["store_id"] = "store-b"

        self.broken(mutate, "cross_tenant_inventory")

    def test_cross_tenant_asset_ownership(self):
        def mutate(c, a):
            a["assets"][0]["tenant_id"] = "retailer-b"

        self.broken(mutate, "asset_ownership_mismatch")

    def test_missing_asset_entry(self):
        self.broken(lambda c, a: a["assets"].pop(0), "missing_asset")

    def test_missing_image_file(self):
        def mutate(c, a):
            (self.root / "products" / a["assets"][0]["filename"]).unlink()

        self.broken(mutate, "missing_asset_file")

    def test_checksum_mismatch(self):
        def mutate(c, a):
            a["assets"][0]["sha256"] = "0" * 64

        self.broken(mutate, "asset_checksum_mismatch")

    def test_review_status_required(self):
        self.broken(lambda c, a: a["assets"][0].pop("review_status"), "invalid_asset_manifest")

    def test_rejected_review_blocks_startup(self):
        self.broken(
            lambda c, a: a["assets"][0].update(review_status="rejected"),
            "asset_rejected_in_review",
        )

    def test_redistribution_basis_required(self):
        self.broken(
            lambda c, a: a["assets"][0].update(redistribution_basis=""), "invalid_asset_manifest"
        )

    def test_filename_cannot_leave_the_asset_directory(self):
        self.broken(
            lambda c, a: a["assets"][0].update(filename="../catalog.jpg"), "invalid_asset_manifest"
        )

    def test_symlink_outside_directory_is_rejected(self):
        def mutate(c, a):
            target = self.root / "products" / a["assets"][0]["filename"]
            outside = Path(self.temp.name) / "outside.jpg"
            shutil.copy(target, outside)
            target.unlink()
            target.symlink_to(outside)

        self.broken(mutate, "asset_outside_directory")

    def test_unreferenced_asset(self):
        def mutate(c, a):
            extra = dict(a["assets"][0], image_id="img-orphan", filename="img-orphan.jpg")
            shutil.copy(
                self.root / "products" / a["assets"][0]["filename"],
                self.root / "products" / "img-orphan.jpg",
            )
            a["assets"].append(extra)

        self.broken(mutate, "unreferenced_asset")

    def test_manifest_version_must_match(self):
        self.broken(lambda c, a: a.update(catalog_version="2026.1.1"), "version_mismatch")


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = default_repository()

    def ids(self, result):
        return [p["product_id"] for p in result["products"]]

    def test_budget_search_respects_price_and_ranks_stably(self):
        first = self.repo.search(
            "retailer-a", "lightweight rain jacket", {"max_price_minor": 15000}
        )
        self.assertEqual(self.ids(first), ["a-drizzle-anorak", "a-downpour-packable", "jacket-a"])
        self.assertTrue(all(p["price_minor"] <= 15000 for p in first["products"]))
        self.assertEqual(first["applied_filters"]["max_price_display"], "$150.00")
        again = self.repo.search(
            "retailer-a", "lightweight rain jacket", {"max_price_minor": 15000}
        )
        self.assertEqual(first, again)
        cheaper = self.repo.search(
            "retailer-a", "lightweight rain jacket", {"max_price_minor": 10000}
        )
        self.assertEqual(self.ids(cheaper), ["a-drizzle-anorak", "a-downpour-packable"])

    def test_raincoat_ranks_rain_jackets_before_rain_hats_and_gloves(self):
        # "raincoat" used to map to "rain" alone, so the rain bucket hat and the gloves won on
        # price. The compound expands to both catalog words.
        for query in ("raincoat", "raincoats under 150", "rain coat"):
            found = self.repo.search("retailer-a", query, {"max_price_minor": 15000})
            self.assertEqual(
                self.ids(found), ["a-drizzle-anorak", "a-downpour-packable", "jacket-a"], query
            )

    def test_superlatives_match_the_base_word(self):
        warmest = self.repo.search("retailer-a", "warmest jacket")
        self.assertEqual(self.ids(warmest)[0], "a-summit-down-jacket")
        self.assertEqual(self.ids(warmest), self.ids(self.repo.search("retailer-a", "warm jacket")))
        self.assertEqual(
            self.ids(self.repo.search("retailer-a", "warmer jacket")), self.ids(warmest)
        )
        # Only base words this catalog uses; look-alikes keep their meaning.
        for word in ("winter", "water", "liner", "best"):
            self.assertEqual(self.repo.stem(word), word)
        self.assertEqual(self.repo.stem("warmest"), "warm")

    def test_explicit_filters(self):
        vests = self.repo.search("retailer-a", "", {"category": "vests"})
        self.assertEqual({p["category"] for p in vests["products"]}, {"vests"})
        navy_m = self.repo.search("retailer-a", "jacket", {"color": "navy", "size": "M"})
        for product in navy_m["products"]:
            self.assertIn(("navy", "M"), {(v["color"], v["size"]) for v in product["variants"]})
        self.assertEqual(self.repo.search("retailer-a", "", {"color": "purple"})["match_count"], 0)

    def test_product_kind_does_not_fall_back_to_other_kinds(self):
        result = self.repo.search("retailer-a", "rain jacket", {"max_price_minor": 5000})
        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["products"], [])

    def test_limit_is_capped_at_three(self):
        self.assertEqual(len(self.repo.search("retailer-a", "")["products"]), 3)
        self.assertEqual(len(self.repo.search("retailer-a", "", limit=1)["products"]), 1)
        for bad in (0, 4, True, "3"):
            with self.assertRaises(ValueError):
                self.repo.search("retailer-a", "", limit=bad)

    def test_invalid_filters_are_rejected(self):
        for filters in (
            {"tenant": "retailer-b"},
            {"category": "coats"},
            {"size": "medium"},
            {"max_price_minor": 150.0},
            {"max_price_minor": "15000"},
            {"color": "Navy!"},
        ):
            with self.assertRaises(ValueError, msg=filters):
                self.repo.search("retailer-a", "jacket", filters)
        with self.assertRaises(ValueError):
            self.repo.search("retailer-a", "x" * 201)

    def test_search_and_counts_are_tenant_scoped(self):
        b_ids = {p for p, r in self.repo.products.items() if r.tenant_id == "retailer-b"}
        everything = self.repo.search("retailer-a", "")
        self.assertEqual(everything["match_count"], 18)
        sailing = self.repo.search("retailer-a", "sailing jacket")
        self.assertFalse(set(self.ids(sailing)) & b_ids)
        rain = self.repo.search("retailer-a", "rain shell", limit=3)
        self.assertNotIn("b-squall-rain-shell", self.ids(rain))
        self.assertEqual(self.repo.search("retailer-b", "")["match_count"], 6)
        b_rain = self.ids(self.repo.search("retailer-b", "rain jacket"))
        self.assertEqual(b_rain[0], "b-squall-rain-shell")
        self.assertTrue(set(b_rain) <= b_ids)

    def test_unknown_and_foreign_ids_raise_the_same_denial(self):
        calls = [
            lambda pid: self.repo.details("retailer-a", pid),
            lambda pid: self.repo.card("retailer-a", pid),
            lambda pid: self.repo.availability("retailer-a", pid, pid + "-navy-m", "store-a"),
            lambda pid: self.repo.image("retailer-a", "img-" + pid + "-navy"),
        ]
        for call in calls:
            messages = []
            for pid in ("jacket-b", "no-such-product"):
                with self.assertRaises(Denied) as caught:
                    call(pid)
                messages.append(str(caught.exception))
            self.assertEqual(messages[0], messages[1])

    def test_add_to_cart_validates_like_a_stock_lookup(self):
        # the same ownership and variant checks as check_availability, at the shopper's
        # store. The line item carries the card's price; out of stock returns added false.
        line = execute_tool(
            "retailer-a",
            "add_to_cart",
            {"product_id": "a-downpour-packable", "variant_id": "a-downpour-packable-navy-m"},
            repository=self.repo,
        )
        self.assertEqual(
            tuple(
                line[k]
                for k in (
                    "added",
                    "product_name",
                    "color",
                    "size",
                    "price_minor",
                    "price_display",
                    "quantity",
                    "store_id",
                    "available",
                )
            ),
            (True, "Downpour Packable Shell", "navy", "M", 8900, "$89.00", 1, "store-a", 4),
        )
        none = execute_tool(
            "retailer-a",
            "add_to_cart",
            {"product_id": "a-stormline-pro-shell", "variant_id": "a-stormline-pro-shell-red-m"},
            repository=self.repo,
        )
        self.assertEqual((none["added"], none["available"]), (False, 0))
        # what the session cart already holds of this variant counts against the stock.
        held = [{"variant_id": "a-downpour-packable-navy-m", "quantity": 3}]
        args = {"product_id": "a-downpour-packable", "variant_id": "a-downpour-packable-navy-m"}
        one = execute_tool("retailer-a", "add_to_cart", args, repository=self.repo, cart=held)
        self.assertEqual((one["added"], one["in_cart"], one["available"]), (True, 3, 4))
        two = execute_tool(
            "retailer-a", "add_to_cart", args | {"quantity": 2}, repository=self.repo, cart=held
        )
        self.assertEqual((two["added"], two["in_cart"]), (False, 3))
        other = [{"variant_id": "a-downpour-packable-navy-l", "quantity": 4}]
        self.assertTrue(
            execute_tool("retailer-a", "add_to_cart", args, repository=self.repo, cart=other)[
                "added"
            ]
        )
        with self.assertRaises(Denied):
            execute_tool(
                "retailer-a",
                "add_to_cart",
                {"product_id": "jacket-b", "variant_id": "jacket-b-red-m"},
                repository=self.repo,
            )
        with self.assertRaises(ToolArgumentError):
            execute_tool(
                "retailer-a",
                "add_to_cart",
                {
                    "product_id": "a-downpour-packable",
                    "variant_id": "a-downpour-packable-navy-m",
                    "quantity": 0,
                },
                repository=self.repo,
            )

    def test_availability_scope(self):
        stock = self.repo.availability(
            "retailer-a", "a-downpour-packable", "a-downpour-packable-navy-m"
        )
        self.assertEqual(
            (stock["store_id"], stock["quantity"], stock["provenance"]), ("store-a", 4, "synthetic")
        )
        out = self.repo.availability(
            "retailer-a", "a-stormline-pro-shell", "a-stormline-pro-shell-red-m"
        )
        self.assertEqual((out["quantity"], out["in_stock"]), (0, False))
        for args in [
            ("jacket-a", "jacket-a-navy-m", "store-b"),  # foreign store
            ("jacket-a", "a-downpour-packable-navy-m", "store-a"),  # variant of another product
            ("jacket-a", "jacket-b-navy-m", "store-a"),  # foreign variant
        ]:
            with self.assertRaises(Denied):
                self.repo.availability("retailer-a", *args)

    def test_cards_use_catalog_facts_and_protected_image_paths(self):
        card = self.repo.card("retailer-a", "jacket-a")
        self.assertEqual((card["name"], card["price_display"]), ("Ridgeline Rain Shell", "$129.00"))
        self.assertTrue(card["image_path"].startswith("/v1/catalog/images/img-jacket-a-"))
        self.assertEqual(format_price(2900), "$29.00")
        self.assertEqual(format_price(1234567), "$12,345.67")


class LegacyToolTests(unittest.TestCase):
    def test_legacy_lookups_are_unchanged(self):
        self.assertEqual(lookup_inventory("retailer-a", "jacket-a", "store-a")["navy_medium"], 3)
        self.assertEqual(lookup_inventory("retailer-b", "jacket-b", "store-b")["navy_medium"], 7)
        self.assertEqual(lookup_inventory("retailer-a", "jacket-a", "store-a")["store"], "store-a")
        for tenant, sku, store in [
            ("retailer-a", "jacket-b", "store-b"),
            ("retailer-a", "jacket-a", "store-b"),
            ("retailer-b", "jacket-a", "store-a"),
            ("retailer-a", "a-trail-sun-cap", "store-a"),  # no navy medium variant
        ]:
            with self.assertRaises(Denied):
                execute_tool(tenant, "lookup_inventory", {"sku": sku, "store_id": store})


class CatalogAPITests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"DEMO_MODE": "mock"})
        self.env.start()
        self.app = create_app(KEYS, OPERATOR)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()

    def test_storefront_requires_tenant_credential(self):
        for headers in [{}, {"Authorization": "Bearer " + OPERATOR}, {"Authorization": "Basic x"}]:
            self.assertEqual(self.client.get("/v1/catalog", headers=headers).status_code, 401)

    def test_storefronts_are_disjoint_and_never_reserve_capacity(self):
        a = self.client.get("/v1/catalog", headers=A).json()
        b = self.client.get("/v1/catalog", headers=B).json()
        self.assertEqual(
            (a["retailer_name"], b["retailer_name"]), ("Northline Outfitters", "Harbor Supply")
        )
        a_ids = {p["product_id"] for p in a["products"]}
        b_ids = {p["product_id"] for p in b["products"]}
        self.assertEqual((len(a_ids), len(b_ids)), (18, 6))
        self.assertFalse(a_ids & b_ids)
        self.assertEqual({s["store_id"] for s in a["stores"]}, {"store-a", "store-a-riverside"})
        self.assertEqual(a["inventory_provenance"], "synthetic")
        self.assertEqual(self.app.state.admission.sessions, {})

    def test_product_detail_scope_and_identical_404(self):
        ok = self.client.get("/v1/catalog/products/jacket-a", headers=A)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["price_display"], "$129.00")
        foreign = self.client.get("/v1/catalog/products/jacket-b", headers=A)
        unknown = self.client.get("/v1/catalog/products/no-such-product", headers=A)
        self.assertEqual((foreign.status_code, unknown.status_code), (404, 404))
        self.assertEqual(foreign.content, unknown.content)
        self.assertEqual(
            self.client.get("/v1/catalog/products/jacket-b", headers=B).status_code, 200
        )

    def test_images_are_scoped_jpeg_and_not_cached(self):
        a = self.client.get("/v1/catalog", headers=A).json()["products"][0]
        own = self.client.get(a["image_path"], headers=A)
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.headers["content-type"], "image/jpeg")
        self.assertEqual(own.headers["cache-control"], "no-store")
        self.assertTrue(own.content.startswith(b"\xff\xd8\xff"))
        foreign = self.client.get(a["image_path"], headers=B)
        unknown = self.client.get("/v1/catalog/images/img-no-such-image", headers=B)
        self.assertEqual((foreign.status_code, unknown.status_code), (404, 404))
        self.assertEqual(foreign.content, unknown.content)
        self.assertEqual(self.client.get(a["image_path"]).status_code, 401)
        for path in ["/v1/catalog/images/..%2Fcatalog.json", "/v1/catalog/images/assets.json"]:
            self.assertIn(self.client.get(path, headers=A).status_code, {404})
