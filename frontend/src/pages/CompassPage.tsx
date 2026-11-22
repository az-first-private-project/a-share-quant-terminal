/**
 * 仓位罗盘（V9）
 * ================
 * 四个视图：今日决策 / 情景卡 / 复盘验证 / 规则与边界。
 * 产品原则：只做风险预算与决策约束，不给单一买卖结论，不展示收益率。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, type UseQueryResult } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import { ArrowRight, ChevronDown, Lock, ShieldCheck, X } from 'lucide-react'
import {
  CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import PageHeader from '../components/PageHeader'
import { EmptyState, ErrorState, SkeletonCard } from '../components/StateViews'
import { cn } from '../lib/utils'
import {
  clampPercent, filterScenarios, formatCap, pctTone, previewScenarios,
  regimeTone, statusLabel, statusTone,
} from '../lib/compass'
import {
  DEMO_MODE, fetchCompassDemoState, fetchCompassReview, fetchCompassRules,
  fetchCompassScenarios, fetchCompassToday, submitCheckAction,
  type CompassCandidate, type CompassReview, type CompassRules, type CompassScenario,
  type CompassToday, type DemoState,
} from '../api/compass'

const TABS = [
  { key: 'today', label: '今日决策' },
  { key: 'scenarios', label: '情景卡' },
  { key: 'review', label: '复盘验证' },
  { key: 'rules', label: '规则与边界' },
] as const

type TabKey = (typeof TABS)[number]['key']

const TEMP_BANDS = [
  { label: '冰点 0–19', color: 'var(--color-temp-0)' },
  { label: '清冷 20–39', color: 'var(--color-temp-1)' },
  { label: '平稳 40–59', color: 'var(--color-temp-2)' },
  { label: '温热 60–79', color: 'var(--color-temp-3)' },
  { label: '沸腾 80–100', color: 'var(--color-temp-4)' },
]

function eyebrow(text: string) {
  return <div className="eyebrow">{text}</div>
}

function SectionTitle({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="mb-4 flex items-baseline justify-between gap-4">
      <h2 className="text-[12px] font-semibold tracking-[0.12em] text-foreground">{title}</h2>
      {hint && <span className="text-[10px] text-muted-foreground">{hint}</span>}
    </div>
  )
}

export default function CompassPage() {
  const [tab, setTab] = useState<TabKey>('today')
  const [demoState, setDemoState] = useState<DemoState>('range')
  const [chainOpen, setChainOpen] = useState(false)

  const todayQuery = useQuery({
    queryKey: ['compass', 'today', demoState],
    queryFn: () => (DEMO_MODE ? fetchCompassDemoState(demoState) : fetchCompassToday()),
    staleTime: 60_000,
  })

  const scenariosQuery = useQuery({
    queryKey: ['compass', 'scenarios', todayQuery.data?.trade_date],
    queryFn: () => fetchCompassScenarios(todayQuery.data?.trade_date),
    enabled: tab === 'scenarios' && !DEMO_MODE && Boolean(todayQuery.data),
  })

  const reviewQuery = useQuery({
    queryKey: ['compass', 'review'],
    queryFn: () => fetchCompassReview(),
    enabled: tab === 'review',
  })

  const rulesQuery = useQuery({
    queryKey: ['compass', 'rules'],
    queryFn: () => fetchCompassRules(),
    enabled: tab === 'rules',
  })

  const scenarios = DEMO_MODE
    ? (todayQuery.data?.scenarios ?? [])
    : (scenariosQuery.data?.scenarios ?? todayQuery.data?.scenarios ?? [])

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.3 }}
      className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8"
    >
      <PageHeader
        eyebrow="Risk Compass · V9"
        title="仓位罗盘"
        description="每天回答四件事：做不做、最多做多大、往哪个方向、什么条件下撤。不给单一买卖结论，不承诺收益。"
        actions={DEMO_MODE ? (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] text-muted-foreground">演示：市场状态</span>
            <div className="flex border border-border">
              {(['attack', 'range', 'defense'] as const).map((state) => (
                <button
                  key={state}
                  type="button"
                  onClick={() => setDemoState(state)}
                  className={cn(
                    'border-r border-border px-3 py-1.5 text-[11px] last:border-r-0',
                    demoState === state
                      ? 'bg-primary text-primary-foreground'
                      : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  {state === 'attack' ? '进攻' : state === 'range' ? '震荡' : '防守'}
                </button>
              ))}
            </div>
          </div>
        ) : undefined}
      />

      <nav className="mt-6 flex gap-1 overflow-x-auto border-b border-border/70" aria-label="仓位罗盘子导航">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setTab(item.key)}
            aria-current={tab === item.key ? 'page' : undefined}
            className={cn(
              'relative shrink-0 px-3 py-2.5 text-[12px] transition-colors',
              tab === item.key ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {item.label}
            {tab === item.key && (
              <motion.span
                layoutId="compass-tab"
                className="absolute inset-x-2 -bottom-px h-px bg-brass"
              />
            )}
          </button>
        ))}
      </nav>

      {tab === 'today' && (
        <TodayView
          query={todayQuery}
          onOpenChain={() => setChainOpen(true)}
          onGoScenarios={() => setTab('scenarios')}
        />
      )}

      {tab === 'scenarios' && (
        <ScenariosView
          scenarios={scenarios}
          lockedIds={todayQuery.data?.locked_scenario_ids ?? []}
          loading={todayQuery.isPending || (scenariosQuery.isFetching && !DEMO_MODE)}
          error={scenariosQuery.error ?? todayQuery.error}
          onRetry={() => (DEMO_MODE ? todayQuery.refetch() : scenariosQuery.refetch())}
        />
      )}

      {tab === 'review' && <ReviewView query={reviewQuery} />}
      {tab === 'rules' && <RulesView query={rulesQuery} />}

      <ChainDrawer
        open={chainOpen}
        data={todayQuery.data}
        onClose={() => setChainOpen(false)}
      />
    </motion.div>
  )
}

/* ============================================================
 * 今日决策
 * ============================================================ */

