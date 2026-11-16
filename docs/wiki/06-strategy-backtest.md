# 06 · 策略与回测

对应代码：`backend/app/strategy/`。

---

## 1. 当前策略状态（以代码为准）

实际运行验证：

```text
SPECS          = ['a3', 'a4']
CANDIDATE_SPECS= ['c1','c2','c3','c4','c5','c6']
```

| 状态 | 策略 | 说明 |
|---|---|---|
| **正式启用** | A3 央企 × 综合评分 | 进入 `/algo/list` 默认返回、每日任务、共识 |
| **正式启用** | A4 高股息质量 × 低估值 | 带防御型市场阶段门槛 |
| 观察候选 | C1 海龟突破、C2 高窄旗形、C3 RPS 突破、C4 涨停洗盘、C5 均线放量突破、C6 布林挤压突破 | 可查看/执行/回测；不进入共识与每日正式任务；`include_observation=true` 才出现在清单 |
| 已停用 | B1–B5 短线策略 | V8 M1 回测未过门槛；`STRATEGY_DEFAULTS`、排序/指标定义和条件构造代码保留，但未注册进 `SPECS`，`CONDITION_SPECS` 为空 |
| 已下线 | A1、A2 | 按回测结果下线，仅历史 run 保留 |

> `CANDIDATE_SPECS` 是从 `SPECS` 中 `pop` 出来的；新增策略必须先通过回测门槛，再显式加入 `SPECS`。

---

## 2. 策略元信息（`StrategySpec`）

文件：[`strategy/strategies.py`](../../backend/app/strategy/strategies.py)

```python
@dataclass(frozen=True)
class StrategySpec:
    code: str
    name: str
    params_schema: list[dict]   # 前端据此渲染表单
    defaults: dict              # 归一化后的默认参数（含 limit）
    algo_type: str = "technical"
    short_desc: str = ""
    full_desc: str = ""
    sort_order: int = 0
```

参数 schema 项由 `_param`（L105-L121）构造：`key/label/type/default/min/max`。

### 2.1 A3

```
ownership == '央企'
composite_score >= min_score（默认 6.0/10）
按 composite_score 降序
参数：limit（1–1000）、min_score（0–10）
```

“央企”来自 `core/soe.py` 的央企名录 + 名称规则，**未知不会误判为央企**。

### 2.2 A4

```
pe_ttm > 0 且 < pe_max（默认 15）
div_min(4%) <= dv_ttm <= dv_max(15%)
0 < pb <= pb_max(2.5)
composite_score >= min_score(5.0)
amount_ma20 >= min_amount(5e7)
close >= MA120 × trend_dev(0.9)
且 market_regime ∈ {bottoming, pull_down, risk_off}
按 dv_ttm 降序
```

阶段数据缺失时按放行处理；纳入 `_build_condition_specs` 生成的结构化依据。

### 2.3 候选策略（C1–C6）

| 代码 | 名称 | 核心条件 | 排序 |
|---|---|---|---|
| C1 | 海龟突破 | 20 日新高 + 成交额过亿 + 阳线真涨 | 流通市值降序 |
| C2 | 高窄旗形 | 40 日强动量后 10 日收敛缩量 | 40 日区间比降序 |
| C3 | RPS 相对强度 | 120 日收益横截面前 10% + 接近新高 | RPS 降序 |
| C4 | 涨停洗盘 | 昨日涨停、今日放量收阴不破昨收 | 成交额降序 |
| C5 | 均线放量突破 | MA5/MA20 金叉 + 1.5 倍量 | 放量比降序 |
| C6 | 布林挤压突破 | 带宽低分位 + 突破上轨新高 + 放量 + 市场门槛 | 带宽分位升序 |

参数默认值统一在 `core/config.py:STRATEGY_DEFAULTS`。

---

## 3. 候选池构建

入口：`build_candidates(algo_code, trade_date, params, db)`（L2008-L2065）

