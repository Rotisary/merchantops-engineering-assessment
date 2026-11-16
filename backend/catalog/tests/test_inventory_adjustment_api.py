from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from catalog import util
from catalog.models import (
    Category,
    Inventory,
    InventoryAdjustment,
    Merchant,
    Product,
    ProductVariant,
)

MAX_INTEGER = 2147483647


class InventoryAdjustmentAPITestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="stock-operator", password="pw")
        cls.colleague = User.objects.create_user(
            username="stock-colleague", password="pw"
        )
        cls.outsider = User.objects.create_user(
            username="stock-outsider", password="pw"
        )

        cls.merchant = Merchant.objects.create(name="Stock Co", slug="stock-co")
        cls.other_merchant = Merchant.objects.create(name="Other Co", slug="other-co")
        cls.merchant.members.add(cls.user, cls.colleague)
        cls.other_merchant.members.add(cls.outsider)

        cls.product, cls.variant = cls.make_product(cls.merchant, "STOCK-001")
        cls.inventory = Inventory.objects.create(
            variant=cls.variant,
            quantity=20,
            reserved=3,
        )
        cls.bare_variant = ProductVariant.objects.create(
            product=cls.product,
            name="No stock record",
            sku="STOCK-001-BARE",
        )
        cls.sibling_product, cls.sibling_variant = cls.make_product(
            cls.merchant, "STOCK-002"
        )
        Inventory.objects.create(variant=cls.sibling_variant, quantity=8)
        cls.foreign_product, cls.foreign_variant = cls.make_product(
            cls.other_merchant, "FOREIGN-001"
        )
        Inventory.objects.create(variant=cls.foreign_variant, quantity=50)

    @staticmethod
    def make_product(merchant, sku):
        category, _ = Category.objects.get_or_create(merchant=merchant, name="Stock")
        product = Product.objects.create(
            merchant=merchant,
            category=category,
            name=f"Product {sku}",
            sku=sku,
        )
        variant = ProductVariant.objects.create(
            product=product,
            name="Default",
            sku=f"{sku}-V1",
        )
        return product, variant

    def setUp(self):
        self.client.force_login(self.user)

    def post(
        self,
        body,
        product=None,
        variant=None,
        merchant_id=None,
        key=None,
        client=None,
    ):
        product = product or self.product
        variant = variant or self.variant
        url = reverse(
            "variant-inventory-adjustments",
            kwargs={"pk": product.pk, "variant_pk": variant.pk},
        )
        merchant_id = self.merchant.pk if merchant_id is None else merchant_id
        if merchant_id != "":
            url += f"?merchant_id={merchant_id}"
        headers = {"Idempotency-Key": key} if key is not None else {}
        return (client or self.client).post(
            url,
            body,
            content_type="application/json",
            headers=headers,
        )

    def assert_nothing_written(self, quantity=20):
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, quantity)
        self.assertEqual(InventoryAdjustment.objects.count(), 0)


