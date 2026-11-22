/**
 * 仓位罗盘 API（V9）
 * 默认 professional；`X-Compass-Tier: free` 查看脱敏视图。
 */
import { request } from './client'
import { DEMO_MODE } from './demo'

export type CompassTier = 'free' | 'pro'
export type RegimeTier = 'attack' | 'range' | 'defense'
export type DemoState = 'attack' | 'range' | 'defense'

export interface CompassCandidate {
  rank?: number
  sector_code: string
  sector_name: string
  composite: number | null
  technical: number | null
  sentiment: number | null
  news: number | null
  sentiment_percentile: number | null
  crowding_level?: string
  etf_code?: string | null
  etf_name?: string | null
  etf_aum?: number | null
  etf_adv20?: number | null
  breadth_below_ma20?: number | null
  breadth_below_count?: number | null
  breadth_valid_members?: number | null
  reasons?: string[]
  checks?: string[]
  tag?: string
}

export interface CompassRejected {
  sector_code: string
  sector_name: string
  composite: number | null
  reason_code: string
  reason_text: string
}

export interface CompassScenario {
  id: string
  title: string
  condition: string
  action: string
  invalid: string
  basis: string
  status: string
  evidence?: Record<string, unknown>
}

export interface CompassCheckItem {
  check_id: string
  etf_code: string
  open_gap: number | null
  recheck_gap?: number | null
  result: string
  user_action?: string | null
  final_status?: string | null
  user_override?: boolean
}

export interface CompassCheck {
  status: string
  title: string
  text: string
  items?: CompassCheckItem[]
}

export interface CompassDerivation {
  step: number
  key: string
  value: string
  note: string
}

export interface CompassToday {
  card_id: string
  trade_date: string
  rule_version: string
  signal_version: string
  regime: { state: string; tier: RegimeTier; label: string }
  temperature: { score: number | null; band: string }
  budget: { market_cap: number; exec_cap: number; per_sector_cap: number }
  candidates: CompassCandidate[]
  rejected: CompassRejected[]
  empty_reasons: { code: string; text: string; count?: number }[]
  derivation: CompassDerivation[]
  check: CompassCheck
  scenarios: CompassScenario[]
  idle_days: number
  locked_scenario_ids?: string[]
  freshness: { dataset: string; status: string; last_trade_date?: string | null }[]
}

export interface CompassScenarios {
  trade_date: string
  card_id: string
  scenarios: CompassScenario[]
  locked_scenario_ids: string[]
  action_priority: string | null
}

export interface CompassReview {
  sample_insufficient: boolean
  source?: string
  violations_available?: boolean
  label_rule?: string
  label_unconstrained?: string
  method_note?: string
  stats: {
    max_drawdown_rule: number | null
    max_drawdown_unconstrained: number | null
    violations_rule: number
    violations_unconstrained: number
    empty_avoidance: number | null
    missed_upside: number | null
    net_avoidance: number | null
    empty_days: number | null
  }
  chart: {
    dates: string[]
    drawdown_rule: number[]
    drawdown_unconstrained: number[]
  }
  failures: {
    date: string
    type: string
    impact_pct: number | null
    root_cause: string
    fix_version: string | null
    status: string
  }[]
  upgrade_required?: boolean
}

export interface CompassRules {
  rule_version: string
  tier_labels: Record<string, string>
  tiers: Record<string, string[]>
  promises: string[]
  not_do: string[]
  criterion: string
  scenario_ids: string[]
}

function tierHeaders(tier: CompassTier): Record<string, string> | undefined {
  return tier === 'free' ? { 'X-Compass-Tier': 'free' } : undefined
}

export function fetchCompassToday(
  tradeDate?: string,
  tier: CompassTier = 'pro',
): Promise<CompassToday> {
  return request<CompassToday>('/compass/today', {
    query: { trade_date: tradeDate ?? undefined },
    headers: tierHeaders(tier),
  })
}

export function fetchCompassScenarios(tradeDate?: string): Promise<CompassScenarios> {
  return request<CompassScenarios>('/compass/scenarios', {
    query: { trade_date: tradeDate ?? undefined },
  })
}

export function fetchCompassReview(days = 252): Promise<CompassReview> {
  return request<CompassReview>('/compass/review', { query: { days } })
}

export function fetchCompassRules(): Promise<CompassRules> {
  return request<CompassRules>('/compass/rules')
}

export function submitCheckAction(
  checkId: string,
  action: 'DELAY' | 'DROP' | 'EXECUTED',
  idempotencyKey: string,
): Promise<{ check_id: string; final_status: string; idempotent_replay: boolean }> {
  return request(`/compass/checks/${encodeURIComponent(checkId)}/action`, {
    method: 'POST',
    body: { action },
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

/** 演示模式：直接读取三态快照，不经过 /compass/today。 */
export async function fetchCompassDemoState(state: DemoState): Promise<CompassToday> {
  if (!DEMO_MODE) throw new Error('fetchCompassDemoState 仅用于演示模式')
  const response = await fetch(`/demo/compass_today_${state}.json`)
  if (!response.ok) throw new Error(`演示快照不存在：${state}`)
  return (await response.json()) as CompassToday
}

export { DEMO_MODE }