```
_fetch_cross_section（一次 SQL）
  fact_stock_daily
  LEFT JOIN fact_feature_daily
  LEFT JOIN fact_stock_score_daily
  LEFT JOIN fact_moneyflow
  JOIN dim_stock
        │
        ▼
apply_common_filters
  - 剔除 ST
  - status != 1（停牌/退市）
  - delist_date 已到
  - 上市不足 60 个交易日
  - is_suspended
  - 当日无行情
        │
        ▼
窗口策略补充历史（_enrich_history，如 C1–C4 需要前 N 日数据）
        │
        ▼
_assert_data_ready（必需列/覆盖率检查）
        │
        ▼
filter_by_strategy（纯 pandas 条件）
        │
        ▼
coerce_numeric_frame（数值列统一 float64）
```

返回 DataFrame 的 `attrs` 带：`trade_date`、`universe_count`、`eligible_count`、`calendar_source`、`algo`、`params`、`market`（A4/C6 等）。

**禁止 N+1 查询**：候选池只查一次库。

### 3.1 排序与信号生成

`rank_signals(algo_code, df, params)`（L1950）：

1. 按 `_SORT_SPEC` 排序（如 A3 `composite_score` 降序、A4 `dv_ttm` 降序）；
2. `tie-break` 用 `code`，保证结果稳定；
3. 截断到 `limit`；
4. 生成 `rank / score / metrics`；
5. `score` = 该策略排序指标的原始值（不同策略量纲不同，不可横向比较）。

---

## 4. 条件树 DSL（`condition.py`）

文件：[`strategy/condition.py`](../../backend/app/strategy/condition.py)

纯本地 DataFrame 计算，不拼接 SQL，无注入面。

### 4.1 结构

```python
Condition(field="pe_ttm", op="lt", value=15)          # 叶子
ConditionGroup(logic="AND", children=[...])           # 枝干
```

### 4.2 操作符

`gt / ge / lt / le / eq / ne / between / in / not_in / top_pct / bottom_pct / notna / isna`

分位口径：

- `top_pct: 20` → 升序排名百分位 > 0.8（最高 20%）；
- `bottom_pct: 20` → 排名百分位 ≤ 0.2（最低 20%）。

### 4.3 示例

```json
{
  "logic": "AND",
  "children": [
    {"field": "pe_ttm", "op": "between", "value": [0, 15]},
    {"field": "dv_ttm", "op": "ge", "value": 4},
    {"field": "composite_score", "op": "ge", "value": 5},
    {"field": "close", "op": "gt", "value": {"field": "ma120", "multiplier": 0.9}}
  ],
  "exclude_industry": ["银行"]
}
```

行业约束两种写法等价：

1. 普通条件：`{"field":"industry_l1","op":"in","value":["电子"]}`；
2. 简写糖：`industry` → `industry_l1 in`；`exclude_industry` → `industry_l1 not_in`。

### 4.4 主要函数

| 函数 | 行号 | 说明 |
|---|---:|---|
| `evaluate` | L246 | 返回布尔 Series |
| `explain` | L434 | 逐条条件命中情况（信号解释） |
| `triggered_row` | L482 | 单只股票触发明细 |
| `apply` | L522 | 过滤 DataFrame |
| `describe` | L556 | 人类可读条件描述（写 run 快照） |
| `from_dict` / `to_dict` | L595 / L677 | 序列化 |
| `flatten_fields` | L710 | 字段清单 |

`ConditionDetail`（L359）是 `/algo/{code}` 响应中“信号逐条解释”的事实源。

---

## 5. 策略运行与可复现（`runner.py`）

文件：[`strategy/runner.py`](../../backend/app/strategy/runner.py)

### 5.1 缓存键

```python
(algo_code, trade_date, params_fingerprint, data_version)
```

- `params_fingerprint`（L94）：参数归一化后的稳定哈希；
- `data_version`（L203)：当日 `fact_stock_daily / fact_feature_daily / fact_stock_score_daily / fact_moneyflow` 行数 + 最近写入时间哈希；行数不变但值被 upsert 更新时同样失效；
- 命中要求 **4 项全等**，否则重算；
- `signal_schema` 必须等于当前版本（3）。

### 5.2 执行流程

`run_strategy`（L454）→ `_run_strategy_impl`（L487）：

