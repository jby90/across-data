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
    const res = await fetch(`/api${path}`, {
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
    login: (email: string, password: string) =>
      request<TokenResponse>('POST', `/auth/${side}/login`, { email, password }),
    me: () => request<UserOut>('GET', `/${side}/me`),
  }
}
