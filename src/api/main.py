import logging
import time

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.api.messages import router as messages_router
from src.api.campaigns import router as campaigns_router
from src.api.domain import router as domain_router
from src.api.exports import router as exports_router
from src.api.metrics import collect_operations_metrics, router as delivery_metrics_router
from src.api.recipients import router as recipients_router
from src.api.templates import router as templates_router
from src.api.unsubscribe import router as unsubscribe_router
from src.api.webhooks import router as webhooks_router
from src.config import get_settings
from src.db.session import get_session_factory
from src.logging_config import configure_logging
from src.queue.rate_limit import redis_client

configure_logging(get_settings().log_level)

logger = logging.getLogger("mailflow.http")
http_requests = Counter(
    "mailflow_http_requests_total",
    "Total HTTP requests handled by MailFlow.",
    ("method", "route", "status_code"),
)
http_request_duration = Histogram(
    "mailflow_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route"),
)
operation_metrics = {
    name: Gauge(
        f"mailflow_{name}",
        f"Current MailFlow operational metric: {name}.",
    )
    for name in (
        "oldest_pending_age_seconds",
        "pending_messages",
        "retrying_messages",
        "processing_messages",
        "unknown_delivery_messages",
        "dead_letters",
        "provider_failures_last_hour",
    )
}
dependency_readiness = Gauge(
    "mailflow_dependency_ready",
    "Whether a required MailFlow dependency passed the most recent readiness check.",
    ("dependency",),
)

app = FastAPI(title="MailFlow API", version="0.1.0")
app.include_router(messages_router)
app.include_router(recipients_router)
app.include_router(campaigns_router)
app.include_router(templates_router)
app.include_router(webhooks_router)
app.include_router(unsubscribe_router)
app.include_router(domain_router)
app.include_router(delivery_metrics_router)
app.include_router(exports_router)


@app.middleware("http")
async def record_http_request(request: Request, call_next):
    started_at = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        duration = time.perf_counter() - started_at
        route = request.scope.get("route")
        route_template = getattr(route, "path", "unmatched")
        http_requests.labels(request.method, route_template, str(status_code)).inc()
        http_request_duration.labels(request.method, route_template).observe(duration)
        logger.info(
            "http_request",
            extra={
                "method": request.method,
                "route": route_template,
                "status_code": status_code,
                "duration_ms": round(duration * 1000, 2),
            },
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def readiness() -> dict[str, object]:
    settings = get_settings()
    checks = {"database": "ok", "redis": "ok"}
    try:
        with get_session_factory(settings.database_url)() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        checks["database"] = "unavailable"
        logger.error(
            "readiness_database_failed",
            extra={"error_type": type(error).__name__},
        )
    dependency_readiness.labels(dependency="database").set(
        0 if checks["database"] == "unavailable" else 1
    )

    redis = redis_client(settings.redis_url)
    try:
        redis.ping()
    except RedisError as error:
        checks["redis"] = "unavailable"
        logger.error(
            "readiness_redis_failed",
            extra={"error_type": type(error).__name__},
        )
    finally:
        redis.close()
        dependency_readiness.labels(dependency="redis").set(
            0 if checks["redis"] == "unavailable" else 1
        )

    if "unavailable" in checks.values():
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "checks": checks},
        )
    return {"status": "ready", "checks": checks}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    settings = get_settings()
    try:
        with get_session_factory(settings.database_url)() as session:
            snapshot = collect_operations_metrics(session)
    except SQLAlchemyError as error:
        logger.error(
            "operational_metrics_database_failed",
            extra={"error_type": type(error).__name__},
        )
        raise HTTPException(
            status_code=503,
            detail="Operational metrics are temporarily unavailable.",
        ) from error
    for name, gauge in operation_metrics.items():
        gauge.set(float(snapshot[name]))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
