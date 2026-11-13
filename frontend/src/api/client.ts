/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export interface ActionEnvelope {
  ok: boolean
  message: string
  entry?: Record<string, unknown> | null
}

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

/**
 * 执行动作类接口。后端动作失败时仍返回 HTTP 200 + { ok:false, message }，
 * 旧实现只看 response.ok 会把拒绝当成功，导致「点击发布没有任何拒绝原因」。
 * 这里统一解析信封并把后端明确的拒绝原因抛给页面展示。
 */
export async function postAction(path: string, body: unknown): Promise<ActionEnvelope> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    let detail = `接口返回 ${response.status}`
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload.detail) detail = payload.detail
    } catch {
      /* 非 JSON 错误体时保留状态码说明 */
    }
    throw new Error(detail)
  }
  return (await response.json()) as ActionEnvelope
}
