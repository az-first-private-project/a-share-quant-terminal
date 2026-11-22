"""仓位罗盘 API 契约测试（不依赖数据库）。

只覆盖不读库的公开端点；读库端点由集成测试在 GFNA_TEST_DB=1 下覆盖。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_rules_endpoint_is_public_and_versioned() -> None:
    response = client.get("/api/v1/compass/rules")
    assert response.status_code == 200
    body = response.json()
    assert body["rule_version"]
    assert body["criterion"]
    assert set(body["scenario_ids"]) == {"SC-01", "SC-02", "SC-03", "SC-04", "SC-05", "SC-06"}


def test_subscription_defaults_to_pro_beta() -> None:
    response = client.get("/api/v1/compass/subscription")
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "pro"
    assert body["beta"] is True


def test_subscription_free_header_switches_tier() -> None:
    response = client.get(
        "/api/v1/compass/subscription",
        headers={"X-Compass-Tier": "free"},
    )
    assert response.status_code == 200
    assert response.json()["tier"] == "free"
