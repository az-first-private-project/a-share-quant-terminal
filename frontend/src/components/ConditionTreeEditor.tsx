/**
 * 条件树可视化编辑器（V9 P1-4）
 * ================================
 * - AND/OR 嵌套；字段/算子/阈值来自 `/algo/fields`；
 * - “预览命中数”调用只读 `/algo/preview`，不写 stg_run；
 * - 图形视图与 JSON 视图双向同步。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { fetchAlgoFields, previewConditionTree } from '../api/algo'
import { ApiError } from '../api/client'
import { DEMO_MODE } from '../api/demo'
import type {
  AlgoFieldSpec,
  ConditionNode,
  ConditionPreviewResponse,
} from '../types/algo'
import { EmptyState, ErrorState, LoadingState } from './StateViews'

type GroupNode = ConditionNode & { logic: 'AND' | 'OR'; children: ConditionNode[] }

function isGroup(node: ConditionNode): node is GroupNode {
  return Array.isArray(node.children)
}

function defaultCondition(fields: AlgoFieldSpec[]): ConditionNode {
  const field = fields[0]
  return {
    field: field?.key ?? 'pe_ttm',
    op: field?.operators?.[0] ?? 'lt',
    value: field?.type === 'string' || field?.type === 'enum' ? '' : field?.type === 'boolean' ? true : 0,
  }
}

function valueForField(field: AlgoFieldSpec | undefined, op: string): unknown {
  if (op === 'notna' || op === 'isna') return undefined
  if (op === 'between') return [0, 0]
  if (op === 'in' || op === 'not_in') return []
  if (field?.type === 'boolean') return true
  if (field?.type === 'string' || field?.type === 'enum') return ''
  return 0
}

function ConditionRow({
  node,
  fields,
  onChange,
  onRemove,
}: {
  node: ConditionNode
  fields: AlgoFieldSpec[]
  onChange: (node: ConditionNode) => void
  onRemove: () => void
}) {
  const field = fields.find((item) => item.key === node.field)
  const operators = field?.operators?.length ? field.operators : ['eq']
  const op = node.op ?? operators[0]

  const setField = (key: string) => {
    const next = fields.find((item) => item.key === key)
    const nextOp = next?.operators?.[0] ?? 'eq'
    onChange({ field: key, op: nextOp, value: valueForField(next, nextOp) })
  }
  const setOp = (nextOp: string) => {
    onChange({ ...node, op: nextOp, value: valueForField(field, nextOp) })
  }

  const renderValue = () => {
    if (op === 'notna' || op === 'isna') return <span className="text-[11px] text-muted-foreground">无需值</span>
    if (op === 'between') {
      const values = Array.isArray(node.value) ? node.value : [0, 0]
      return (
        <div className="flex items-center gap-1">
          <input
            type="number"
            value={String(values[0] ?? '')}
            onChange={(event) => onChange({ ...node, value: [Number(event.target.value), values[1]] })}
            className="field !h-8 w-20 !py-0 text-[12px]"
          />
          <span className="text-[11px] text-muted-foreground">~</span>
          <input
            type="number"
            value={String(values[1] ?? '')}
            onChange={(event) => onChange({ ...node, value: [values[0], Number(event.target.value)] })}
            className="field !h-8 w-20 !py-0 text-[12px]"
          />
        </div>
      )
    }
    if (op === 'in' || op === 'not_in') {
      const text = Array.isArray(node.value) ? node.value.join(',') : String(node.value ?? '')
      return (
        <input
          value={text}
          onChange={(event) =>
            onChange({
              ...node,
              value: event.target.value.split(',').map((item) => item.trim()).filter(Boolean),
            })
          }
          placeholder="用逗号分隔"
          className="field !h-8 w-44 !py-0 text-[12px]"
        />
      )
    }
    if (field?.type === 'boolean') {
      return (
        <select
          value={String(node.value ?? true)}
          onChange={(event) => onChange({ ...node, value: event.target.value === 'true' })}
          className="field !h-8 !w-auto !py-0 text-[12px]"
        >
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      )
    }
    if (field?.type === 'string' || field?.type === 'enum') {
      return (
        <input
          value={String(node.value ?? '')}
          onChange={(event) => onChange({ ...node, value: event.target.value })}
          className="field !h-8 w-36 !py-0 text-[12px]"
        />
      )
    }
    return (
      <input
        type="number"
        value={String(node.value ?? '')}
        onChange={(event) =>
          onChange({ ...node, value: event.target.value === '' ? '' : Number(event.target.value) })
        }
        className="field !h-8 w-24 !py-0 text-[12px]"
      />
    )
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <select
        value={node.field ?? ''}
        onChange={(event) => setField(event.target.value)}
        className="field !h-8 !w-auto max-w-[12rem] !py-0 text-[12px]"
        aria-label="字段"
      >
        {fields.map((item) => (
          <option key={item.key} value={item.key}>
            {item.label}（{item.key}）
          </option>
        ))}
      </select>
      <select
        value={op}
        onChange={(event) => setOp(event.target.value)}
        className="field !h-8 !w-auto !py-0 text-[12px]"
        aria-label="操作符"
      >
        {operators.map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
      {renderValue()}
      <button
        type="button"
        onClick={onRemove}
        className="btn btn-sm h-8 !px-2"
        aria-label="删除条件"
        title="删除条件"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </button>
    </div>
  )
}

function GroupEditor({
  group,
  fields,
  onChange,
  onRemove,
  depth = 0,
}: {
  group: GroupNode
  fields: AlgoFieldSpec[]
  onChange: (group: GroupNode) => void
  onRemove?: () => void
  depth?: number
}) {
  const setChild = (index: number, child: ConditionNode) => {
    const next = [...group.children]
    next[index] = child
    onChange({ ...group, children: next })
  }
  const removeChild = (index: number) => {
    onChange({ ...group, children: group.children.filter((_, idx) => idx !== index) })
  }
  const addCondition = () => onChange({ ...group, children: [...group.children, defaultCondition(fields)] })
  const addGroup = () =>
    onChange({
      ...group,
      children: [...group.children, { logic: 'AND', children: [defaultCondition(fields)] }],
    })

  return (
    <div className={depth === 0 ? 'space-y-2' : 'rounded-lg border border-border/60 bg-muted/10 p-2.5 space-y-2'}>
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={group.logic}
          onChange={(event) => onChange({ ...group, logic: event.target.value as 'AND' | 'OR' })}
          className="field !h-8 !w-auto !py-0 text-[12px]"
          aria-label="组合逻辑"
        >
          <option value="AND">AND（全部满足）</option>
          <option value="OR">OR（任一满足）</option>
        </select>
        <button type="button" onClick={addCondition} className="btn btn-sm h-8">
          <Plus className="h-3.5 w-3.5" /> 条件
        </button>
        <button type="button" onClick={addGroup} className="btn btn-sm h-8" disabled={depth >= 4}>
          <Plus className="h-3.5 w-3.5" /> 分组
        </button>
        {onRemove && (
          <button type="button" onClick={onRemove} className="btn btn-sm h-8 !px-2" aria-label="删除分组">
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      <div className="space-y-2">
        {group.children.map((child, index) =>
          isGroup(child) ? (
            <GroupEditor
              key={index}
              group={child}
              fields={fields}
              onChange={(next) => setChild(index, next)}
              onRemove={() => removeChild(index)}
              depth={depth + 1}
            />
          ) : (
            <ConditionRow
              key={index}
              node={child}
              fields={fields}
              onChange={(next) => setChild(index, next)}
              onRemove={() => removeChild(index)}
            />
          ),
        )}
        {group.children.length === 0 && (
          <p className="text-[11px] text-muted-foreground">空条件组，请添加条件或分组。</p>
        )}
      </div>
    </div>
  )
}

export default function ConditionTreeEditor() {
  const fieldsQuery = useQuery({
    queryKey: ['algo-fields'],
    queryFn: fetchAlgoFields,
    staleTime: 30 * 60 * 1000,
  })
  const fields = fieldsQuery.data?.fields ?? []
  const [root, setRoot] = useState<GroupNode>({
    logic: 'AND',
    children: [{ field: 'pe_ttm', op: 'lt', value: 20 }],
  })
  const [jsonOpen, setJsonOpen] = useState(false)
  const [jsonText, setJsonText] = useState('')
  const [jsonError, setJsonError] = useState<string | null>(null)

  const preview = useMutation({
    mutationFn: () => previewConditionTree({ tree: root, limit: 20 }),
  })
  const data: ConditionPreviewResponse | undefined = preview.data
  const previewError = preview.error instanceof ApiError ? preview.error : null

  const json = useMemo(() => JSON.stringify(root, null, 2), [root])

  const openJson = () => {
    setJsonText(json)
    setJsonError(null)
    setJsonOpen(true)
  }
  const applyJson = () => {
    try {
      const parsed = JSON.parse(jsonText) as ConditionNode
      if (!isGroup(parsed)) throw new Error('根节点必须是条件组（包含 logic/children）')
      setRoot(parsed)
      setJsonOpen(false)
      setJsonError(null)
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : 'JSON 解析失败')
    }
  }

  if (fieldsQuery.isLoading) return <LoadingState text="正在读取字段清单…" />
  if (fieldsQuery.isError) {
    return (
      <ErrorState
        title="字段清单加载失败"
        error={fieldsQuery.error instanceof ApiError ? fieldsQuery.error : null}
        onRetry={() => fieldsQuery.refetch()}
      />
    )
  }

  return (
    <div className="space-y-3">
      <p className="text-[11px] leading-relaxed text-muted-foreground">
        条件树仅做只读预览：复用正式选股的横截面与统一过滤，但不写 run、不进入共识。
        AND/OR 最多 5 层、50 个节点。
      </p>

      <GroupEditor group={root} fields={fields} onChange={setRoot} />

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => preview.mutate()}
          disabled={preview.isPending || DEMO_MODE}
          className="btn btn-sm h-8"
          title={DEMO_MODE ? '演示模式为只读，不能执行预览' : '只读预览命中数'}
        >
          {preview.isPending ? '预览中…' : '预览命中数'}
        </button>
        <button type="button" onClick={openJson} className="btn btn-sm h-8">
          JSON 视图
        </button>
        {DEMO_MODE && <span className="text-[11px] text-muted-foreground">演示模式只读，预览已禁用</span>}
      </div>

      {jsonOpen && (
        <div className="space-y-2">
          <textarea
            value={jsonText}
            onChange={(event) => setJsonText(event.target.value)}
            className="field min-h-[220px] w-full font-mono text-[12px]"
            spellCheck={false}
          />
          {jsonError && <p className="text-[12px] text-alert">{jsonError}</p>}
          <div className="flex gap-2">
            <button type="button" onClick={applyJson} className="btn btn-sm h-8">应用 JSON</button>
            <button type="button" onClick={() => setJsonOpen(false)} className="btn btn-sm h-8">取消</button>
          </div>
        </div>
      )}

      {previewError && (
        <p className="text-[12px] text-alert">预览失败：{previewError.message}</p>
      )}

      {data ? (
        <div className="space-y-2 rounded-lg border border-border/60 p-3">
          <div className="flex flex-wrap items-center gap-3 text-[12px]">
            <span className="font-medium">命中 {data.hit_count} / {data.universe_count}</span>
            <span className="text-muted-foreground">{data.trade_date}</span>
            <span className="font-mono text-[11px] text-muted-foreground">指纹 {data.param_fingerprint}</span>
            <span className="text-muted-foreground">{data.elapsed_ms}ms</span>
            {data.warning && <span className="text-warning">{data.warning}</span>}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {data.conditions.map((item, index) => (
              <span
                key={`${item.key}-${index}`}
                className="rounded-full border border-border/70 px-2 py-0.5 text-[11px] text-muted-foreground"
                title={item.label ?? undefined}
              >
                {item.label ?? item.key}：{item.passed_count}/{item.total}
              </span>
            ))}
          </div>
          {data.data.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-[12px]">
                <thead>
                  <tr className="border-b border-border/60 text-left text-[11px] text-muted-foreground">
                    <th className="py-1.5 pr-3">代码</th>
                    <th className="py-1.5 pr-3">名称</th>
                    <th className="py-1.5 pr-3 text-right">收盘</th>
                    <th className="py-1.5 pr-3 text-right">涨跌幅</th>
                    <th className="py-1.5 text-right">综合分</th>
                  </tr>
                </thead>
                <tbody>
                  {data.data.slice(0, 20).map((row, index) => (
                    <tr key={`${row.code}-${index}`} className="border-b border-border/40">
                      <td className="py-1.5 pr-3 font-mono">{String(row.code ?? '')}</td>
                      <td className="py-1.5 pr-3">{String(row.name ?? '')}</td>
                      <td className="py-1.5 pr-3 text-right num">{String(row.close ?? '—')}</td>
                      <td className="py-1.5 pr-3 text-right num">{String(row.pct_chg ?? '—')}%</td>
                      <td className="py-1.5 text-right num">{String(row.composite_score ?? '—')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="没有命中" description="当前条件树在该交易日没有命中股票。" />
          )}
        </div>
      ) : null}
    </div>
  )
}
