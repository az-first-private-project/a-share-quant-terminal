"""迁移脚本的目标库解析：必须与 alembic/env.py 的隔离规则一致。"""
from __future__ import annotations

import pytest

from scripts import migrate_db


def test_target_url_defaults_to_configured_db(monkeypatch) -> None:
    monkeypatch.delenv("GFNA_DEMO_DATABASE_NAME", raising=False)
    monkeypatch.delenv("GFNA_TEST_DB", raising=False)
    assert migrate_db._target_url().database == "gfna_db"


def test_target_url_respects_test_override(monkeypatch) -> None:
    monkeypatch.delenv("GFNA_DEMO_DATABASE_NAME", raising=False)
    monkeypatch.setenv("GFNA_TEST_DB", "1")
    monkeypatch.setenv("GFNA_TEST_DATABASE_NAME", "gfna_unit_test")
    assert migrate_db._target_url().database == "gfna_unit_test"


def test_target_url_respects_demo_override(monkeypatch) -> None:
    monkeypatch.setenv("GFNA_DEMO_DATABASE_NAME", "gfna_demo")
    monkeypatch.delenv("GFNA_TEST_DB", raising=False)
    assert migrate_db._target_url().database == "gfna_demo"


def test_target_url_rejects_unsafe_suffixes(monkeypatch) -> None:
    monkeypatch.setenv("GFNA_DEMO_DATABASE_NAME", "gfna_db")
    with pytest.raises(RuntimeError):
        migrate_db._target_url()

    monkeypatch.delenv("GFNA_DEMO_DATABASE_NAME", raising=False)
    monkeypatch.setenv("GFNA_TEST_DB", "1")
    monkeypatch.setenv("GFNA_TEST_DATABASE_NAME", "gfna_db")
    with pytest.raises(RuntimeError):
        migrate_db._target_url()
