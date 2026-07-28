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

export interface TelegramStatus {
  linked: boolean
  linked_at: string | null
  chat_id: string | null
  notify_fills: boolean
  notify_timeouts: boolean
  failed_last_24h: number
  last_delivery_status: string | null
}

export interface TelegramLink {
  deep_link: string
  link_token: string
  expires_at: string
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
