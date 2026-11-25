from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.main import create_app


class HealthyConnection:
    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False

    def execute(self, statement):
        return 1


class HealthyDatabase:
    def connect(self):
        return HealthyConnection()


class UnavailableDatabase:
    def connect(self):
        raise OperationalError("SELECT 1", {}, ConnectionError("database unavailable"))


class HealthyRedis:
    def ping(self):
        return True


class UnavailableRedis:
    def ping(self):
        from redis.exceptions import ConnectionError

        raise ConnectionError("redis unavailable")


def make_client(database_engine=None, redis_client=None):
    app = create_app(
        Settings(),
        database_engine=database_engine or HealthyDatabase(),
        redis_client=redis_client or HealthyRedis(),
    )
    return TestClient(app)


def test_liveness_is_available_without_dependency_probes():
    response = make_client().get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_healthy_dependencies():
    response = make_client().get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "redis": "ok"},
    }


def test_readiness_returns_service_unavailable_when_a_dependency_is_down():
    response = make_client(
        database_engine=UnavailableDatabase(),
        redis_client=UnavailableRedis(),
    ).get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "unavailable", "redis": "unavailable"},
    }


def test_http_errors_use_the_shared_error_shape():
    response = make_client().get("/api/v1/unknown")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_error"
