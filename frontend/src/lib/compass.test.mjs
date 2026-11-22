/**
 * 仓位罗盘纯函数单测（Node 原生 TS 支持，不新增依赖）。
 * 运行：node --test src/lib/compass.test.mjs
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clampPercent,
  filterScenarios,
  formatCap,
  formatYi,
  pctTone,
  previewScenarios,
  statusLabel,
  statusTone,
} from './compass.ts'

test('formatCap 区分空仓与正预算', () => {
  assert.equal(formatCap(40), '40%')
  assert.equal(formatCap(0), '空仓')
  assert.equal(formatCap(80), '80%')
})

test('clampPercent 处理越界与非法值', () => {
  assert.equal(clampPercent(-5), 0)
  assert.equal(clampPercent(120), 100)
  assert.equal(clampPercent(Number.NaN), 0)
})

test('情景卡状态文案与色调', () => {
  assert.equal(statusLabel('TRIGGERED'), '已触发')
  assert.equal(statusLabel('STANDING'), '生效中')
  assert.equal(statusLabel('ARMED'), '未触发')
  assert.match(statusTone('TRIGGERED'), /warning/)
})

test('情景卡筛选只按已触发/未触发分组', () => {
  const rows = [
    { id: 'SC-01', status: 'TRIGGERED' },
    { id: 'SC-02', status: 'STANDING' },
    { id: 'SC-03', status: 'ARMED' },
  ]
  assert.deepEqual(filterScenarios(rows, 'on').map((r) => r.id), ['SC-01'])
  assert.deepEqual(filterScenarios(rows, 'off').map((r) => r.id), ['SC-02', 'SC-03'])
  assert.equal(filterScenarios(rows, 'all').length, 3)
})

test('今日页预览固定优先 SC-01 / SC-03 / SC-04', () => {
  const rows = [
    { id: 'SC-05' },
    { id: 'SC-04' },
    { id: 'SC-01' },
    { id: 'SC-03' },
  ]
  assert.deepEqual(previewScenarios(rows).map((r) => r.id), ['SC-01', 'SC-03', 'SC-04'])
})

test('情绪分位色调与金额格式', () => {
  assert.match(pctTone(94), /text-up/)
  assert.match(pctTone(85), /text-warning/)
  assert.match(pctTone(null), /muted/)
  assert.equal(formatYi(1.28e10), '128.0 亿')
  assert.equal(formatYi(null), '—')
})