```
参数校验/补默认
  → 解析交易日（None 时取最近就绪交易日）
  → data_version + params_fingerprint
  → 查缓存（force=False）
  → 写 stg_run(status=running) + commit/flush
  → build_candidates
  → rank_signals
  → 写 stg_signal
  → 更新 run(success, elapsed, counts)
  → 旧成功 run 标 superseded
  → 返回 {algo, algo_name, trade_date, run:{...}, from_cache, count, data}
```

失败也会留下 `stg_run(status=failed, error=...)`，异常继续抛出。

### 5.3 事务所有权

| 调用方式 | 行为 |
|---|---|
| `db=None` | 自建 `AsyncSessionLocal`，每阶段 commit；失败也写 failed |
| 传入 `db`（API / pipeline / 回测） | 只 `flush`，commit/rollback 由调用方负责 |

### 5.4 查询接口

`get_run`（L620）、`count_runs`（L648）、`list_runs`（L658）；`get_cached_run`（L404）。

---

## 6. 回测（`backtest.py`）

文件：[`strategy/backtest.py`](../../backend/app/strategy/backtest.py)

### 6.1 成交规则（用户定稿）

| 项 | 规则 |
|---|---|
| 调仓 | 每 `rebalance_days`（默认 20）个交易日 |
| 持仓 | `max_positions`（默认 20）只等权 |
| 信号 | T 日收盘出信号 |
| 成交 | **T+1 开盘** |
| 买入价 | `open × (1 + slippage)` |
| 卖出价 | `open × (1 − slippage)` |
| 佣金 | `max(amount × 0.0001, 5)`，双边 |
| 印花税 | `amount × 0.0005`，仅卖出 |
| 不可成交 | 停牌 / 一字涨停买不进 / 一字跌停卖不出 / 无开盘价 / 不足 1 手 |
| 卖出失败 | 继续持有，下一次调仓再试 |
| 初始资金 | 100 万 |
| 收益 | 后复权 `close × adj_factor` |
| 基准 | `sh000300`（沪深300） |

### 6.2 防未来函数

- 代码层禁止 `.shift(-`；
- `detect_lookahead_usage`（L69）/ `assert_no_lookahead`（L88）静态扫描 `app/strategy/*.py`；
- 持仓决策只使用信号日 T 及以前数据；
- 成交只用 T+1 开盘（执行假设，不是未来函数）。

### 6.3 关键函数

| 函数 | 行号 | 说明 |
|---|---:|---|
| `BacktestParams` | L150 | 参数 dataclass，`from_dict` 支持短名与范围校验 |
| `phase_signal_dates` | L308 | 按绝对相位取信号日（纸面增量与全量一致） |
| `load_trading_days` | L329 | 交易日序列（db / fallback） |
| `load_price_book` | L386 | 预取区间行情（零查询模拟） |
| `load_benchmark_closes` | L424 | 基准收盘 |
| `build_pending_orders` | L492 | 次日待执行清单（纸面用） |
| `simulate_portfolio` | L577 | 核心资金流模拟，返回 `(equity_curve, trades, adjusted)` |
| `run_backtest` | L853 | 落 `backtest_run`，返回契约结构 |
| `_run_backtest_impl` | L918 | 写 running → 信号 → 模拟 → 指标 → success/failed |

### 6.4 组合状态与复权

- 状态 = 现金 + 持仓（adjusted units）；
- 买入：`units = shares / adj_factor`，市值 = `units × close × adj_factor`，天然包含分红送转；
- 卖出：现金 += 成交额 − 佣金 − 印花税；
- 每日净值 = 现金 + Σ(units × close × adj_factor)；
- 停牌/缺数据沿用上一有效市值；
- `adj_factor` 缺失时退化为原始价，并在 metrics 标 `adjusted=false`。

### 6.5 绩效指标（`metrics.py`）

[`strategy/metrics.py`](../../backend/app/strategy/metrics.py)

`METRIC_KEYS`（L31）：

```
total_return, annual_return, max_drawdown, sharpe, sortino, calmar,
win_rate, profit_loss_ratio, turnover, trade_count,
benchmark_return, excess_return, volatility
```

口径：

- 无风险利率 0；夏普/索提诺用日收益 ×√252；
- 最大回撤为负值；
- 胜率按 FIFO 完整买卖回合，买入含佣金、卖出扣佣金+印花税；
- 换手率 = (买入额+卖出额)/2/初始资金（区间单边，未年化）；
- 基准缺失时 `benchmark_return/excess_return = None`。