function TodayView({
  query,
  onOpenChain,
  onGoScenarios,
}: {
  query: UseQueryResult<CompassToday, Error>
  onOpenChain: () => void
  onGoScenarios: () => void
}) {
  const actionMutation = useMutation({
    mutationFn: ({ checkId, action }: { checkId: string; action: 'DELAY' | 'DROP' }) =>
      submitCheckAction(checkId, action, `${checkId}:${action}:${Date.now()}`),
    onSuccess: () => { void query.refetch() },
  })

  if (query.isPending) {
    return (
      <div className="mt-6 space-y-3">
        <SkeletonCard lines={2} />
        <SkeletonCard lines={5} />
        <SkeletonCard lines={4} />
      </div>
    )
  }
  if (query.isError) {
    return <ErrorState className="mt-6" error={query.error} onRetry={() => query.refetch()} />
  }
  const data = query.data
  if (!data) return <EmptyState className="mt-6" title="暂无决策卡" />

  const initialPool = data.candidates.length + data.rejected.length
  const summary = data.budget.exec_cap > 0
    ? `温度 ${data.temperature.score ?? '—'} / 100，${data.temperature.band}区间。初筛 ${initialPool} 个板块，输出 ${data.candidates.length} 个。总预算上限 ${data.budget.market_cap}%，单板块不超过 ${data.budget.per_sector_cap}%。`
    : `温度 ${data.temperature.score ?? '—'} / 100，${data.temperature.band}区间。候选池为空，执行仓位归零。已连续空仓 ${data.idle_days} 个交易日。`

  return (
    <>
      <CheckBanner
        data={data}
        demo={DEMO_MODE}
        busy={actionMutation.isPending}
        error={actionMutation.error}
        onAction={(checkId, action) => actionMutation.mutate({ checkId, action })}
      />

      <section className="grid gap-10 border-b border-border/70 py-9 lg:grid-cols-[1.35fr_.65fr]">
        <div>
          {eyebrow(`今日执行结论 · 依据 ${data.trade_date} 收盘`)}
          <div className="mt-3 flex items-end gap-2">
            <span
              className={cn(
                'font-mono leading-[0.9] tracking-[-0.06em]',
                data.budget.exec_cap > 0 ? 'text-[72px] sm:text-[88px]' : 'text-[52px] sm:text-[64px]',
              )}
            >
              {formatCap(data.budget.exec_cap)}
            </span>
          </div>
          <p className="mt-4 max-w-xl text-[13px] leading-7 text-muted-foreground">{summary}</p>
          <BudgetBar marketCap={data.budget.market_cap} perSector={data.budget.per_sector_cap} />
        </div>

        <div className="lg:border-l lg:border-border/70 lg:pl-8">
          {eyebrow('市场状态')}
          <div className={cn('mt-2 text-[34px] font-semibold leading-none tracking-[-0.03em]', regimeTone(data.regime.tier))}>
            {data.regime.label}
          </div>
          <div className="mt-6 grid grid-cols-2 gap-5 border-t border-border/60 pt-5">
            <MiniStat label="市场温度" value={data.temperature.score ?? '—'} suffix="/100" />
            <MiniStat label="输出板块" value={data.candidates.length} suffix="个" />
            <MiniStat label="单板块上限" value={`${data.budget.per_sector_cap}%`} />
            <MiniStat label="空仓天数" value={data.idle_days} suffix="日" />
          </div>
          <button type="button" onClick={onOpenChain} className="mt-6 flex items-center gap-1.5 border-b border-border pb-0.5 text-[12px] hover:border-foreground">
            查看推导链 <ArrowRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </section>

      <section className="grid gap-10 border-b border-border/70 py-9 lg:grid-cols-[1.5fr_.9fr]">
        <DashboardPanel data={data} />
        <aside className="lg:border-l lg:border-border/70 lg:pl-8">
          <SectionTitle title="情景卡 · 当前触发" hint="只给条件与动作" />
          <div>
            {previewScenarios(data.scenarios).map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={onGoScenarios}
                className="grid w-full grid-cols-[44px_1fr_64px] items-start gap-3 border-b border-border/60 py-3 text-left transition-colors hover:bg-muted/20"
              >
                <span className="pt-0.5 font-mono text-[9px] text-muted-foreground">{item.id}</span>
                <span>
                  <span className="block text-[13px]">{item.title}</span>
                  <span className="mt-0.5 block font-mono text-[10px] leading-5 text-muted-foreground">{item.condition}</span>
                </span>
                <span className={cn('pt-0.5 text-right text-[10px]', item.status === 'TRIGGERED' ? 'font-semibold text-warning' : 'text-muted-foreground')}>
                  {statusLabel(item.status)}
                </span>
              </button>
            ))}
          </div>
          <p className="mt-4 border-t border-dashed border-border pt-3 text-[10px] leading-6 text-muted-foreground">
            只给条件与动作，不给“买哪个”的结论。
          </p>
        </aside>
      </section>

      <section className="py-9">
        <SectionTitle
          title="候选池 · 通过五道门槛"
          hint={`全量评估 ${data.candidates.length + data.rejected.length} 个 · 输出 ${data.candidates.length} 个`}
        />
        <CandidateTable data={data} />
      </section>
    </>
  )
}

