# Performance measurement: baseline

- Captured: 2026-11-16T16:27:17+00:00
- Command: `docker compose exec backend python manage.py measure_performance --output perf/baseline.md`
- Runs per scenario: 3 warm-up, 20 measured
- Dataset: Northstar Retail (assessment-merchant-01), 2500 products
- Timings are in-process Django server time through the test client, with no network, proxy, or browser cost. Requests replay the frontend's sequence one after another. Query counts include session and auth lookups.

| Scenario | Requests | Queries | DB ms (median) | Wall ms (median) | Wall ms (p95) | Bytes |
|---|---:|---:|---:|---:|---:|---:|
| Catalogue page 1 | 26 | 130 | 129.18 | 521.62 | 856.43 | 9607 |
| Catalogue search `ASSESS-0001` | 26 | 130 | 131.56 | 474.72 | 760.93 | 9680 |
| Editor ASSESS-000001 (50 variants) | 2 | 110 | 95.86 | 300.61 | 595.69 | 11458 |
| Editor ASSESS-000005 (2 variants) | 2 | 14 | 12.71 | 44.74 | 61.62 | 970 |
| Activity ASSESS-000001 | 1 | 5 | 7.39 | 38.90 | 52.25 | 52 |
