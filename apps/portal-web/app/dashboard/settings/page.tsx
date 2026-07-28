'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import QRCode from 'qrcode'
import { api, ApiError } from '@/lib/api'
import type { TelegramLink, TelegramStatus } from '@/lib/types'

// Settings → Telegram, implementing docs/design/telegram-notifications-ux.md:
// states = not configured / not connected / connecting (QR + countdown,
// 3 s status polling) / connected (prefs, delivery-health badge, disconnect).

function remainingSeconds(expiresAt: string): number {
  return Math.max(0, Math.floor((new Date(expiresAt).getTime() - Date.now()) / 1000))
}

export default function SettingsPage() {
  const [status, setStatus] = useState<TelegramStatus | null>(null)
  const [link, setLink] = useState<TelegramLink | null>(null)
  const [notConfigured, setNotConfigured] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [countdown, setCountdown] = useState(0)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  const load = useCallback(async () => {
    try {
      const s = await api.getTelegram()
      setStatus(s)
      if (s.linked) setLink(null)
      return s
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        window.location.href = '/login'
      }
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  // Connecting state: poll link status every 3 s so the panel flips to
  // Connected without a page refresh (confirmation is pull-based).
  useEffect(() => {
    if (!link || status?.linked) return
    const t = setInterval(() => { void load() }, 3000)
    return () => clearInterval(t)
  }, [link, status?.linked, load])

  // Countdown for the 15-minute link token; on expiry the QR is replaced by
  // a "Generate new link" button in place (no error state).
  useEffect(() => {
    if (!link) return
    setCountdown(remainingSeconds(link.expires_at))
    const t = setInterval(() => setCountdown(remainingSeconds(link.expires_at)), 1000)
    return () => clearInterval(t)
  }, [link])

  // The QR encodes exactly deep_link — nothing else.
  useEffect(() => {
    if (link && countdown > 0 && canvasRef.current) {
      void QRCode.toCanvas(canvasRef.current, link.deep_link, { width: 180, margin: 1 })
    }
  }, [link, countdown])

  async function connect() {
    if (
      status?.linked &&
      !window.confirm('Connecting a new device will disconnect the current one. Continue?')
    ) {
      return
    }
    setError('')
    setBusy(true)
    try {
      setLink(await api.createTelegramLink())
    } catch (err) {
      if (err instanceof ApiError && err.status === 503) setNotConfigured(true)
      else setError('Failed to generate a link. Try again.')
    } finally {
      setBusy(false)
    }
  }

  async function togglePref(key: 'notify_fills' | 'notify_timeouts') {
    if (!status) return
    const prev = status
    // Optimistic toggle; revert on failure.
    setStatus({ ...status, [key]: !status[key] })
    try {
      setStatus(await api.patchTelegram({ [key]: !prev[key] }))
    } catch {
      setStatus(prev)
      setError('Failed to save preference.')
    }
  }

  async function disconnect() {
    if (!window.confirm('Stop all Telegram notifications? You can reconnect any time.')) return
    setBusy(true)
    setError('')
    try {
      await api.deleteTelegram()
      setLink(null)
      await load()
    } catch {
      setError('Failed to disconnect.')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <p className="text-sm text-gray-500">Loading…</p>

  const deliveryWarning =
    status?.linked &&
    (status.failed_last_24h > 0 || status.last_delivery_status === 'failed')

  return (
    <div>
      <h1 className="mb-6 text-xl font-semibold">Settings</h1>

      <section className="rounded-lg border border-gray-200 bg-white p-5">
        <h2 className="text-base font-semibold">Telegram notifications</h2>
        <p className="mt-1 text-sm text-gray-500">
          Get a message when a trade fills, is placed, fails, or times out.
        </p>

        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}

        {notConfigured ? (
          <p className="mt-4 text-sm text-gray-500">
            Telegram notifications aren&apos;t enabled on this deployment.
          </p>
        ) : status?.linked ? (
          <div className="mt-4 space-y-4">
            <p className="flex items-center gap-2 text-sm">
              <span className="inline-block h-2.5 w-2.5 rounded-full bg-green-500" />
              Connected
              {status.chat_id && (
                <span className="text-gray-500">
                  · Chat {status.chat_id.length > 6
                    ? `${status.chat_id.slice(0, 3)}…${status.chat_id.slice(-2)}`
                    : status.chat_id}
                </span>
              )}
              {status.linked_at && (
                <span className="text-gray-500">
                  · since {new Date(status.linked_at).toLocaleDateString()}
                </span>
              )}
            </p>

            {deliveryWarning && (
              <p className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
                ⚠ Recent notifications failed to deliver
                {status.failed_last_24h > 0 && ` (${status.failed_last_24h} in the last 24 h)`}
                . Check that you haven&apos;t blocked the bot, or reconnect.
              </p>
            )}

            <div className="space-y-2">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={status.notify_fills}
                  onChange={() => void togglePref('notify_fills')}
                  className="h-4 w-4 rounded border-gray-300"
                />
                Trade results (fills and rejections)
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={status.notify_timeouts}
                  onChange={() => void togglePref('notify_timeouts')}
                  className="h-4 w-4 rounded border-gray-300"
                />
                Fill timeouts (signal accepted, no broker confirmation)
              </label>
            </div>

            <div className="flex gap-3">
              <button
                onClick={disconnect}
                disabled={busy}
                className="rounded-lg border border-red-300 px-4 py-1.5 text-sm font-medium text-red-600 hover:bg-red-50 disabled:opacity-50"
              >
                Disconnect
              </button>
              <button
                onClick={connect}
                disabled={busy}
                className="rounded-lg border border-gray-300 px-4 py-1.5 text-sm text-gray-600 hover:bg-gray-50 disabled:opacity-50"
              >
                Connect a different device
              </button>
            </div>
          </div>
        ) : link ? (
          <div className="mt-4 space-y-3">
            {countdown > 0 ? (
              <>
                <p className="text-sm text-gray-600">
                  Scan with your phone, or open the link on this device:
                </p>
                <canvas ref={canvasRef} className="rounded-lg border border-gray-200" />
                <p className="text-xs text-gray-500">
                  This link is unique to your account — don&apos;t share it.
                </p>
                <div className="flex items-center gap-3">
                  <a
                    href={link.deep_link}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
                  >
                    Open in Telegram
                  </a>
                  <span className="text-sm text-gray-500">
                    Link expires in {Math.floor(countdown / 60)}:
                    {String(countdown % 60).padStart(2, '0')} ⏳
                  </span>
                </div>
                <p className="text-sm text-gray-500">
                  Waiting for you to tap <span className="font-medium">Start</span> in Telegram…
                </p>
              </>
            ) : (
              <button
                onClick={connect}
                disabled={busy}
                className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                Generate new link
              </button>
            )}
          </div>
        ) : (
          <div className="mt-4 space-y-3">
            <p className="flex items-center gap-2 text-sm">
              <span className="inline-block h-2.5 w-2.5 rounded-full bg-gray-300" />
              Not connected
            </p>
            <button
              onClick={connect}
              disabled={busy}
              className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {busy ? 'Generating…' : 'Connect Telegram'}
            </button>
          </div>
        )}
      </section>
    </div>
  )
}