class InventoryAdjustmentAPITests(InventoryAdjustmentAPITestBase):
    def test_increase_records_adjustment(self):
        response = self.post({"delta": 5, "reason": "  Delivery recount  "})

        self.assertEqual(response.status_code, 201)
        adjustment = InventoryAdjustment.objects.get()
        self.assertEqual(
            response.data["adjustment"],
            {
                "id": adjustment.pk,
                "variant_id": self.variant.pk,
                "delta": 5,
                "quantity_before": 20,
                "quantity_after": 25,
                "reason": "Delivery recount",
                "actor": "stock-operator",
                "created_at": response.data["adjustment"]["created_at"],
            },
        )
        self.assertEqual(
            response.data["inventory"],
            {"id": self.inventory.pk, "quantity": 25, "reserved": 3},
        )
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 25)
        self.assertEqual(adjustment.actor, self.user)
        self.assertEqual(adjustment.variant, self.variant)
        self.assertEqual(adjustment.reason, "Delivery recount")
        self.assertIsNotNone(adjustment.created_at)
        self.assertIsNone(adjustment.idempotency_key)

    def test_decrease_down_to_reserved_is_allowed(self):
        first = self.post({"delta": -3, "reason": "Damaged"})
        second = self.post({"delta": -14, "reason": "Count correction"})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.data["adjustment"]["quantity_after"], 17)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.data["inventory"]["quantity"], 3)
        self.assertEqual(InventoryAdjustment.objects.count(), 2)

    def test_invalid_body_is_rejected_without_writes(self):
        cases = [
            ({"reason": "No amount"}, "delta"),
            ({"delta": 0, "reason": "Zero"}, "delta"),
            ({"delta": "1.5", "reason": "Fraction"}, "delta"),
            ({"delta": 1.5, "reason": "Fraction"}, "delta"),
            ({"delta": True, "reason": "Boolean"}, "delta"),
            ({"delta": "abc", "reason": "Text"}, "delta"),
            ({"delta": MAX_INTEGER + 1, "reason": "Too big"}, "delta"),
            ({"delta": -MAX_INTEGER - 2, "reason": "Too small"}, "delta"),
            ({"delta": 1}, "reason"),
            ({"delta": 1, "reason": ""}, "reason"),
            ({"delta": 1, "reason": "   "}, "reason"),
            ({"delta": 1, "reason": "x" * 256}, "reason"),
            ({"delta": 1, "reason": "Extra", "quantity": 99}, "quantity"),
        ]

        for body, field in cases:
            with self.subTest(body=body):
                response = self.post(body)

                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
                self.assert_nothing_written()

    def test_invalid_merchant_id_is_rejected(self):
        for merchant_id in ("", "abc", "0"):
            with self.subTest(merchant_id=merchant_id):
                response = self.post(
                    {"delta": 1, "reason": "Scope"}, merchant_id=merchant_id
                )

                self.assertEqual(response.status_code, 400)
                self.assertIn("merchant_id", response.data)
                self.assert_nothing_written()

    def test_falling_below_reserved_is_a_conflict(self):
        response = self.post({"delta": -18, "reason": "Too many"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["delta"],
            "Stock cannot fall below the 3 units already reserved.",
        )
        self.assert_nothing_written()

    def test_exceeding_maximum_stock_is_a_conflict(self):
        Inventory.objects.filter(pk=self.inventory.pk).update(quantity=MAX_INTEGER - 1)

        response = self.post({"delta": 2, "reason": "Overflow"})

        self.assertEqual(response.status_code, 409)
        self.assertIn("delta", response.data)
        self.assert_nothing_written(quantity=MAX_INTEGER - 1)

    def test_variant_without_inventory_is_a_conflict(self):
        response = self.post(
            {"delta": 1, "reason": "No record"}, variant=self.bare_variant
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data["variant"], "This variant has no inventory record."
        )
        self.assertFalse(Inventory.objects.filter(variant=self.bare_variant).exists())
        self.assert_nothing_written()

    def test_out_of_scope_targets_are_not_found(self):
        cases = {
            "other merchant's product": {
                "product": self.foreign_product,
                "variant": self.foreign_variant,
            },
            "variant of another product": {"variant": self.sibling_variant},
            "non-member merchant": {
                "product": self.foreign_product,
                "variant": self.foreign_variant,
                "merchant_id": self.other_merchant.pk,
            },
        }

        for label, target in cases.items():
            with self.subTest(label):
                response = self.post({"delta": 1, "reason": "Scope"}, **target)

                self.assertEqual(response.status_code, 404)

        self.assertEqual(
            Inventory.objects.get(variant=self.foreign_variant).quantity, 50
        )
        self.assertEqual(
            Inventory.objects.get(variant=self.sibling_variant).quantity, 8
        )
        self.assert_nothing_written()

    def test_unauthenticated_request_is_rejected(self):
        self.client.logout()

        response = self.post({"delta": 1, "reason": "Anonymous"})

        self.assertIn(response.status_code, (401, 403))
        self.assert_nothing_written()

    def test_request_without_csrf_token_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = self.post({"delta": 1, "reason": "No token"}, client=client)

        self.assertEqual(response.status_code, 403)
        self.assert_nothing_written()

    def test_failed_audit_write_rolls_back_stock_change(self):
        with mock.patch(
            "catalog.util.InventoryAdjustment.objects.create",
            side_effect=RuntimeError("audit storage unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.post({"delta": 5, "reason": "Should roll back"})

        self.assert_nothing_written()


class InventoryAdjustmentIdempotencyTests(InventoryAdjustmentAPITestBase):
    body = {"delta": 4, "reason": "Recount"}

    def test_repeated_key_replays_without_second_change(self):
        first = self.post(self.body, key="retry-1")
        second = self.post(self.body, key="retry-1")

        self.assertEqual(first.status_code, 201)
        self.assertNotIn("Idempotent-Replayed", first.headers)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.headers["Idempotent-Replayed"], "true")
        self.assertEqual(
            second.data["adjustment"]["id"], first.data["adjustment"]["id"]
        )
        self.assertEqual(second.data["inventory"]["quantity"], 24)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 24)
        adjustment = InventoryAdjustment.objects.get()
        self.assertEqual(adjustment.idempotency_key, "retry-1")

    def test_reused_key_with_different_request_is_a_conflict(self):
        self.post(self.body, key="retry-2")

        for label, overrides in {
            "delta": {"body": {"delta": 5, "reason": "Recount"}},
            "reason": {"body": {"delta": 4, "reason": "Other reason"}},
            "variant": {
                "body": self.body,
                "product": self.sibling_product,
                "variant": self.sibling_variant,
            },
        }.items():
            with self.subTest(label):
                body = overrides.pop("body")
                response = self.post(body, key="retry-2", **overrides)

                self.assertEqual(response.status_code, 409)
                self.assertIn("idempotency_key", response.data)

        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 24)
        self.assertEqual(
            Inventory.objects.get(variant=self.sibling_variant).quantity, 8
        )
        self.assertEqual(InventoryAdjustment.objects.count(), 1)

    def test_keys_are_scoped_to_the_operator(self):
        colleague = Client()
        colleague.force_login(self.colleague)

        mine = self.post(self.body, key="shared-key")
        theirs = self.post(self.body, key="shared-key", client=colleague)

        self.assertEqual(mine.status_code, 201)
        self.assertEqual(theirs.status_code, 201)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 28)
        self.assertEqual(InventoryAdjustment.objects.count(), 2)

    def test_invalid_key_is_rejected(self):
        for key in ("", "x" * 256, "has space", "café"):
            with self.subTest(key=key):
                response = self.post(self.body, key=key)

                self.assertEqual(response.status_code, 400)
                self.assertIn("idempotency_key", response.data)
                self.assert_nothing_written()

    def test_key_from_failed_attempt_can_be_retried(self):
        failed = self.post({"delta": -18, "reason": "Recount"}, key="retry-3")
        retried = self.post({"delta": -17, "reason": "Recount"}, key="retry-3")

        self.assertEqual(failed.status_code, 409)
        self.assertEqual(retried.status_code, 201)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 3)

    def simulate_concurrent_commit(self):
        """Hide a committed key from the locked lookup, as a race would."""

        real_lookup = util.previous_adjustment
        calls = []

        def lookup(*args):
            calls.append(args)
            return None if len(calls) == 1 else real_lookup(*args)

        return mock.patch.object(util, "previous_adjustment", side_effect=lookup)

    def test_unique_key_race_resolves_to_replay(self):
        existing = InventoryAdjustment.objects.create(
            variant=self.variant,
            actor=self.user,
            delta=4,
            quantity_before=16,
            quantity_after=20,
            reason="Recount",
            idempotency_key="race-1",
        )

        with self.simulate_concurrent_commit():
            response = self.post(self.body, key="race-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Idempotent-Replayed"], "true")
        self.assertEqual(response.data["adjustment"]["id"], existing.pk)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 20)
        self.assertEqual(InventoryAdjustment.objects.count(), 1)

    def test_unique_key_race_for_other_variant_is_a_conflict(self):
        existing = InventoryAdjustment.objects.create(
            variant=self.sibling_variant,
            actor=self.user,
            delta=4,
            quantity_before=4,
            quantity_after=8,
            reason="Recount",
            idempotency_key="race-2",
        )

        with self.simulate_concurrent_commit():
            response = self.post(self.body, key="race-2")

        self.assertEqual(response.status_code, 409)
        self.assertIn("idempotency_key", response.data)
        self.assert_nothing_written_except(existing)

    def assert_nothing_written_except(self, existing):
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 20)
        self.assertEqual(list(InventoryAdjustment.objects.all()), [existing])
