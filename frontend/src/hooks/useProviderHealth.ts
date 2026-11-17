import { useQuery } from '@tanstack/react-query'
import { fetchProviderHealth } from '../api/meta'

/** 数据源健康度（30 秒刷新；任务监控页与看板徽标共用） */
export function useProviderHealth(days = 7) {
  return useQuery({
    queryKey: ['provider-health', days],
    queryFn: () => fetchProviderHealth(days),
    refetchInterval: 30_000,
    staleTime: 15_000,
  })
}
