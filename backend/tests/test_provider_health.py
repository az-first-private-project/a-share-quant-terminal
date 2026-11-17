"""V9 P0-3：Provider 健康度纯逻辑测试（不连库）。"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ingest.provider_health import (  # noqa: E402
    attempts_to_rows,
    record_provider_attempts,
    sanitize_error,
)


def test_sanitize_error_redacts_url_and_secret() -> None:
    text = sanitize_error(
        "ProviderError: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123 "
        "token=secret-value"
    )
    assert "https://" not in (text or "")
    assert "abc123" not in (text or "")
    assert "secret-value" not in (text or "")
    assert "<url>" in (text or "")


def test_attempts_to_rows_maps_status_and_duration() -> None:
    rows = attempts_to_rows(
        "snapshot",
        date(2026, 11, 17),
        [
            {"source": "akshare", "status": "failed", "elapsed_ms": 123, "error": "boom"},
            {"source": "baostock", "status": "ok", "rows": 10, "elapsed_ms": 45},
        ],
    )
    assert len(rows) == 2
    assert rows[0].provider == "akshare"
    assert rows[0].result == "failed"
    assert rows[0].duration_ms == 123
    assert rows[1].result == "ok"
    assert rows[1].duration_ms == 45


def test_record_without_session_is_noop() -> None:
    assert asyncio.run(
        record_provider_attempts(None, "snapshot", date(2026, 11, 17), [{"source": "x", "status": "ok"}])
    ) == 0
