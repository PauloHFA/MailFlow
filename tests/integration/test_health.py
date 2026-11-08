from fastapi.testclient import TestClient

from src.api.main import app


def test_health_endpoint_reports_application_liveness() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_metrics_endpoint_returns_prometheus_metrics() -> None:
    client = TestClient(app)
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "mailflow_http_requests_total" in response.text
