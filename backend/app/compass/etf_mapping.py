"""ETF ↔ 行业板块映射：种子 ETF 池 + 别名匹配 + 覆盖率统计。

设计：先由数据源拿到“可交易 ETF 池”，再用板块名/别名匹配。
没有匹配的板块不生成映射，覆盖率如实返回，不用虚构映射填充。
"""
from __future__ import annotations

import re
from typing import Any

# 种子 ETF 池：名称、规模为已知参考值；实际规模/价格以同步到的日行情为准。
SEED_ETF_UNIVERSE: list[dict[str, Any]] = [
    {"etf_code": "512480", "etf_name": "半导体ETF", "sector_hint": "半导体", "aum": 1.28e10},
    {"etf_code": "512760", "etf_name": "芯片ETF", "sector_hint": "半导体", "aum": 2.20e10},
    {"etf_code": "515790", "etf_name": "光伏ETF", "sector_hint": "光伏设备", "aum": 9.60e9},
    {"etf_code": "512660", "etf_name": "军工ETF", "sector_hint": "军工电子", "aum": 5.40e9},
    {"etf_code": "512710", "etf_name": "军工龙头ETF", "sector_hint": "军工电子", "aum": 4.00e9},
    {"etf_code": "159992", "etf_name": "创新药ETF", "sector_hint": "创新药", "aum": 4.10e9},
    {"etf_code": "159732", "etf_name": "消费电子ETF", "sector_hint": "消费电子", "aum": 3.30e9},
    {"etf_code": "515220", "etf_name": "煤炭ETF", "sector_hint": "煤炭", "aum": 2.50e9},
    {"etf_code": "512800", "etf_name": "银行ETF", "sector_hint": "银行", "aum": 1.20e10},
    {"etf_code": "512690", "etf_name": "酒ETF", "sector_hint": "白酒", "aum": 8.00e9},
    {"etf_code": "512010", "etf_name": "医药ETF", "sector_hint": "化学制药", "aum": 7.00e9},
    {"etf_code": "512170", "etf_name": "医疗ETF", "sector_hint": "化学制药", "aum": 6.00e9},
    {"etf_code": "516110", "etf_name": "汽车ETF", "sector_hint": "汽车零部件", "aum": 1.50e9},
    {"etf_code": "515030", "etf_name": "新能源车ETF", "sector_hint": "汽车零部件", "aum": 5.00e9},
    {"etf_code": "512400", "etf_name": "有色金属ETF", "sector_hint": "有色金属", "aum": 6.00e9},
    {"etf_code": "515210", "etf_name": "钢铁ETF", "sector_hint": "钢铁", "aum": 1.20e9},
    {"etf_code": "512200", "etf_name": "房地产ETF", "sector_hint": "房地产", "aum": 2.00e9},
    {"etf_code": "512980", "etf_name": "传媒ETF", "sector_hint": "传媒", "aum": 2.50e9},
    {"etf_code": "159869", "etf_name": "游戏ETF", "sector_hint": "传媒", "aum": 2.00e9},
    {"etf_code": "512720", "etf_name": "计算机ETF", "sector_hint": "计算机", "aum": 2.00e9},
    {"etf_code": "515880", "etf_name": "通信ETF", "sector_hint": "通信", "aum": 1.50e9},
    {"etf_code": "159611", "etf_name": "电力ETF", "sector_hint": "电力", "aum": 1.20e9},
    {"etf_code": "512000", "etf_name": "券商ETF", "sector_hint": "证券", "aum": 3.00e9},
    {"etf_code": "512070", "etf_name": "证券保险ETF", "sector_hint": "非银金融", "aum": 2.00e9},
    {"etf_code": "515170", "etf_name": "食品饮料ETF", "sector_hint": "食品饮料", "aum": 2.00e9},
    {"etf_code": "159928", "etf_name": "消费ETF", "sector_hint": "食品饮料", "aum": 2.50e9},
    {"etf_code": "159745", "etf_name": "建材ETF", "sector_hint": "建筑材料", "aum": 1.00e9},
    {"etf_code": "512580", "etf_name": "环保ETF", "sector_hint": "环保", "aum": 0.80e9},
    {"etf_code": "159865", "etf_name": "养殖ETF", "sector_hint": "农林牧渔", "aum": 0.90e9},
    {"etf_code": "159766", "etf_name": "旅游ETF", "sector_hint": "社会服务", "aum": 0.80e9},
    {"etf_code": "159996", "etf_name": "家电ETF", "sector_hint": "家用电器", "aum": 1.00e9},
    {"etf_code": "159870", "etf_name": "化工ETF", "sector_hint": "基础化工", "aum": 1.20e9},
    {"etf_code": "159930", "etf_name": "能源ETF", "sector_hint": "石油石化", "aum": 0.90e9},
    {"etf_code": "516970", "etf_name": "基建ETF", "sector_hint": "建筑装饰", "aum": 1.00e9},
    {"etf_code": "159819", "etf_name": "人工智能ETF", "sector_hint": "计算机", "aum": 1.50e9},
    {"etf_code": "159770", "etf_name": "机器人ETF", "sector_hint": "机械设备", "aum": 1.20e9},
    {"etf_code": "515000", "etf_name": "科技ETF", "sector_hint": "电子", "aum": 3.00e9},
    {"etf_code": "159666", "etf_name": "交通运输ETF", "sector_hint": "公路铁路运输", "aum": 1.00e9},
    {"etf_code": "159825", "etf_name": "农业ETF", "sector_hint": "种植业与林业", "aum": 1.00e9},
    {"etf_code": "513360", "etf_name": "教育ETF", "sector_hint": "教育", "aum": 0.80e9},
]

