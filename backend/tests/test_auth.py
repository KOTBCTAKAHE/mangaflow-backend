from __future__ import annotations

from .conftest import H, register


async def test_register_login_me(client):
    data = await register(client, "alice")
    assert data["user"]["username"] == "alice"
    r = await client.post("/api/v1/auth/login", json={"login": "ALICE@example.com", "password": "secret123"})
    assert r.status_code == 200
    token = r.json()["tokens"]["access_token"]
    me = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["email"] == "alice@example.com"


async def test_duplicate_and_validation(client):
    await register(client, "bob")
    r = await client.post("/api/v1/auth/register", json={"email": "bob@example.com", "username": "bob2",
                                                         "password": "secret123"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"
    r = await client.post("/api/v1/auth/register", json={"email": "x@example.com", "username": "x1",
                                                         "password": "onlyletters"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


async def test_wrong_password_is_generic(client):
    await register(client, "carol")
    r1 = await client.post("/api/v1/auth/login", json={"login": "carol", "password": "nope12345"})
    r2 = await client.post("/api/v1/auth/login", json={"login": "nobody", "password": "nope12345"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["error"]["message"] == r2.json()["error"]["message"]


async def test_concurrent_refresh_is_not_theft(client):
    """Two parts of the app refreshing with the same token at once must not log the user out."""
    data = await register(client, "erin")
    rt1 = data["tokens"]["refresh_token"]
    a = await client.post("/api/v1/auth/refresh", json={"refresh_token": rt1})
    b = await client.post("/api/v1/auth/refresh", json={"refresh_token": rt1})  # same token, seconds later
    assert a.status_code == 200 and b.status_code == 200
    # both resulting sessions keep working
    for r in (a, b):
        nxt = await client.post("/api/v1/auth/refresh", json={"refresh_token": r.json()["tokens"]["refresh_token"]})
        assert nxt.status_code == 200


async def test_reuse_after_logout_is_rejected_even_within_grace(client):
    data = await register(client, "fred")
    rt1 = data["tokens"]["refresh_token"]
    rt2 = (await client.post("/api/v1/auth/refresh", json={"refresh_token": rt1})).json()["tokens"]["refresh_token"]
    assert (await client.post("/api/v1/auth/logout", json={"refresh_token": rt2})).status_code == 200
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": rt1})).status_code == 401


async def test_refresh_rotation_and_reuse_detection(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "refresh_reuse_grace_seconds", 0)
    data = await register(client, "dave")
    rt1 = data["tokens"]["refresh_token"]
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": rt1})
    assert r.status_code == 200
    rt2 = r.json()["tokens"]["refresh_token"]
    assert rt2 != rt1
    # reuse of the rotated token (outside the grace window) revokes the whole family, including rt2
    reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": rt1})
    assert reuse.status_code == 401 and reuse.json()["error"]["code"] == "refresh_token_reused"
    again = await client.post("/api/v1/auth/refresh", json={"refresh_token": rt2})
    assert again.status_code == 401


async def test_logout_and_protected_routes(client, auth):
    assert (await client.get("/api/v1/library")).status_code == 401
    bad = await client.get("/api/v1/library", headers={"Authorization": "Bearer garbage"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_token"
    rt = auth["_data"]["tokens"]["refresh_token"]
    assert (await client.post("/api/v1/auth/logout", json={"refresh_token": rt})).status_code == 200
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": rt})).status_code == 401


async def test_password_change_and_settings_lww(client, auth):
    r = await client.post("/api/v1/users/me/password", headers=H(auth),
                          json={"current_password": "secret123", "new_password": "newpass456"})
    assert r.status_code == 200
    ok = await client.post("/api/v1/auth/login", json={"login": "reader", "password": "newpass456"})
    assert ok.status_code == 200
    put = await client.put("/api/v1/users/me/settings", headers=H(auth),
                           json={"settings": {"theme": "dark"}, "updated_at": "2030-01-01T00:00:00Z"})
    assert put.json()["settings"] == {"theme": "dark"}
    stale = await client.put("/api/v1/users/me/settings", headers=H(auth),
                             json={"settings": {"theme": "light"}, "updated_at": "2020-01-01T00:00:00Z"})
    assert stale.json()["settings"] == {"theme": "dark"}


async def test_rate_limit(client, app):
    app.state.settings.rate_limit_auth = 3
    from app.core.ratelimit import RateLimit

    limiter = RateLimit("t", 2)
    from starlette.requests import Request

    req = Request({"type": "http", "headers": [], "client": ("1.2.3.4", 1), "app": app})
    await limiter(req)
    await limiter(req)
    import pytest

    from app.core.errors import RateLimitedError
    with pytest.raises(RateLimitedError):
        await limiter(req)
