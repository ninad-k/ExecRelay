export interface License {
  id: string
  license_key: string
  hmac_secret?: string
  active: boolean
  platform: string
  created_at: string
}

export interface Instance {
  id: string
  license_id: string
  instance_key: string
  platform: string
  active: boolean
  created_at: string
}

export interface Signal {
  id: string
  trace_id: string
  command: string
  symbol: string
  received_at: string
  ingress_region: string
}

export interface Fill {
  id: string
  trace_id: string
  status: string
  broker_order_id: string
  error_message?: string
  filled_at: string
}

export interface TraceTimeline {
  trace_id: string
  signal: {
    id: string
    received_at: string
    command: string
    symbol: string
    ingress_region: string
    payload: Record<string, unknown>
  } | null
  fills: {
    id: string
    created_at: string
    status: string
    broker_order_id: string | null
    error_code: string | null
    error_message: string | null
  }[]
  events: {
    event_type: string
    severity: string
    payload: Record<string, unknown>
    created_at: string
  }[]
}
