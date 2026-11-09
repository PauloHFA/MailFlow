from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.main import app
from src.db import models  # noqa: F401
from src.db.base import Base


def test_health_endpoint_reports_application_liveness() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_metrics_endpoint_returns_prometheus_metrics(monkeypatch, tmp_path) -> None:
    import src.api.main as main
    from src.config import Settings

    database_url = f"sqlite+pysqlite:///{tmp_path / 'metrics.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(_env_file=None, database_url=database_url),
    )
    monkeypatch.setattr(
        main,
        "get_session_factory",
        lambda _database_url: sessionmaker(bind=engine),
    )
    client = TestClient(app)
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "mailflow_http_requests_total" in response.text
    assert "mailflow_oldest_pending_age_seconds" in response.text
    engine.dispose()


def test_readiness_checks_database_and_redis(monkeypatch) -> None:
    import src.api.main as main
    from src.config import Settings

    class ReadyRedis:
        def ping(self) -> bool:
            return True

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(_env_file=None, database_url="sqlite+pysqlite://"),
    )
    monkeypatch.setattr(main, "redis_client", lambda redis_url: ReadyRedis())
    main.get_session_factory.cache_clear()

    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "redis": "ok"},
    }
    assert main.dependency_readiness.labels(dependency="database")._value.get() == 1
    assert main.dependency_readiness.labels(dependency="redis")._value.get() == 1


def test_readiness_returns_service_unavailable_when_redis_is_down(monkeypatch) -> None:
    import src.api.main as main
    from src.config import Settings

    class FailedRedis:
        def ping(self) -> bool:
            raise RedisError("redis is down")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(_env_file=None, database_url="sqlite+pysqlite://"),
    )
    monkeypatch.setattr(main, "redis_client", lambda redis_url: FailedRedis())
    main.get_session_factory.cache_clear()

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["checks"]["redis"] == "unavailable"
    assert main.dependency_readiness.labels(dependency="redis")._value.get() == 0
