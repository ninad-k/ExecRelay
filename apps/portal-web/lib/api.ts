import { clearToken, getToken } from './auth'
import type { Fill, Instance, License, Signal, TraceTimeline } from './types'

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message)
    this.name = 'ApiError'
  }
}

async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init.headers as Record<string, string>),
  }
  if (token) headers['Authorization'] = `Bearer ${token}`

  const res = await fetch(`/api${path}`, { ...init, headers })
  if (!res.ok) {
    if (res.status === 401 && typeof window !== 'undefined') {
      clearToken()
      window.location.href = '/login'
      return {} as T
    }
    const text = await res.text().catch(() => '')
    throw new ApiError(res.status, text || `HTTP ${res.status}`)
  }
  if (res.status === 204) return {} as T
  return res.json() as Promise<T>
}

export const api = {
  login(email: string, password: string) {
    return apiFetch<{ access_token: string }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    })
  },

  register(email: string, password: string) {
    return apiFetch<{ access_token: string }>('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    })
  },

  getLicenses() {
    return apiFetch<License[]>('/licenses')
  },

  createLicense(platform: string) {
    return apiFetch<License>('/licenses', {
      method: 'POST',
      body: JSON.stringify({ platform }),
    })
  },

  patchLicense(id: string, active: boolean) {
    return apiFetch<License>(`/licenses/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ active }),
    })
  },

  getInstances(licenseId: string) {
    return apiFetch<Instance[]>(`/licenses/${licenseId}/instances`)
  },

  createInstance(licenseId: string, instanceKey: string, platform: string) {
    return apiFetch<Instance>(`/licenses/${licenseId}/instances`, {
      method: 'POST',
      body: JSON.stringify({ instance_key: instanceKey, platform }),
    })
  },

  getConfig(licenseId: string) {
    return apiFetch<{ config: string }>(`/licenses/${licenseId}/config`)
  },

  getSignals(licenseId: string, limit = 50) {
    return apiFetch<Signal[]>(`/licenses/${licenseId}/signals?limit=${limit}`)
  },

  getFills(licenseId: string, instanceId: string, limit = 50) {
    return apiFetch<Fill[]>(
      `/licenses/${licenseId}/instances/${instanceId}/fills?limit=${limit}`,
    )
  },

  getTrace(traceId: string) {
    return apiFetch<TraceTimeline>(`/traces/${traceId}`)
  },

  replaySignal(signalId: string) {
    return apiFetch<{ trace_id: string; subject: string }>(
      `/signals/${signalId}/replay`,
      { method: 'POST' },
    )
  },

  testSignal(licenseId: string) {
    return apiFetch<{ trace_id: string; status: string }>(
      `/licenses/${licenseId}/test-signal`,
      { method: 'POST' },
    )
  },

  rotateHmac(licenseId: string) {
    return apiFetch<{ pending_hmac_secret: string; message: string }>(
      `/licenses/${licenseId}/rotate-hmac`,
      { method: 'POST' },
    )
  },

  confirmRotation(licenseId: string) {
    return apiFetch<{ message: string }>(
      `/licenses/${licenseId}/confirm-rotation`,
      { method: 'POST' },
    )
  },

  correlateSignals(licenseId: string, signalIds: string[]) {
    return apiFetch<{
      signals: Signal[]
      correlation_matrix: Record<string, number>
      symbol_groups: Record<string, number>
      conflicts: string[]
    }>(`/licenses/${licenseId}/signals/correlate`, {
      method: 'POST',
      body: JSON.stringify({ signal_ids: signalIds }),
    })
  },

  getPortfolioExposure(licenseId: string) {
    return apiFetch<{
      license_id: string
      accounts: Array<{
        account_id: string
        notional_usd: number
        limit_usd: number
        utilization_pct: number
        positions: Array<{
          symbol: string
          size: number
        }>
      }>
    }>(`/licenses/${licenseId}/portfolio-exposure`)
  },

  getRiskMetrics(licenseId: string) {
    return apiFetch<{
      license_id: string
      accounts: Array<{
        account_id: string
        notional_exposure: number
        exposure_limit: number
        exposure_ratio: number
        peak_equity: number
        current_equity: number
        drawdown_pct: number
        largest_position: {
          symbol: string
          size: number
          value: number
        } | null
      }>
      total_exposure: number
      total_limit: number
      breaches: Array<{
        account_id: string
        breach_type: string
        current_value: number
        limit_value: number
        created_at: string
      }>
    }>(`/licenses/${licenseId}/risk-metrics`)
  },
}
