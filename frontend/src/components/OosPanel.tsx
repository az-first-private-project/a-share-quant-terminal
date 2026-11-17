/**
 * 样本外验证面板（V9 P0-2）
 * ==========================
 * 与回测同页展示，不新增路由：
 * - 运行 walk-forward（默认 24/6/6），显示 4 折训练/测试区间与指标；
 * - 汇总年化/超额均值、中位数、正超额折数与降级标记；
 * - 样本外结果不保证未来收益，页面显式提示。
 */
import { useMemo, useState } from 'react'
import { Play, RefreshCw } from 'lucide-react'
import { useOos, useRunOos } from '../hooks/useOos'
import { ApiError } from '../api/client'
import { DEMO_MODE } from '../api/demo'
import SectionHeader from './SectionHeader'
import { EmptyState, ErrorState, LoadingState } from './StateViews'
import type { OOSResult } from '../types/backtest'

interface OosPanelProps {
  algoCode: string
  strategyParams: Record<string, string | number | undefined>
}

function isoDaysAgo(days: number): string {
  const value = new Date()
  value.setDate(value.getDate() - days)
  return value.toISOString().slice(0, 10)
}

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toFixed(digits)
}

function statusText(status: string | undefined): string {
  switch (status) {
    case 'queued':
      return '排队中'
    case 'pending':
      return '等待中'
    case 'running':
      return '运行中'
    case 'success':
      return '已完成'
    case 'failed':
      return '失败'
    default:
      return status ?? '未运行'
  }
}

function statusClass(status: string | undefined): string {
  if (status === 'success') return 'border-success/40 bg-success/10 text-success'
  if (status === 'failed') return 'border-alert/40 bg-alert/10 text-alert'
  if (status === 'running' || status === 'queued' || status === 'pending') {
    return 'border-brass/45 bg-brass/[0.08] text-brass-strong dark:text-brass'
  }
  return 'border-border bg-muted text-muted-foreground'
}

