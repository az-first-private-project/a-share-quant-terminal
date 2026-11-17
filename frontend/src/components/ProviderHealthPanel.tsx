/**
 * 数据源健康度面板（V9 P0-3）
 * ============================
 * 展示近 N 天 provider × task 的成功率、降级次数与最近脱敏错误。
 * 只读；空态说明“近 7 天无采集记录”。
 */
import { useProviderHealth } from '../hooks/useProviderHealth'
import { ApiError } from '../api/client'
import SectionHeader from './SectionHeader'
import { EmptyState, ErrorState, LoadingState } from './StateViews'

function pct(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

export default function ProviderHealthPanel({ days = 7 }: { days?: number }) {
  const query = useProviderHealth(days)
  const items = query.data?.items ?? []
  const error = query.error instanceof ApiError ? query.error : null

  return (
    <section id="provider-health" className="space-y-2">
      <SectionHeader
        index="03"
        title="数据源健康度"
        hint={`近 ${days} 天 · 按数据源 × 任务聚合；成功率低或降级 > 0 标黄`}
      />
      {query.isLoading ? (
        <LoadingState text="正在读取数据源健康度…" />
      ) : error ? (
        <ErrorState title="数据源健康度加载失败" error={error} onRetry={() => query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title="近 7 天无采集记录"
          description="运行一次采集任务后，这里会显示每个数据源的成功率、降级次数与最近错误。"
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border/60">
          <table className="w-full min-w-[680px] text-[12px]">
            <thead>
              <tr className="border-b border-border/60 text-left text-[11px] text-muted-foreground">
                <th className="px-3 py-2">数据源</th>
                <th className="px-3 py-2">任务</th>
                <th className="px-3 py-2 text-right">尝试</th>
                <th className="px-3 py-2 text-right">成功率</th>
                <th className="px-3 py-2 text-right">降级</th>
                <th className="px-3 py-2">最近错误</th>
                <th className="px-3 py-2">最近时间</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const warning = item.success_rate < 0.9 || item.degraded > 0
                return (
                  <tr key={`${item.provider}-${item.task}`} className="border-b border-border/40 last:border-0">
                    <td className="px-3 py-2 font-mono">{item.provider}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-muted-foreground">{item.task}</td>
                    <td className="px-3 py-2 text-right num">{item.total}</td>
                    <td className={`px-3 py-2 text-right num ${warning ? 'text-warning' : 'text-success'}`}>
                      {pct(item.success_rate)}
                    </td>
                    <td className={`px-3 py-2 text-right num ${item.degraded > 0 ? 'text-warning' : ''}`}>
                      {item.degraded}
                    </td>
                    <td className="max-w-[260px] truncate px-3 py-2 text-[11px] text-muted-foreground" title={item.last_error ?? ''}>
                      {item.last_error ?? '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-muted-foreground">
                      {item.last_seen_at ? item.last_seen_at.slice(0, 19).replace('T', ' ') : '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
