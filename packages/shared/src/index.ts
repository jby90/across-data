export type Role = 'platform_admin' | 'reviewer' | 'org_admin' | 'member'

export const ROLE_LABEL: Record<Role, string> = {
  platform_admin: '平台管理员',
  reviewer: '复核员',
  org_admin: '企业管理员',
  member: '企业成员',
}

export interface UserOut {
  id: string
  org_id: string | null
  email: string
  display_name: string
  role: Role
  is_active: boolean
}

export interface OrgOut {
  id: string
  name: string
  is_active: boolean
}

export interface TokenResponse {
  access_token: string
  token_type: string
  role: Role
  org_id: string | null
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

export type Side = 'user' | 'admin'

function detailToMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const data = detail as { message?: unknown; issues?: unknown }
    if (typeof data.message === 'string') {
      const issues = Array.isArray(data.issues)
        ? data.issues.filter((x): x is string => typeof x === 'string')
        : []
      return [data.message, ...issues.slice(0, 5)].join('；')
    }
  }
  if (Array.isArray(detail) && detail.length > 0) {
    // FastAPI 校验错误:取第一条的 msg
    const first = detail[0] as { msg?: string }
    return first?.msg ? `输入不合法:${first.msg}` : fallback
  }
  return fallback
}

/** 创建某一端的 API 客户端。令牌放 sessionStorage,关闭标签页即失效;两端使用不同的存储键,互不干扰。 */
export function createClient(side: Side, onUnauthorized: () => void) {
  const key = `cyshuhe.${side}.token`

  const token = () => sessionStorage.getItem(key)
  const setToken = (t: string | null) =>
    t ? sessionStorage.setItem(key, t) : sessionStorage.removeItem(key)

  async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = {}
    const t = token()
    if (t) headers.Authorization = `Bearer ${t}`
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    const url = path.startsWith('/api/') ? path : `/api${path}`
    const res = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    if (res.status === 401 && !path.startsWith('/auth/')) {
      setToken(null)
      onUnauthorized()
    }
    if (!res.ok) {
      let message = `请求失败(${res.status})`
      try {
        message = detailToMessage((await res.json()).detail, message)
      } catch {
        /* 非 JSON 响应 */
      }
      throw new ApiError(res.status, message)
    }
    return (await res.json()) as T
  }

  return {
    token,
    setToken,
    get: <T>(path: string) => request<T>('GET', path),
    post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
    patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
    delete: <T>(path: string) => request<T>('DELETE', path),
    login: (email: string, password: string) =>
      request<TokenResponse>('POST', `/auth/${side}/login`, { email, password }),
    me: () => request<UserOut>('GET', `/${side}/me`),
  }
}

// ---------- 采集与文档库(管理端) ----------

export interface SiteOut {
  terms_approval_scope?: 'legacy' | 'development' | 'production'
  id: string
  country_code: string
  publisher: string
  language: string
  document_type: string
  entry_url: string
  strategy: string
  config: Record<string, unknown>
  enabled: boolean
  interval_hours: number
  next_run_at: string | null
  last_run_at: string | null
  last_status: string | null
  consecutive_failures: number
  terms_status: 'pending' | 'approved' | 'rejected'
  terms_url: string | null
  terms_note: string | null
  terms_decided_at: string | null
  document_count: number
  sample_titles: string[]
}

export interface RunOut {
  id: string
  trigger: string
  started_at: string
  finished_at: string | null
  status: string
  pages_read: number
  links_found: number
  documents_new: number
  versions_new: number
  errors: number
  message: string | null
}

export interface VersionOut {
  id: string
  version_no: number
  content_hash: string
  size_bytes: number
  mime_type: string
  fetched_at: string
  state: string
  page_count: number | null
  char_count: number | null
  parse_strategy: string | null
  error: string | null
  text_source: 'text_layer' | 'ocr'
  ocr_confidence: number | null
  ocr_required: boolean
  review_status: 'pending_review' | 'approved' | 'rejected'
  official_number: string | null
  version_label: string | null
  published_date: string | null
  effective_date: string | null
  repealed_date: string | null
  legal_status: string
  metadata_source: string | null
}

export interface DocumentOut {
  safety_hold?: boolean
  safety_note?: string | null
  id: string
  site_id: string
  country_code: string
  source_url: string
  title: string
  language: string
  document_type: string
  discovered_at: string
  last_checked_at: string | null
  last_error: string | null
  instrument_family: string | null
  instrument_kind: string | null
  relevance: 'relevant' | 'related' | 'out_of_scope' | 'irrelevant' | 'needs_review'
  relevance_note: string | null
  relevance_source?: 'rule' | 'llm' | 'manual'
  latest_version: VersionOut | null
}

export interface DocumentDetail extends DocumentOut {
  versions: VersionOut[]
}

export interface ClauseOut {
  id: string
  ordinal: number
  clause_number: string | null
  heading: string | null
  section_path: string | null
  original_text: string
  page_start: number | null
  page_end: number | null
  language: string
  ocr_required: boolean
  needs_review: boolean
}

export interface EventOut {
  step: string
  status: string
  message: string | null
  created_at: string
}

// 界面一律用中文业务用语,不暴露枚举值
export const TERMS_LABEL: Record<string, string> = {
  pending: '待确认',
  approved: '已确认可抓取',
  rejected: '已否决',
}
export const RUN_STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  success: '成功',
  partial: '部分成功',
  failed: '失败',
  skipped: '已跳过',
}
export const VERSION_STATE_LABEL: Record<string, string> = {
  downloaded: '已下载',
  parsed: '已解析',
  needs_ocr: '需要 OCR(扫描件)',
  failed: '解析失败',
}
export const DOC_TYPE_LABEL: Record<string, string> = {
  LAW: '法律',
  REGULATION: '法规',
  GUIDANCE: '监管指南',
  STANDARD: '标准',
  FORM: '申报表',
  OTHER: '其他',
}
export const STRATEGY_LABEL: Record<string, string> = {
  INDEX_PAGE: '目录页发现',
  DIRECT: '直接链接',
  EGAZETTE: '电子宪报检索发现',
}
export const PARSE_STRATEGY_LABEL: Record<string, string> = {
  numbered: '按条款编号',
  chunk: '按篇幅分块(原文无可靠条款编号)',
  page: '按页(未识别到编号)',
  paragraph: '按段落(未识别到编号)',
  line: '按行(网页)',
}
export const LEGAL_STATUS_LABEL: Record<string, string> = {
  unknown: '现行状态未核实',
  in_force: '现行有效',
  amended: '已被修订',
  repealed: '已废止',
}
