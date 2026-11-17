/**
 * 样本外验证 Hook（V9 P0-2）
 * - GET /backtest/{algo}/oos 读取最新或指定批次；
 * - pending/running 时 2 秒轮询；
 * - POST 创建后由调用方保存 oos_id 并切换查询。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchOos, runOos } from '../api/backtest'

export function useOos(algoCode: string | null, oosId?: string | null) {
  return useQuery({
    queryKey: ['oos', algoCode, oosId ?? 'latest'],
    queryFn: () => fetchOos(algoCode as string, oosId),
    enabled: Boolean(algoCode),
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'pending' || status === 'running' ? 2000 : false
    },
  })
}

export function useRunOos(algoCode: string | null) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: {
      start_date: string
      end_date: string
      train_months?: number
      test_months?: number
      step_months?: number
      strategy?: Record<string, string | number | undefined>
    }) => runOos(algoCode as string, payload),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: ['oos', algoCode] })
      void queryClient.invalidateQueries({ queryKey: ['oos', algoCode, data.oos_id] })
    },
  })
}
