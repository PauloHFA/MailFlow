import logging
import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from src.api.messages import router as messages_router
from src.api.campaigns import router as campaigns_router
from src.api.domain import router as domain_router
from src.api.exports import router as exports_router
from src.api.metrics import router as delivery_metrics_router
from src.api.recipients import router as recipients_router
from src.api.templates import router as templates_router
from src.api.unsubscribe import router as unsubscribe_router
from src.api.webhooks import router as webhooks_router
from src.config import get_settings
from src.logging_config import configure_logging

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


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
