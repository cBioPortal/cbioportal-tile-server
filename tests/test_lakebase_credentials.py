"""Lakebase password provider and annotation pool wiring (no network)."""

import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from app import annotation_db, lakebase_credentials as creds

ENV = {
    "ANNOTATION_DATABASE_PASSWORD_PROVIDER": "databricks",
    "ANNOTATION_LAKEBASE_INSTANCE": "annotations-instance",
    "DATABRICKS_HOST": "example.cloud.databricks.com",
    "DATABRICKS_CLIENT_ID": "sp-client",
    "DATABRICKS_CLIENT_SECRET": "sp-secret",
}


@pytest.fixture
def provider_env(monkeypatch):
    for name in (*ENV, "DATABRICKS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    creds.reset()
    yield
    creds.reset()


@pytest.fixture
def fake_post(monkeypatch):
    calls = []
    state = {"db_lifetime": timedelta(hours=1), "db_counter": 0}

    def post(url, data, headers):
        calls.append((url, data, headers))
        if url.endswith("/oidc/v1/token"):
            return {"access_token": "workspace-token", "expires_in": 3600}
        if url.endswith("/api/2.0/database/credentials"):
            state["db_counter"] += 1
            expires = datetime.now(timezone.utc) + state["db_lifetime"]
            return {
                "token": f"db-token-{state['db_counter']}",
                "expiration_time": expires.isoformat().replace("+00:00", "Z"),
            }
        raise AssertionError(url)

    monkeypatch.setattr(creds, "_post", post)
    return calls, state


@pytest.mark.asyncio
async def test_password_comes_from_a_service_principal_credential(provider_env, fake_post):
    calls, _ = fake_post
    assert await creds.database_password() == "db-token-1"

    token_url, token_body, token_headers = calls[0]
    assert token_url == "https://example.cloud.databricks.com/oidc/v1/token"
    assert urllib.parse.parse_qs(token_body.decode()) == {
        "grant_type": ["client_credentials"],
        "scope": ["all-apis"],
    }
    assert token_headers["Authorization"].startswith("Basic ")

    db_url, db_body, db_headers = calls[1]
    assert db_url == "https://example.cloud.databricks.com/api/2.0/database/credentials"
    body = json.loads(db_body)
    assert body["instance_names"] == ["annotations-instance"]
    assert body["request_id"]
    assert db_headers["Authorization"] == "Bearer workspace-token"


@pytest.mark.asyncio
async def test_password_is_cached_until_shortly_before_expiry(provider_env, fake_post, monkeypatch):
    calls, _ = fake_post
    assert await creds.database_password() == "db-token-1"
    assert await creds.database_password() == "db-token-1"
    assert len(calls) == 2

    # Within the refresh margin of expiry, a new credential is generated.
    later = time.time() + 3600 - creds.REFRESH_MARGIN_SECONDS + 5
    monkeypatch.setattr(creds.time, "time", lambda: later)
    assert await creds.database_password() == "db-token-2"


@pytest.mark.asyncio
async def test_a_personal_token_is_used_without_a_client(provider_env, fake_post, monkeypatch):
    calls, _ = fake_post
    monkeypatch.delenv("DATABRICKS_CLIENT_ID")
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET")
    monkeypatch.setenv("DATABRICKS_TOKEN", "pat-token")
    assert await creds.database_password() == "db-token-1"
    assert [url for url, _, _ in calls] == [
        "https://example.cloud.databricks.com/api/2.0/database/credentials"
    ]
    assert calls[0][2]["Authorization"] == "Bearer pat-token"


@pytest.mark.asyncio
async def test_a_response_without_a_token_fails(provider_env, monkeypatch):
    monkeypatch.setattr(
        creds,
        "_post",
        lambda url, data, headers: {"access_token": "t", "expires_in": 60}
        if url.endswith("/token")
        else {"expiration_time": "2030-01-01T00:00:00Z"},
    )
    with pytest.raises(creds.CredentialError):
        await creds.database_password()


def test_static_dsn_needs_no_provider(monkeypatch):
    monkeypatch.delenv("ANNOTATION_DATABASE_PASSWORD_PROVIDER", raising=False)
    assert creds.password_option() is None


@pytest.mark.parametrize(
    "missing", ["ANNOTATION_LAKEBASE_INSTANCE", "DATABRICKS_HOST", "DATABRICKS_CLIENT_SECRET"]
)
def test_provider_settings_are_validated(provider_env, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(creds.CredentialError):
        creds.password_option()


def test_unknown_provider_is_refused(monkeypatch):
    monkeypatch.setenv("ANNOTATION_DATABASE_PASSWORD_PROVIDER", "vault")
    with pytest.raises(creds.CredentialError):
        creds.password_option()


class FakePool:
    def __init__(self):
        self.expired = 0
        self.closed = False

    async def expire_connections(self):
        self.expired += 1

    async def close(self):
        self.closed = True

    def acquire(self):
        pool = self

        class Acquire:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False

        return Acquire()


@pytest.fixture
def fake_pool(monkeypatch):
    captured = {}
    pool = FakePool()

    async def create_pool(dsn, **kwargs):
        captured["dsn"] = dsn
        captured.update(kwargs)
        return pool

    monkeypatch.setattr(annotation_db.asyncpg, "create_pool", create_pool)
    yield pool, captured
    annotation_db._pool = None
    annotation_db._pool_dsn = ""


@pytest.mark.asyncio
async def test_pool_uses_the_provider_and_recycles_connections(provider_env, fake_pool, monkeypatch):
    pool, captured = fake_pool
    monkeypatch.setenv("ANNOTATION_DB_MAX_CONNECTION_AGE_SECONDS", "1800")
    await annotation_db.open_pool("postgresql://sp-client@lakebase:5432/annotations?sslmode=require")
    assert captured["password"] is creds.database_password
    assert 0 < captured["max_inactive_connection_lifetime"] < 3600

    async with annotation_db.connection("postgresql://sp-client@lakebase:5432/annotations?sslmode=require"):
        pass
    assert pool.expired == 0

    started = annotation_db._pool_generation_started
    monkeypatch.setattr(annotation_db.time, "monotonic", lambda: started + 1801)
    async with annotation_db.connection("postgresql://sp-client@lakebase:5432/annotations?sslmode=require"):
        pass
    assert pool.expired == 1
    async with annotation_db.connection("postgresql://sp-client@lakebase:5432/annotations?sslmode=require"):
        pass
    assert pool.expired == 1


@pytest.mark.asyncio
async def test_pool_keeps_the_static_dsn_password(fake_pool, monkeypatch):
    _, captured = fake_pool
    monkeypatch.delenv("ANNOTATION_DATABASE_PASSWORD_PROVIDER", raising=False)
    await annotation_db.open_pool("postgresql://user:static@db:5432/annotations")
    assert captured["password"] is None
    assert captured["dsn"] == "postgresql://user:static@db:5432/annotations"


def test_default_connection_age_is_below_the_token_lifetime(monkeypatch):
    monkeypatch.delenv("ANNOTATION_DB_MAX_CONNECTION_AGE_SECONDS", raising=False)
    assert 0 < annotation_db.max_connection_age() < creds.DEFAULT_LIFETIME_SECONDS