function CheckBanner({
  data,
  demo,
  busy,
  error,
  onAction,
}: {
  data: CompassToday
  demo: boolean
  busy: boolean
  error: Error | null
  onAction: (checkId: string, action: 'DELAY' | 'DROP') => void
}) {
  const status = data.check.status
  const tone = status === 'PASS'
    ? 'border-success/50 bg-success/5'
    : status === 'IDLE'
      ? 'border-border bg-muted/20'
      : status === 'WARN' || status === 'DELAYED' || status === 'SUSPENDED'
        ? 'border-warning/50 bg-warning/10'
        : 'border-border bg-muted/20'
  const actionable = (data.check.items ?? []).find(
    (item) => item.result === 'WARN' && !item.final_status,
  )
  return (
    <div className={cn('mt-5 flex flex-col gap-3 border-l-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between', tone)}>
      <div>
        <div className="text-[12px] font-semibold">{data.check.title}</div>
        <div className="mt-0.5 text-[11px] text-muted-foreground">{data.check.text}</div>
        {error && <div className="mt-1 text-[10px] text-destructive">{error.message}</div>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {data.check.items && data.check.items.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {data.check.items.map((item) => (
              <span key={item.check_id} className="border border-border/70 bg-background/60 px-2 py-1 font-mono text-[10px]">
                {item.etf_code} {item.open_gap === null ? '—' : `${item.open_gap > 0 ? '+' : ''}${item.open_gap}%`}
              </span>
            ))}
          </div>
        )}
        {actionable && !demo && (
          <div className="flex gap-1.5">
            <button
              type="button"
              disabled={busy}
              onClick={() => onAction(actionable.check_id, 'DELAY')}
              className="border border-foreground bg-foreground px-2.5 py-1.5 text-[10px] text-background disabled:opacity-50"
            >
              延迟至 10:00
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onAction(actionable.check_id, 'DROP')}
              className="border border-border bg-background px-2.5 py-1.5 text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
            >
              放弃本次
            </button>
          </div>
        )}
        {actionable && demo && (
          <span className="text-[10px] text-muted-foreground">演示模式不可操作</span>
        )}
      </div>
    </div>
  )
}

function MiniStat({ label, value, suffix }: { label: string; value: string | number; suffix?: string }) {
  return (
    <div>
      <div className="text-[9px] tracking-[0.08em] text-muted-foreground">{label}</div>
      <div className="mt-1 font-mono text-[21px] leading-none">
        {value}
        {suffix && <span className="ml-0.5 text-[10px] text-muted-foreground">{suffix}</span>}
      </div>
    </div>
  )
}

function BudgetBar({ marketCap, perSector }: { marketCap: number; perSector: number }) {
  return (
    <div className="mt-8 max-w-xl">
      <div className="flex items-center justify-between text-[9px] font-mono text-muted-foreground">
        <span>0%</span>
        <span>仓位预算上限（不代表已使用）</span>
        <span>100%</span>
      </div>
      <div className="relative mt-2 h-2 bg-muted">
        <motion.div
          className="h-full bg-brass"
          initial={{ width: 0 }}
          animate={{ width: `${clampPercent(marketCap)}%` }}
          transition={{ duration: 0.5, ease: [0.2, 0.8, 0.2, 1] }}
        />
        {perSector > 0 && (
          <div className="absolute -top-1 h-4 w-px bg-foreground" style={{ left: `${clampPercent(perSector)}%` }}>
            <span className="absolute -top-5 left-1/2 -translate-x-1/2 whitespace-nowrap font-mono text-[9px] text-muted-foreground">
              单板块 {perSector}%
            </span>
          </div>
        )}
      </div>
      <div className="mt-2 text-[10px] text-muted-foreground">
        市场层预算 <b className="font-mono text-foreground">{marketCap}%</b> · 单板块上限{' '}
        <b className="font-mono text-foreground">{perSector}%</b> · 开盘后由用户分配，系统不代下单
      </div>
    </div>
  )
}

