import axios, { type AxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'

/**
 * 后端统一响应结构：{ code: 0, data: ..., message: '' }
 * 这里在拦截器里直接解包成 data，业务代码不用每次写 res.data.data。
 * code 非 0 一律抛错并弹提示——后端的错误信息都是写给人看的、可操作的。
 */
export interface ApiEnvelope<T = unknown> {
  code: number
  data: T
  message: string
}

export interface Paged<T> {
  total: number
  page: number
  page_size: number
  items: T[]
}

const http = axios.create({
  baseURL: '/',
  timeout: 60_000,
})

http.interceptors.response.use(
  (response) => response,
  (error) => {
    // 后端异常处理器保证了错误体也是 {code, data, message}
    const payload = error?.response?.data as ApiEnvelope | undefined
    const message = payload?.message || error?.message || '请求失败'
    return Promise.reject(new Error(message))
  },
)

async function request<T>(config: AxiosRequestConfig, silent = false): Promise<T> {
  try {
    const response = await http.request<ApiEnvelope<T>>(config)
    const body = response.data
    if (body && typeof body === 'object' && 'code' in body) {
      if (body.code !== 0) throw new Error(body.message || '请求失败')
      return body.data
    }
    return body as unknown as T
  } catch (error) {
    if (!silent) ElMessage.error((error as Error).message)
    throw error
  }
}

export const api = {
  get: <T>(url: string, params?: Record<string, unknown>, silent = false) =>
    request<T>({ method: 'GET', url, params }, silent),
  post: <T>(url: string, data?: unknown, params?: Record<string, unknown>, silent = false) =>
    request<T>({ method: 'POST', url, data, params }, silent),
  put: <T>(url: string, data?: unknown, params?: Record<string, unknown>, silent = false) =>
    request<T>({ method: 'PUT', url, data, params }, silent),
  patch: <T>(url: string, data?: unknown, params?: Record<string, unknown>, silent = false) =>
    request<T>({ method: 'PATCH', url, data, params }, silent),
  del: <T>(url: string, params?: Record<string, unknown>, silent = false) =>
    request<T>({ method: 'DELETE', url, params }, silent),
  upload: <T>(url: string, file: File, silent = false) => {
    const form = new FormData()
    form.append('file', file)
    return request<T>(
      { method: 'POST', url, data: form, headers: { 'Content-Type': 'multipart/form-data' } },
      silent,
    )
  },
}

/** 触发浏览器下载。导出是流式响应，直接开新窗口交给浏览器处理最省事。 */
export function download(url: string, params: Record<string, unknown> = {}): void {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.append(key, String(value))
  })
  const suffix = query.toString()
  window.open(suffix ? `${url}?${suffix}` : url, '_blank')
}

export default http
