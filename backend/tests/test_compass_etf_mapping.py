"""ETF 板块映射纯函数测试（先于实现验证）。"""
from __future__ import annotations

from app.compass.etf_mapping import build_mapping, match_etf


def etf(code: str, name: str, aum: float, sector_hint: str = "") -> dict:
    return {
        "etf_code": code,
        "etf_name": name,
        "aum": aum,
        "sector_hint": sector_hint,
    }


def test_exact_name_match() -> None:
    rows = [etf("512480", "半导体ETF", 1.0e10, "半导体")]
    match = match_etf("半导体", rows)
    assert match is not None
    assert match["etf_code"] == "512480"


def test_alias_match() -> None:
    rows = [etf("515790", "光伏ETF", 9.0e9, "光伏设备")]
    match = match_etf("光伏设备", rows)
    assert match is not None
    assert match["etf_code"] == "515790"


def test_exact_name_match_beats_alias() -> None:
    rows = [
        etf("512480", "半导体ETF", 1.0e10, "半导体"),
        etf("512760", "芯片ETF", 2.0e10, "半导体"),
    ]
    match = match_etf("半导体", rows)
    assert match is not None
    # 精确包含板块名的 ETF 优先于仅命中别名的 ETF
    assert match["etf_code"] == "512480"


def test_higher_aum_wins_on_equal_name_score() -> None:
    rows = [
        etf("512480", "半导体ETF", 1.0e10, "半导体"),
        etf("512481", "半导体龙头ETF", 2.0e10, "半导体"),
    ]
    match = match_etf("半导体", rows)
    assert match is not None
    assert match["etf_code"] == "512481"


def test_no_match_returns_none() -> None:
    rows = [etf("512480", "半导体ETF", 1.0e10, "半导体")]
    assert match_etf("综合", rows) is None


def test_ths_industry_names_match_via_alias() -> None:
    rows = [
        etf("515220", "煤炭ETF", 2.5e9, "煤炭"),
        etf("512660", "军工ETF", 5.4e9, "军工电子"),
    ]
    coal = match_etf("煤炭开采加工", rows)
    military = match_etf("军工装备", rows)
    assert coal is not None and coal["etf_code"] == "515220"
    assert military is not None and military["etf_code"] == "512660"


def test_build_mapping_reports_coverage_and_missing() -> None:
    sectors = [
        {"sector_code": "A", "sector_name": "半导体"},
        {"sector_code": "B", "sector_name": "光伏设备"},
        {"sector_code": "C", "sector_name": "综合"},
    ]
    rows = [
        etf("512480", "半导体ETF", 1.0e10, "半导体"),
        etf("515790", "光伏ETF", 9.0e9, "光伏设备"),
    ]
    result = build_mapping(sectors, rows)

    assert result["total"] == 3
    assert result["mapped"] == 2
    assert result["coverage"] == 0.6667
    assert [item["sector_code"] for item in result["items"]] == ["A", "B"]
    assert result["missing"] == [{"sector_code": "C", "sector_name": "综合"}]


def test_empty_sectors_is_zero_coverage() -> None:
    result = build_mapping([], [])
    assert result["total"] == 0
    assert result["coverage"] == 0.0
    assert result["items"] == []
