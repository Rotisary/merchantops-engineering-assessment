import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from catalog.models import Merchant

COMMAND_MODULE = "catalog.management.commands.measure_performance"

SCENARIO_LABELS = [
    "| Catalogue page 1 |",
    "| Catalogue search `ASSESS-0001` |",
    "| Editor ASSESS-000001 (50 variants) |",
    "| Editor ASSESS-000005 (2 variants) |",
    "| Activity ASSESS-000001 |",
]


@override_settings(DEBUG=True, ALLOWED_HOSTS=["localhost"])
@mock.patch(f"{COMMAND_MODULE}.WARMUP_RUNS", 1)
@mock.patch(f"{COMMAND_MODULE}.MEASURED_RUNS", 2)
class MeasurePerformanceCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_catalog", products=6, seed=42, verbosity=0)
        call_command("seed_complex_products", verbosity=0)

        cls.user = get_user_model().objects.create_user(
            username="demo_operator",
            password="test-password",
        )
        Merchant.objects.get(slug="assessment-merchant-01").members.add(cls.user)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "perf" / "baseline.md"

    def run_command(self):
        stdout = StringIO()
        call_command("measure_performance", output=str(self.output), stdout=stdout)
        return stdout.getvalue()

    def test_writes_and_prints_the_same_report(self):
        printed = self.run_command()

        report = self.output.read_text(encoding="utf-8")
        self.assertEqual(printed, report)
        self.assertIn("# Performance measurement: baseline", report)
        self.assertIn("| Scenario | Requests | Queries |", report)

        for label in SCENARIO_LABELS:
            self.assertIn(label, report)

    def test_refuses_to_overwrite_an_existing_report(self):
        self.run_command()
        original = self.output.read_bytes()

        with self.assertRaisesMessage(CommandError, "already exists"):
            self.run_command()

        self.assertEqual(self.output.read_bytes(), original)

    def test_missing_demo_operator_writes_nothing(self):
        self.user.delete()

        with self.assertRaisesMessage(CommandError, "demo_operator was not found"):
            self.run_command()

        self.assertFalse(self.output.exists())

    @override_settings(DEBUG=False)
    def test_debug_off_writes_nothing(self):
        with self.assertRaisesMessage(CommandError, "DEBUG must be enabled"):
            self.run_command()

        self.assertFalse(self.output.exists())
