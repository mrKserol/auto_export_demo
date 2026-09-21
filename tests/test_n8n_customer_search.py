from __future__ import annotations

from unittest.mock import AsyncMock
from datetime import datetime, timezone

import httpx
import pytest

from app.web.app import create_fastapi_app
from tests.test_telegram_proxy import _settings as base_settings


SECRET = "search-secret"


def settings():
    return base_settings(n8n_webhook_secret=SECRET)


@pytest.fixture
def api():
    database = AsyncMock()
    app = create_fastapi_app(settings=settings(), database=database, bot=AsyncMock())
    return app, database


@pytest.mark.asyncio
async def test_customer_search_auth_and_not_found(api):
    app, database = api
    database.search_customers_by_name.return_value = []
    database.fuzzy_search_customers_by_name.return_value = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/internal/n8n/customers/search", json={"name": "Ирина"})).status_code == 401
        assert (await client.post("/internal/n8n/customers/search", headers={"X-N8N-Webhook-Secret": "bad"}, json={"name": "Ирина"})).status_code == 401
        response = await client.post("/internal/n8n/customers/search", headers={"X-N8N-Webhook-Secret": SECRET}, json={"name": "  Ирина   Губайдулина "})
    assert response.json() == {"ok": True, "status": "not_found", "match_type": "fuzzy", "customers": []}
    database.search_customers_by_name.assert_awaited_once_with("Ирина Губайдулина", limit=10)


@pytest.mark.asyncio
async def test_customer_search_returns_found_and_multiple_statuses(api):
    app, database = api
    created_at = datetime(2026, 9, 21, 10, 30, tzinfo=timezone.utc)
    updated_at = datetime(2026, 9, 21, 11, 45, tzinfo=timezone.utc)
    row = {
        "id": 1,
        "first_name": "Ирина",
        "last_name": "Губайдулина",
        "surname": None,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    database.search_customers_by_name.return_value = [row]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/internal/n8n/customers/search", headers={"X-N8N-Webhook-Secret": SECRET}, json={"name": "ирина"})
    assert response.json()["status"] == "found"
    assert response.json()["match_type"] == "exact"
    assert response.json()["customers"][0]["created_at"] == created_at.isoformat()
    assert response.json()["customers"][0]["updated_at"] == updated_at.isoformat()
    database.search_customers_by_name.return_value = [row, {**row, "id": 2}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/internal/n8n/customers/search", headers={"X-N8N-Webhook-Secret": SECRET}, json={"name": "Ирина"})
    assert response.json()["status"] == "multiple"
    assert response.json()["match_type"] == "exact"


@pytest.mark.asyncio
async def test_customer_search_fuzzy_fallback_returns_scores_and_fuzzy_type(api):
    app, database = api
    database.search_customers_by_name.return_value = []
    database.fuzzy_search_customers_by_name.return_value = [
        {"id": 7, "first_name": "Ирина", "last_name": "Губайдулина", "match_score": 0.71}
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/internal/n8n/customers/search",
            headers={"X-N8N-Webhook-Secret": SECRET},
            json={"name": "Губайдулин"},
        )
    body = response.json()
    assert body["status"] == "found"
    assert body["match_type"] == "fuzzy"
    assert body["customers"][0]["match_score"] == 0.71


@pytest.mark.asyncio
async def test_customer_search_rejects_blank_name_and_caps_limit_in_database_method():
    from app.database import Database
    database = object.__new__(Database)
    database._pool = AsyncMock()
    database._pool.acquire.return_value.__aenter__.return_value.fetch.return_value = []
    assert await database.search_customers_by_name("  ", limit=100) == []