### 6.6 派生视图

`engine/backtest_view.build_backtest_extras`（L362）：月度收益、回撤曲线、20 日滚动超额、行业暴露。

---

## 7. 参数敏感性矩阵（`grid.py`）

文件：[`strategy/grid.py`](../../backend/app/strategy/grid.py)

| 项 | 规则 |
|---|---|
| 扫描轴 | `rebalance_days × max_positions` |
| 单矩阵格数上限 | `MAX_GRID_CELLS = 24` |
| 并发上限 | `GRID_CONCURRENCY = 2`（按事件循环惰性绑定信号量） |
| 单格 | 一条独立 `backtest_run`，复用同一回测引擎 |
| 复用 | `status='success'` 且 `(algo,start,end,参数)` 完全一致 → 只新增 `backtest_grid_cell` 关联 |
| 失败重试 | `POST /backtest/grid/{grid_id}/retry` 只重置 failed 格 |
| 矩阵身份 | `backtest_grid` 主表；同一回测可属于多个矩阵 |

函数：`normalize_axes` L74、`create_grid` L133、`execute_grid` L226、`requeue_failed_cells` L278、`get_grid` L331。

---

## 8. 纸面组合（`engine/paper.py`）

规则：

- 一策略一组合；
- 起点 = 策略首个实盘 run 交易日；
- 每天从起点到最新行情日整段重算（不做增量状态机，避免漂移）；
- 与回测完全相同的成交规则；
- 每日净值行保存收盘持仓与下一交易日待执行清单；
- 增量续算：水位 + 60 日重叠窗口，每周全量校准；
- 复用 `run_backtest(..., initial_state, phase_anchor, signal_from, capture_state_on)`。

API：`GET /api/v1/paper/portfolios`、`GET /api/v1/paper/portfolio/{algo_code}`、`POST .../sync`。

---

## 9. 选股/回测 API 映射

| 能力 | 端点 | 后端入口 |
|---|---|---|
| 策略清单 | `GET /algo/list` | `SPECS` / `CANDIDATE_SPECS` |
| 执行策略 | `GET /algo/{code}` | `runner.run_strategy` |
| 强制重跑 | `POST /algo/{code}/refresh` | `run_strategy(force=True)` |
| 历史 run | `GET /algo/{code}/runs` | `runner.list_runs` |
| run 明细 | `GET /algo/runs/{run_id}` | `runner.get_run` |
| 导出 | `GET /algo/{code}/export.csv` | 从 run 信号生成 CSV |
| 策略天梯 | `GET /algo/ladder` | `signal_perf.ladder_snapshot` |
| 共识 | `GET /algo/consensus` | `consensus.consensus_signals` |
| 回测 | `POST /backtest/{code}` | `backtest.run_backtest` |
| 矩阵 | `POST /backtest/{code}/grid` | `grid.create_grid` |
| 纸面 | `/paper/*` | `engine.paper` |

---

## 10. 新增策略的步骤

1. 在 `STRATEGY_DEFAULTS` 增加默认参数；
2. 在 `StrategySpec` 注册（先放 `CANDIDATE_SPECS`，通过回测后再移入 `SPECS`）；
3. 在 `_SORT_SPEC` 定义排序字段；
4. 在 `_METRIC_FIELDS` 定义信号依据字段；
5. 在 `_evaluate_strategy` / `_build_condition_specs` 增加条件与逐条解释；
6. 若需要窗口数据，在 `_enrich_history` / `_WINDOW_STRATEGIES` 注册；
7. 补单测：指标已知值、参数生效、run 可复现、无未来函数；
8. 用 `pipeline_cli.py backtest <code>` 跑统一窗口回测，达标后再启用。

---

## 11. 调试入口

```bash
# 单策略回测
python scripts/pipeline_cli.py backtest a3 --start 2025-10-21 --end 2026-10-21

# 跑一次正式策略
python scripts/pipeline_cli.py algos

# 只跑某日策略
python scripts/pipeline_cli.py algos --date 2026-11-14
```

前端：`/algo-center` 参数面板 → 执行 → Run 元信息 → 回测 → 参数矩阵。
