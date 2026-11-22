"""仓位罗盘口径常量（集中管理，变更必须升级 RULE_VERSION）。"""
from __future__ import annotations

RULE_VERSION = "compass-1.0.0"

# 五态 → 三档预算。bottoming 归入防守是保守选择，见 DRD 4.2.1。
REGIME_TIER: dict[str, str] = {
    "trend_up": "attack",
    "repair_up": "range",
    "bottoming": "defense",
    "pull_down": "defense",
    "risk_off": "defense",
}

TIER_CAP: dict[str, int] = {
    "attack": 80,
    "range": 40,
    "defense": 10,
}

TIER_LABEL: dict[str, str] = {
    "attack": "进攻",
    "range": "震荡",
    "defense": "防守",
}

# 候选池
RANK_POOL_SIZE = 8
OUTPUT_LIMIT = 3
MIN_MEMBERS = 5
MIN_BREADTH = 0.55
CORRELATION_LIMIT = 0.85
CORRELATION_MIN_OVERLAP = 40

# 绝对门槛
MIN_COMPOSITE = 6.5
MIN_TECHNICAL = 6.0
MIN_SENTIMENT = 5.5

# 单板块上限
MAX_PER_SECTOR = 20
CAP_STEP = 5
CAP_RATIO = 0.40

# ETF 可执行性
ETF_MIN_AUM = 2.0e8          # 2 亿元
ETF_MIN_ADV20 = 3.0e7        # 3000 万元
ETF_MAX_PREMIUM = 1.5        # 绝对溢价 1.5%

# 情绪分位
SENTIMENT_WINDOW = 250
SENTIMENT_MIN_SAMPLES = 60
CROWDING_HOT = 90
CROWDING_WARM = 80

# T+1 校验
OPEN_GAP_THRESHOLD = 2.0

# 空仓原因优先级（数字越小越靠前）
EMPTY_REASON_PRIORITY: dict[str, int] = {
    "REGIME_DEFENSE": 1,
    "NO_ABSOLUTE_PASS": 2,
    "CROWDING_REVERSAL": 3,
    "BREADTH_BREAKDOWN": 4,
    "NO_TRADABLE_ETF": 5,
    "DATA_INCOMPLETE": 6,
}

# 情景卡动作优先级（从高到低）
ACTION_PRIORITY: tuple[str, ...] = ("SC-06", "SC-01", "SC-05", "SC-03", "SC-04", "SC-02")

# 免费版可读情景卡
FREE_SCENARIO_IDS: tuple[str, ...] = ("SC-01", "SC-03")
ALL_SCENARIO_IDS: tuple[str, ...] = ("SC-01", "SC-02", "SC-03", "SC-04", "SC-05", "SC-06")

SCENARIO_TITLES: dict[str, str] = {
    "SC-01": "空仓优先",
    "SC-02": "单板块上限",
    "SC-03": "停止加仓",
    "SC-04": "延迟执行",
    "SC-05": "退出",
    "SC-06": "失效降级",
}
