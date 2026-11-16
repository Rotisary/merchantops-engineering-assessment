import logging
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.urls import reverse

from catalog.models import Merchant, Product

WARMUP_RUNS = 3
MEASURED_RUNS = 20
SEARCH_TERM = "ASSESS-0001"
MERCHANT_SLUG = "assessment-merchant-01"
LARGE_PRODUCT_SKU = "ASSESS-000001"
SMALL_PRODUCT_SKU = "ASSESS-000005"
DEFAULT_OUTPUT = "perf/baseline.md"

DB_DURATION = re.compile(r"db;dur=([0-9.]+)")
TABLE_HEADER = (
    "| Scenario | Requests | Queries | DB ms (median) | Wall ms (median) "
    "| Wall ms (p95) | Bytes |\n"
    "|---|---:|---:|---:|---:|---:|---:|"
)


class Replay:
    """One run of a scenario: sends its requests and totals their cost."""

    def __init__(self, client, merchant_id, label):
        self.client = client
        self.merchant_id = merchant_id
        self.label = label
        self.requests = 0
        self.queries = 0
        self.db_ms = 0.0
        self.bytes = 0

    def get(self, path, **params):
        response = self.client.get(path, {"merchant_id": self.merchant_id, **params})
        count = response.headers.get("X-DB-Query-Count")

        if response.status_code != 200 or count is None:
            raise CommandError(
                f"{self.label}: GET {path} returned {response.status_code} "
                "without a measurable response. Nothing was written."
            )

        self.requests += 1
        self.queries += int(count)
        self.db_ms += float(DB_DURATION.search(response.headers["Server-Timing"])[1])
        self.bytes += len(response.content)
        return response


def replay_catalogue(replay, search=""):
    listing = replay.get(reverse("product-list"), search=search, page=1).json()

    # The catalogue view loads each listed product's detail, one at a time.
    for item in listing["results"]:
        replay.get(reverse("product-detail", kwargs={"pk": item["id"]}))


def replay_editor(replay, product):
    replay.get(reverse("product-editor", kwargs={"pk": product.pk}))
    replay.get(reverse("product-detail", kwargs={"pk": product.pk}))


def replay_activity(replay, product):
    replay.get(reverse("product-activity", kwargs={"pk": product.pk}), page=1)


def load_fixtures():
    user = get_user_model().objects.filter(username="demo_operator").first()

    if user is None:
        raise CommandError("User demo_operator was not found. Run ./setup.sh first.")

    merchant = Merchant.objects.filter(slug=MERCHANT_SLUG, members=user).first()

    if merchant is None:
        raise CommandError(
            f"demo_operator is not a member of {MERCHANT_SLUG}. Run ./setup.sh first."
        )

    products = {}

    for sku in (LARGE_PRODUCT_SKU, SMALL_PRODUCT_SKU):
        products[sku] = Product.objects.filter(merchant=merchant, sku=sku).first()

        if products[sku] is None:
            raise CommandError(f"Product {sku} was not found. Run ./setup.sh first.")

    return user, merchant, products


def scenarios(products):
    large = products[LARGE_PRODUCT_SKU]
    small = products[SMALL_PRODUCT_SKU]

    def editor_label(product):
        return f"Editor {product.sku} ({product.variants.count()} variants)"

    return [
        ("Catalogue page 1", replay_catalogue),
        (
            f"Catalogue search `{SEARCH_TERM}`",
            lambda replay: replay_catalogue(replay, SEARCH_TERM),
        ),
        (editor_label(large), lambda replay: replay_editor(replay, large)),
        (editor_label(small), lambda replay: replay_editor(replay, small)),
        (f"Activity {large.sku}", lambda replay: replay_activity(replay, large)),
    ]


def measure(client, merchant_id, label, scenario):
    for _ in range(WARMUP_RUNS):
        scenario(Replay(client, merchant_id, label))

    runs = []

    for _ in range(MEASURED_RUNS):
        replay = Replay(client, merchant_id, label)
        started = time.perf_counter()
        scenario(replay)
        runs.append((replay, (time.perf_counter() - started) * 1000))

    walls = [wall for _, wall in runs]
    queries = sorted({replay.queries for replay, _ in runs})

    return {
        "label": label,
        "requests": runs[0][0].requests,
        "queries": (
            str(queries[0]) if len(queries) == 1 else f"{queries[0]}-{queries[-1]}"
        ),
        "db_ms": statistics.median(replay.db_ms for replay, _ in runs),
        "wall_ms": statistics.median(walls),
        "wall_p95_ms": statistics.quantiles(walls, n=20, method="inclusive")[-1],
        "bytes": runs[0][0].bytes,
    }


def render(output, merchant, rows):
    lines = [
        f"# Performance measurement: {Path(output).stem}",
        "",
        f"- Captured: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "- Command: `docker compose exec backend python manage.py "
        f"measure_performance --output {output}`",
        f"- Runs per scenario: {WARMUP_RUNS} warm-up, {MEASURED_RUNS} measured",
        f"- Dataset: {merchant.name} ({merchant.slug}), "
        f"{merchant.products.count()} products",
        "- Timings are in-process Django server time through the test client, "
        "with no network, proxy, or browser cost. Requests replay the frontend's "
        "sequence one after another. Query counts include session and auth "
        "lookups.",
        "",
        TABLE_HEADER,
    ]

    for row in rows:
        lines.append(
            f"| {row['label']} | {row['requests']} | {row['queries']} "
            f"| {row['db_ms']:.2f} | {row['wall_ms']:.2f} "
            f"| {row['wall_p95_ms']:.2f} | {row['bytes']} |"
        )

    return "\n".join(lines) + "\n"


class Command(BaseCommand):
    help = "Measure query counts and timings for the main product screens."

    def add_arguments(self, parser):
        parser.add_argument("--output", default=DEFAULT_OUTPUT)

    def handle(self, *args, **options):
        output = options["output"]

        if not settings.DEBUG:
            raise CommandError("DEBUG must be enabled to measure query timings.")

        if Path(output).exists():
            raise CommandError(already_exists(output))

        user, merchant, products = load_fixtures()
        rows = run_scenarios(user, merchant, products)
        report = render(output, merchant, rows)

        Path(output).parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(output, "x", encoding="utf-8") as handle:
                handle.write(report)
        except FileExistsError:
            raise CommandError(already_exists(output))

        self.stdout.write(report, ending="")


def run_scenarios(user, merchant, products):
    perf_logger = logging.getLogger("catalog.perf")
    previous_level = perf_logger.level
    perf_logger.setLevel(logging.WARNING)

    client = Client(HTTP_HOST="localhost")
    client.force_login(user)

    try:
        return [
            measure(client, merchant.pk, label, scenario)
            for label, scenario in scenarios(products)
        ]
    finally:
        client.logout()
        perf_logger.setLevel(previous_level)


def already_exists(output):
    return f"{output} already exists. Choose another --output or remove it first."
