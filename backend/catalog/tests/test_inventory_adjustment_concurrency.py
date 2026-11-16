import threading

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TransactionTestCase
from django.urls import reverse

from catalog.models import (
    Category,
    Inventory,
    InventoryAdjustment,
    Merchant,
    Product,
    ProductVariant,
)

THREAD_TIMEOUT = 30


class InventoryAdjustmentConcurrencyTests(TransactionTestCase):
    """Competing requests on real, separate MySQL connections."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="race-operator",
            password="test-password",
        )
        self.merchant = Merchant.objects.create(name="Race Co", slug="race-co")
        self.merchant.members.add(self.user)
        category = Category.objects.create(merchant=self.merchant, name="Race")
        self.product = Product.objects.create(
            merchant=self.merchant,
            category=category,
            name="Contended Item",
            sku="RACE-001",
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            name="Default",
            sku="RACE-001-V1",
        )
        self.url = (
            reverse(
                "variant-inventory-adjustments",
                kwargs={"pk": self.product.pk, "variant_pk": self.variant.pk},
            )
            + f"?merchant_id={self.merchant.pk}"
        )

    def stock(self, quantity, reserved=0):
        return Inventory.objects.create(
            variant=self.variant,
            quantity=quantity,
            reserved=reserved,
        )

    def run_concurrently(self, body, count, key=None):
        """Send `count` identical requests at once; return (status, replayed)."""

        barrier = threading.Barrier(count)
        results = [None] * count
        errors = []
        headers = {"Idempotency-Key": key} if key else {}

        def worker(index):
            try:
                client = Client()
                client.force_login(self.user)
                barrier.wait(timeout=THREAD_TIMEOUT)
                response = client.post(
                    self.url,
                    body,
                    content_type="application/json",
                    headers=headers,
                )
                results[index] = (
                    response.status_code,
                    response.headers.get("Idempotent-Replayed") == "true",
                )
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join(timeout=THREAD_TIMEOUT)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        return results

    def test_concurrent_increments_are_all_applied(self):
        inventory = self.stock(quantity=5)

        results = self.run_concurrently({"delta": 1, "reason": "Recount"}, 10)

        self.assertEqual([status for status, _ in results], [201] * 10)
        inventory.refresh_from_db()
        self.assertEqual(inventory.quantity, 15)
        adjustments = InventoryAdjustment.objects.order_by("quantity_before")
        self.assertEqual(
            [adjustment.quantity_before for adjustment in adjustments],
            list(range(5, 15)),
        )
        for adjustment in adjustments:
            self.assertEqual(adjustment.quantity_after, adjustment.quantity_before + 1)

    def test_concurrent_decrements_never_breach_reserved(self):
        inventory = self.stock(quantity=10, reserved=4)

        results = self.run_concurrently({"delta": -2, "reason": "Damaged"}, 5)

        self.assertEqual(
            sorted(status for status, _ in results), [201, 201, 201, 409, 409]
        )
        inventory.refresh_from_db()
        self.assertEqual(inventory.quantity, 4)
        self.assertEqual(InventoryAdjustment.objects.count(), 3)

    def test_concurrent_retries_with_one_key_apply_once(self):
        inventory = self.stock(quantity=10)

        results = self.run_concurrently(
            {"delta": 3, "reason": "Delivery"}, 5, key="double-submit"
        )

        self.assertEqual(
            sorted(results),
            [(200, True), (200, True), (200, True), (200, True), (201, False)],
        )
        inventory.refresh_from_db()
        self.assertEqual(inventory.quantity, 13)
        self.assertEqual(InventoryAdjustment.objects.count(), 1)
