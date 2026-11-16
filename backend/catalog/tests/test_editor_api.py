from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import (
    Availability,
    Category,
    Inventory,
    Merchant,
    PriceRule,
    Product,
    ProductVariant,
)

# Session, user, membership, product with category, variants with inventory,
# prices, and availability.
EDITOR_QUERIES = 7


class ProductEditorAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="editor-user",
            password="test-password",
        )

        cls.merchant = Merchant.objects.create(
            name="Editor Merchant",
            slug="editor-merchant",
        )
        cls.other_merchant = Merchant.objects.create(
            name="Other Merchant",
            slug="other-editor-merchant",
        )
        cls.merchant.members.add(cls.user)

        category = Category.objects.create(
            merchant=cls.merchant,
            name="Electronics",
        )
        other_category = Category.objects.create(
            merchant=cls.other_merchant,
            name="Electronics",
        )

        cls.product = Product.objects.create(
            merchant=cls.merchant,
            category=category,
            name="Workstation",
            sku="WORK-001",
            description="Development workstation",
        )

        cls.other_product = Product.objects.create(
            merchant=cls.other_merchant,
            category=other_category,
            name="Private Product",
            sku="PRIVATE-001",
        )

        variant = ProductVariant.objects.create(
            product=cls.product,
            name="16GB RAM",
            sku="WORK-001-16",
        )
        PriceRule.objects.create(
            variant=variant,
            tier="retail",
            amount=Decimal("1299.00"),
        )
        Inventory.objects.create(
            variant=variant,
            quantity=20,
            reserved=3,
        )
        Availability.objects.create(
            product=cls.product,
            channel="online",
            enabled=True,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def editor_url(self, product):
        return reverse(
            "product-editor",
            kwargs={"pk": product.pk},
        )

    def test_editor_returns_related_data(self):
        response = self.client.get(
            self.editor_url(self.product),
            {"merchant_id": self.merchant.pk},
        )

        self.assertEqual(response.status_code, 200)

        data = response.data

        self.assertEqual(data["id"], self.product.pk)
        self.assertEqual(data["name"], "Workstation")
        self.assertEqual(
            data["description"],
            "Development workstation",
        )
        self.assertEqual(data["category"]["name"], "Electronics")

        self.assertEqual(len(data["variants"]), 1)
        variant = data["variants"][0]

        self.assertEqual(variant["sku"], "WORK-001-16")
        self.assertEqual(
            variant["prices"][0]["amount"],
            "1299.00",
        )
        self.assertEqual(variant["inventory"]["quantity"], 20)
        self.assertEqual(variant["inventory"]["reserved"], 3)

        self.assertEqual(len(data["availability"]), 1)
        self.assertEqual(
            data["availability"][0]["channel"],
            "online",
        )

    def test_other_merchant_product_is_inaccessible(self):
        response = self.client.get(
            self.editor_url(self.other_product),
            {"merchant_id": self.merchant.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_nonmember_merchant_is_inaccessible(self):
        response = self.client.get(
            self.editor_url(self.other_product),
            {"merchant_id": self.other_merchant.pk},
        )
        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_user_is_rejected(self):
        self.client.logout()

        response = self.client.get(
            self.editor_url(self.product),
            {"merchant_id": self.merchant.pk},
        )
        self.assertIn(response.status_code, (401, 403))

    def test_missing_merchant_context_is_rejected(self):
        response = self.client.get(
            self.editor_url(self.product),
        )
        self.assertEqual(response.status_code, 400)

    def create_product(self, sku, variant_count):
        product = Product.objects.create(
            merchant=self.merchant,
            category=self.product.category,
            name=f"Counted {sku}",
            sku=sku,
            image_url="/product-images/camera.webp",
        )

        for number in range(variant_count):
            variant = ProductVariant.objects.create(
                product=product,
                name=f"Variant {number}",
                sku=f"{sku}-V{number}",
            )
            PriceRule.objects.create(
                variant=variant, tier="retail", amount=Decimal("10.00")
            )
            PriceRule.objects.create(
                variant=variant, tier="wholesale", amount=Decimal("8.00")
            )
            Inventory.objects.create(variant=variant, quantity=10, reserved=1)

        return product

    def test_editor_query_count_does_not_grow_with_variants(self):
        for product in (
            self.create_product("ONE-001", 1),
            self.create_product("THREE-001", 3),
        ):
            with self.subTest(sku=product.sku):
                with self.assertNumQueries(EDITOR_QUERIES):
                    response = self.client.get(
                        self.editor_url(product),
                        {"merchant_id": self.merchant.pk},
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    len(response.data["variants"]), product.variants.count()
                )

    def test_variant_without_inventory_reports_null(self):
        product = self.create_product("MIXED-001", 2)
        first, second = product.variants.order_by("id")
        Inventory.objects.filter(variant=second).delete()

        response = self.client.get(
            self.editor_url(product),
            {"merchant_id": self.merchant.pk},
        )

        self.assertEqual(response.status_code, 200)
        variants = {variant["id"]: variant for variant in response.data["variants"]}
        self.assertEqual(variants[first.pk]["inventory"]["quantity"], 10)
        self.assertIsNone(variants[second.pk]["inventory"])

    def test_prices_are_returned_in_id_order(self):
        product = self.create_product("ORDER-001", 0)
        variant = ProductVariant.objects.create(
            product=product, name="Only", sku="ORDER-001-V1"
        )
        created = [
            PriceRule.objects.create(variant=variant, tier=tier, amount=Decimal("1"))
            for tier in ("wholesale", "retail", "member")
        ]

        response = self.client.get(
            self.editor_url(product),
            {"merchant_id": self.merchant.pk},
        )

        prices = response.data["variants"][0]["prices"]
        self.assertEqual([price["id"] for price in prices], [p.pk for p in created])
        self.assertEqual(
            [price["tier"] for price in prices], ["wholesale", "retail", "member"]
        )
        self.assertEqual(prices[0]["amount"], "1.00")

    def test_editor_includes_image_url(self):
        product = self.create_product("IMAGE-001", 1)

        response = self.client.get(
            self.editor_url(product),
            {"merchant_id": self.merchant.pk},
        )

        self.assertEqual(response.data["image_url"], "/product-images/camera.webp")