export default function OosPanel({ algoCode, strategyParams }: OosPanelProps) {
  const [startDate, setStartDate] = useState(() => isoDaysAgo(730))
  const [endDate, setEndDate] = useState(() => isoDaysAgo(0))
  const [oosId, setOosId] = useState<string | null>(null)
  const query = useOos(algoCode, oosId)
  const mutation = useRunOos(algoCode)
  const data: OOSResult | null = query.data ?? null

  const summary = useMemo(() => data?.summary ?? {}, [data])
  const canRun = !mutation.isPending && !DEMO_MODE

  const handleRun = () => {
    mutation.mutate(
      {
        start_date: startDate,
        end_date: endDate,
        train_months: 24,
        test_months: 6,
        step_months: 6,
        strategy: strategyParams,
      },
      {
        onSuccess: (payload) => setOosId(payload.oos_id),
      },
    )
  }

  const failure = mutation.error
  const queryError = query.error instanceof ApiError ? query.error : null
  const failedFolds = data?.folds.filter((fold) => fold.test.status === 'failed').length ?? 0

  return (
    <section id="oos" className="card p-4 sm:p-5 space-y-4">
      <SectionHeader
        index="OOS"
        title="样本外验证 · Walk-Forward"
        hint="训练窗/测试窗严格不重叠；样本外不保证未来收益"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              起
              <input
                type="date"
                value={startDate}
                onChange={(event) => setStartDate(event.target.value)}
                className="field !h-8 !w-auto !py-0 text-[12px]"
              />
            </label>
            <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              止
              <input
                type="date"
                value={endDate}
                onChange={(event) => setEndDate(event.target.value)}
                className="field !h-8 !w-auto !py-0 text-[12px]"
              />
            </label>
            <button
              type="button"
              onClick={handleRun}
              disabled={!canRun}
              className="btn btn-sm h-8"
              title={DEMO_MODE ? '演示模式为只读，不能发起样本外任务' : '运行样本外验证'}
            >
              {mutation.isPending ? (
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              {mutation.isPending ? '提交中…' : '运行样本外'}
            </button>
          </div>
        }
      />

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        默认训练 24 个月、测试 6 个月、步进 6 个月；数据不足时自动降级为 expanding window，
        并在每折标注。结果仅作时间稳定性检查，不构成投资建议。
      </p>

      {mutation.isError && (
        <p className="text-[12px] text-alert">
          提交失败：{failure instanceof Error ? failure.message : '未知错误'}
        </p>
      )}

      {query.isLoading ? (
        <LoadingState text="正在读取样本外结果…" />
      ) : queryError ? (
        <ErrorState title="样本外结果加载失败" error={queryError} onRetry={() => query.refetch()} />
      ) : !data ? (
        <EmptyState
          title="尚无样本外结果"
          description="点击「运行样本外」后，系统会按折执行训练/测试窗并落库；结果会保留最近一次。"
        />
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <span className={`rounded-full border px-2 py-0.5 ${statusClass(data.status)}`}>
              {statusText(data.status)}
            </span>
            <span className="rounded-full border border-border px-2 py-0.5 text-muted-foreground">
              {data.window_type === 'expanding' ? 'expanding window（已降级）' : '固定窗口'}
            </span>
            {data.degraded && (
              <span className="rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-warning">
                数据不足 · 降级
              </span>
            )}
            <span className="text-muted-foreground">
              {data.folds.length} 折 · {data.created_at ? data.created_at.slice(0, 19).replace('T', ' ') : '—'}
            </span>
            {failedFolds > 0 && <span className="text-alert">失败 {failedFolds} 折</span>}
          </div>

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <div className="inset px-3 py-2">
              <p className="text-[11px] text-muted-foreground">年化均值 / 中位数</p>
              <p className="num text-[13px] font-medium">
                {pct(summary.annual_return_mean as number | null)} / {pct(summary.annual_return_median as number | null)}
              </p>
            </div>
            <div className="inset px-3 py-2">
              <p className="text-[11px] text-muted-foreground">超额均值 / 中位数</p>
              <p className="num text-[13px] font-medium">
                {pct(summary.excess_return_mean as number | null)} / {pct(summary.excess_return_median as number | null)}
              </p>
            </div>
            <div className="inset px-3 py-2">
              <p className="text-[11px] text-muted-foreground">Sharpe 均值</p>
              <p className="num text-[13px] font-medium">{num(summary.sharpe_mean as number | null)}</p>
            </div>
            <div className="inset px-3 py-2">
              <p className="text-[11px] text-muted-foreground">正超额折数</p>
              <p className="num text-[13px] font-medium">
                {String(summary.positive_excess_folds ?? '—')} / {String(summary.n_folds ?? data.folds.length)}
              </p>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-[12px]">
              <thead>
                <tr className="border-b border-border/60 text-left text-[11px] text-muted-foreground">
                  <th className="py-2 pr-3">Fold</th>
                  <th className="py-2 pr-3">训练窗</th>
                  <th className="py-2 pr-3">测试窗</th>
                  <th className="py-2 pr-3 text-right">年化</th>
                  <th className="py-2 pr-3 text-right">超额</th>
                  <th className="py-2 pr-3 text-right">Sharpe</th>
                  <th className="py-2 pr-3 text-right">最大回撤</th>
                  <th className="py-2 text-right">成交笔数</th>
                </tr>
              </thead>
              <tbody>
                {data.folds.map((fold) => {
                  const metrics = fold.test.metrics
                  return (
                    <tr key={fold.fold_index} className="border-b border-border/40">
                      <td className="py-2 pr-3 font-mono">{fold.fold_index}</td>
                      <td className="py-2 pr-3 font-mono text-[11px] text-muted-foreground">
                        {fold.train_start} → {fold.train_end}
                      </td>
                      <td className="py-2 pr-3 font-mono text-[11px]">
                        {fold.test_start} → {fold.test_end}
                        {fold.test.status === 'failed' && (
                          <span className="ml-1 text-alert" title={fold.test.error ?? ''}>失败</span>
                        )}
                      </td>
                      <td className="py-2 pr-3 text-right num">{pct(metrics.annual_return)}</td>
                      <td className="py-2 pr-3 text-right num">{pct(metrics.excess_return)}</td>
                      <td className="py-2 pr-3 text-right num">{num(metrics.sharpe)}</td>
                      <td className="py-2 pr-3 text-right num">{pct(metrics.max_drawdown)}</td>
                      <td className="py-2 text-right num">{metrics.trade_count ?? '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}
