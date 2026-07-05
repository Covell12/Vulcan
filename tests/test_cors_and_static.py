"""CORS + static-frontend serving.

The web/ site is a standalone client that may be hosted on a different origin
than the API (it talks to the API over HTTP via web/api.js). These tests lock in
the two things that makes possible: permissive CORS by default, and that the new
config.js/api.js client seam is actually served.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_cors_allows_cross_origin_get():
    """A browser on another origin gets an Access-Control-Allow-Origin header."""
    resp = client.get("/health", headers={"Origin": "https://vulcan.example"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") in (
        "*",
        "https://vulcan.example",
    )


def test_cors_preflight_allows_token_header():
    """The founder token is a custom header; a cross-origin POST triggers a
    preflight, which must be allowed (methods + the X-Review-Token header)."""
    resp = client.options(
        "/review/whatever",
        headers={
            "Origin": "https://vulcan.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-review-token, content-type",
        },
    )
    assert resp.status_code in (200, 204)
    assert resp.headers.get("access-control-allow-origin") in (
        "*",
        "https://vulcan.example",
    )
    allow_headers = (resp.headers.get("access-control-allow-headers") or "").lower()
    # allow_headers="*" or an explicit echo of the requested header both pass.
    assert "*" in allow_headers or "x-review-token" in allow_headers


def test_frontend_client_seam_is_served():
    """config.js + api.js (the standalone-client seam) are served as static
    assets, so a same-origin deploy keeps working out of the box."""
    for path in ("/config.js", "/api.js"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "javascript" in resp.headers.get("content-type", "")
        assert len(resp.content) > 0


def test_index_loads_client_before_app_scripts():
    """index.html must load config.js + api.js before app.js/intents.js, or the
    bare describeFetchError/errorText globals + VulcanAPI won't exist yet."""
    html = client.get("/").text
    order = [
        html.find(f"/{name}")
        for name in ("config.js", "api.js", "app.js", "intents.js")
    ]
    assert all(i != -1 for i in order), order
    assert order == sorted(order), f"client seam must load before flow scripts: {order}"
