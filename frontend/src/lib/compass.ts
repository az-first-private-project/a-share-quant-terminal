/**
 * 仓位罗盘前端纯函数（可单测，不依赖 DOM）。
 */
export type RegimeTier = 'attack' | 'range' | 'defense'
export type ScenarioFilter = 'all' | 'on' | 'off'

export function formatCap(execCap: number): string {
  return execCap > 0 ? `${execCap}%` : '空仓'
}

export function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(100, Math.max(0, value))
}

export function regimeTone(tier: RegimeTier): string {
  if (tier === 'attack') return 'text-regime-trend'
  if (tier === 'range') return 'text-regime-repair'
  return 'text-regime-riskoff'
}

export function statusLabel(status: string): string {
  switch (status) {
    case 'TRIGGERED':
      return '已触发'
    case 'STANDING':
      return '生效中'
    case 'EXPIRED':
      return '已失效'
    case 'MUTED':
      return '已静音'
    default:
      return '未触发'
  }
}

export function statusTone(status: string): string {
  if (status === 'TRIGGERED') return 'text-warning border-warning/40 bg-warning/10'
  if (status === 'STANDING') return 'text-brass border-brass/40 bg-brass/10'
  if (status === 'EXPIRED') return 'text-muted-foreground border-border bg-muted/30'
  return 'text-muted-foreground border-border/70 bg-transparent'
}

export function filterScenarios<T extends { status: string }>(
  scenarios: T[],
  filter: ScenarioFilter,
): T[] {
  if (filter === 'on') return scenarios.filter((item) => item.status === 'TRIGGERED')
  if (filter === 'off') return scenarios.filter((item) => item.status !== 'TRIGGERED')
  return scenarios
}

export function previewScenarios<T extends { id: string }>(scenarios: T[]): T[] {
  const preferred = ['SC-01', 'SC-03', 'SC-04']
  return preferred
    .map((id) => scenarios.find((item) => item.id === id))
    .filter((item): item is T => Boolean(item))
}

export function pctTone(pct: number | null): string {
  if (pct === null) return 'text-muted-foreground'
  if (pct >= 90) return 'text-up'
  if (pct >= 80) return 'text-warning'
  return 'text-foreground'
}

export function formatYi(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${(value / 1e8).toFixed(1)} 亿`
}