function DashboardPanel({ data }: { data: CompassToday }) {
  const score = data.temperature.score
  const flags = useMemo(() => {
    const rows: string[] = []
    data.candidates.forEach((cand) => {
      if (cand.sentiment_percentile !== null && cand.sentiment_percentile >= 90) {
        rows.push(`${cand.sector_name} 情绪分位 ${cand.sentiment_percentile}，进入过热区`)
      }
      if (cand.breadth_below_ma20 !== null && cand.breadth_below_ma20 !== undefined && cand.breadth_below_ma20 > 0.6) {
        rows.push(`${cand.sector_name} 成分股跌破 MA20 占比 ${(cand.breadth_below_ma20 * 100).toFixed(0)}%`)
      }
    })
    data.rejected.forEach((row) => {
      if (row.reason_code === 'CORRELATION_DUPLICATE') {
        rows.push(`${row.sector_name} 因高相关被剔除：${row.reason_text}`)
      }
    })
    data.empty_reasons.forEach((row) => rows.push(row.text))
    return rows.slice(0, 4)
  }, [data])

  return (
    <div>
      <SectionTitle title="风险仪表盘" hint="只描述状态，不给方向" />
      <div className="relative mt-7">
        <div className="flex h-1.5 overflow-hidden">
          {TEMP_BANDS.map((band) => (
            <div key={band.label} style={{ width: '20%', background: band.color }} />
          ))}
        </div>
        {score !== null && (
          <>
            <div
              className="absolute top-1/2 h-5 w-0.5 -translate-y-1/2 bg-foreground"
              style={{ left: `calc(${clampPercent(score)}% - 1px)` }}
            />
            <span
              className="absolute -top-6 -translate-x-1/2 bg-foreground px-1.5 py-0.5 font-mono text-[10px] text-background"
              style={{ left: `${clampPercent(score)}%` }}
            >
              {score}
            </span>
          </>
        )}
      </div>
      <div className="mt-3 flex text-[9px] text-muted-foreground">
        {TEMP_BANDS.map((band) => (
          <span key={band.label} className="w-1/5">{band.label}</span>
        ))}
      </div>

      <div className="mt-8 text-[10px] tracking-[0.1em] text-muted-foreground">板块情绪分位 · 250 日滚动</div>
      <div className="mt-2">
        {data.candidates.length > 0 ? data.candidates.map((cand) => (
          <div key={cand.sector_code} className="grid grid-cols-[88px_1fr_38px_76px] items-center gap-3 border-b border-dashed border-border/60 py-2.5">
            <span className="truncate text-[12px]">{cand.sector_name}</span>
            <div className="h-1 bg-muted">
              <div
                className={cn('h-full', cand.sentiment_percentile !== null && cand.sentiment_percentile >= 90 ? 'bg-up' : 'bg-foreground')}
                style={{ width: `${clampPercent(cand.sentiment_percentile ?? 0)}%` }}
              />
            </div>
            <span className={cn('text-right font-mono text-[12px]', pctTone(cand.sentiment_percentile))}>
              {cand.sentiment_percentile ?? '—'}
            </span>
            <span className="text-right text-[10px] text-muted-foreground">{cand.crowding_level ?? ''}</span>
          </div>
        )) : (
          <p className="py-4 text-[12px] text-muted-foreground">候选池为空，暂无可观察板块。</p>
        )}
      </div>

      {flags.length > 0 && (
        <div className="mt-5">
          {flags.map((flag) => (
            <div key={flag} className="flex gap-2.5 py-1.5 text-[11px] leading-6 text-muted-foreground">
              <span className="mt-2.5 h-1 w-1 shrink-0 bg-brass" />
              <span>{flag}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function CandidateTable({ data }: { data: CompassToday }) {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const toggle = (code: string) => {
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(code)) next.delete(code)
      else next.add(code)
      return next
    })
  }

  if (data.candidates.length === 0) {
    return (
      <div>
        <div className="flex items-end gap-3">
          <span className="font-mono text-[64px] leading-none tracking-[-0.04em]">0</span>
          <span className="pb-2 text-[12px] text-muted-foreground">个板块通过五道门槛</span>
        </div>
        <p className="mt-4 text-[14px]">候选池为空。这不是数据故障，是今天的结论。</p>
        <ul className="mt-4 max-w-xl">
          {data.empty_reasons.map((row) => (
            <li key={row.code} className="relative border-b border-dashed border-border/60 py-2 pl-5 text-[12px] text-muted-foreground">
              <span className="absolute left-0 text-brass">—</span>{row.text}
            </li>
          ))}
        </ul>
        <p className="mt-4 text-[11px] text-muted-foreground">
          空仓第 <b className="font-mono text-foreground">{data.idle_days}</b> 个交易日 · 规则要求：市场状态为防守且候选池为空时，不新开仓。
        </p>
      </div>
    )
  }

  const grid = 'grid grid-cols-[34px_minmax(140px,1.4fr)_repeat(4,62px)_78px_92px_56px] gap-2.5 items-center'

  return (
    <div>
      <div className="overflow-x-auto">
        <div className="min-w-[760px]">
          <div className={cn(grid, 'border-b border-border/80 px-2 pb-2 text-[9px] tracking-[0.08em] text-muted-foreground')}>
            <span>#</span><span>板块</span>
            <span className="text-right">综合</span><span className="text-right">技术</span>
            <span className="text-right">情绪</span><span className="text-right">消息</span>
            <span className="text-right">情绪分位</span><span>对应 ETF</span><span>状态</span>
          </div>
          {data.candidates.map((cand, index) => {
            const expanded = open.has(cand.sector_code)
            return (
              <div key={cand.sector_code}>
                <button
                  type="button"
                  onClick={() => toggle(cand.sector_code)}
                  aria-expanded={expanded}
                  className={cn(grid, 'w-full border-b border-border/60 px-2 py-3.5 text-left transition-colors hover:bg-muted/20', expanded && 'bg-brass/5')}
                >
                  <span className="font-mono text-[10px] text-muted-foreground">{cand.rank ?? index + 1}</span>
                  <span>
                    <span className="block text-[13px] font-medium">{cand.sector_name}</span>
                    <span className="mt-0.5 block font-mono text-[9px] text-muted-foreground">{cand.sector_code}</span>
                  </span>
                  <NumberCell value={cand.composite} />
                  <NumberCell value={cand.technical} dim />
                  <NumberCell value={cand.sentiment} dim />
                  <NumberCell value={cand.news} dim />
                  <span className={cn('text-right font-mono text-[12px]', pctTone(cand.sentiment_percentile))}>
                    {cand.sentiment_percentile ?? '—'}
                  </span>
                  <span className="font-mono text-[11px]">{cand.etf_code ?? '—'}</span>
                  <span className="text-[10px] text-brass">关注</span>
                </button>
                {expanded && <CandidateDetail cand={cand} />}
              </div>
            )
          })}
        </div>
      </div>

      {data.rejected.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer list-none text-[10px] text-muted-foreground hover:text-foreground">
            未进入输出 {data.rejected.length} 个 <ChevronDown className="inline h-3 w-3" />
          </summary>
          <div className="mt-2">
            {data.rejected.map((row) => (
              <div key={row.sector_code} className="grid grid-cols-[minmax(120px,1fr)_60px_1.4fr] gap-4 border-b border-dashed border-border/60 py-2 text-[11px]">
                <span>{row.sector_name}</span>
                <span className="text-right font-mono text-muted-foreground">{row.composite?.toFixed(1) ?? '—'}</span>
                <span className="text-[10px] text-muted-foreground">{row.reason_text}</span>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  )
}

function NumberCell({ value, dim = false }: { value: number | null; dim?: boolean }) {
  return (
    <span className={cn('text-right font-mono text-[12px]', dim && 'text-muted-foreground')}>
      {value === null ? '—' : value.toFixed(1)}
    </span>
  )
}

function CandidateDetail({ cand }: { cand: CompassCandidate }) {
  return (
    <div className="border-b border-border/60 bg-brass/5 px-2 py-5 pl-10">
      <div className="grid gap-7 md:grid-cols-2">
        <div>
          <div className="text-[9px] tracking-[0.1em] text-muted-foreground">可归因理由</div>
          <ul className="mt-2 space-y-1.5">
            {(cand.reasons ?? []).map((reason) => (
              <li key={reason} className="relative pl-3.5 text-[11px] leading-6 text-muted-foreground">
                <span className="absolute left-0 top-2.5 h-1 w-1 bg-border" />{reason}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <div className="text-[9px] tracking-[0.1em] text-muted-foreground">门槛校验</div>
          <ul className="mt-2 space-y-1.5">
            {(cand.checks ?? []).map((check) => (
              <li key={check} className="relative pl-3.5 text-[11px] leading-6 text-muted-foreground">
                <span className="absolute left-0 top-2.5 h-1 w-1 bg-success" />{check}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}

/* ============================================================
 * 情景卡
 * ============================================================ */

function ScenariosView({
  scenarios,
  lockedIds,
  loading,
  error,
  onRetry,
}: {
  scenarios: CompassScenario[]
  lockedIds: string[]
  loading: boolean
  error: unknown
  onRetry: () => void
}) {
  const [filter, setFilter] = useState<'all' | 'on' | 'off'>('all')
  const [open, setOpen] = useState<Set<string>>(new Set(['SC-01']))
  const filtered = filterScenarios(scenarios, filter)

  const toggle = (id: string) => {
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <section className="py-8">
      <div className="flex flex-wrap items-end justify-between gap-4 border-b border-border/70 pb-5">
        <div>
          {eyebrow('决策约束')}
          <h2 className="mt-2 text-[28px] font-semibold tracking-[-0.02em]">6 张情景卡</h2>
          <p className="mt-2 max-w-2xl text-[12px] leading-6 text-muted-foreground">
            每张卡包含触发条件、执行动作、失效条件与依据。系统不替你决策，只把决策变成可执行的规则。
          </p>
        </div>
        <div className="flex border border-border">
          {([['all', '全部'], ['on', '已触发'], ['off', '待命']] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setFilter(key)}
              className={cn(
                'border-r border-border px-3 py-1.5 text-[11px] last:border-r-0',
                filter === key ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {loading && <div className="mt-5 space-y-3"><SkeletonCard lines={4} /></div>}
      {!loading && Boolean(error) && <ErrorState className="mt-5" error={error} onRetry={onRetry} />}

      {!loading && !error && (
        <div className="mt-2">
          {filtered.map((item) => {
            const expanded = open.has(item.id)
            return (
              <div key={item.id} className="border-b border-border/70">
                <button
                  type="button"
                  onClick={() => toggle(item.id)}
                  aria-expanded={expanded}
                  className="grid w-full grid-cols-[56px_1fr_78px_20px] items-start gap-3 py-4 text-left transition-colors hover:bg-muted/20"
                >
                  <span className="pt-0.5 font-mono text-[10px] text-muted-foreground">{item.id}</span>
                  <span>
                    <span className="block text-[14px] font-medium">{item.title}</span>
                    <span className="mt-1 block font-mono text-[10px] leading-5 text-muted-foreground">{item.condition}</span>
                  </span>
                  <span className={cn('border px-2 py-0.5 text-right text-[10px]', statusTone(item.status))}>
                    {statusLabel(item.status)}
                  </span>
                  <span className={cn('text-right text-[15px] text-muted-foreground transition-transform', expanded && 'rotate-45')}>+</span>
                </button>
                <AnimatePresence initial={false}>
                  {expanded && (
                    <motion.div
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: 'auto', opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.25 }}
                      className="overflow-hidden"
                    >
                      <div className="grid gap-5 pb-6 pl-[72px] pr-2 md:grid-cols-2">
                        <Field label="执行动作" value={item.action} />
                        <Field label="失效条件" value={item.invalid} />
                        <Field label="依据" value={item.basis} />
                        <Field label="当前证据" value={formatEvidence(item.evidence)} mono />
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            )
          })}

          {filtered.length === 0 && (
            <p className="py-8 text-center text-[12px] text-muted-foreground">当前筛选下没有情景卡。</p>
          )}

          {lockedIds.length > 0 && (
            <div className="mt-5 border border-dashed border-border px-4 py-4">
              <div className="flex items-center gap-2 text-[12px]">
                <Lock className="h-3.5 w-3.5 text-brass" />
                另有 {lockedIds.length} 张情景卡属于专业版
              </div>
              <p className="mt-1 text-[10px] text-muted-foreground">
                {lockedIds.join(' / ')} · 升级后可查看完整条件、动作与失效条件。
              </p>
            </div>
          )}
        </div>
      )}
    </section>
  )
}

function Field({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[9px] tracking-[0.1em] text-muted-foreground">{label}</div>
      <p className={cn('mt-1.5 text-[12px] leading-6 text-muted-foreground', mono && 'font-mono text-[10px]')}>{value}</p>
    </div>
  )
}

function formatEvidence(evidence?: Record<string, unknown>): string {
  if (!evidence || Object.keys(evidence).length === 0) return '—'
  try {
    return JSON.stringify(evidence, null, 0)
  } catch {
    return '—'
  }
}

/* ============================================================
 * 复盘验证
 * ============================================================ */

type ReviewQuery = UseQueryResult<CompassReview, Error>

function ReviewView({ query }: { query: ReviewQuery }) {
  if (query.isPending) return <div className="mt-6 space-y-3"><SkeletonCard lines={4} /><SkeletonCard lines={6} /></div>
  if (query.isError) return <ErrorState className="mt-6" error={query.error} onRetry={() => query.refetch()} />
  const data = query.data
  if (!data) return <EmptyState className="mt-6" title="暂无复盘数据" />

  const chartData = data.chart.dates.map((date, index) => ({
    date,
    rule: data.chart.drawdown_rule[index] ?? 0,
    loose: data.chart.drawdown_unconstrained[index] ?? 0,
  }))

  return (
    <section className="py-8">
      <div className="flex flex-wrap items-end justify-between gap-4 border-b border-border/70 pb-5">
        <div>
          {eyebrow('可证伪')}
          <h2 className="mt-2 text-[28px] font-semibold tracking-[-0.02em]">复盘验证</h2>
          <p className="mt-2 max-w-2xl text-[12px] leading-6 text-muted-foreground">
            只统计回撤、错误交易次数与空仓避损，不展示收益率。失效记录全部公开，不删改。
          </p>
        </div>
        <span className="text-[10px] text-muted-foreground">等权纸面组合 · 含双边 0.15% 摩擦</span>
      </div>
      {data.method_note && (
        <p className="mt-3 text-[10px] leading-5 text-muted-foreground">{data.method_note}</p>
      )}

      {data.sample_insufficient ? (
        <EmptyState
          className="mt-6"
          title="样本不足"
          description="纸面组合不足 60 个交易日，暂不展示统计值，避免用短窗口制造结论。"
        />
      ) : (
        <>
          <div className="mt-6 grid grid-cols-2 gap-px border-y border-border bg-border lg:grid-cols-4">
            <ReviewStat label="最大回撤" value={fmtPct(data.stats.max_drawdown_rule)} sub={`${data.label_unconstrained ?? '无约束'} ${fmtPct(data.stats.max_drawdown_unconstrained)}`} />
            <ReviewStat
              label="错误交易次数"
              value={data.violations_available === false ? '—' : String(data.stats.violations_rule)}
              sub={data.violations_available === false ? '待接入情景卡判定' : `${data.label_unconstrained ?? '无约束'} ${data.stats.violations_unconstrained}`}
            />
            <ReviewStat label="空仓避损" value={fmtPct(data.stats.empty_avoidance, true)} sub={`空仓 ${data.stats.empty_days ?? 0} 天`} />
            <ReviewStat label="错过涨幅" value={fmtPct(data.stats.missed_upside, true)} sub={`净避损 ${fmtPct(data.stats.net_avoidance, true)}`} />
          </div>

          <div className="mt-8">
            <div className="mb-2 flex items-center justify-between gap-4">
              <div className="flex items-center gap-5 text-[10px] text-muted-foreground">
                <span className="flex items-center gap-2"><span className="h-0.5 w-4 bg-foreground" />{data.label_rule ?? '遵守规则'}</span>
                <span className="flex items-center gap-2"><span className="h-px w-4 border-t border-dashed border-muted-foreground" />{data.label_unconstrained ?? '无约束'}</span>
              </div>
              <span className="text-[10px] text-muted-foreground">纵轴：最大回撤 %</span>
            </div>
            <div className="h-[280px] w-full">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData} margin={{ top: 8, right: 16, left: -12, bottom: 0 }}>
                  <CartesianGrid stroke="var(--color-grid)" strokeDasharray="2 4" vertical={false} />
                  <XAxis dataKey="date" tick={{ fontSize: 10, fill: 'var(--color-muted-foreground)' }} tickLine={false} axisLine={false} minTickGap={24} />
                  <YAxis domain={[-20, 0]} tick={{ fontSize: 10, fill: 'var(--color-muted-foreground)' }} tickLine={false} axisLine={false} tickFormatter={(value) => `${value}%`} />
                  <Tooltip
                    contentStyle={{ background: 'var(--color-card)', border: '1px solid var(--color-border)', fontSize: 11 }}
                    labelStyle={{ color: 'var(--color-muted-foreground)' }}
                  />
                  <ReferenceLine y={0} stroke="var(--color-border)" />
                  <Line type="monotone" dataKey="rule" name={data.label_rule ?? '遵守规则'} stroke="var(--color-foreground)" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="loose" name={data.label_unconstrained ?? '无约束'} stroke="var(--color-muted-foreground)" strokeWidth={1.2} strokeDasharray="4 4" dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className="mt-8">
            <SectionTitle title="信号失效记录" hint="不删除，不归因于“用户没执行”" />
            {data.failures.length === 0 ? (
              <p className="py-4 text-[12px] text-muted-foreground">暂无记录。</p>
            ) : data.failures.map((row) => (
              <div key={`${row.date}-${row.type}`} className="grid gap-2 border-b border-border/60 py-3 text-[11px] md:grid-cols-[92px_1.1fr_1.4fr] md:gap-5">
                <span className="font-mono text-muted-foreground">{row.date}</span>
                <span>
                  {row.type}
                  {row.impact_pct !== null && <span className="mt-0.5 block text-[9px] text-warning">影响 {row.impact_pct}%</span>}
                </span>
                <span className="text-muted-foreground">{row.root_cause}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  )
}

function fmtPct(value: number | null, signed = false): string {
  if (value === null) return '—'
  const prefix = signed && value > 0 ? '+' : ''
  return `${prefix}${value.toFixed(1)}%`
}

function ReviewStat({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="bg-background px-4 py-5">
      <div className="text-[10px] tracking-[0.06em] text-muted-foreground">{label}</div>
      <div className="mt-2 font-mono text-[28px] leading-none tracking-[-0.03em]">{value}</div>
      <div className="mt-1.5 text-[10px] text-muted-foreground">{sub}</div>
    </div>
  )
}

/* ============================================================
 * 规则与边界
 * ============================================================ */

type RulesQuery = UseQueryResult<CompassRules, Error>

function RulesView({ query }: { query: RulesQuery }) {
  if (query.isPending) return <div className="mt-6 space-y-3"><SkeletonCard lines={5} /></div>
  if (query.isError) return <ErrorState className="mt-6" error={query.error} onRetry={() => query.refetch()} />
  const rules = query.data
  if (!rules) return <EmptyState className="mt-6" title="暂无规则内容" />

  return (
    <section className="py-8">
      <div className="border-b border-border/70 pb-5">
        {eyebrow('产品边界')}
        <h2 className="mt-2 text-[28px] font-semibold tracking-[-0.02em]">规则与边界</h2>
        <p className="mt-2 max-w-2xl text-[12px] leading-6 text-muted-foreground">
          这是一个风险预算工具，不是收益承诺工具。下面写清楚我们做什么、不做什么、凭什么收费。
        </p>
      </div>

      <div className="mt-6 grid gap-px border-y border-border bg-border lg:grid-cols-4">
        {[
          ['15:30', '数据落地', '行情、资金流、两融、北向、ETF 折溢价'],
          ['16:20', '生成决策卡', '四层判定 + 五道门槛 + 情景状态'],
          ['次日 09:35', '开盘校验', 'ETF 偏离 > 2% 触发延迟'],
          ['收盘后', '回填复盘', '只记回撤、错误次数、避损'],
        ].map(([time, title, text]) => (
          <div key={time} className="bg-background px-4 py-4">
            <span className="font-mono text-[10px] text-brass">{time}</span>
            <div className="mt-1.5 text-[13px] font-medium">{title}</div>
            <p className="mt-1 text-[10px] leading-5 text-muted-foreground">{text}</p>
          </div>
        ))}
      </div>

      <div className="mt-8 grid gap-10 lg:grid-cols-[1.05fr_.95fr]">
        <div>
          <SectionTitle title="分层与权限" />
          <table className="w-full border-collapse text-left text-[11px]">
            <thead>
              <tr className="border-b border-border/70 text-[9px] tracking-[0.1em] text-muted-foreground">
                <th className="py-2 font-normal">层级</th>
                <th className="py-2 font-normal">免费版</th>
                <th className="py-2 font-normal">专业版</th>
              </tr>
            </thead>
            <tbody>
              {[
                ['风险仪表盘', '全部', '全部'],
                ['情景卡', '只读 SC-01 / SC-03', '6 张 + 触发推送'],
                ['复盘验证', '月度摘要', '全量 + 导出'],
                ['历史决策卡', '7 天', '全部'],
              ].map(([layer, free, pro]) => (
                <tr key={layer} className="border-b border-border/60">
                  <td className="py-2.5">{layer}</td>
                  <td className="py-2.5 text-muted-foreground">{free}</td>
                  <td className="py-2.5 text-muted-foreground">{pro}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div>
          <SectionTitle title="我们承诺" />
          <ul className="space-y-2">
            {rules.promises.map((item) => (
              <li key={item} className="relative border-b border-dashed border-border/60 py-2 pl-4 text-[12px] leading-6 text-muted-foreground">
                <span className="absolute left-0 top-4 h-1 w-1 bg-brass" />{item}
              </li>
            ))}
          </ul>
          <SectionTitle title="我们不做" hint="硬边界" />
          <ul className="space-y-1.5">
            {rules.not_do.map((item) => (
              <li key={item} className="flex items-center gap-2 text-[12px] text-muted-foreground">
                <X className="h-3 w-3 text-muted-foreground/60" />{item}
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="mt-10 border-t border-border pt-7">
        <ShieldCheck className="h-5 w-5 text-brass" />
        <p className="mt-4 max-w-3xl text-[22px] leading-[1.6] tracking-[-0.01em]">“{rules.criterion}”</p>
        <p className="mt-3 text-[10px] text-muted-foreground">— 正反双方共同认可的判据 · 规则版本 {rules.rule_version}</p>
      </div>
    </section>
  )
}

/* ============================================================
 * 推导抽屉
 * ============================================================ */

function ChainDrawer({
  open,
  data,
  onClose,
}: {
  open: boolean
  data?: CompassToday
  onClose: () => void
}) {
  return (
    <AnimatePresence>
      {open && data && (
        <>
          <motion.div
            className="fixed inset-0 z-50 bg-foreground/30"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
          />
          <motion.aside
            className="fixed inset-y-0 right-0 z-50 w-[min(460px,92vw)] overflow-auto border-l border-border bg-background p-6 shadow-2xl"
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ duration: 0.35, ease: [0.2, 0.8, 0.2, 1] }}
            role="dialog"
            aria-label="规则推导链"
          >
            <div className="flex items-start justify-between gap-4 border-b border-border pb-4">
              <div>
                {eyebrow('规则推导')}
                <h3 className="mt-2 text-[22px] font-semibold tracking-[-0.02em]">
                  为什么是 {data.budget.exec_cap > 0 ? `${data.budget.exec_cap}%` : '空仓'}
                </h3>
              </div>
              <button type="button" onClick={onClose} className="border border-border p-1.5 text-muted-foreground hover:text-foreground" aria-label="关闭">
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            <div>
              {data.derivation.map((step, index) => (
                <div
                  key={step.step}
                  className={cn(
                    'grid grid-cols-[28px_1fr] gap-3 border-b border-dashed border-border/70 py-4',
                    index === data.derivation.length - 1 && 'mt-2 border-b-0 bg-brass/10 px-3',
                  )}
                >
                  <span className="pt-0.5 font-mono text-[9px] text-muted-foreground">{String(step.step).padStart(2, '0')}</span>
                  <div>
                    <div className="text-[9px] tracking-[0.1em] text-muted-foreground">{step.key}</div>
                    <div className={cn('mt-0.5 font-mono text-[16px] tracking-[-0.02em]', index === data.derivation.length - 1 && 'font-semibold text-brass')}>
                      {step.value}
                    </div>
                    <p className="mt-1 text-[10px] leading-5 text-muted-foreground">{step.note}</p>
                  </div>
                </div>
              ))}
            </div>
            <p className="mt-5 border-t border-border pt-4 text-[10px] leading-6 text-muted-foreground">
              以上为规则推导过程，不构成买卖建议。执行决策与结果由用户自行承担。
              <br />
              signal_version {data.signal_version} · rule_version {data.rule_version}
            </p>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  )
}
