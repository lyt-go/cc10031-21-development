import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
  values: Record<string, string> = {},
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action, values }),
  })
  return await response.json()
}

export type LocateResult = {
  query: Record<string, string>
  total: number
  items: {
    index_id: string
    point: string
    unit: string
    reference: string
    calibrate_id: string
    version: string
    source: string
    artifact: string
    matched_count: number
    sample_count: number
    samples: { timestamp: string; value: number; unit: string; reference: string }[]
  }[]
}

export async function locateSeries(
  key: string,
  query: Record<string, string>,
): Promise<LocateResult> {
  const params = new URLSearchParams()
  Object.entries(query).forEach(([name, value]) => {
    if (value) params.set(name, value)
  })
  // 后端参数名用 point/unit/start/end/rule，页面上仍展示中文字段。
  const response = await request(`/api/${key}/locate?${params.toString()}`)
  if (!response.ok) throw new Error('按换算出处定位失败')
  return await response.json()
}

export type ReverifyRun = {
  run: number
  action: string
  scope: { start: string; end: string }
  requested_reference: string
  actual_references: string[]
  unit: string
  units_seen: string[]
  sample_count: number
  conclusion: string
  digest: string
  recorded_at: string
  samples: {
    timestamp: string
    value: number
    unit: string
    raw_value: number
    source_unit: string
    scale: number
    offset: number
    reference: string
    source: string
  }[]
  overlaps: {
    timestamp: string
    chosen: string
    chosen_value: number
    unit: string
    candidates: { reference: string; value: number; unit: string; source: string }[]
  }[]
}

export type ReverifyDetail = EntryRow & {
  window?: { start: string; end: string }
  unit_name?: string
  reviewed?: { start: string; end: string }[]
  pending_ranges?: { start: string; end: string }[]
  提交记录?: ReverifyRun[]
  提交记录数?: number
}

export async function loadReverifyDetail(id: number): Promise<ReverifyDetail> {
  const response = await request(`/api/reverify_session/${id}`)
  if (!response.ok) throw new Error('复验单详情读取失败')
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}
