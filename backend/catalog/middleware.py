import logging
import time
from collections import Counter

from django.conf import settings
from django.db import connection

logger = logging.getLogger("catalog.perf")

SQL_PREVIEW_LENGTH = 160


class QueryTimingMiddleware:
    """Report per-request SQL query count and timing while DEBUG is on."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.DEBUG:
            return self.get_response(request)

        statements = Counter()
        db_seconds = 0.0

        def record_query(execute, sql, params, many, context):
            nonlocal db_seconds
            started = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                db_seconds += time.perf_counter() - started
                statements[sql] += 1

        started = time.perf_counter()
        with connection.execute_wrapper(record_query):
            response = self.get_response(request)
        app_ms = (time.perf_counter() - started) * 1000
        db_ms = db_seconds * 1000
        count = sum(statements.values())

        response["X-DB-Query-Count"] = str(count)
        response["Server-Timing"] = (
            f'db;dur={db_ms:.2f};desc="{count} queries", app;dur={app_ms:.2f}'
        )
        logger.info(_log_line(request, response, count, db_ms, app_ms, statements))
        return response


def _log_line(request, response, count, db_ms, app_ms, statements):
    line = (
        f"{request.method} {request.path} {response.status_code} "
        f"queries={count} db={db_ms:.2f}ms app={app_ms:.2f}ms"
    )
    if statements:
        sql, repeats = statements.most_common(1)[0]
        if repeats > 1:
            preview = " ".join(sql.split())[:SQL_PREVIEW_LENGTH]
            line += f' top={repeats}x "{preview}"'
    return line
