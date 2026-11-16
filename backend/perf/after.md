# Performance measurement: after

- Captured: 2026-11-16T21:54:30+00:00
- Command: `docker compose exec backend python manage.py measure_performance --output perf/after.md`
- Runs per scenario: 3 warm-up, 20 measured
- Dataset: Northstar Retail (assessment-merchant-01), 2500 products
- Timings are in-process Django server time through the test client, with no network, proxy, or browser cost. Requests replay the frontend's sequence one after another. Query counts include session and auth lookups.

| Scenario | Requests | Queries | DB ms (median) | Wall ms (median) | Wall ms (p95) | Bytes |
|---|---:|---:|---:|---:|---:|---:|
| Catalogue page 1 | 1 | 5 | 6.39 | 20.82 | 36.55 | 3934 |
| Catalogue search `ASSESS-0001` | 1 | 5 | 16.92 | 31.43 | 39.83 | 3975 |
| Editor ASSESS-000001 (50 variants) | 1 | 7 | 8.91 | 33.70 | 42.73 | 11275 |
| Editor ASSESS-000005 (2 variants) | 1 | 7 | 6.66 | 21.35 | 25.66 | 788 |
| Activity ASSESS-000001 | 1 | 5 | 4.61 | 16.99 | 20.68 | 52 |
