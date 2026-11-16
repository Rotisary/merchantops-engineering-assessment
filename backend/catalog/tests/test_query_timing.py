import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from catalog.models import (
    Category,
    Inventory,
    Merchant,
    PriceRule,
    Product,
    ProductVariant,
)

SERVER_TIMING = re.compile(
    r'^db;dur=\d+\.\d{2};desc="\d+ queries", app;dur=\d+\.\d{2}$'
)


class QueryTimingMiddlewareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="timing-user",
            password="test-password",
        )
        cls.merchant = Merchant.objects.create(
            name="Timing Merchant",
            slug="timing-merchant",
        )
        other_merchant = Merchant.objects.create(
            name="Other Timing Merchant",
            slug="other-timing-merchant",
        )
        cls.merchant.members.add(cls.user)

        category = Category.objects.create(merchant=cls.merchant, name="Tools")
        cls.product = Product.objects.create(
            merchant=cls.merchant,
            category=category,
            name="Drill",
            sku="DRILL-001",
        )
        cls.other_product = Product.objects.create(
            merchant=other_merchant,
            category=Category.objects.create(merchant=other_merchant, name="Tools"),
            name="Private Drill",
            sku="DRILL-002",
        )

        for number in (1, 2):
            variant = ProductVariant.objects.create(
                product=cls.product,
                name=f"Variant {number}",
                sku=f"DRILL-001-V{number}",
            )
            PriceRule.objects.create(
                variant=variant,
                tier="retail",
                amount=Decimal("10.00"),
            )
            Inventory.objects.create(variant=variant, quantity=5, reserved=0)

    def setUp(self):
        self.client.force_login(self.user)

    def get_editor(self, product):
        return self.client.get(
            reverse("product-editor", kwargs={"pk": product.pk}),
            {"merchant_id": self.merchant.pk},
        )

    def test_no_headers_when_debug_is_off(self):
        response = self.get_editor(self.product)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("X-DB-Query-Count", response.headers)
        self.assertNotIn("Server-Timing", response.headers)

    @override_settings(DEBUG=True)
    def test_headers_report_every_query_when_debug_is_on(self):
        with CaptureQueriesContext(connection) as captured:
            response = self.get_editor(self.product)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["X-DB-Query-Count"],
            str(len(captured.captured_queries)),
        )
        self.assertRegex(response.headers["Server-Timing"], SERVER_TIMING)

    @override_settings(DEBUG=True)
    def test_log_line_names_repeated_statement_without_query_values(self):
        with self.assertLogs("catalog.perf", "INFO") as logs:
            self.get_editor(self.product)

        self.assertEqual(len(logs.output), 1)
        line = logs.output[0]
        self.assertIn("queries=", line)
        self.assertIn(" top=", line)
        self.assertIn("catalog_pricerule", line)
        self.assertNotIn("merchant_id=", line)

    @override_settings(DEBUG=True)
    def test_not_found_response_keeps_status_and_gets_headers(self):
        response = self.get_editor(self.other_product)

        self.assertEqual(response.status_code, 404)
        self.assertIn("X-DB-Query-Count", response.headers)
        self.assertRegex(response.headers["Server-Timing"], SERVER_TIMING)
