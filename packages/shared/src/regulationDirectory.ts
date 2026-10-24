/** UI classification from existing metadata only; never infer legal status from a title. */
export interface DirectoryDocument {
  id: string
  country_code: string
  title: string
  document_type: string
}
export interface DirectoryCountry { code: string; name: string; count: number }

export const COUNTRY_NAMES: Record<string, string> = {
  SG: '新加坡', EG: '埃及', TN: '突尼斯', MA: '摩洛哥', LB: '黎巴嫩',
}
const LEVELS: Record<string, { name: string; rank: number }> = {
  LAW: { name: '法律', rank: 0 },
  REGULATION: { name: '法规与实施规则', rank: 1 },
  GUIDANCE: { name: '监管指南与框架', rank: 2 },
  STANDARD: { name: '标准', rank: 3 },
  FORM: { name: '申报表与材料', rank: 4 },
  OTHER: { name: '其他文件', rank: 5 },
}

export function directoryLevels<T extends DirectoryDocument>(documents: T[], country: string) {
  const groups = new Map<string, { key: string; name: string; rank: number; items: T[] }>()
  for (const item of documents) {
    if (item.country_code !== country) continue
    const key = item.document_type || '__unclassified__'
    const meta = LEVELS[key] || { name: key === '__unclassified__' ? '未分类' : `其他类型（${key}）`, rank: 6 }
    if (!groups.has(key)) groups.set(key, { key, ...meta, items: [] })
    groups.get(key)!.items.push(item)
  }
  for (const group of groups.values()) group.items.sort((a, b) =>
    a.title.localeCompare(b.title, 'zh-CN', { numeric: true }) || a.id.localeCompare(b.id),
  )
  return [...groups.values()].sort((a, b) => a.rank - b.rank || a.key.localeCompare(b.key))
}

export function directoryCountries(documents: DirectoryDocument[]): DirectoryCountry[] {
  const counts = new Map<string, number>()
  for (const d of documents) counts.set(d.country_code, (counts.get(d.country_code) || 0) + 1)
  return [...counts].sort(([a], [b]) => a.localeCompare(b)).map(([code, count]) => ({
    code, name: COUNTRY_NAMES[code] || code, count,
  }))
}

/** Keep the selected document reachable even in a large category. */
export function documentPage<T>(items: T[], page: number, size = 8) {
  const pageSize = Number.isFinite(size) ? Math.max(1, Math.floor(size)) : 8
  const pages = Math.max(1, Math.ceil(items.length / pageSize))
  const current = Math.max(1, Math.min(pages, Math.floor(page) || 1))
  return { page: current, pages, items: items.slice((current - 1) * pageSize, current * pageSize) }
}