SECTOR_ALIASES: dict[str, list[str]] = {
    "半导体": ["半导体", "芯片"],
    "光伏设备": ["光伏"],
    "军工电子": ["军工", "国防"],
    "军工装备": ["军工", "国防"],
    "创新药": ["创新药", "医药"],
    "消费电子": ["消费电子"],
    "煤炭": ["煤炭"],
    "煤炭开采加工": ["煤炭"],
    "银行": ["银行"],
    "白酒": ["白酒", "酒"],
    "化学制药": ["医药", "医疗", "化学制药"],
    "生物制品": ["生物医药", "医药", "生物"],
    "医疗服务": ["医疗"],
    "医疗器械": ["医疗", "器械"],
    "医药商业": ["医药"],
    "中药": ["中药", "医药"],
    "医药生物": ["医药", "医疗"],
    "汽车零部件": ["汽车", "新能源车"],
    "汽车整车": ["汽车"],
    "汽车服务及其他": ["汽车"],
    "有色金属": ["有色"],
    "工业金属": ["有色", "金属"],
    "贵金属": ["黄金", "有色", "金属"],
    "小金属": ["有色", "金属"],
    "金属新材料": ["有色", "材料"],
    "能源金属": ["有色", "锂", "新能源"],
    "钢铁": ["钢铁"],
    "房地产": ["房地产"],
    "传媒": ["传媒", "游戏"],
    "文化传媒": ["传媒"],
    "游戏": ["游戏", "传媒"],
    "影视院线": ["传媒", "影视"],
    "计算机": ["计算机", "软件", "人工智能"],
    "软件开发": ["软件", "计算机"],
    "计算机设备": ["计算机"],
    "IT服务": ["计算机", "软件", "IT"],
    "通信": ["通信", "5G"],
    "通信设备": ["通信", "5G"],
    "通信服务": ["通信"],
    "电力": ["电力"],
    "电网设备": ["电力", "电网"],
    "其他电源设备": ["电力", "新能源"],
    "电力设备": ["新能源", "电力设备"],
    "证券": ["券商", "证券"],
    "非银金融": ["券商", "证券", "保险"],
    "食品饮料": ["食品饮料", "消费"],
    "食品加工制造": ["食品", "消费"],
    "饮料制造": ["食品饮料", "饮料", "消费"],
    "建筑材料": ["建材"],
    "建筑装饰": ["基建", "建筑"],
    "环保": ["环保"],
    "环保设备": ["环保"],
    "环境治理": ["环保"],
    "农林牧渔": ["农业", "养殖"],
    "养殖业": ["养殖", "农业"],
    "种植业与林业": ["农业", "种植"],
    "农产品加工": ["农业"],
    "农化制品": ["化工", "农业"],
    "社会服务": ["旅游", "服务"],
    "旅游及酒店": ["旅游"],
    "其他社会服务": ["服务"],
    "机场航运": ["机场", "航空", "交通运输", "运输"],
    "公路铁路运输": ["公路", "铁路", "交通运输", "运输"],
    "港口航运": ["港口", "航运", "运输"],
    "物流": ["物流", "运输"],
    "教育": ["教育"],
    "多元金融": ["金融", "证券", "保险"],
    "电池": ["电池", "新能源"],
    "电机": ["电机", "新能源"],
    "风电设备": ["风电", "新能源"],
    "元件": ["元件", "电子"],
    "光学光电子": ["光电", "电子", "光学"],
    "其他电子": ["电子"],
    "包装印刷": ["包装"],
    "造纸": ["造纸"],
    "非金属材料": ["材料"],
    "家用电器": ["家电"],
    "白色家电": ["家电"],
    "黑色家电": ["家电"],
    "小家电": ["家电"],
    "厨卫电器": ["家电"],
    "基础化工": ["化工"],
    "化学原料": ["化工"],
    "化学制品": ["化工"],
    "化学纤维": ["化工", "化纤"],
    "塑料制品": ["化工", "材料"],
    "橡胶制品": ["化工", "材料"],
    "非金属材料": ["材料"],
    "电子化学品": ["化工", "电子"],
    "石油石化": ["能源", "石油"],
    "石油加工贸易": ["石油", "能源"],
    "油气开采及服务": ["石油", "能源"],
    "燃气": ["燃气", "能源"],
    "国防军工": ["军工", "国防"],
    "电子": ["电子", "科技", "芯片"],
    "机械设备": ["机械", "机器人"],
    "通用设备": ["机械", "设备", "高端装备"],
    "专用设备": ["机械", "设备", "高端装备"],
    "自动化设备": ["自动化", "机器人", "机械"],
    "工程机械": ["机械", "工程机械"],
    "公用事业": ["电力", "公用"],
    "商贸零售": ["消费", "零售"],
    "零售": ["零售", "消费"],
    "互联网电商": ["电商", "互联网"],
    "纺织制造": ["纺织"],
    "服装家纺": ["纺织", "服装"],
    "包装印刷": ["包装"],
    "造纸": ["造纸"],
    "美容护理": ["消费", "美容"],
    "家居用品": ["家居", "消费"],
    "综合": [],
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _aliases(sector_name: str) -> list[str]:
    rows = [sector_name, *SECTOR_ALIASES.get(sector_name, [])]
    seen: set[str] = set()
    result = []
    for item in rows:
        text = _clean(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def match_etf(sector_name: str, etf_rows: list[dict]) -> dict | None:
    """按“板块名/别名命中长度 + 规模”选最佳 ETF；无命中返回 None。"""
    aliases = _aliases(sector_name)
    best: tuple[float, float, dict] | None = None
    for etf in etf_rows:
        name = _clean(etf.get("etf_name"))
        if not name:
            continue
        score = 0.0
        for alias in aliases:
            if alias in name:
                score = max(score, len(alias) * 10.0 + (6.0 if alias == sector_name else 0.0))
        if str(etf.get("sector_hint") or "") == sector_name:
            score += 3.0
        if score <= 0:
            continue
        aum = float(etf.get("aum") or 0.0)
        candidate = (score, aum, etf)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return best[2] if best else None


def build_mapping(sectors: list[dict], etf_rows: list[dict]) -> dict:
    """为行业板块生成主映射；返回映射项与覆盖率。"""
    items: list[dict] = []
    missing: list[dict] = []
    for sector in sectors:
        match = match_etf(str(sector.get("sector_name") or ""), etf_rows)
        if match is None:
            missing.append({
                "sector_code": sector.get("sector_code"),
                "sector_name": sector.get("sector_name"),
            })
            continue
        items.append({
            "sector_code": sector.get("sector_code"),
            "sector_name": sector.get("sector_name"),
            "etf_code": match.get("etf_code"),
            "etf_name": match.get("etf_name"),
            "is_primary": True,
            "aum": float(match.get("aum") or 0.0),
        })

    total = len(sectors)
    mapped = len(items)
    return {
        "items": items,
        "total": total,
        "mapped": mapped,
        "coverage": round(mapped / total, 4) if total else 0.0,
        "missing": missing,
    }
