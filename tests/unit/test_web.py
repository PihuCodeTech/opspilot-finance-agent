import pytest
from fastapi.testclient import TestClient

from opspilot.web.app import create_app
from opspilot.web.middleware import REQUEST_ID_HEADER, SECURITY_HEADERS


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz_is_ok_without_dependencies(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_not_ready_when_database_is_unavailable(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "not_ready"
    assert error["details"]["database"] == "unavailable"
    assert error["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert "postgresql" not in response.text  # no connection details leak


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/"])
def test_security_headers_on_every_response(client: TestClient, path: str) -> None:
    response = client.get(path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "'unsafe-inline'" not in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    ("cookie", "expected"),
    [("light", 'data-theme="light"'), ("dark", 'data-theme="dark"')],
)
def test_theme_cookie_sets_data_theme(client: TestClient, cookie: str, expected: str) -> None:
    client.cookies.set("theme", cookie)
    assert expected in client.get("/").text


@pytest.mark.parametrize("cookie", [None, "system", "neon"])
def test_system_or_unknown_theme_leaves_css_to_decide(
    client: TestClient, cookie: str | None
) -> None:
    if cookie:
        client.cookies.set("theme", cookie)
    assert "data-theme=" not in client.get("/").text


def test_views3d_cookie(client: TestClient) -> None:
    assert 'data-views3d="on"' in client.get("/").text
    client.cookies.set("views3d", "off")
    assert 'data-views3d="off"' in client.get("/").text


def test_static_assets_are_served_locally(client: TestClient) -> None:
    for path in [
        "/static/vendor/htmx/htmx.min.js",
        "/static/css/tokens.css",
        "/static/fonts/geist/Geist-Regular.woff2",
    ]:
        assert client.get(path).status_code == 200, path
