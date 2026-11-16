import re
from collections import Counter
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from catalog.middleware import SQL_PREVIEW_LENGTH, _log_line
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
    def test_log_line_has_no_query_values_or_repeats_for_editor(self):
        with self.assertLogs("catalog.perf", "INFO") as logs:
            self.get_editor(self.product)

        self.assertEqual(len(logs.output), 1)
        line = logs.output[0]
        self.assertIn("queries=", line)
        # The editor loads prices in one query, so no statement repeats.
        self.assertNotIn(" top=", line)
        self.assertNotIn("merchant_id=", line)

    def test_log_line_names_most_repeated_statement(self):
        request = RequestFactory().get("/api/products/", {"merchant_id": 7})
        statements = Counter(
            {
                "SELECT  `catalog_pricerule`.`id`\n  FROM `catalog_pricerule`": 3,
                "SELECT 1": 1,
            }
        )

        line = _log_line(request, HttpResponse(), 4, 1.5, 9.25, statements)

        self.assertEqual(
            line,
            "GET /api/products/ 200 queries=4 db=1.50ms app=9.25ms "
            'top=3x "SELECT `catalog_pricerule`.`id` FROM `catalog_pricerule`"',
        )

    def test_log_line_truncates_long_statements(self):
        request = RequestFactory().get("/api/products/")
        long_sql = "SELECT " + "x" * 300

        line = _log_line(request, HttpResponse(), 2, 1.0, 2.0, Counter({long_sql: 2}))

        self.assertIn(f'top=2x "{long_sql[:SQL_PREVIEW_LENGTH]}"', line)

    @override_settings(DEBUG=True)
    def test_not_found_response_keeps_status_and_gets_headers(self):
        response = self.get_editor(self.other_product)

        self.assertEqual(response.status_code, 404)
        self.assertIn("X-DB-Query-Count", response.headers)
        self.assertRegex(response.headers["Server-Timing"], SERVER_TIMING)
