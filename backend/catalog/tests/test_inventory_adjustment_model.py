from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from catalog.models import (
    Category,
    Inventory,
    InventoryAdjustment,
    Merchant,
    Product,
    ProductVariant,
)


class InventoryAdjustmentModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="ledger-operator",
            password="test-password",
        )
        cls.other_user = get_user_model().objects.create_user(
            username="ledger-other",
            password="test-password",
        )
        merchant = Merchant.objects.create(name="Ledger", slug="ledger")
        cls.category = Category.objects.create(merchant=merchant, name="Stock")
        cls.product = Product.objects.create(
            merchant=merchant,
            category=cls.category,
            name="Counted Item",
            sku="LEDGER-001",
        )
        cls.variant = ProductVariant.objects.create(
            product=cls.product,
            name="Default",
            sku="LEDGER-001-V1",
        )
        Inventory.objects.create(variant=cls.variant, quantity=10, reserved=0)

    def adjustment(self, **overrides):
        values = {
            "variant": self.variant,
            "actor": self.user,
            "delta": 2,
            "quantity_before": 10,
            "quantity_after": 12,
            "reason": "Cycle count",
        }
        values.update(overrides)
        return InventoryAdjustment(**values)

    def assert_rejected(self, adjustment):
        with self.assertRaises(IntegrityError), transaction.atomic():
            adjustment.save()

    def test_consistent_adjustment_is_saved(self):
        self.adjustment().save()

        self.assertEqual(InventoryAdjustment.objects.count(), 1)

    def test_zero_delta_is_rejected(self):
        self.assert_rejected(
            self.adjustment(delta=0, quantity_before=10, quantity_after=10)
        )

    def test_inconsistent_quantities_are_rejected(self):
        self.assert_rejected(
            self.adjustment(delta=2, quantity_before=5, quantity_after=10)
        )

    def test_idempotency_key_is_unique_per_actor(self):
        self.adjustment(idempotency_key="key-1").save()

        self.assert_rejected(self.adjustment(idempotency_key="key-1"))

    def test_missing_keys_and_other_actors_do_not_collide(self):
        self.adjustment().save()
        self.adjustment().save()
        self.adjustment(idempotency_key="shared").save()
        self.adjustment(actor=self.other_user, idempotency_key="shared").save()

        self.assertEqual(InventoryAdjustment.objects.count(), 4)

    def test_variant_with_adjustments_cannot_be_deleted(self):
        self.adjustment().save()

        with self.assertRaises(ProtectedError):
            self.variant.delete()

    def test_product_with_adjustments_cannot_be_deleted(self):
        self.adjustment().save()

        with self.assertRaises(ProtectedError):
            self.product.delete()

        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())

    def test_actor_with_adjustments_cannot_be_deleted(self):
        self.adjustment().save()

        with self.assertRaises(ProtectedError):
            self.user.delete()

    def test_product_without_adjustments_still_deletes(self):
        self.product.delete()

        self.assertFalse(Product.objects.filter(pk=self.product.pk).exists())
